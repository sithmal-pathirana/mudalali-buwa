"""
The risk layer. Every order passes through here, and it can veto anything the
strategy proposes. Written before the strategy on purpose: these limits are
the part of the system that decides whether the account survives.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("risk")

# Anchored to the repo, not the working directory: config.yaml already resolved
# that way, so `KILL` and `data/` resolving differently made the kill switch
# silently miss when the bot was started from elsewhere. (QA F21)
ROOT = Path(__file__).resolve().parent.parent
KILL_FILE = ROOT / "KILL"    # touch this file to stop the bot from any shell


@dataclass
class Decision:
    allowed: bool
    reason: str = ""
    qty_notional: float = 0.0

    def __bool__(self) -> bool:
        return self.allowed


class RiskManager:
    def __init__(self, cfg, state):
        self.cfg = cfg.risk
        self.state = state

    # ------------------------------------------------------- gate: may trade
    def preflight(self, equity: float) -> Decision:
        """Checks that apply before any strategy logic runs at all."""
        if KILL_FILE.exists():
            self.state.halt(f"kill file present at {KILL_FILE.resolve()}")
            return Decision(False, "kill file")

        if self.state.halted:
            return Decision(False, f"halted: {self.state.halt_reason}")

        if equity < self.cfg.min_equity_usdt:
            self.state.halt(f"equity {equity:.2f} below floor {self.cfg.min_equity_usdt:.2f}")
            return Decision(False, "below equity floor")

        if self.state.day_start_equity > 0:
            drawdown_pct = (self.state.day_start_equity - equity) / self.state.day_start_equity * 100
            if drawdown_pct >= self.cfg.daily_loss_limit_pct:
                self.state.halt(
                    f"daily loss limit hit: -{drawdown_pct:.2f}% "
                    f"(limit {self.cfg.daily_loss_limit_pct:.2f}%). "
                    f"Restart by hand after reviewing why.")
                return Decision(False, "daily loss limit")

        if self.state.trades_today >= self.cfg.max_trades_per_day:
            return Decision(False, f"trade cap reached ({self.cfg.max_trades_per_day}/day)")

        return Decision(True)

    # ------------------------------------------------------- gate: this order
    def size_position(self, equity: float, entry: float, stop: float, rules,
                      risk_pct_override: float | None = None) -> Decision:
        """
        Position size follows from the stop distance, not from a fixed lot.
        Risk a fixed % of equity between entry and stop, then clamp by leverage.
        """
        if stop <= 0 or entry <= 0:
            return Decision(False, "invalid entry/stop")
        stop_distance = abs(entry - stop) / entry
        if stop_distance < 1e-6:
            return Decision(False, "stop distance is zero")

        # In portfolio mode the allocator has already divided the budget by
        # how many coins qualified, so it passes the per-position figure in.
        risk_pct = (risk_pct_override if risk_pct_override is not None
                    else self.cfg.risk_per_trade_pct)
        risk_usdt = equity * risk_pct / 100.0
        notional_by_risk = risk_usdt / stop_distance
        notional_cap = equity * self.cfg.max_leverage * self.cfg.max_position_pct / 100.0
        notional = min(notional_by_risk, notional_cap)

        cheapest = rules.min_affordable_notional(entry)
        if notional < cheapest:
            return Decision(
                False,
                f"correct size is ${notional:,.2f} but the cheapest legal order on "
                f"{rules.symbol} is ${cheapest:,.2f}. Trading anyway would mean "
                f"risking {cheapest * stop_distance / equity * 100:.1f}% of equity "
                f"per trade instead of {risk_pct:.2f}%. Skipping.")

        implied_leverage = notional / equity
        if implied_leverage > self.cfg.max_leverage + 1e-9:
            return Decision(False, f"implied leverage {implied_leverage:.2f}x exceeds cap")

        return Decision(True, f"risking ${risk_usdt:.2f} over a {stop_distance*100:.2f}% stop",
                        qty_notional=notional)

    def check_add_to_position(self, current_amt: float, side: str) -> Decision:
        """Averaging down is the fastest route to a zero. Blocked by default."""
        if current_amt == 0:
            return Decision(True)
        same_direction = (current_amt > 0 and side == "BUY") or (current_amt < 0 and side == "SELL")
        if same_direction and not self.cfg.allow_averaging_down:
            return Decision(False, "already in this position; averaging down is disabled")
        return Decision(True)

    def check_portfolio(self, open_positions: dict, equity: float,
                        new_notional: float, symbol: str,
                        portfolio_cfg) -> Decision:
        """
        The gate that per-trade sizing cannot see.

        Each position can be correctly sized on its own and the book still be
        wrong: twenty-five individually-sane trades can breach total leverage,
        or stack the whole risk budget into one hour. Checked here, above the
        per-trade layer, and never instead of it.
        """
        if not portfolio_cfg.enabled:
            if open_positions:
                return Decision(False, "already holding a position "
                                       "(portfolio mode is off)")
            return Decision(True)

        if symbol in open_positions:
            return Decision(False, f"already holding {symbol}; "
                                   f"one position per symbol")

        slots = portfolio_cfg.resolved_slots
        if slots and len(open_positions) >= slots:
            return Decision(False, f"all {slots} slots in use")

        held_notional = sum(p.notional for p in open_positions.values())
        ceiling = equity * self.cfg.max_leverage
        if held_notional + new_notional > ceiling + 1e-9:
            return Decision(
                False,
                f"would deploy ${held_notional + new_notional:,.2f} against a "
                f"${ceiling:,.2f} leverage ceiling")

        used_risk = len(open_positions) * portfolio_cfg.resolved_risk_pct
        if used_risk + portfolio_cfg.resolved_risk_pct > portfolio_cfg.portfolio_risk_pct + 1e-9:
            return Decision(
                False,
                f"would take total risk to "
                f"{used_risk + portfolio_cfg.resolved_risk_pct:.2f}%, over the "
                f"{portfolio_cfg.portfolio_risk_pct:.2f}% cap")

        return Decision(True, f"slot {len(open_positions) + 1} of {slots or '?'}")

    def record_attempt(self) -> None:
        """
        An entry was SUBMITTED. Counts against max_trades_per_day, because the
        cap exists to limit activity, not just successful trades. Kept separate
        from record_fill so the two are not conflated. (QA F15)
        """
        self.state.trades_today += 1
        self.state.save()

    def record_fill(self, realized_pnl: float = 0.0) -> None:
        """An entry actually FILLED. This is what total_trades counts."""
        self.state.total_trades += 1
        self.state.realized_today += realized_pnl
        self.state.save()
