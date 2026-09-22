"""Long-only and EMA-lead entry filters on trend_atr (2026-09-22)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.strategies.base import Bar            # noqa: E402
from bot.strategies.trend_atr import TrendATR  # noqa: E402


def bars_from(closes):
    return [Bar(i, c, c * 1.004, c * 0.996, c, 100.0) for i, c in enumerate(closes)]


def rally(n=130, step=0.004):
    """A steady climb that ends on a fresh 20-bar high."""
    closes = [100.0 * (1 + step) ** i for i in range(n)]
    closes[-1] *= 1.01
    return bars_from(closes)


def slide(n=130, step=0.004):
    closes = [100.0 * (1 - step) ** i for i in range(n)]
    closes[-1] *= 0.99
    return bars_from(closes)


class EntryMomentumTest(unittest.TestCase):
    def test_defaults_leave_behaviour_unchanged(self):
        s = TrendATR()
        self.assertEqual(s.on_bars(slide(), 0.0).side, "SELL")
        self.assertEqual(s.warmup, 25)

    def test_shorts_off_refuses_a_downside_breakout(self):
        s = TrendATR(allow_shorts=False)
        self.assertIsNone(s.on_bars(slide(), 0.0))
        self.assertEqual(s.on_bars(rally(), 0.0).side, "BUY")

    def test_ema_lead_takes_a_breakout_well_clear_of_the_ema(self):
        s = TrendATR(min_ema_lead_pct=3.0, lead_ema_period=50)
        self.assertEqual(s.warmup, 105)
        self.assertEqual(s.on_bars(rally(step=0.004), 0.0).side, "BUY")

    def test_ema_lead_refuses_a_breakout_hugging_the_ema(self):
        s = TrendATR(min_ema_lead_pct=3.0, lead_ema_period=50)
        self.assertIsNone(s.on_bars(rally(step=0.0003), 0.0))
        self.assertIn("EMA", s.entry_filter(rally(step=0.0003), "BUY"))

    def test_ema_lead_is_measured_in_the_trade_direction(self):
        s = TrendATR(min_ema_lead_pct=3.0, lead_ema_period=50)
        self.assertEqual(s.on_bars(slide(step=0.004), 0.0).side, "SELL")

    def test_too_little_history_gives_no_signal(self):
        s = TrendATR(min_ema_lead_pct=3.0, lead_ema_period=50)
        self.assertIsNone(s.on_bars(rally()[-62:], 0.0))


if __name__ == "__main__":
    unittest.main()
