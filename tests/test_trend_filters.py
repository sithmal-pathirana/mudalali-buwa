"""
Entry-quality filters on trend_atr. LITUSDT on 2026-09-14 was bought on a
6-bar high inside a five-hour range, under the real high, on thin volume.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.strategies.base import Bar               # noqa: E402
from bot.strategies.trend_atr import TrendATR     # noqa: E402


def bars_from(closes, vols=None, spread=0.5):
    vols = vols or [100.0] * len(closes)
    return [Bar(i * 900_000, c, c + spread, c - spread, c, v)
            for i, (c, v) in enumerate(zip(closes, vols))]


# A pump to 110, then a long chop around 104, then a pop to 106.5: a 6-bar
# high broken, the 20-bar high (110.5) not, and the last 20 bars go nowhere.
PUMP_THEN_CHOP = ([100.0] * 10 + [102, 105, 108, 110]
                  + [104, 103, 104, 103] * 4 + [106.5])


class TestDefaultsChangeNothing(unittest.TestCase):
    def test_unfiltered_strategy_still_buys_the_short_breakout(self):
        s = TrendATR(channel=6)
        self.assertIsNotNone(s.on_bars(bars_from(PUMP_THEN_CHOP), 0.0))


class TestFilters(unittest.TestCase):
    def test_confirm_channel_refuses_a_break_under_the_real_high(self):
        s = TrendATR(channel=6, confirm_channel=20)
        self.assertIsNone(s.on_bars(bars_from(PUMP_THEN_CHOP), 0.0))

    def test_recent_er_refuses_a_stale_trend(self):
        s = TrendATR(channel=6, recent_er_window=20, min_recent_er=0.25)
        self.assertIsNone(s.on_bars(bars_from(PUMP_THEN_CHOP), 0.0))

    def test_volume_filter_refuses_a_thin_breakout(self):
        vols = [100.0] * (len(PUMP_THEN_CHOP) - 1) + [69.0]
        s = TrendATR(channel=6, min_volume_ratio=1.0)
        self.assertIsNone(s.on_bars(bars_from(PUMP_THEN_CHOP, vols), 0.0))

    def test_a_clean_current_breakout_on_volume_passes_every_filter(self):
        closes = [100.0 + 0.5 * i for i in range(40)] + [121.0]
        vols = [100.0] * 40 + [180.0]
        s = TrendATR(channel=6, confirm_channel=20, recent_er_window=20,
                     min_recent_er=0.25, min_volume_ratio=1.0)
        sig = s.on_bars(bars_from(closes, vols), 0.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "BUY")

    def test_sell_side_mirrors(self):
        closes = [200.0 - 0.5 * i for i in range(40)] + [179.0]
        s = TrendATR(channel=6, confirm_channel=20, recent_er_window=20,
                     min_recent_er=0.25)
        sig = s.on_bars(bars_from(closes), 0.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "SELL")


if __name__ == "__main__":
    unittest.main()
