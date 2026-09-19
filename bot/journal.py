"""
What happened to every trade, and whether the supervisor's exits paid.

Before this the only trade history was bot.log, and judging the supervisor
meant replaying bars by hand. The 2026-09-18 review did exactly that and
found its exits roughly break even: three losers cut before their stops
(+$0.63 saved), two winners cut before their targets (-$1.15 given up). One
day is not evidence either way. This keeps score automatically so the
2026-09-25 go-live decision can rest on a week of it.

Three pieces:
  TradeJournal       one CSV row per closed trade (data/trades.csv)
  SupervisorReview   after a supervisor exit, keeps watching the market and
                     records whether the ORIGINAL stop or target would have
                     been hit first, and what holding would have made
                     (data/supervisor_review.csv, pending in .json)
  position_report    the hourly "what is open and how is it doing" message
"""

from __future__ import annotations

import csv
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("journal")

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

#: Fees used for the "if held" figure: maker in, taker out, as in the
#: dry-run simulator. The real close was booked net of its real fees.
ENTRY_FEE = 0.0002
EXIT_FEE = 0.0005
BAR_MS = 900_000


@dataclass
class MonitorConfig:
    #: Send an "open positions" report this often while anything is open.
    #: 0 = never. Each line: P&L, R, distance to stop and target, age, and
    #: whether both protective orders are on the exchange.
    report_minutes: int = 60
    #: Score every supervisor exit against what holding would have done.
    review_exits: bool = True
    #: Give up on a review after this long with neither level reached, and
    #: score it at the last price.
    review_hours: float = 24.0


