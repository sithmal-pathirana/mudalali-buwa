"""
Market regime detection.

The premise behind switching strategies is that different edges exist in
different conditions: a breakout system needs price to travel, a fade system
needs it to oscillate. Running either one in the wrong regime is how each
loses money.

The measure here is Kaufman's Efficiency Ratio -- net movement divided by
total path length over the same window:

    ER = |close[n] - close[0]| / sum(|close[i] - close[i-1]|)

ER near 1.0 means price went somewhere in a straight line (trending).
ER near 0.0 means it covered a lot of ground and ended up where it started
(choppy). It is cheap, has no fitted parameters beyond the window, and unlike
an indicator crossover it measures something structural about the path rather
than predicting direction.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .strategies.base import Bar


class Regime(Enum):
    TRENDING = "trending"
    RANGING = "ranging"
    UNCLEAR = "unclear"      # deliberately tradeable by nobody


@dataclass
class RegimeReading:
    regime: Regime
    efficiency: float
    atr_pct: float
    note: str = ""

    def __str__(self) -> str:
        return (f"{self.regime.value} (ER {self.efficiency:.3f}, "
                f"ATR {self.atr_pct:.2f}%)")


def efficiency_ratio(bars: list[Bar], window: int) -> float:
    """Kaufman's Efficiency Ratio over the last `window` closes."""
    if len(bars) < window + 1:
        return 0.0
    closes = [b.close for b in bars[-(window + 1):]]
    net = abs(closes[-1] - closes[0])
    path = sum(abs(b - a) for a, b in zip(closes, closes[1:]))
    return net / path if path > 0 else 0.0


def realised_vol_pct(bars: list[Bar], window: int) -> float:
    """Mean true range over the window, as a percentage of price."""
    if len(bars) < window + 1:
        return 0.0
    trs = []
    for prev, cur in zip(bars[-window - 1:-1], bars[-window:]):
        trs.append(max(cur.high - cur.low,
                       abs(cur.high - prev.close),
                       abs(cur.low - prev.close)))
    atr = sum(trs) / len(trs) if trs else 0.0
    last = bars[-1].close
    return (atr / last * 100) if last else 0.0


class RegimeDetector:
    """
    Classifies the current bar, with hysteresis.

    Hysteresis matters more than the thresholds. A detector that flips regime
    on a single marginal bar will hand you whipsaw losses from switching that
    look exactly like strategy losses -- so a new regime must persist for
    `confirm_bars` before it is adopted.
    """

    def __init__(self, window: int = 30, trend_above: float = 0.35,
                 range_below: float = 0.20, min_atr_pct: float = 0.15,
                 confirm_bars: int = 3):
        if not 0.0 < range_below < trend_above < 1.0:
            raise ValueError("need 0 < range_below < trend_above < 1")
        self.window = window
        self.trend_above = trend_above
        self.range_below = range_below
        self.min_atr_pct = min_atr_pct
        self.confirm_bars = confirm_bars
        self.current = Regime.UNCLEAR
        self._candidate = Regime.UNCLEAR
        self._streak = 0
        self.switches = 0

    @property
    def warmup(self) -> int:
        return self.window + 2

    def classify(self, bars: list[Bar]) -> RegimeReading:
        """The instantaneous reading, before hysteresis."""
        er = efficiency_ratio(bars, self.window)
        atr_pct = realised_vol_pct(bars, self.window)

        if atr_pct < self.min_atr_pct:
            return RegimeReading(Regime.UNCLEAR, er, atr_pct,
                                 "too quiet to cover fees")
        if er >= self.trend_above:
            return RegimeReading(Regime.TRENDING, er, atr_pct,
                                 "price is travelling")
        if er <= self.range_below:
            return RegimeReading(Regime.RANGING, er, atr_pct,
                                 "price is oscillating")
        return RegimeReading(Regime.UNCLEAR, er, atr_pct,
                             "between regimes -- no strategy claims this")

    def update(self, bars: list[Bar]) -> RegimeReading:
        """Apply hysteresis and return the reading with the ADOPTED regime."""
        reading = self.classify(bars)
        if reading.regime == self.current:
            self._candidate = self.current
            self._streak = 0
            return reading

        if reading.regime == self._candidate:
            self._streak += 1
        else:
            self._candidate = reading.regime
            self._streak = 1

        if self._streak >= self.confirm_bars:
            self.current = self._candidate
            self._streak = 0
            self.switches += 1

        # Report what is actually being acted on, not the raw reading.
        return RegimeReading(self.current, reading.efficiency,
                             reading.atr_pct, reading.note)
