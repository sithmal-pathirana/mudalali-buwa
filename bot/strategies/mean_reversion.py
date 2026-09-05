"""
Z-score fade: the natural complement to a breakout system.

Where trend_atr buys a break of the range, this sells it -- it assumes an
extension away from the recent mean is more likely to snap back than continue.
The two are close to mirror images, which is the point: a regime that is
hostile to one tends to be hospitable to the other.

Same discipline as every other strategy here: an explicit stop is computed
before the signal is returned, and the trade is skipped when the market is too
quiet for the move to cover its own fees.
"""

from __future__ import annotations

import statistics

from .base import Bar, Signal, Strategy
from .trend_atr import atr


class MeanReversion(Strategy):
    name = "mean_reversion"

    def __init__(self, window: int = 20, z_entry: float = 2.0,
                 atr_stop_mult: float = 1.5, min_atr_pct: float = 0.15,
                 target_at_mean: bool = True):
        self.window = window
        self.z_entry = z_entry
        self.atr_stop_mult = atr_stop_mult
        self.min_atr_pct = min_atr_pct
        self.target_at_mean = target_at_mean
        self.warmup = window + 5

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        if len(bars) < self.warmup or position_amt != 0:
            return None

        closes = [b.close for b in bars[-self.window:]]
        mean = statistics.fmean(closes)
        sd = statistics.pstdev(closes)
        if sd <= 0:
            return None

        last = bars[-1]
        a = atr(bars, 14)
        if a <= 0 or a / last.close * 100 < self.min_atr_pct:
            return None

        z = (last.close - mean) / sd

        # Stretched below the mean -> buy the snap back.
        if z <= -self.z_entry:
            stop = last.close - self.atr_stop_mult * a
            target = mean if self.target_at_mean else last.close + self.atr_stop_mult * a
            if target <= last.close:
                return None
            return Signal("BUY", last.close, stop=stop, take_profit=target,
                          reason=f"z={z:.2f} below {self.window}-bar mean {mean:.4f}")

        # Stretched above -> sell it.
        if z >= self.z_entry:
            stop = last.close + self.atr_stop_mult * a
            target = mean if self.target_at_mean else last.close - self.atr_stop_mult * a
            if target >= last.close:
                return None
            return Signal("SELL", last.close, stop=stop, take_profit=target,
                          reason=f"z={z:+.2f} above {self.window}-bar mean {mean:.4f}")

        return None
