"""
The slower-chart context: bot/context.py, the entry filter
(context.entry.block_against_trend) and supervisor patience
(supervise.patience).
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.binanceapi import BinanceError                      # noqa: E402
from bot.context import ContextConfig, closed_closes, trend_direction  # noqa: E402
from bot.strategies.base import Signal                       # noqa: E402
from bot.supervise import SuperviseConfig, supervise         # noqa: E402
from test_protect import ExchangeAPI, _cleanup, _engine      # noqa: E402
from test_supervise import long_pos, ok, short_pos        # noqa: E402

H4 = 4 * 3600_000


def rising(n=30, start=1.0, step=0.01):
    return [start + i * step for i in range(n)]


class TestTrendDirection(unittest.TestCase):
    def test_up(self):
        self.assertEqual(trend_direction(rising()), 1)

    def test_down(self):
        self.assertEqual(trend_direction(rising(step=-0.01)), -1)

    def test_flat_when_price_and_average_disagree(self):
        closes = rising()
        closes[-1] = 0.5            # last close under a rising average
        self.assertEqual(trend_direction(closes), 0)

    def test_too_few_bars_is_flat_not_a_guess(self):
        self.assertEqual(trend_direction(rising(n=24)), 0)
        self.assertEqual(trend_direction(rising(n=25)), 1)

    def test_a_forming_bar_never_counts(self):
        kl = [[i * H4, 0, 0, 0, str(1.0 + i), 0, (i + 1) * H4 - 1] for i in range(3)]
        self.assertEqual(closed_closes(kl, now_ms=2 * H4 + 5), [1.0, 2.0])


class TestConfig(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(ContextConfig().entry.block_against_trend)
        self.assertFalse(SuperviseConfig().patience.enabled)

    def test_the_shipped_config_leaves_both_off(self):
        from bot.config import Config
        cfg = Config.load(overlay=False)
        self.assertFalse(cfg.context.entry.block_against_trend)
        self.assertFalse(cfg.supervise.patience.enabled)
        self.assertEqual(cfg.context.trend.interval, "4h")

    def test_a_bad_key_names_its_group(self):
        with self.assertRaises(TypeError) as ctx:
            ContextConfig(entry={"block_against": True})
        self.assertIn("context.entry", str(ctx.exception))


def patient(**skip):
    return SuperviseConfig(enabled=True, patience={"enabled": True, **skip})


class TestPatience(unittest.TestCase):
    def far(self, **kw):
        """Target 3.0 away, drifting 0.01/bar: 300 bars, well past 20."""
        return ok(100.0, atr=0.5, efficiency=0.1, net_move_per_bar=0.01, **kw)

    def test_a_losing_trade_with_the_trend_behind_it_is_not_cut(self):
        p = long_pos()
        r = self.far(age_seconds=7200.0, higher_trend=1)
        self.assertFalse(supervise(p, r, patient()).exit_now)

    def test_the_same_trade_is_cut_when_the_trend_is_flat(self):
        p = long_pos()
        r = self.far(age_seconds=7200.0, higher_trend=0)
        self.assertTrue(supervise(p, r, patient()).exit_now)

    def test_a_trend_against_the_trade_gives_no_patience(self):
        p = long_pos()
        r = self.far(age_seconds=7200.0, higher_trend=-1)
        self.assertTrue(supervise(p, r, patient()).exit_now)

    def test_a_short_is_patient_in_a_downtrend(self):
        p = short_pos()
        r = ok(100.3, atr=0.5, efficiency=0.9, net_move_per_bar=+0.3,
               age_seconds=7200.0, higher_trend=-1)
        self.assertFalse(supervise(p, r, patient()).exit_now)

    def test_patience_off_changes_nothing(self):
        p = long_pos()
        r = self.far(age_seconds=7200.0, higher_trend=1)
        self.assertTrue(supervise(p, r, SuperviseConfig(enabled=True)).exit_now)

    def test_failed_breakout_is_skipped_while_patient(self):
        p = long_pos(peak=102.0, ref_level=100.5)
        self.assertFalse(supervise(p, ok(100.4, higher_trend=1), patient()).exit_now)
        self.assertTrue(supervise(p, ok(100.4, higher_trend=0), patient()).exit_now)

    def test_only_the_chosen_exits_are_skipped(self):
        p = long_pos(peak=102.0, ref_level=100.5)
        cfg = patient(skip_failed_breakout=False)
        self.assertTrue(supervise(p, ok(100.4, higher_trend=1), cfg).exit_now)

    def test_break_even_still_moves_the_stop_while_patient(self):
        p = long_pos(peak=102.1)
        plan = supervise(p, ok(102.0, higher_trend=1), patient())
        self.assertIsNotNone(plan.stop)


class _TrendAPI(ExchangeAPI):
    def __init__(self, trend=None, **kw):
        super().__init__(**kw)
        self.trend = trend
        self.kline_calls = 0

    def klines(self, symbol, interval, limit=200, end_ms=None):
        self.kline_calls += 1
        if self.trend is None:
            raise BinanceError(-1003, "too many requests", "/klines")
        closes = rising(n=limit, step=0.01 * self.trend) if self.trend else [1.0] * limit
        return [[i * H4, 0, 0, 0, str(c), 0, (i + 1) * H4 - 1]
                for i, c in enumerate(closes)]


SIG = Signal("BUY", entry=0.1566, stop=0.1500, take_profit=0.1650)


class TestEntryFilter(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def _engine(self, trend, block=True):
        api = _TrendAPI(trend=trend, marks={"STGUSDT": 0.1566})
        e = _engine(api)
        e.cfg.context.entry.block_against_trend = block
        return api, e

    def test_a_buy_into_a_4h_downtrend_is_skipped(self):
        api, e = self._engine(trend=-1)
        e.place(SIG, 5.36, "n", symbol="STGUSDT")
        self.assertFalse([c for c in api.calls if c[0] == "order"])
        self.assertNotIn("STGUSDT", e.book)

    def test_a_buy_with_the_trend_trades(self):
        api, e = self._engine(trend=1)
        e.place(SIG, 5.36, "n", symbol="STGUSDT")
        self.assertIn("STGUSDT", e.book)

    def test_a_flat_trend_never_blocks(self):
        api, e = self._engine(trend=0)
        e.place(SIG, 5.36, "n", symbol="STGUSDT")
        self.assertIn("STGUSDT", e.book)

    def test_an_unreadable_trend_never_blocks(self):
        api, e = self._engine(trend=None)
        e.place(SIG, 5.36, "n", symbol="STGUSDT")
        self.assertIn("STGUSDT", e.book)

    def test_off_means_no_request_at_all(self):
        api, e = self._engine(trend=-1, block=False)
        e.place(SIG, 5.36, "n", symbol="STGUSDT")
        self.assertIn("STGUSDT", e.book)
        self.assertEqual(api.kline_calls, 0)

    def test_the_reading_is_cached(self):
        api, e = self._engine(trend=1)
        e.higher_trend("STGUSDT")
        e.higher_trend("STGUSDT")
        self.assertEqual(api.kline_calls, 1)


if __name__ == "__main__":
    unittest.main()
