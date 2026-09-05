"""
Event-driven backtester.

Deliberately pessimistic, because a backtester that flatters you is worse than
no backtester at all:
  * bar-by-bar, never vectorised, so it cannot see the future
  * when a bar's range touches both the stop and the target, the STOP fills
  * entry pays the maker fee, stop exit pays the taker fee
  * a stop gap-through fills at the bar open, not at the stop price
  * a LIMIT entry only fills if a later bar actually trades through its price,
    and expires unfilled after `entry_expiry_bars` -- previously every signal
    was assumed to fill at the signal bar's close, which is the one optimism
    the module did not disclose. Breakout entries rest at a level price has
    just left, so those are precisely the fills least likely to happen. (QA F11)
Validate it before you trust it:  python3 run.py backtest --validate
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from .strategies.base import Bar

log = logging.getLogger("backtest")

MAKER_FEE = 0.0002
TAKER_FEE = 0.0005


@dataclass
class Trade:
    side: str
    entry: float
    exit: float
    qty: float
    bars_held: int
    reason: str
    pnl: float = 0.0
    fees: float = 0.0
    entry_index: int = 0


@dataclass
class Fills:
    """Bookkeeping for how many signals actually became trades."""
    signals: int = 0
    filled: int = 0
    expired: int = 0

    @property
    def fill_rate(self) -> float:
        return self.filled / self.signals if self.signals else 0.0


@dataclass
class Result:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    start_equity: float = 0.0
    fills: Fills = field(default_factory=Fills)
    capped_days: set = field(default_factory=set)

    @property
    def end_equity(self) -> float:
        return self.equity_curve[-1] if self.equity_curve else self.start_equity

    @property
    def net(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def fees_paid(self) -> float:
        return sum(t.fees for t in self.trades)

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        peak, worst = self.start_equity, 0.0
        for e in self.equity_curve:
            peak = max(peak, e)
            if peak > 0:
                worst = max(worst, (peak - e) / peak)
        return worst * 100

    def sharpe(self, bars_per_day: float) -> float:
        rets = []
        for a, b in zip(self.equity_curve, self.equity_curve[1:]):
            rets.append((b - a) / a if a > 0 else 0.0)
        if len(rets) < 2:
            return 0.0
        sd = statistics.pstdev(rets)
        if sd == 0:
            return 0.0
        return statistics.fmean(rets) / sd * (bars_per_day * 365) ** 0.5

    def report(self, label: str, bars_per_day: float, days: float) -> str:
        per_day = self.net / days if days else 0.0
        lines = [
            f"  {'result':<22} {label}",
            f"  {'bars':<22} {len(self.equity_curve)}  (~{days:.0f} days)",
            f"  {'trades':<22} {len(self.trades)}",
            f"  {'win rate':<22} {self.win_rate*100:.1f}%",
            f"  {'start equity':<22} ${self.start_equity:,.2f}",
            f"  {'end equity':<22} ${self.end_equity:,.2f}",
            f"  {'net P&L':<22} ${self.net:,.2f}",
            f"  {'fees paid':<22} ${self.fees_paid:,.2f}",
            f"  {'P&L per day':<22} ${per_day:,.4f}",
            f"  {'max drawdown':<22} {self.max_drawdown_pct:.2f}%",
            f"  {'Sharpe (annualised)':<22} {self.sharpe(bars_per_day):.2f}",
            f"  {'signals':<22} {self.fills.signals}",
            f"  {'entries filled':<22} {self.fills.filled}"
            f"  ({self.fills.fill_rate*100:.0f}% of signals)",
            f"  {'entries expired':<22} {self.fills.expired}",
        ]
        return "\n".join(lines)


def run(bars: list[Bar], strategy, equity: float, risk_pct: float,
        max_leverage: float, min_notional: float = 5.0,
        entry_expiry_bars: int = 4, daily_target: float = 0.0) -> Result:
    """
    daily_target > 0 models the live `stop_when_reached` rule: once the day's
    realised P&L reaches the target, no new entry opens until the next UTC day.
    0 disables it, which is how every earlier measurement in this repo ran.
    """
    res = Result(start_equity=equity)
    day_pnl = 0.0
    current_day = None
    pos = None            # dict(side, entry, stop, target, qty, opened_at)
    pending = None        # a resting limit entry awaiting a touch

    for i in range(strategy.warmup, len(bars)):
        window = bars[:i + 1]
        bar = bars[i]

        if pos is not None:
            long = pos["side"] == "BUY"
            hit_stop = bar.low <= pos["stop"] if long else bar.high >= pos["stop"]
            hit_tp = (bar.high >= pos["target"] if long else bar.low <= pos["target"]) \
                if pos["target"] else False

            exit_px = reason = None
            if hit_stop:
                # gap through the stop fills at the open, which is worse
                gapped = bar.open < pos["stop"] if long else bar.open > pos["stop"]
                exit_px = bar.open if gapped else pos["stop"]
                reason = "stop-gap" if gapped else "stop"
            elif hit_tp:
                exit_px = pos["target"]
                reason = "target"

            if exit_px is not None:
                gross = (exit_px - pos["entry"]) * pos["qty"]
                if not long:
                    gross = -gross
                fees = pos["entry"] * pos["qty"] * MAKER_FEE + exit_px * pos["qty"] * TAKER_FEE
                pnl = gross - fees
                equity += pnl
                day_pnl += pnl
                res.trades.append(Trade(pos["side"], pos["entry"], exit_px, pos["qty"],
                                        i - pos["opened_at"], reason, pnl, fees,
                                        entry_index=pos["opened_at"]))
                pos = None

        # Day boundary, in UTC, matching the live engine's roll.
        bar_day = bar.open_time // 86_400_000
        if bar_day != current_day:
            current_day = bar_day
            day_pnl = 0.0
        capped = daily_target > 0 and day_pnl >= daily_target
        if capped:
            res.capped_days.add(bar_day)

        # A resting entry fills only when a bar's range reaches its price.
        if pos is None and pending is not None:
            touched = bar.low <= pending["entry"] <= bar.high
            if touched:
                pending["opened_at"] = i
                pos = pending
                pending = None
                res.fills.filled += 1
            elif i - pending["placed_at"] >= entry_expiry_bars:
                pending = None
                res.fills.expired += 1

        if pos is None and pending is None and equity > 0 and not capped:
            sig = strategy.on_bars(window, 0.0)
            if sig:
                res.fills.signals += 1
                stop_dist = abs(sig.entry - sig.stop) / sig.entry
                if stop_dist > 1e-6:
                    notional = min(equity * risk_pct / 100 / stop_dist, equity * max_leverage)
                    if notional >= min_notional:
                        pending = {"side": sig.side, "entry": sig.entry, "stop": sig.stop,
                                   "target": sig.take_profit, "qty": notional / sig.entry,
                                   "opened_at": i, "placed_at": i}

        res.equity_curve.append(equity)
        if equity <= 0:
            log.warning("account wiped out at bar %d", i)
            break

    return res


# ------------------------------------------------------------------ validation
def validate() -> bool:
    """
    A backtester you have not tested will tell you whatever you want to hear.
    Feed it a known price path and a strategy with a forced signal, then check
    the P&L against a figure computed by hand.
    """
    from .strategies.base import Signal, Strategy

    class ForcedLong(Strategy):
        name, warmup = "forced", 0      # fire on the very first bar

        def __init__(self):
            self.fired = False

        def on_bars(self, bars, position_amt):
            if self.fired:
                return None
            self.fired = True
            return Signal("BUY", entry=100.0, stop=90.0, take_profit=120.0)

    # bar 0: signal fires, limit entry rests at 100.
    # bar 1: range covers 100, so the entry fills.
    # bar 2: rises through 120 -> target.
    bars = [Bar(0, 100, 100, 100, 100, 1),
            Bar(1, 100, 101, 99, 100, 1),
            Bar(2, 100, 125, 99, 120, 1)]
    res = run(bars, ForcedLong(), equity=1000.0, risk_pct=10.0, max_leverage=10.0)

    # By hand: risk 10% of 1000 = $100 over a 10% stop -> $1000 notional = 10 units.
    # Gross = (120-100)*10 = $200. Fees = 100*10*0.0002 + 120*10*0.0005 = 0.20 + 0.60 = $0.80.
    expected = 200.0 - 0.80
    got = res.trades[0].pnl if res.trades else 0.0
    ok = abs(got - expected) < 1e-9
    print(f"  hand-computed P&L: ${expected:.2f}")
    print(f"  backtester P&L:    ${got:.2f}")
    print(f"  fees modelled:     ${res.trades[0].fees:.2f}" if res.trades else "  NO TRADE")
    print(f"  {'PASS' if ok else 'FAIL'} -- harness {'agrees with' if ok else 'DISAGREES WITH'} hand calculation")

    # And the stop-priority rule: a bar touching both must resolve as a loss.
    bars2 = [Bar(0, 100, 100, 100, 100, 1),
             Bar(1, 100, 101, 99, 100, 1),
             Bar(2, 100, 125, 85, 110, 1)]
    res2 = run(bars2, ForcedLong(), equity=1000.0, risk_pct=10.0, max_leverage=10.0)
    ok2 = bool(res2.trades) and res2.trades[0].reason == "stop" and res2.trades[0].pnl < 0
    print(f"  {'PASS' if ok2 else 'FAIL'} -- ambiguous bar resolves as a stop, not a win")

    # An entry price the market never revisits must expire, not fill. (QA F11)
    bars3 = [Bar(0, 100, 100, 100, 100, 1)] + [
        Bar(i, 120 + i, 121 + i, 119 + i, 120 + i, 1) for i in range(1, 8)]
    res3 = run(bars3, ForcedLong(), equity=1000.0, risk_pct=10.0, max_leverage=10.0)
    ok3 = not res3.trades and res3.fills.expired == 1
    print(f"  signals={res3.fills.signals} filled={res3.fills.filled} "
          f"expired={res3.fills.expired}")
    print(f"  {'PASS' if ok3 else 'FAIL'} -- unreachable limit entry expires instead of filling")
    return ok and ok2 and ok3
