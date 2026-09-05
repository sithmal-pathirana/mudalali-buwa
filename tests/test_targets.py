"""Target schedule and proximity-alert logic."""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.positions import ActivePosition          # noqa: E402
from bot.targets import TargetSchedule            # noqa: E402

SCHEDULE = {"stop_when_reached": True,
            "schedule": [{"from_day": 1, "usd_per_day": 2.0},
                         {"from_day": 16, "usd_per_day": 3.0}]}


def sched(start="2026-08-01") -> TargetSchedule:
    s = TargetSchedule.from_config(SCHEDULE)
    s.start_date = start
    return s


class TestSchedule(unittest.TestCase):
    def test_day_numbering_is_one_based(self):
        s = sched()
        self.assertEqual(s.day_number(date(2026, 8, 1)), 1)
        self.assertEqual(s.day_number(date(2026, 8, 15)), 15)

    def test_target_escalates_on_day_16_not_15(self):
        s = sched()
        self.assertEqual(s.today_target(date(2026, 8, 15)), 2.0)   # 15th day at $2
        self.assertEqual(s.today_target(date(2026, 8, 16)), 3.0)

    def test_escalation_is_announced_once_on_the_switch_day(self):
        s = sched()
        self.assertIsNone(s.escalates_today(date(2026, 8, 15)))
        self.assertIsNotNone(s.escalates_today(date(2026, 8, 16)))
        self.assertIsNone(s.escalates_today(date(2026, 8, 17)))

    def test_schedule_must_start_at_day_one(self):
        with self.assertRaises(ValueError):
            TargetSchedule.from_config({"schedule": [{"from_day": 3, "usd_per_day": 2.0}]})

    def test_progress_stops_trading_only_once_target_is_met(self):
        s = sched()
        below = s.progress(1.99, date(2026, 8, 1))
        self.assertFalse(below.reached)
        self.assertFalse(below.stop_trading)
        self.assertAlmostEqual(below.remaining, 0.01)

        met = s.progress(2.00, date(2026, 8, 1))
        self.assertTrue(met.reached)
        self.assertTrue(met.stop_trading)
        self.assertEqual(met.remaining, 0.0)

    def test_stop_when_reached_can_be_disabled(self):
        raw = dict(SCHEDULE, stop_when_reached=False)
        s = TargetSchedule.from_config(raw)
        s.start_date = "2026-08-01"
        p = s.progress(5.0, date(2026, 8, 1))
        self.assertTrue(p.reached)
        self.assertFalse(p.stop_trading)

    def test_a_losing_day_never_reads_as_reached(self):
        s = sched()
        p = s.progress(-4.0, date(2026, 8, 1))
        self.assertFalse(p.reached)
        self.assertEqual(p.remaining, 6.0)

    def test_escalation_raises_the_required_daily_return(self):
        """The finding from tools/projection.py, locked into a test."""
        s = sched()
        eq_day15 = 43.0 + 14 * 2.0          # 71.00
        eq_day16 = eq_day15 + 2.0           # 73.00
        ask15 = s.today_target(date(2026, 8, 15)) / eq_day15
        ask16 = s.today_target(date(2026, 8, 16)) / eq_day16
        self.assertLess(ask15, 0.030)       # 2.82%/day
        self.assertGreater(ask16, 0.040)    # 4.11%/day -- back where it started
        self.assertGreater(ask16, ask15)


class TestProximity(unittest.TestCase):
    def long(self):
        return ActivePosition("BTCUSDT", "BUY", entry=100.0, stop=90.0,
                              take_profit=120.0, qty=1.0, entry_order_id="e1", tag="e1")

    def short(self):
        return ActivePosition("BTCUSDT", "SELL", entry=100.0, stop=110.0,
                              take_profit=80.0, qty=1.0, entry_order_id="e2", tag="e2")

    def test_long_progress(self):
        p = self.long()
        self.assertAlmostEqual(p.progress_to_tp(100.0), 0.0)
        self.assertAlmostEqual(p.progress_to_tp(116.0), 0.8)
        self.assertAlmostEqual(p.progress_to_stop(92.0), 0.8)

    def test_short_progress_mirrors_long(self):
        p = self.short()
        self.assertAlmostEqual(p.progress_to_tp(84.0), 0.8)
        self.assertAlmostEqual(p.progress_to_stop(108.0), 0.8)

    def test_moving_the_wrong_way_gives_negative_progress(self):
        p = self.long()
        self.assertLess(p.progress_to_tp(95.0), 0.0)

    def test_unrealised_pnl_sign_is_correct_for_both_sides(self):
        self.assertGreater(self.long().unrealized(110.0), 0)
        self.assertLess(self.long().unrealized(95.0), 0)
        self.assertGreater(self.short().unrealized(95.0), 0)
        self.assertLess(self.short().unrealized(110.0), 0)

    def test_alert_threshold_crossing(self):
        """80% threshold must trigger for both directions, both sides."""
        for pos, tp_px, sl_px in [(self.long(), 116.0, 92.0), (self.short(), 84.0, 108.0)]:
            self.assertGreaterEqual(pos.progress_to_tp(tp_px), 0.8)
            self.assertGreaterEqual(pos.progress_to_stop(sl_px), 0.8)


class TestNotifierDedupe(unittest.TestCase):
    def test_dedupe_key_suppresses_repeats_until_cleared(self):
        from bot.notify import Event, Notifier
        n = Notifier(symbol="BTCUSDT")
        sent = []
        n._safe = staticmethod(lambda c, s, b: sent.append(s))   # no channels enabled anyway
        for _ in range(50):
            n.send(Event.APPROACH_SL, "body", dedupe_key="sl:e1")
        self.assertEqual(len(n._fired), 1)
        n.clear_position_alerts("e1")
        self.assertEqual(len(n._fired), 0)
