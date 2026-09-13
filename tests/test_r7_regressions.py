"""
Regressions for the phantom KAVAUSDT position and the collapsed 1R of
2026-09-12/13.

1. Entries rest as GTC limit orders, but ActivePosition went into the book the
   moment the order was PLACED, with nothing downstream asking whether it had
   filled. On 2026-09-13 KAVAUSDT's entry never filled; the bot announced
   "trade opened", supervised it for 34 minutes, moved its stop 11 times, split
   its take-profit ("banking 264.7") and sent an approaching-take-profit alert,
   all against a position that did not exist. It booked +0.0000 and held the
   only position slot while KAVA ran 8%.

2. adopt_open_positions pinned 1R to the stop as it rested on the exchange.
   That is the ORIGINAL stop only if nothing has moved it -- and on the second
   adoption of UAIUSDT on 2026-09-12 the supervisor had already walked it to
   break even, so 1R read 0.0008 against a true 0.0671 and a 0.6R trade was
   logged as "peak reached 19.70R". Every R-gated rule was firing off a number
   84x too small.

3. /status now says how long the trading day has left to run, because the
   daily target is measured against the UTC day roll and nothing showed it.
"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.engine import Engine                                  # noqa: E402
from bot.positions import RISK_UNKNOWN, ActivePosition         # noqa: E402
from bot.stream import Tick                                    # noqa: E402
from bot.supervise import (Reading, SuperviseConfig,           # noqa: E402
                           risk_per_unit, supervise)
from bot.targets import format_duration, seconds_to_day_end    # noqa: E402
from test_r2_regressions import StubAPI, _cleanup, engine      # noqa: E402


def _resting(**kw):
    """A position whose entry order is placed but not yet filled."""
    d = dict(symbol="KAVAUSDT", side="BUY", entry=0.075530, stop=0.074410,
             take_profit=0.077190, qty=529.4, entry_order_id="e-17",
             tag="e-17", initial_stop=0.074410, initial_target=0.077190,
             initial_risk=0.001120)
    d.update(kw)
    return ActivePosition(**d)


class TestAnUnfilledEntryIsNotAPosition(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_a_new_position_starts_unfilled(self):
        self.assertFalse(_resting().filled,
                         "a placed entry must not claim to have filled")

    def test_the_supervisor_stands_down_on_a_resting_entry(self):
        e = engine(api=StubAPI())
        e.cfg.supervise = SuperviseConfig(enabled=True)
        pos = _resting()
        e.book["KAVAUSDT"] = pos
        moved = []
        e.apply_plan = lambda p, plan, price: moved.append(plan)
        e.held_bars = lambda sym: []
        e.supervise_position(pos, 0.0792)
        self.assertEqual(moved, [],
                         "the supervisor acted on an entry that never filled")

    def test_a_resting_entry_gets_no_proximity_alert(self):
        e = engine(api=StubAPI())
        e.cfg.dry_run = False
        e._tick_guard = lambda: False
        e.book["KAVAUSDT"] = _resting()
        e.last_prices = {}
        e.on_tick(Tick(symbol="KAVAUSDT", mark_price=0.07719, event_time=1))
        self.assertEqual(e.sent, [],
                         "an unfilled entry sent an approaching-target alert")

    def test_a_resting_entry_does_not_accumulate_a_peak(self):
        e = engine(api=StubAPI())
        e.cfg.dry_run = False
        e._tick_guard = lambda: False
        pos = _resting()
        e.book["KAVAUSDT"] = pos
        e.last_prices = {}
        e.on_tick(Tick(symbol="KAVAUSDT", mark_price=0.0792709, event_time=1))
        self.assertEqual(pos.high_water, 0.0,
                         "high water rose on a position that was never opened")

    def test_the_exchange_reporting_a_size_is_the_fill(self):
        e = engine(api=StubAPI())
        pos = _resting()
        e.book["KAVAUSDT"] = pos
        e.reconcile_position(
            {"position_amt": 529.4, "entry_price": 0.0756,
             "open_order_ids": {"e-17"}}, symbol="KAVAUSDT")
        self.assertTrue(pos.filled, "a confirmed size did not confirm the fill")
        self.assertAlmostEqual(pos.entry, 0.0756,
                               msg="the book kept the price we asked for, "
                                   "not the price we got")

    def test_the_fill_is_only_counted_once(self):
        e = engine(api=StubAPI())
        pos = _resting()
        e.book["KAVAUSDT"] = pos
        snap = {"position_amt": 529.4, "entry_price": 0.0756,
                "open_order_ids": {"e-17"}}
        before = e.state.total_trades
        e.reconcile_position(snap, symbol="KAVAUSDT")
        e.reconcile_position(snap, symbol="KAVAUSDT")
        e.reconcile_position(snap, symbol="KAVAUSDT")
        self.assertEqual(e.state.total_trades, before + 1,
                         "every reconcile booked the same fill again")

    def test_a_filled_position_is_supervised_normally(self):
        e = engine(api=StubAPI())
        e.cfg.supervise = SuperviseConfig(enabled=True)
        e.cfg.dry_run = False
        pos = _resting(filled=True)
        e.book["KAVAUSDT"] = pos
        seen = []
        e.apply_plan = lambda p, plan, price: seen.append(plan)
        e.held_bars = lambda sym: []
        e.scale_out_qty = lambda p: 0.0
        e.interval_seconds = lambda: 900.0
        pos.high_water = 0.0792709          # +1R and then some
        e.supervise_position(pos, 0.0792709)
        self.assertTrue(seen, "a filled position was not supervised")


class TestAdoptedRiskIsNotGuessed(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_an_original_stop_gives_the_real_1r(self):
        e = engine(api=StubAPI())
        # UAIUSDT as first adopted: stop still where the strategy put it.
        self.assertAlmostEqual(e.adopted_risk(0.7639, 0.6968), 0.0671, places=6)

    def test_a_breakeven_stop_reports_1r_as_unknown(self):
        e = engine(api=StubAPI())
        # UAIUSDT re-adopted after the supervisor moved the stop to break even.
        # 0.0008 is not 1R, it is the cost buffer, and pinning to it made a
        # 0.6R trade read as 19.70R.
        self.assertEqual(e.adopted_risk(0.7639, 0.7647), RISK_UNKNOWN,
                         "a break-even stop was mistaken for the original")

    def test_a_missing_stop_reports_1r_as_unknown(self):
        e = engine(api=StubAPI())
        self.assertEqual(e.adopted_risk(0.7639, 0.0), RISK_UNKNOWN)

    def test_unknown_1r_stands_the_supervisor_down(self):
        pos = _resting(entry=0.7639, stop=0.7647, initial_stop=0.7647,
                       initial_target=0.8149, initial_risk=0.0, filled=True,
                       high_water=0.7797)
        # Without the fix risk_per_unit falls back to the live stop, 1R reads
        # 0.0008, and rule 1 fires on a move of a tenth of a percent.
        self.assertAlmostEqual(risk_per_unit(pos), 0.0008, places=6)
        plan = supervise(pos, Reading(price=0.7797, atr=0.002, efficiency=0.5,
                                      age_seconds=6000.0),
                         SuperviseConfig(enabled=True))
        self.assertTrue(plan, "sanity: the fallback still produces a plan")

        # What adoption now records. The live stop is still sitting there at
        # 0.7647 -- the point is that it is no longer consulted.
        pos.initial_risk = RISK_UNKNOWN
        self.assertEqual(risk_per_unit(pos), 0.0,
                         "1R was invented from the live stop")
        plan = supervise(pos, Reading(price=0.7797, atr=0.002, efficiency=0.5,
                                      age_seconds=6000.0),
                         SuperviseConfig(enabled=True))
        self.assertFalse(plan,
                         "the supervisor acted without knowing what it risked")

    def test_initial_risk_beats_a_stop_that_has_moved(self):
        pos = _resting(stop=0.0756, filled=True)      # walked to break even
        self.assertAlmostEqual(risk_per_unit(pos), 0.001120, places=6,
                               msg="1R followed the stop instead of staying "
                                   "put at what was actually risked")


class TestTheDayHasAClock(unittest.TestCase):
    def test_seconds_run_to_the_utc_day_roll(self):
        now = datetime(2026, 9, 13, 19, 48, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(seconds_to_day_end(now), 4 * 3600 + 12 * 60)

    def test_midnight_is_a_whole_day(self):
        now = datetime(2026, 9, 13, 0, 0, 0, tzinfo=timezone.utc)
        self.assertAlmostEqual(seconds_to_day_end(now), 24 * 3600)

    def test_durations_read_as_a_clock(self):
        self.assertEqual(format_duration(4 * 3600 + 12 * 60), "4h 12m")
        self.assertEqual(format_duration(47 * 60), "47m")
        self.assertEqual(format_duration(30), "under a minute")
        self.assertEqual(format_duration(-5), "under a minute")

    def test_status_carries_it_and_the_alerts_do_not(self):
        from bot.targets import TargetSchedule
        prog = TargetSchedule.from_config(
            {"schedule": [{"from_day": 1, "usd_per_day": 2.0}]}).progress(-0.10)
        self.assertNotIn("ends in", str(prog),
                         "the day clock leaked into the alert progress line")


if __name__ == "__main__":
    unittest.main()
