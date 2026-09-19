"""
Telegram P&L wording, the tracking equity cap, and the supervisor's report
card (trade journal, exit review, position report).
"""

import csv
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.journal import (PendingReview, SupervisorReview, TradeJournal,  # noqa: E402
                         position_report, score)
from bot.positions import ActivePosition                      # noqa: E402
from bot.strategies.base import Bar                           # noqa: E402
from bot.targets import TargetSchedule                        # noqa: E402
from test_r2_regressions import _cleanup, engine              # noqa: E402

M15 = 900_000
T0 = 1789725600000        # 2026-09-18 08:00 UTC


def schedule(enforced):
    return TargetSchedule.from_config(
        {"stop_when_reached": enforced,
         "schedule": [{"from_day": 1, "usd_per_day": 2.0}]})


class TestPnlWording(unittest.TestCase):
    def test_target_off_reads_against_equity_not_two_dollars(self):
        text = str(schedule(False).progress(2.72, equity=100.0, since_restart=5.1))
        self.assertNotIn("$2.00", text)
        self.assertNotIn("136%", text)
        self.assertIn("+2.72 USDT (+2.72% of $100.00)", text)
        self.assertIn("since restart +5.10 USDT", text)

    def test_target_on_keeps_the_bar(self):
        text = str(schedule(True).progress(1.0, equity=100.0))
        self.assertIn("$+1.00 / $2.00 (50%)", text)

    def test_describe_does_not_quote_a_target_that_is_off(self):
        text = schedule(False).describe(100.0)
        self.assertIn("daily target off", text)
        self.assertNotIn("required", text)

    def test_no_target_banked_alert_when_it_is_off(self):
        e = engine()
        e.schedule = schedule(False)
        e.state.realized_today = 3.0
        e.check_target_reached()
        self.assertFalse([b for _, b in e.sent if "banked" in b])
        _cleanup()

    def test_since_restart_accumulates_every_booked_close(self):
        e = engine()
        e.session_realized = 0.0
        e.note_realized(0.5)
        e.note_realized(-0.2)
        self.assertAlmostEqual(e.progress().since_restart, 0.3)
        _cleanup()


class TestTrackingEquityCap(unittest.TestCase):
    def setUp(self):
        self.e = engine()
        self.e.cfg.risk.equity_cap_usdt = 100.0
        self.e.cfg.risk.equity_cap_tracks_pnl = True
        self.e.state.cap_anchor_equity = 0.0

    def tearDown(self):
        _cleanup()

    def test_starts_at_the_cap(self):
        self.assertEqual(self.e.effective_equity(5000.0), 100.0)

    def test_a_loss_on_testnet_shrinks_the_rehearsed_account(self):
        self.e.effective_equity(5000.0)
        self.assertAlmostEqual(self.e.effective_equity(4991.5), 91.5)

    def test_a_gain_grows_it_past_the_cap(self):
        self.e.effective_equity(5000.0)
        self.assertAlmostEqual(self.e.effective_equity(5002.64), 102.64)

    def test_the_anchor_survives_a_restart(self):
        self.e.effective_equity(5000.0)
        self.assertEqual(self.e.state.cap_anchor_equity, 5000.0)

    def test_changing_the_cap_starts_a_fresh_rehearsal(self):
        self.e.effective_equity(5000.0)
        self.e.cfg.risk.equity_cap_usdt = 50.0
        self.assertEqual(self.e.effective_equity(4990.0), 50.0)

    def test_off_is_the_old_fixed_cap(self):
        self.e.cfg.risk.equity_cap_tracks_pnl = False
        self.e.effective_equity(5000.0)
        self.assertEqual(self.e.effective_equity(4000.0), 100.0)

    def test_the_equity_floor_can_now_fire(self):
        """The point of it: at $8.00 the floor halts, as it would live."""
        self.e.effective_equity(5000.0)
        self.assertLess(self.e.effective_equity(4907.0), 8.0)

    def test_a_day_anchored_on_the_real_balance_is_rebased(self):
        self.e.equity = self.e.effective_equity(5000.0)
        self.e.state.day_start_equity = 5000.0
        self.e.rebase_day_start_equity()
        self.assertEqual(self.e.state.day_start_equity, 100.0)

    def test_a_real_gain_day_is_not_rebased(self):
        self.e.effective_equity(5000.0)
        self.e.equity = self.e.effective_equity(5003.0)
        self.e.state.day_start_equity = 102.0
        self.e.rebase_day_start_equity()
        self.assertEqual(self.e.state.day_start_equity, 102.0)


def _pos(**kw):
    base = dict(symbol="METUSDT", side="BUY", entry=0.25662, stop=0.2507,
                take_profit=0.2656, qty=63.0, entry_order_id="e-1",
                opened_ms=T0, initial_stop=0.2507, initial_target=0.2656,
                initial_risk=0.25662 - 0.2507, filled=True)
    base.update(kw)
    return ActivePosition(**base)


