"""
Gainer mining: trade whichever USDT perpetual has just become the top gainer.

The rules, as asked for:

  * When a symbol takes FIRST place on the 24h gainer board, open a long on it
    with a take-profit sized to bank `target_usd` (default $2) and a
    protective stop. The exchange closes it when either triggers.
  * When a different symbol takes first place, open that one too. Every other
    gainer position is re-judged at that moment: still in profit (net of the
    estimated round-trip cost) -> keep it; not in profit -> close it.
  * Notify every step: new leader, position opened, kept or closed, each
    profit milestone ($1, $1.5, $2 by default), and an hourly status of the
    open positions and the top of the board.

A monitor runs inside the engine loop every `poll_seconds`. It keeps a short
history of the top of the board and reports how the leader is behaving
(accelerating, holding, fading) and which challenger is closing on it.

Honest label, same as every other strategy in this repo: the "prediction" is an
EXTRAPOLATION of each symbol's recent rate of change in 24h %, not a forecast.
It is right while momentum persists and wrong exactly when it turns. Coins
reach first place AFTER their move, and a coin at the top of the 24h board is
as often at the end of its run as in the middle of it. Nothing here has been
backtested. `dry_run: true` (the default) runs everything -- board, alerts,
simulated fills, simulated take-profit and stop -- with nothing sent.

This module never sizes past the engine's own guards: the risk preflight
(halt, KILL file, equity floor, daily loss limit, trade cap), isolated margin
at risk.max_leverage, and the leverage ceiling across the whole book. Every
live position gets an exchange-side stop, or is closed at market straight away.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .binanceapi import BinanceError
from .notify import Event
from .positions import ActivePosition
from .state import client_order_id

log = logging.getLogger("gainer")

ROOT = Path(__file__).resolve().parent.parent
GAINER_STATE_PATH = ROOT / "data" / "gainer.json"

#: Marks a book position as belonging to this strategy. The supervisor stands
#: aside for these and the tracker re-attaches to them after a restart.
STRATEGY = "gainer"

EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}


@dataclass
class GainerConfig:
    enabled: bool = False
    #: Paper-trade: watch the board, simulate fills and exits, send every
    #: alert, and send NOTHING to the exchange. Independent of the top-level
    #: dry_run, which forces this on when it is true.
    dry_run: bool = True
    poll_seconds: int = 30
    top_n: int = 10
    #: 24h quote volume floor. A $300k coin can top the board on one order.
    min_quote_volume: float = 10_000_000
    #: Consecutive polls a new symbol must hold first place before it counts,
    #: so two coins trading places tick by tick do not open two positions.
    confirm_polls: int = 2
    #: False: the leader found at boot is a baseline, not a new leader.
    trade_on_start: bool = False

    # -- sizing
    notional_usdt: float = 5.5
    target_usd: float = 2.0
    stop_pct: float = 5.0
    max_positions: int = 2
    #: Round-trip cost estimate as % of notional. Added to the take-profit
    #: distance so the target is NET, and subtracted before calling a position
    #: "still profitable" when a new leader arrives.
    fee_pct: float = 0.15

    # -- alerts and monitor
    milestones_usd: list = field(default_factory=lambda: [1.0, 1.5, 2.0])
    status_minutes: int = 60
    history_minutes: int = 60
    #: Window the rate of change is measured over, and how far it is projected.
    slope_minutes: float = 10.0
    predict_minutes: float = 15.0
    #: |slope| below this (24h % points per minute) reads as "holding".
    trend_eps: float = 0.05


# ------------------------------------------------------------------ the board
@dataclass
class Row:
    symbol: str
    change_pct: float
    price: float
    quote_volume: float
    rank: int = 0


def rank_board(tickers: list, tradable: set, min_quote_volume: float) -> list[Row]:
    """Every eligible symbol, best 24h change first, ranks from 1."""
    rows = []
    for t in tickers or []:
        sym = t.get("symbol")
        if tradable and sym not in tradable:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            row = Row(sym, float(t["priceChangePercent"]),
                      float(t.get("lastPrice") or 0), qv)
        except (KeyError, TypeError, ValueError):
            continue
        if qv < min_quote_volume or row.price <= 0:
            continue
        rows.append(row)
    rows.sort(key=lambda r: -r.change_pct)
    for i, r in enumerate(rows, 1):
        r.rank = i
    return rows


@dataclass
class Forecast:
    leader: str
    leader_trend: str           # accelerating | holding | fading | unknown
    leader_slope: float         # 24h % points per minute
    challenger: str = ""
    eta_minutes: float = 0.0    # 0 = not closing
    lines: list = field(default_factory=list)

    def text(self) -> str:
        head = f"leader {self.leader} is {self.leader_trend} ({self.leader_slope:+.2f} %/min)"
        if self.challenger:
            eta = (f", could overtake in ~{self.eta_minutes:.0f} min"
                   if self.eta_minutes else "")
            head += f"\nlikely next: {self.challenger}{eta}"
        else:
            head += "\nno challenger closing on it"
        return head + "\n(extrapolated from the last few minutes, not a prediction)"


class GainerBoard:
    """A short history of the top of the board, and what it is doing."""

    def __init__(self, cfg: GainerConfig):
        self.cfg = cfg
        #: (timestamp, {symbol: change_pct}) for the top 3*top_n each poll, so
        #: a climber from outside the top 10 already has history when it enters.
        self.history: deque = deque()
        self.rows: list[Row] = []

    def update(self, rows: list[Row], now: float) -> None:
        self.rows = rows
        keep = rows[: self.cfg.top_n * 3]
        self.history.append((now, {r.symbol: r.change_pct for r in keep}))
        horizon = now - self.cfg.history_minutes * 60
        while self.history and self.history[0][0] < horizon:
            self.history.popleft()

    @property
    def top(self) -> list[Row]:
        return self.rows[: self.cfg.top_n]

    @property
    def leader(self) -> Row | None:
        return self.rows[0] if self.rows else None

    def slope(self, symbol: str, now: float) -> float | None:
        """24h % points per minute over about slope_minutes; None if unknown."""
        if not self.history or symbol not in self.history[-1][1]:
            return None
        t_now, latest = self.history[-1]
        target = t_now - self.cfg.slope_minutes * 60
        base = None
        for t, snap in self.history:
            if symbol in snap:
                base = (t, snap[symbol])
                if t >= target:
                    break
        if base is None or t_now - base[0] < 60:
            return None
        return (latest[symbol] - base[1]) / ((t_now - base[0]) / 60.0)

    def trend_word(self, slope: float | None) -> str:
        if slope is None:
            return "unknown"
        if slope > self.cfg.trend_eps:
            return "accelerating"
        if slope < -self.cfg.trend_eps:
            return "fading"
        return "holding"

    def forecast(self, now: float) -> Forecast | None:
        lead = self.leader
        if lead is None:
            return None
        ls = self.slope(lead.symbol, now)
        fc = Forecast(lead.symbol, self.trend_word(ls), ls or 0.0)
        horizon = self.cfg.predict_minutes
        lead_proj = lead.change_pct + (ls or 0.0) * horizon
        best, best_proj = None, lead_proj
        for r in self.top[1:]:
            s = self.slope(r.symbol, now)
            if s is None:
                continue
            proj = r.change_pct + s * horizon
            if proj > best_proj:
                best, best_proj = (r, s), proj
        if best is not None:
            r, s = best
            closing = s - (ls or 0.0)
            fc.challenger = r.symbol
            if closing > 0:
                fc.eta_minutes = max(1.0, (lead.change_pct - r.change_pct) / closing)
        return fc

    def board_lines(self, now: float, held: set) -> list[str]:
        out = []
        for r in self.top:
            s = self.slope(r.symbol, now)
            arrow = {"accelerating": "^", "fading": "v", "holding": "=",
                     "unknown": "?"}[self.trend_word(s)]
            mark = " *held" if r.symbol in held else ""
            out.append(f"{r.rank:>2}. {r.symbol:<14} {r.change_pct:+7.2f}% {arrow}{mark}")
        return out


# ---------------------------------------------------------------- arithmetic
def target_price(entry: float, qty: float, target_usd: float, fee_pct: float) -> float:
    """Long take-profit that nets target_usd after the estimated round trip."""
    cost = entry * qty * fee_pct / 100.0
    return entry + (target_usd + cost) / qty


def stop_price(entry: float, stop_pct: float) -> float:
    return entry * (1 - stop_pct / 100.0)


def net_pnl(entry: float, qty: float, price: float, fee_pct: float) -> float:
    return (price - entry) * qty - entry * qty * fee_pct / 100.0


# ----------------------------------------------------------------- positions
@dataclass
class Track:
    symbol: str
    entry: float
    qty: float
    stop: float
    take_profit: float
    opened_at: float
    paper: bool = True
    milestones_hit: list = field(default_factory=list)


class GainerMiner:
    EXCHANGE_INFO_MAX_AGE = 3600.0     # new listings are often the top gainers
    FILL_WAIT_ATTEMPTS = 4
    FILL_WAIT_SECONDS = 0.5

    def __init__(self, engine, cfg: GainerConfig, path: Path = GAINER_STATE_PATH):
        self.engine = engine
        self.cfg = cfg
        self.path = path
        self.board = GainerBoard(cfg)
        self.leader = ""                # confirmed leader
        self._candidate = ""
        self._candidate_polls = 0
        self._baselined = False
        self.tracks: dict[str, Track] = {}
        self._last_poll = 0.0
        self._last_status = time.time()
        self._tradable: set = set()
        self._tradable_at = 0.0
        self.load()

    @property
    def paper(self) -> bool:
        return bool(self.cfg.dry_run or self.engine.cfg.dry_run)

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.leader = raw.get("leader", "")
        for sym, t in (raw.get("tracks") or {}).items():
            try:
                self.tracks[sym] = Track(**t)
            except TypeError:
                log.warning("ignoring unreadable gainer track for %s", sym)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"leader": self.leader,
                 "tracks": {s: asdict(t) for s, t in self.tracks.items()}},
                indent=2))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save gainer state: %s", e)

    def restore(self) -> None:
        """
        After a restart the engine re-adopts open positions from the exchange
        but cannot know which strategy opened them. Re-mark ours so the
        supervisor keeps standing aside, and drop live tracks that are gone.
        """
        for sym, t in list(self.tracks.items()):
            if t.paper:
                if not self.paper:
                    del self.tracks[sym]        # paper trades do not survive going live
                continue
            pos = self.engine.book.get(sym)
            if pos is None:
                log.info("gainer: %s closed while the bot was down", sym)
                del self.tracks[sym]
                continue
            pos.strategy = STRATEGY
        self.save()

    # -------------------------------------------------------------------- loop
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._last_poll < self.cfg.poll_seconds:
            return
        self._last_poll = now
        try:
            tickers = self.engine.api.ticker_24hr()
        except BinanceError as e:
            log.warning("gainer board unavailable: %s", e)
            return
        rows = rank_board(tickers, self.tradable(now), self.cfg.min_quote_volume)
        if not rows:
            return
        self.board.update(rows, now)
        prices = {r.symbol: r.price for r in rows}
        # Held symbols can fall under the volume floor; price them anyway.
        for t in tickers or []:
            if t.get("symbol") in self.tracks and t.get("symbol") not in prices:
                try:
                    prices[t["symbol"]] = float(t.get("lastPrice") or 0)
                except (TypeError, ValueError):
                    pass

        self.sync_tracks(prices)
        self.check_leader(now, prices)
        self.check_milestones(prices)
        if self.cfg.status_minutes and now - self._last_status >= self.cfg.status_minutes * 60:
            self._last_status = now
            self.send_status(now, prices)

    def tradable(self, now: float) -> set:
        if self._tradable and now - self._tradable_at < self.EXCHANGE_INFO_MAX_AGE:
            return self._tradable
        try:
            info = self.engine.api.exchange_info()
        except BinanceError as e:
            log.warning("exchange info unavailable (%s); keeping the old symbol list", e)
            return self._tradable
        self._tradable = {
            s["symbol"] for s in info.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT" and s.get("baseAsset") not in EXCLUDE_BASES}
        self._tradable_at = now
        return self._tradable

    def check_leader(self, now: float, prices: dict) -> None:
        lead = self.board.leader
        if lead is None:
            return
        if not self._baselined:
            self._baselined = True
            if not self.cfg.trade_on_start:
                self.leader = lead.symbol
                self._candidate, self._candidate_polls = lead.symbol, self.cfg.confirm_polls
                log.info("gainer: baseline leader %s (%+.2f%%); waiting for a new one",
                         lead.symbol, lead.change_pct)
                self.save()
                return
        if lead.symbol == self.leader:
            self._candidate, self._candidate_polls = lead.symbol, 0
            return
        if lead.symbol != self._candidate:
            self._candidate, self._candidate_polls = lead.symbol, 0
        self._candidate_polls += 1
        if self._candidate_polls < self.cfg.confirm_polls:
            return
        previous, self.leader = self.leader, lead.symbol
        self.save()
        self.on_new_leader(lead, previous, now, prices)

    def on_new_leader(self, lead: Row, previous: str, now: float, prices: dict) -> None:
        fc = self.board.forecast(now)
        self.notify(f"NEW TOP GAINER: {lead.symbol} {lead.change_pct:+.2f}% "
                    f"(was {previous or 'none'})"
                    + (f"\n{fc.text()}" if fc else ""), symbol=lead.symbol)

        for sym, t in list(self.tracks.items()):
            if sym == lead.symbol:
                continue
            px = prices.get(sym, 0.0)
            if px <= 0:
                self.notify(f"{sym}: no price to judge it by; left open", symbol=sym)
                continue
            pnl = net_pnl(t.entry, t.qty, px, self.cfg.fee_pct)
            if pnl > 0:
                self.notify(f"{sym} KEPT: still profitable {pnl:+.2f} USDT "
                            f"(now {px:,.6g}, entry {t.entry:,.6g})", symbol=sym)
            else:
                self.close(sym, px, f"no longer leader and not profitable ({pnl:+.2f} USDT)")

        if lead.symbol in self.tracks:
            self.notify(f"{lead.symbol} is back on top; already holding it", symbol=lead.symbol)
            return
        self.open(lead.symbol, lead.price, lead.change_pct)

    # ------------------------------------------------------------------- open
    def refuse(self, symbol: str, why: str) -> None:
        log.info("gainer: not opening %s -- %s", symbol, why)
        self.notify(f"{symbol} NOT opened: {why}", symbol=symbol)

    def open(self, symbol: str, price: float, change_pct: float = 0.0) -> bool:
        cfg, eng = self.cfg, self.engine
        if len(self.tracks) >= cfg.max_positions:
            self.refuse(symbol, f"already holding {len(self.tracks)}/{cfg.max_positions} "
                                f"gainer positions")
            return False
        if symbol in eng.book:
            self.refuse(symbol, "another strategy already holds it")
            return False
        if cfg.target_usd <= 0 or cfg.stop_pct <= 0 or cfg.notional_usdt <= 0:
            self.refuse(symbol, "gainer target_usd, stop_pct and notional_usdt must all be > 0")
            return False

        try:
            rules = eng.rules_for(symbol)
        except (KeyError, BinanceError) as e:
            self.refuse(symbol, f"no symbol filters ({e})")
            return False
        sized = rules.size_for_notional(cfg.notional_usdt, price)
        if sized is None:
            self.refuse(symbol, f"${cfg.notional_usdt:.2f} is under the cheapest legal "
                                f"order (${rules.min_affordable_notional(price):.2f})")
            return False
        qty = float(sized[0])

        if self.paper:
            return self._opened(symbol, price, qty, paper=True, change_pct=change_pct)

        gate = eng.risk.preflight(eng.equity)
        if not gate:
            self.refuse(symbol, gate.reason)
            return False
        held = sum(p.notional for p in eng.book.values())
        ceiling = eng.equity * eng.cfg.risk.max_leverage
        if held + qty * price > ceiling + 1e-9:
            self.refuse(symbol, f"would deploy ${held + qty * price:,.2f} against a "
                                f"${ceiling:,.2f} leverage ceiling")
            return False
        if not eng.prepare_symbol(symbol):
            return False

        eng._seq += 1
        entry_id = client_order_id("g", eng._seq)
        try:
            eng.api.order(symbol=symbol, side="BUY", type="MARKET", quantity=sized[0],
                          newClientOrderId=entry_id)
        except BinanceError as e:
            self.refuse(symbol, f"entry order refused: {e}")
            return False
        eng.risk.record_attempt()

        amt, entry = self._read_fill(symbol)
        if amt <= 0:
            eng.state.halt(f"gainer entry {entry_id} on {symbol} sent but no position "
                           f"could be read back")
            self.notify(f"{symbol}: market entry sent but the position could not be "
                        f"read back. Bot HALTED. Check Binance for an unprotected "
                        f"position.", symbol=symbol, event=Event.HALT)
            return False
        return self._protect(symbol, entry, amt, entry_id, rules, change_pct)

    def _read_fill(self, symbol: str) -> tuple[float, float]:
        for attempt in range(self.FILL_WAIT_ATTEMPTS):
            try:
                rows = self.engine.api.positions(symbol)
            except BinanceError as e:
                log.warning("gainer: reading %s fill failed: %s", symbol, e)
                rows = []
            for r in rows or []:
                amt = float(r.get("positionAmt") or 0.0)
                if amt > 0:
                    return amt, float(r.get("entryPrice") or 0.0)
            if attempt + 1 < self.FILL_WAIT_ATTEMPTS:
                time.sleep(self.FILL_WAIT_SECONDS)
        return 0.0, 0.0

    def _protect(self, symbol, entry, qty, entry_id, rules, change_pct) -> bool:
        """Stop and take-profit for a filled market entry, or flatten it."""
        eng, cfg = self.engine, self.cfg
        sl = float(rules.round_price(stop_price(entry, cfg.stop_pct)))
        tp = float(rules.round_price(target_price(entry, qty, cfg.target_usd, cfg.fee_pct)))
        qty_s = rules.round_qty(qty)
        eng._seq += 1
        stop_id = client_order_id("s", eng._seq)
        tp_id = client_order_id("t", eng._seq)
        leg = "stop"
        try:
            eng.api.algo_order(symbol=symbol, side="SELL", type="STOP_MARKET",
                               triggerPrice=rules.round_price(sl), quantity=qty_s,
                               reduceOnly="true", workingType="MARK_PRICE",
                               clientAlgoId=stop_id)
            leg = "take-profit"
            eng.api.algo_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                               triggerPrice=rules.round_price(tp), quantity=qty_s,
                               reduceOnly="true", workingType="MARK_PRICE",
                               clientAlgoId=tp_id)
        except BinanceError as e:
            log.critical("gainer: %s %s failed (%s) -- flattening", symbol, leg, e)
            try:
                eng._seq += 1
                eng.api.order(symbol=symbol, side="SELL", type="MARKET", quantity=qty_s,
                              reduceOnly="true",
                              newClientOrderId=client_order_id("x", eng._seq))
                eng.api.cancel_all(symbol)
                self.notify(f"{symbol}: could not place the {leg} ({e}). The position "
                            f"was closed at market. Nothing is open.",
                            symbol=symbol, event=Event.ERROR)
            except BinanceError as e2:
                eng.state.halt(f"gainer {symbol}: {leg} failed and the flatten failed: {e2}")
                self.notify(f"{symbol}: {leg} failed AND the market close failed ({e2}). "
                            f"Bot HALTED. {symbol} may be OPEN WITH NO STOP -- close it "
                            f"on Binance now.", symbol=symbol, event=Event.HALT)
            return False

        eng.book[symbol] = ActivePosition(
            symbol=symbol, side="BUY", entry=entry, stop=sl, take_profit=tp, qty=qty,
            entry_order_id=entry_id, stop_order_id=stop_id, tp_order_id=tp_id,
            tag=entry_id, opened_ms=int(time.time() * 1000),
            initial_stop=sl, initial_target=tp, initial_risk=entry - sl,
            filled=True, strategy=STRATEGY)
        eng.risk.record_fill()
        if eng.stream is not None:
            eng.stream.add_symbol(symbol)
        return self._opened(symbol, entry, qty, paper=False, change_pct=change_pct,
                            stop=sl, tp=tp)

    def _opened(self, symbol, entry, qty, paper, change_pct, stop=0.0, tp=0.0) -> bool:
        cfg = self.cfg
        stop = stop or stop_price(entry, cfg.stop_pct)
        tp = tp or target_price(entry, qty, cfg.target_usd, cfg.fee_pct)
        self.tracks[symbol] = Track(symbol, entry, qty, stop, tp, time.time(), paper=paper)
        self.save()
        loss = net_pnl(entry, qty, stop, cfg.fee_pct)
        self.notify(f"{'PAPER ' if paper else ''}OPENED {symbol} (top gainer "
                    f"{change_pct:+.2f}%)\nBUY {qty:g} @ {entry:,.6g}  "
                    f"(${entry * qty:,.2f})\n"
                    f"TP {tp:,.6g} (+{(tp / entry - 1) * 100:.1f}%, nets "
                    f"${cfg.target_usd:.2f})\n"
                    f"SL {stop:,.6g} (-{cfg.stop_pct:g}%, about {loss:+.2f} USDT)",
                    symbol=symbol, event=Event.TRADE_OPEN)
        return True

    # ------------------------------------------------------------------ close
    def close(self, symbol: str, price: float, why: str) -> None:
        t = self.tracks.get(symbol)
        if t is None:
            return
        pnl = net_pnl(t.entry, t.qty, price, self.cfg.fee_pct)
        if t.paper:
            del self.tracks[symbol]
            self.save()
            self.notify(f"PAPER CLOSED {symbol} at {price:,.6g} for {pnl:+.2f} USDT\n{why}",
                        symbol=symbol)
            return
        if self.engine.close_position(f"gainer mining -- {why}", symbol=symbol):
            del self.tracks[symbol]
            self.save()
            self.notify(f"CLOSED {symbol} near {price:,.6g} (about {pnl:+.2f} USDT)\n{why}",
                        symbol=symbol)
        else:
            self.notify(f"{symbol}: close FAILED; its stop and take-profit are still "
                        f"on the exchange\n{why}", symbol=symbol, event=Event.ERROR)

    def sync_tracks(self, prices: dict) -> None:
        """Resolve paper exits, and forget live ones the exchange already closed."""
        for sym, t in list(self.tracks.items()):
            if not t.paper:
                if sym not in self.engine.book:
                    # The engine's reconcile has booked it and sent the TP/SL alert.
                    log.info("gainer: %s closed exchange-side", sym)
                    del self.tracks[sym]
                    self.save()
                continue
            px = prices.get(sym, 0.0)
            if px <= 0:
                continue
            if px >= t.take_profit:
                self.close(sym, t.take_profit, f"take-profit hit (target ${self.cfg.target_usd:.2f})")
            elif px <= t.stop:
                self.close(sym, t.stop, f"stop-loss hit (-{self.cfg.stop_pct:g}%)")

    def check_milestones(self, prices: dict) -> None:
        for sym, t in self.tracks.items():
            px = prices.get(sym, 0.0)
            if px <= 0:
                continue
            pnl = net_pnl(t.entry, t.qty, px, self.cfg.fee_pct)
            for m in sorted(float(x) for x in self.cfg.milestones_usd):
                if pnl >= m and m not in t.milestones_hit:
                    t.milestones_hit.append(m)
                    self.save()
                    self.notify(f"{sym} hit the ${m:g} milestone: {pnl:+.2f} USDT\n"
                                f"now {px:,.6g}, TP {t.take_profit:,.6g}", symbol=sym)

    # ----------------------------------------------------------------- alerts
    def send_status(self, now: float, prices: dict) -> None:
        lines = ["GAINER MINING hourly status" + (" (paper)" if self.paper else "")]
        if self.tracks:
            for sym, t in self.tracks.items():
                px = prices.get(sym, 0.0)
                rank = next((r.rank for r in self.board.rows if r.symbol == sym), 0)
                if px > 0:
                    pnl = net_pnl(t.entry, t.qty, px, self.cfg.fee_pct)
                    span = t.take_profit - t.entry
                    prog = (px - t.entry) / span * 100 if span > 0 else 0.0
                    lines.append(f"{sym}: {pnl:+.2f} USDT, {prog:.0f}% to TP, "
                                 f"rank {rank or '>'+str(len(self.board.rows))}, "
                                 f"held {(now - t.opened_at) / 3600:.1f}h")
                else:
                    lines.append(f"{sym}: no price")
        else:
            lines.append("no open gainer positions")
        lines.append("")
        lines += self.board.board_lines(now, set(self.tracks))
        fc = self.board.forecast(now)
        if fc:
            lines += ["", fc.text()]
        self.notify("\n".join(lines))

    def notify(self, body: str, symbol: str | None = None, event: Event = Event.GAINER) -> None:
        self.engine.notify.send(event, body, symbol=symbol or "gainer")
