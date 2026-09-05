"""
momentum_burst: the aggressive-mode strategy.

Chases fresh movement on a short timeframe and rides it with a trailing stop
rather than a fixed target, so a winner runs until it turns. Used only when
aggressive mode is on; the switcher never routes to it.

Honest label, same as every other strategy here: this is the family that looks
best in a bull run and produces the fastest drawdowns in a chop. It has not
been measured out-of-sample. Run tools/oos.py before believing anything.
"""

from __future__ import annotations

from .base import Bar, Signal, Strategy
from .trend_atr import atr


class MomentumBurst(Strategy):
    name = "momentum_burst"

    def __init__(self, lookback: int = 12, breakout_bars: int = 6,
                 atr_period: int = 14, trailing_atr_mult: float = 2.0,
                 min_atr_pct: float = 0.30, expansion_ratio: float = 1.2,
                 target_atr_mult: float = 4.0):
        self.lookback = lookback
        self.breakout_bars = breakout_bars
        self.atr_period = atr_period
        self.trailing_atr_mult = trailing_atr_mult
        self.min_atr_pct = min_atr_pct
        self.expansion_ratio = expansion_ratio
        self.target_atr_mult = target_atr_mult
        self.warmup = max(lookback, atr_period) + breakout_bars + 5

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        if len(bars) < self.warmup or position_amt != 0:
            return None

        last = bars[-1]
        a = atr(bars, self.atr_period)
        if a <= 0 or a / last.close * 100 < self.min_atr_pct:
            return None

        # Range must be EXPANDING, not merely wide. A breakout into a
        # contracting range is the shape that fakes out most reliably.
        recent = sum(b.high - b.low for b in bars[-self.breakout_bars:]) / self.breakout_bars
        older = sum(b.high - b.low for b in bars[-self.lookback:-self.breakout_bars])
        older /= max(1, self.lookback - self.breakout_bars)
        if older <= 0 or recent / older < self.expansion_ratio:
            return None

        window = bars[-self.breakout_bars - 1:-1]
        hi = max(b.high for b in window)
        lo = min(b.low for b in window)

        # Trailing distance is the stop at entry; the engine ratchets it.
        if last.close > hi:
            return Signal("BUY", last.close,
                          stop=last.close - self.trailing_atr_mult * a,
                          take_profit=last.close + self.target_atr_mult * a,
                          reason=f"burst up through {hi:.6f}, range x{recent/older:.2f}")
        if last.close < lo:
            return Signal("SELL", last.close,
                          stop=last.close + self.trailing_atr_mult * a,
                          take_profit=last.close - self.target_atr_mult * a,
                          reason=f"burst down through {lo:.6f}, range x{recent/older:.2f}")
        return None