def _utc(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- journal
TRADE_FIELDS = ["closed_utc", "symbol", "side", "qty", "entry", "pnl_usdt",
                "r_multiple", "closed_by", "reason", "opened_utc",
                "minutes_held", "initial_stop", "initial_target", "mode"]


class TradeJournal:
    def __init__(self, path: Path | str = DATA / "trades.csv"):
        self.path = Path(path)

    def record(self, pos, pnl: float, closed_by: str, mode: str,
               now_ms: float | None = None) -> dict:
        now_ms = now_ms if now_ms is not None else time.time() * 1000
        risk = abs(getattr(pos, "initial_risk", 0.0) or 0.0) * pos.qty
        row = {
            "closed_utc": _utc(now_ms),
            "symbol": pos.symbol, "side": pos.side, "qty": f"{pos.qty:g}",
            "entry": f"{pos.entry:.8g}", "pnl_usdt": f"{pnl:+.4f}",
            "r_multiple": f"{pnl / risk:+.2f}" if risk > 0 else "",
            "closed_by": closed_by,
            "reason": getattr(pos, "exit_reason", "") or "",
            "opened_utc": _utc(pos.opened_ms) if pos.opened_ms else "",
            "minutes_held": (f"{(now_ms - pos.opened_ms) / 60000:.0f}"
                             if pos.opened_ms else ""),
            "initial_stop": f"{pos.initial_stop:.8g}" if pos.initial_stop else "",
            "initial_target": f"{pos.initial_target:.8g}" if pos.initial_target else "",
            "mode": mode,
        }
        try:
            new = not self.path.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.error("could not write the trade journal: %s", e)
        return row


# --------------------------------------------------------- supervisor review
@dataclass
class PendingReview:
    symbol: str
    long: bool
    entry: float
    qty: float
    stop: float
    target: float
    actual_pnl: float
    closed_ms: int
    reason: str


@dataclass
class ReviewResult:
    symbol: str
    outcome: str            # "stop" | "target" | "expired"
    at_ms: int
    held_pnl: float
    actual_pnl: float

    @property
    def difference(self) -> float:
        """Positive: the exit did better than holding would have."""
        return self.actual_pnl - self.held_pnl


def score(p: PendingReview, bars: list, now_ms: float,
          expire_hours: float) -> ReviewResult | None:
    """
    Resolve one review from 15m bars, or None while it is still open.

    The bar containing the exit counts: until the exit the resting stop and
    take-profit were at or inside the originals, so the market cannot have
    reached an original level earlier in that bar without closing the trade
    there instead. A bar that reaches both is scored as the stop -- the
    conservative reading, the same one the backtester makes.
    """
    sign = 1.0 if p.long else -1.0
    last = None
    for b in bars:
        if b.open_time + BAR_MS <= p.closed_ms:
            continue
        last = b
        hit_stop = (b.low <= p.stop) if p.long else (b.high >= p.stop)
        hit_tp = p.target > 0 and ((b.high >= p.target) if p.long else (b.low <= p.target))
        if hit_stop or hit_tp:
            level = p.stop if hit_stop else p.target
            return ReviewResult(p.symbol, "stop" if hit_stop else "target",
                                b.open_time, _held(p, level, sign), p.actual_pnl)
    if now_ms - p.closed_ms >= expire_hours * 3600_000 and last is not None:
        return ReviewResult(p.symbol, "expired", last.open_time,
                            _held(p, last.close, sign), p.actual_pnl)
    return None


def _held(p: PendingReview, exit_px: float, sign: float) -> float:
    gross = (exit_px - p.entry) * p.qty * sign
    return gross - p.entry * p.qty * ENTRY_FEE - exit_px * p.qty * EXIT_FEE


REVIEW_FIELDS = ["exit_utc", "symbol", "reason", "actual_pnl", "if_held",
                 "held_outcome", "resolved_utc", "exit_did_better_by"]


class SupervisorReview:
    def __init__(self, pending_path: Path | str = DATA / "supervisor_review.json",
                 csv_path: Path | str = DATA / "supervisor_review.csv"):
        self.pending_path = Path(pending_path)
        self.csv_path = Path(csv_path)
        self.pending: list[PendingReview] = []
        self.totals = {"reviews": 0, "better": 0.0}
        self.load()

    def load(self) -> None:
        try:
            raw = json.loads(self.pending_path.read_text())
            self.pending = [PendingReview(**r) for r in raw.get("pending", [])]
            self.totals.update(raw.get("totals", {}))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            log.error("supervisor review state unreadable, starting fresh: %s", e)

    def save(self) -> None:
        try:
            self.pending_path.parent.mkdir(parents=True, exist_ok=True)
            self.pending_path.write_text(json.dumps(
                {"pending": [asdict(p) for p in self.pending],
                 "totals": self.totals}, indent=2))
        except OSError as e:
            log.error("could not save the supervisor review: %s", e)

    def add(self, pos, pnl: float, reason: str, now_ms: float | None = None) -> None:
        stop = pos.initial_stop or 0.0
        if stop <= 0 or pos.entry <= 0:
            return                      # nothing to replay against
        self.pending.append(PendingReview(
            symbol=pos.symbol, long=pos.is_long, entry=pos.entry, qty=pos.qty,
            stop=stop, target=pos.initial_target or 0.0, actual_pnl=pnl,
            closed_ms=int(now_ms if now_ms is not None else time.time() * 1000),
            reason=reason))
        self.save()

    def resolve(self, fetch_bars, now_ms: float | None = None,
                expire_hours: float = 24.0) -> list[ReviewResult]:
        """fetch_bars(symbol) -> recent 15m bars. Returns what resolved."""
        now_ms = now_ms if now_ms is not None else time.time() * 1000
        done, keep = [], []
        for p in self.pending:
            try:
                bars = fetch_bars(p.symbol)
            except Exception as e:
                log.debug("review of %s waits: %s", p.symbol, e)
                keep.append(p)
                continue
            r = score(p, bars, now_ms, expire_hours)
            if r is None:
                keep.append(p)
                continue
            done.append(r)
            self.totals["reviews"] = self.totals.get("reviews", 0) + 1
            self.totals["better"] = self.totals.get("better", 0.0) + r.difference
            self._write(p, r)
        if done:
            self.pending = keep
            self.save()
        return done

    def _write(self, p: PendingReview, r: ReviewResult) -> None:
        try:
            new = not self.csv_path.exists()
            self.csv_path.parent.mkdir(parents=True, exist_ok=True)
            with self.csv_path.open("a", newline="") as f:
                w = csv.DictWriter(f, fieldnames=REVIEW_FIELDS)
                if new:
                    w.writeheader()
                w.writerow({"exit_utc": _utc(p.closed_ms), "symbol": p.symbol,
                            "reason": p.reason, "actual_pnl": f"{r.actual_pnl:+.4f}",
                            "if_held": f"{r.held_pnl:+.4f}",
                            "held_outcome": r.outcome,
                            "resolved_utc": _utc(r.at_ms),
                            "exit_did_better_by": f"{r.difference:+.4f}"})
        except OSError as e:
            log.error("could not write the supervisor review: %s", e)


def describe(r: ReviewResult, totals: dict) -> str:
    held = {"stop": "hit its original stop", "target": "reached its original target",
            "expired": "reached neither level in the review window"}[r.outcome]
    verdict = (f"the exit saved ${r.difference:.2f}" if r.difference >= 0
               else f"the exit gave up ${-r.difference:.2f}")
    return (f"Supervisor review, {r.symbol}: the exit made {r.actual_pnl:+.2f}; "
            f"holding {held} ({_utc(r.at_ms)} UTC) for {r.held_pnl:+.2f}, so "
            f"{verdict}.\nAll {totals.get('reviews', 0)} reviewed exits: "
            f"{totals.get('better', 0.0):+.2f} USDT versus holding.")


# ------------------------------------------------------------ position report
def position_report(positions: list, now_ms: float | None = None) -> str:
    """`positions`: (ActivePosition, price, protected) tuples."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000
    if not positions:
        return ""
    lines = [f"{len(positions)} open position(s)"]
    total = 0.0
    for pos, px, protected in positions:
        if px <= 0:
            lines.append(f"{pos.symbol} {pos.side} {pos.qty:g}: no price yet")
            continue
        upnl = pos.unrealized(px)
        total += upnl
        risk = abs(pos.initial_risk or 0.0)
        r_now = ((px - pos.entry) * (1 if pos.is_long else -1) / risk
                 if risk > 0 else None)
        sl = abs(pos.stop / px - 1) * 100 if pos.stop else None
        tp = abs(pos.take_profit / px - 1) * 100 if pos.take_profit else None
        age = (now_ms - pos.opened_ms) / 60000 if pos.opened_ms else None
        lines.append(
            f"{pos.symbol} {'long' if pos.is_long else 'short'} {pos.qty:g} "
            f"@ {pos.entry:,.6g} -> {px:,.6g}  {upnl:+.2f} USDT"
            + (f" ({r_now:+.2f}R)" if r_now is not None else "")
            + f"\n   SL " + (f"{sl:.1f}% away" if sl is not None else "NONE")
            + "  TP " + (f"{tp:.1f}% away" if tp is not None else "NONE")
            + (f"  held {age // 60:.0f}h{age % 60:02.0f}m" if age is not None else "")
            + ("" if protected else "  NOT PROTECTED"))
    lines.append(f"unrealised total {total:+.2f} USDT")
    return "\n".join(lines)
