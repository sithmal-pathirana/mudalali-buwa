"""
Donchian breakout with an ATR stop.

Honest label: this is a *research harness*, not a proven edge. Breakout systems
have historically worked in trending markets and bled out in ranging ones, and
after fees the margin is thin. It is here because it is simple enough to reason
about, it always defines a stop, and it gives the backtester something real to
measure. Do not deploy it with money you need until YOU have measured it.
"""

from __future__ import annotations

from ..regime import efficiency_ratio
from .base import Bar, Signal, Strategy


def ema(values: list[float], period: int) -> float:
    """Exponential moving average, seeded at the first value given."""
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


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
                 min_atr_pct: float = 0.15,
                 confirm_channel: int = 0, recent_er_window: int = 0,
                 min_recent_er: float = 0.0, min_volume_ratio: float = 0.0,
                 volume_window: int = 20, allow_shorts: bool = True,
                 min_ema_lead_pct: float = 0.0, lead_ema_period: int = 50):
        self.channel = channel
        self.atr_period = atr_period
        self.atr_stop_mult = atr_stop_mult
        self.atr_target_mult = atr_target_mult
        self.min_atr_pct = min_atr_pct
        # Entry-quality filters, all off at their defaults. LITUSDT on
        # 2026-09-14 is the case each one refuses: a 6-bar high broken in the
        # middle of a five-hour range under the day's real high (4.6978 not
        # cleared), called "trending" only because the 30-bar ER still counted
        # a pump from 7 hours earlier (20-bar ER 0.01), on 0.69x average volume.
        #   confirm_channel   close must also clear this longer channel
        #   recent_er_*       the trend must be CURRENT, not a stale window
        #   min_volume_ratio  breakout bar volume vs the prior volume_window
        self.confirm_channel = confirm_channel
        self.recent_er_window = recent_er_window
        self.min_recent_er = min_recent_er
        self.min_volume_ratio = min_volume_ratio
        self.volume_window = volume_window
        # Measured 2026-09-22 over 80 coins x 78 days (see config.yaml):
        #   allow_shorts      False = breakouts to the downside are ignored
        #   min_ema_lead_pct  close must be at least this far beyond its
        #                     lead_ema_period EMA, in percent, in the trade's
        #                     direction -- a breakout with momentum behind it
        # The EMA is seeded 2 x lead_ema_period bars back, so it needs that
        # much history; warmup grows to match while the filter is on.
        self.allow_shorts = allow_shorts
        self.min_ema_lead_pct = min_ema_lead_pct
        self.lead_ema_period = lead_ema_period
        self.warmup = max(channel, atr_period, confirm_channel,
                          recent_er_window, volume_window,
                          2 * lead_ema_period if min_ema_lead_pct > 0 else 0) + 5

    def entry_filter(self, bars: list[Bar], side: str) -> str:
        """Why this breakout is refused, or "" to take it."""
        last = bars[-1]
        if side == "SELL" and not self.allow_shorts:
            return "shorts are off"
        if self.min_ema_lead_pct > 0:
            closes = [b.close for b in bars[-2 * self.lead_ema_period:]]
            base = ema(closes, self.lead_ema_period)
            lead = (last.close / base - 1) * 100 if base > 0 else 0.0
            if side == "SELL":
                lead = -lead
            if lead < self.min_ema_lead_pct:
                return (f"{lead:.2f}% beyond the {self.lead_ema_period}-bar EMA, "
                        f"under {self.min_ema_lead_pct:.2f}%")
        if self.confirm_channel > self.channel:
            prior = bars[-self.confirm_channel - 1:-1]
            if side == "BUY" and last.close <= max(b.high for b in prior):
                return f"under the {self.confirm_channel}-bar high"
            if side == "SELL" and last.close >= min(b.low for b in prior):
                return f"above the {self.confirm_channel}-bar low"
        if self.recent_er_window and self.min_recent_er > 0:
            er = efficiency_ratio(bars, self.recent_er_window)
            if er < self.min_recent_er:
                return f"{self.recent_er_window}-bar ER {er:.2f} (trend is stale)"
        if self.min_volume_ratio > 0:
            prior = bars[-self.volume_window - 1:-1]
            avg = sum(b.volume for b in prior) / len(prior) if prior else 0.0
            if avg > 0 and last.volume < self.min_volume_ratio * avg:
                return f"breakout volume {last.volume / avg:.2f}x average"
        return ""

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

        side = "BUY" if last.close > hi else "SELL" if last.close < lo else ""
        if side and self.entry_filter(bars, side):
            return None

        if last.close > hi:
            return Signal("BUY", last.close,
                          stop=last.close - self.atr_stop_mult * a,
                          take_profit=last.close + self.atr_target_mult * a,
                          ref_level=hi,
                          reason=f"close {last.close:.2f} broke {self.channel}-bar high {hi:.2f}, ATR {a:.2f}")
        if last.close < lo:
            return Signal("SELL", last.close,
                          stop=last.close + self.atr_stop_mult * a,
                          take_profit=last.close - self.atr_target_mult * a,
                          ref_level=lo,
                          reason=f"close {last.close:.2f} broke {self.channel}-bar low {lo:.2f}, ATR {a:.2f}")
        return None
