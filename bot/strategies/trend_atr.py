"""
Donchian breakout with an ATR stop.

Honest label: this is a *research harness*, not a proven edge. Breakout systems
have historically worked in trending markets and bled out in ranging ones, and
after fees the margin is thin. It is here because it is simple enough to reason
about, it always defines a stop, and it gives the backtester something real to
measure. Do not deploy it with money you need until YOU have measured it.
"""

from __future__ import annotations

from .base import Bar, Signal, Strategy


def atr(bars: list[Bar], period: int) -> float:
    trs = []
    for prev, cur in zip(bars[-period - 1:-1], bars[-period:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    return sum(trs) / len(trs) if trs else 0.0


class TrendATR(Strategy):
    name = "trend_atr"

    def __init__(self, channel: int = 20, atr_period: int = 14,
                 atr_stop_mult: float = 2.0, atr_target_mult: float = 3.0,
                 min_atr_pct: float = 0.15):
        self.channel = channel
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.min_atr_pct = min_atr_pct
        self.warmup = max(channel, atr_period) + 5

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        if len(bars) < self.warmup or position_amt != 0:
            return None

        window = bars[-self.channel - 1:-1]      # exclude the forming bar
        last = bars[-1]
        hi = max(b.high for b in window)
        lo = min(b.low for b in window)
        a = atr(bars, self.atr_period)
        if a <= 0:
            return None

        # Skip dead markets: if the range is smaller than the round-trip cost,
        # there is nothing to win even when the direction is right.
        if a / last.close * 100 < self.min_atr_pct:
            return None

        if last.close > hi:
            return Signal("BUY", last.close,
                          stop=last.close - self.atr_stop_mult * a,
                          take_profit=last.close + self.atr_target_mult * a,
                          reason=f"close {last.close:.2f} broke {self.channel}-bar high {hi:.2f}, ATR {a:.2f}")
        if last.close < lo:
            return Signal("SELL", last.close,
                          stop=last.close + self.atr_stop_mult * a,
                          take_profit=last.close - self.atr_target_mult * a,
                          reason=f"close {last.close:.2f} broke {self.channel}-bar low {lo:.2f}, ATR {a:.2f}")
        return None
