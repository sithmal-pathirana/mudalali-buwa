"""
Persistent state, and reconciliation against the exchange.

Rule of the house: the exchange is the source of truth for positions and
orders. Local state exists only to track things Binance does not know about,
like the daily loss counter and whether the halt flag has been tripped.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("state")

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"


def today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class State:
    day: str = field(default_factory=today_utc)
    day_start_equity: float = 0.0
    trades_today: int = 0
    realized_today: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    last_bar_open_ms: int = 0
    entry_order_id: str = ""
    stop_order_id: str = ""
    total_trades: int = 0
    schedule_start_date: str = ""      # ISO date of day 1, for the target schedule
    target_reached_today: bool = False
    strategy_override: str = ""        # "" = automatic regime routing

    path: Path = field(default=STATE_PATH, repr=False)

    # ------------------------------------------------------------- lifecycle
    @classmethod
    def load(cls, path: str | Path | None = None) -> "State":
        """
        Tolerate state files written by a different version. An unknown key
        used to be a TypeError at boot, which combined with a zero exit code
        looked exactly like a clean shutdown. (QA F12)
        """
        p = Path(path) if path is not None else STATE_PATH
        if not p.exists():
            return cls(path=p)
        try:
            raw = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("state file %s is unreadable (%s); starting from a fresh "
                      "state. Previous halt flags and day counters are lost.", p, e)
            return cls(path=p)
        raw.pop("path", None)
        known = {f for f in cls.__dataclass_fields__ if f != "path"}
        unknown = set(raw) - known
        if unknown:
            log.warning("ignoring unknown keys in %s: %s", p, ", ".join(sorted(unknown)))
        missing = known - set(raw)
        if missing:
            log.info("state file predates fields %s; using defaults", ", ".join(sorted(missing)))
        return cls(path=p, **{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        d = asdict(self)
        d.pop("path", None)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=2))
        tmp.replace(self.path)            # atomic; a killed process cannot corrupt state

    # ---------------------------------------------------------------- daily
    def roll_day_if_needed(self, equity: float) -> bool:
        """Returns True when a new UTC day started. Halts do NOT auto-clear."""
        now = today_utc()
        if now == self.day and self.day_start_equity > 0:
            return False
        log.info("new trading day %s, opening equity %.2f USDT", now, equity)
        self.day = now
        self.day_start_equity = equity
        self.trades_today = 0
        self.realized_today = 0.0
        self.target_reached_today = False
        if not self.schedule_start_date:
            self.schedule_start_date = now
        self.save()
        return True

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} :: {reason}"
        log.critical("HALTED: %s", reason)
        self.save()


def reconcile(api, symbol: str) -> dict:
    """
    Ask the exchange what is actually true. Called on every boot, because a
    process that crashed mid-order must never assume its own last-known state.
    """
    positions = api.positions(symbol)
    orders = api.open_orders(symbol)
    equity = api.usdt_equity()
    pos = positions[0] if positions else None
    snapshot = {
        "equity": equity,
        "position_amt": float(pos["positionAmt"]) if pos else 0.0,
        "entry_price": float(pos["entryPrice"]) if pos else 0.0,
        "unrealized": float(pos["unRealizedProfit"]) if pos else 0.0,
        "liquidation": float(pos["liquidationPrice"]) if pos else 0.0,
        "open_orders": [{"id": o["clientOrderId"], "side": o["side"],
                         "type": o["type"], "qty": o["origQty"],
                         "price": o["price"]} for o in orders],
        "open_order_ids": {o["clientOrderId"] for o in orders},
    }
    log.info("reconciled: equity=%.2f position=%s open_orders=%d",
             equity, snapshot["position_amt"], len(orders))
    return snapshot


def client_order_id(tag: str, seq: int) -> str:
    """Deterministic enough to be traceable, unique enough to be idempotent."""
    return f"{tag}-{seq}-{int(time.time())}"[:36]
