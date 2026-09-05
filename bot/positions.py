"""
Tracking an open position between entry and exit, and deciding when to warn.

Split out of the engine because the proximity logic is fiddly enough to want
its own tests: "80% of the way to the stop" has to mean the same thing for a
short as for a long, and must fire once rather than on every tick.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ActivePosition:
    symbol: str
    side: str                 # BUY (long) | SELL (short)
    entry: float
    stop: float
    take_profit: float
    qty: float
    entry_order_id: str
    stop_order_id: str = ""
    tp_order_id: str = ""
    tag: str = ""             # used to scope alert de-duplication

    @property
    def is_long(self) -> bool:
        return self.side == "BUY"

    @property
    def notional(self) -> float:
        return self.entry * self.qty

    def unrealized(self, price: float) -> float:
        move = (price - self.entry) if self.is_long else (self.entry - price)
        return move * self.qty

    # ------------------------------------------------------------- progress
    def progress_to_tp(self, price: float) -> float:
        """0.0 at entry, 1.0 at the take-profit. Negative when moving away."""
        span = (self.take_profit - self.entry) if self.is_long else (self.entry - self.take_profit)
        if span <= 0:
            return 0.0
        travelled = (price - self.entry) if self.is_long else (self.entry - price)
        return travelled / span

    def progress_to_stop(self, price: float) -> float:
        """0.0 at entry, 1.0 at the stop."""
        span = (self.entry - self.stop) if self.is_long else (self.stop - self.entry)
        if span <= 0:
            return 0.0
        travelled = (self.entry - price) if self.is_long else (price - self.entry)
        return travelled / span

    def distance_pct(self, price: float, level: float) -> float:
        return abs(level - price) / price * 100 if price else 0.0

    def status_line(self, price: float) -> str:
        return (f"{self.side} {self.qty:g} @ {self.entry:,.4f}\n"
                f"now {price:,.4f}  ({self.unrealized(price):+.2f} USDT unrealised)\n"
                f"TP {self.take_profit:,.4f}  {self.progress_to_tp(price)*100:5.1f}% of the way\n"
                f"SL {self.stop:,.4f}  {self.progress_to_stop(price)*100:5.1f}% of the way")
