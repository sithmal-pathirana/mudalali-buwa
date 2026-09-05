"""Strategy interface. A strategy proposes; the risk layer disposes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bar:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_kline(cls, k: list) -> "Bar":
        return cls(int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5]))


@dataclass
class Signal:
    side: str            # "BUY" | "SELL"
    entry: float         # limit price to post at
    stop: float          # protective stop price -- required, never optional
    reason: str = ""
    take_profit: float = 0.0


class Strategy:
    """Subclasses implement on_bars. Returning None means 'do nothing', which
    is the correct answer the overwhelming majority of the time."""

    name = "base"
    warmup = 50

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        raise NotImplementedError

    def feasible(self, equity: float, price: float, rules) -> tuple[bool, str]:
        """Can this strategy be run at all with this much capital? Default: yes."""
        return True, ""