def _bar(i, lo, hi, close=None):
    return Bar(T0 + i * M15, (lo + hi) / 2, hi, lo, close or (lo + hi) / 2, 1.0)


class TestExitReview(unittest.TestCase):
    """The 2026-09-18 replays, done by the bot instead of by hand."""

    def _p(self, **kw):
        base = dict(symbol="METUSDT", long=True, entry=0.25662, qty=63.0,
                    stop=0.2507, target=0.2656, actual_pnl=-0.0456,
                    closed_ms=T0 + 2 * M15 + 60_000, reason="supervisor -- x")
        base.update(kw)
        return PendingReview(**base)

    def test_met_would_have_hit_its_stop(self):
        bars = [_bar(0, 0.20, 0.30), _bar(3, 0.2550, 0.2580), _bar(4, 0.2500, 0.2560)]
        r = score(self._p(), bars, T0 + 5 * M15, 24)
        self.assertEqual(r.outcome, "stop")
        self.assertLess(r.held_pnl, -0.37)
        self.assertGreater(r.difference, 0.3)       # the exit saved money

    def test_bars_before_the_exit_are_ignored(self):
        bars = [_bar(0, 0.20, 0.30)]                # both levels, long before
        self.assertIsNone(score(self._p(), bars, T0 + 5 * M15, 24))

    def test_a_target_first_means_the_exit_gave_up_money(self):
        bars = [_bar(3, 0.2560, 0.2670)]
        r = score(self._p(actual_pnl=0.03), bars, T0 + 5 * M15, 24)
        self.assertEqual(r.outcome, "target")
        self.assertLess(r.difference, 0)

    def test_a_bar_through_both_counts_as_the_stop(self):
        bars = [_bar(3, 0.2400, 0.2700)]
        self.assertEqual(score(self._p(), bars, T0 + 5 * M15, 24).outcome, "stop")

    def test_it_expires_at_the_last_price(self):
        bars = [_bar(3, 0.2550, 0.2580, close=0.2570)]
        r = score(self._p(), bars, T0 + 2 * M15 + 25 * 3600_000, 24)
        self.assertEqual(r.outcome, "expired")

    def test_the_review_persists_and_keeps_a_running_total(self):
        with tempfile.TemporaryDirectory() as d:
            rv = SupervisorReview(Path(d) / "r.json", Path(d) / "r.csv")
            rv.add(_pos(), -0.0456, "supervisor -- x", now_ms=T0 + 2 * M15 + 60_000)
            again = SupervisorReview(Path(d) / "r.json", Path(d) / "r.csv")
            self.assertEqual(len(again.pending), 1)
            done = again.resolve(lambda s: [_bar(4, 0.2500, 0.2560)],
                                 now_ms=T0 + 5 * M15)
            self.assertEqual(len(done), 1)
            self.assertEqual(again.totals["reviews"], 1)
            self.assertFalse(again.pending)
            rows = list(csv.DictReader((Path(d) / "r.csv").open()))
            self.assertEqual(rows[0]["held_outcome"], "stop")


class TestJournalAndReport(unittest.TestCase):
    def test_every_close_is_one_csv_row_with_its_r(self):
        with tempfile.TemporaryDirectory() as d:
            j = TradeJournal(Path(d) / "t.csv")
            p = _pos(exit_reason="supervisor -- target needs 21 bars")
            j.record(p, -0.0456, "manually", "testnet", now_ms=T0 + 27 * 60_000)
            row = next(csv.DictReader((Path(d) / "t.csv").open()))
            self.assertEqual(row["r_multiple"], "-0.12")
            self.assertEqual(row["minutes_held"], "27")
            self.assertTrue(row["reason"].startswith("supervisor"))

    def test_the_engine_queues_supervisor_exits_only(self):
        e = engine()
        with tempfile.TemporaryDirectory() as d:
            e.journal = TradeJournal(Path(d) / "t.csv")
            e.review = SupervisorReview(Path(d) / "r.json", Path(d) / "r.csv")
            e.after_close(_pos(exit_reason="supervisor -- x"), "manually", -0.05)
            e.after_close(_pos(), "exchange-side", 0.5)
            self.assertEqual(len(e.review.pending), 1)
            self.assertEqual(len(list(csv.DictReader((Path(d) / "t.csv").open()))), 2)
        _cleanup()

    def test_position_report_flags_a_naked_position(self):
        text = position_report([(_pos(), 0.2600, True),
                                (_pos(symbol="STGUSDT", stop=0.0, take_profit=0.0),
                                 0.2600, False)], now_ms=T0 + 95 * 60_000)
        self.assertIn("2 open position(s)", text)
        self.assertIn("held 1h35m", text)
        self.assertIn("NOT PROTECTED", text)
        self.assertIn("SL NONE", text)
        self.assertIn("R)", text)


if __name__ == "__main__":
    unittest.main()
