"""
The position supervisor: bot/supervise.py, and the engine that executes it.

The pure rules are tested against hand-built positions with no exchange in
sight. The engine layer is tested for the three things that can lose money if
they are wrong: that the runner stays dormant on an account too small to split
a position, that a protective order is PLACED before the old one is cancelled,
and that an exit actually reaches close_position.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.positions import ActivePosition                 # noqa: E402
from bot.supervise import (Reading, SuperviseConfig,      # noqa: E402
                           r_multiple, supervise)
from test_r2_regressions import StubAPI, engine           # noqa: E402


def cfg(**kw):
    base = dict(enabled=True)
    base.update(kw)
    return SuperviseConfig(**base)


def long_pos(entry=100.0, stop=98.0, tp=103.0, qty=10.0, peak=None, **kw):
    """1R = 2.0, target = +1.5R -- the same geometry config.yaml produces."""
    p = ActivePosition(symbol="DOGEUSDT", side="BUY", entry=entry, stop=stop,
                       take_profit=tp, qty=qty, entry_order_id="e-1",
                       stop_order_id="s-1", tp_order_id="t-1",
                       initial_stop=stop, initial_target=tp, **kw)
    p.high_water = peak if peak is not None else entry
    return p


def short_pos(entry=100.0, stop=102.0, tp=97.0, qty=10.0, peak=None, **kw):
    p = ActivePosition(symbol="DOGEUSDT", side="SELL", entry=entry, stop=stop,
                       take_profit=tp, qty=qty, entry_order_id="e-1",
                       stop_order_id="s-1", tp_order_id="t-1",
                       initial_stop=stop, initial_target=tp, **kw)
    p.high_water = peak if peak is not None else entry
    return p


# A reading with everything known and the trade comfortably on track:
# drifting up 0.2/bar, so a long's target 2.0 away is ~10 bars off.
def ok(price, **kw):
    base = dict(atr=1.0, efficiency=0.6, age_seconds=3600.0, bar_seconds=900.0,
                net_move_per_bar=0.2)
    base.update(kw)
    return Reading(price=price, **base)


# The mirror of ok() for a short: price falling is progress for a seller.
# Passing ok() to a short reads as "the market is running away", which rule 3
# correctly acts on -- fine in a rule 3 test, noise in a rule 1 one.
def ok_short(price, **kw):
    kw.setdefault("net_move_per_bar", -0.2)
    return ok(price, **kw)


class TestDisabled(unittest.TestCase):
    def test_disabled_config_never_returns_a_plan(self):
        p = long_pos(peak=110.0)
        self.assertFalse(supervise(p, ok(110.0), SuperviseConfig(enabled=False)))

    def test_a_position_with_no_risk_is_left_alone(self):
        """entry == stop means 1R is zero; every rule divides by it."""
        p = long_pos(entry=100.0, stop=100.0)
        p.initial_stop = 100.0
        self.assertFalse(supervise(p, ok(120.0), cfg()))


class TestBreakEven(unittest.TestCase):
    def test_fires_once_the_peak_reaches_one_r(self):
        p = long_pos(peak=102.0)          # +1.0R on a 2.0 risk
        plan = supervise(p, ok(101.0), cfg())
        self.assertIsNotNone(plan.stop)
        self.assertGreater(plan.stop, p.entry)          # above entry, not at it
        self.assertAlmostEqual(plan.stop, 100.0 + 100.0 * 0.0015)

    def test_does_not_fire_below_one_r(self):
        p = long_pos(peak=101.8)          # +0.9R
        self.assertIsNone(supervise(p, ok(101.5), cfg()).stop)

    def test_uses_the_peak_not_the_current_price(self):
        """Ran to +1R then gave it back: the stop still moves up."""
        p = long_pos(peak=102.5)
        plan = supervise(p, ok(100.2), cfg())
        self.assertIsNotNone(plan.stop)

    def test_short_moves_the_stop_down(self):
        p = short_pos(peak=98.0)          # +1.0R
        plan = supervise(p, ok_short(99.0), cfg())
        self.assertLess(plan.stop, p.entry)
        self.assertAlmostEqual(plan.stop, 100.0 - 100.0 * 0.0015)

    def test_never_moves_a_stop_backwards(self):
        p = long_pos(peak=110.0)
        p.stop = 105.0                    # already trailed well past break even
        plan = supervise(p, ok(108.0), cfg(trail_atr_mult=99.0))
        self.assertIsNone(plan.stop)

    def test_r_is_measured_against_the_original_stop(self):
        """After rule 1 the live stop is at entry; R must not collapse to 0."""
        p = long_pos(peak=104.0)
        p.stop = 100.15                   # already moved to break even
        self.assertAlmostEqual(r_multiple(p, 104.0), 2.0)


class TestRunner(unittest.TestCase):
    def test_dormant_when_the_account_cannot_split_the_position(self):
        """THE small-account guard. Without it this cancels a filling target."""
        p = long_pos(peak=102.6)          # 1.3R of a 1.5R target -- past 0.8x
        plan = supervise(p, ok(102.6), cfg(), scale_out_qty=0.0)
        self.assertIsNone(plan.target_qty)

    def test_arms_once_a_split_is_affordable(self):
        p = long_pos(peak=102.6)
        plan = supervise(p, ok(102.6), cfg(), scale_out_qty=5.0)
        self.assertEqual(plan.target_qty, 5.0)

    def test_does_not_arm_before_the_trigger_fraction(self):
        p = long_pos(peak=101.0)          # 0.5R of a 1.5R target
        plan = supervise(p, ok(101.0), cfg(), scale_out_qty=5.0)
        self.assertIsNone(plan.target_qty)

    def test_does_not_arm_when_the_trend_has_decayed(self):
        p = long_pos(peak=102.6)
        plan = supervise(p, ok(102.6, efficiency=0.10), cfg(), scale_out_qty=5.0)
        self.assertIsNone(plan.target_qty)

    def test_does_not_arm_when_the_trend_is_unknown(self):
        p = long_pos(peak=102.6)
        plan = supervise(p, ok(102.6, efficiency=-1.0), cfg(), scale_out_qty=5.0)
        self.assertIsNone(plan.target_qty)

    def test_does_not_re_arm_on_a_position_already_running(self):
        p = long_pos(peak=104.0, runner=True)
        plan = supervise(p, ok(104.0), cfg(), scale_out_qty=5.0)
        self.assertIsNone(plan.target_qty)

    def test_runner_trails_from_the_peak(self):
        p = long_pos(peak=106.0, runner=True)
        p.stop = 100.15
        plan = supervise(p, ok(105.0), cfg(trail_atr_mult=1.5))
        self.assertAlmostEqual(plan.stop, 106.0 - 1.5 * 1.0)

    def test_runner_trail_never_drops_below_break_even(self):
        p = long_pos(peak=102.2, runner=True)      # trail would land at 100.7
        p.stop = 98.0
        plan = supervise(p, ok(102.0), cfg(trail_atr_mult=4.0))
        self.assertGreaterEqual(plan.stop, 100.0)


class TestHorizon(unittest.TestCase):
    def far(self, **kw):
        """Target 3.0 away, drifting 0.01/bar: 300 bars, well past 20."""
        return ok(100.0, atr=0.5, efficiency=0.1, net_move_per_bar=0.01, **kw)

    def test_silent_inside_the_grace_period(self):
        p = long_pos()
        plan = supervise(p, self.far(age_seconds=60.0), cfg())
        self.assertFalse(plan.exit_now)

    def test_exits_when_unreachable_and_not_in_profit(self):
        p = long_pos()
        plan = supervise(p, self.far(age_seconds=7200.0), cfg())
        self.assertTrue(plan.exit_now)

    def test_stays_when_the_target_is_reachable(self):
        p = long_pos()
        on_track = ok(102.0, atr=1.0, efficiency=0.8, age_seconds=7200.0)
        self.assertFalse(supervise(p, on_track, cfg()).exit_now)

    def test_a_clean_trend_running_AWAY_is_not_read_as_progress(self):
        """ER is direction-blind. A short in a strong uptrend scores ER 0.9
        and must still be judged as losing ground, not as nearly there."""
        p = short_pos()
        against = ok(100.5, atr=0.5, efficiency=0.9, net_move_per_bar=+0.3,
                     age_seconds=7200.0)
        self.assertTrue(supervise(p, against, cfg()).exit_now)

    def test_in_profit_but_drifting_away_banks_the_profit(self):
        p = long_pos(peak=101.5)
        turning = ok(101.2, atr=0.5, efficiency=0.7, net_move_per_bar=-0.2,
                     age_seconds=7200.0)
        plan = supervise(p, turning, cfg())
        self.assertTrue(plan.exit_now)

    def test_harvests_rather_than_exits_when_in_profit(self):
        p = long_pos(peak=101.0)
        r = ok(101.0, atr=0.5, efficiency=0.05, net_move_per_bar=0.005,
               age_seconds=7200.0)
        plan = supervise(p, r, cfg())
        self.assertFalse(plan.exit_now)
        self.assertIsNotNone(plan.target)
        self.assertLess(plan.target, p.take_profit)     # pulled in, not pushed out

    def test_unknown_trend_does_not_trigger_an_exit(self):
        """A cold start knows least; it must not close the position for it."""
        p = long_pos()
        r = ok(100.0, atr=0.5, efficiency=-1.0, net_move_per_bar=0.001,
               age_seconds=7200.0)
        self.assertFalse(supervise(p, r, cfg()).exit_now)

    def test_no_bar_data_does_not_trigger_an_exit(self):
        p = long_pos()
        r = ok(100.0, atr=0.0, efficiency=0.5, net_move_per_bar=0.001,
               age_seconds=7200.0)
        self.assertFalse(supervise(p, r, cfg()).exit_now)


class TestFailedBreakout(unittest.TestCase):
    def test_exits_when_price_returns_through_the_trigger_in_profit(self):
        p = long_pos(peak=102.0, ref_level=100.5)
        plan = supervise(p, ok(100.4), cfg())
        self.assertTrue(plan.exit_now)

    def test_holds_while_price_is_still_above_the_trigger(self):
        p = long_pos(peak=102.0, ref_level=100.5)
        self.assertFalse(supervise(p, ok(101.5), cfg()).exit_now)

    def test_never_exits_a_losing_position(self):
        """Underwater, the resting stop is a better price than market."""
        p = long_pos(peak=100.1, ref_level=100.5)
        self.assertFalse(supervise(p, ok(99.0), cfg()).exit_now)

    def test_off_when_the_strategy_reported_no_level(self):
        p = long_pos(peak=102.0, ref_level=0.0)
        self.assertFalse(supervise(p, ok(99.0), cfg()).exit_now)

    def test_short_exits_when_price_comes_back_up_through_the_level(self):
        p = short_pos(peak=98.0, ref_level=99.5)
        self.assertTrue(supervise(p, ok_short(99.6), cfg()).exit_now)


class TestSafety(unittest.TestCase):
    def test_a_stop_through_the_market_is_never_sent(self):
        """Binance rejects a trigger price has already passed (-2021)."""
        p = long_pos(peak=110.0, runner=True)
        p.stop = 100.15
        plan = supervise(p, ok(104.0, atr=0.1), cfg(trail_atr_mult=0.1))
        if plan.stop is not None:
            self.assertLess(plan.stop, 104.0)

    def test_an_exit_cancels_every_other_instruction(self):
        p = long_pos(peak=102.0, ref_level=100.5)
        plan = supervise(p, ok(100.4), cfg())
        self.assertTrue(plan.exit_now)
        self.assertIsNone(plan.stop)
        self.assertIsNone(plan.target)
        self.assertIsNone(plan.target_qty)

    def test_every_plan_explains_itself(self):
        p = long_pos(peak=102.0)
        self.assertTrue(supervise(p, ok(101.0), cfg()).why)


# --------------------------------------------------------------- engine layer
class SpyAPI(StubAPI):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.algo_placed = []
        self.algo_cancelled = []

    def algo_order(self, **params):
        self.calls.append(("algo_order", params))
        self.algo_placed.append(params)
        return {"algoId": 1, "algoStatus": "NEW"}

    def cancel_algo_order(self, client_algo_id):
        self.calls.append(("cancel_algo_order", client_algo_id))
        self.algo_cancelled.append(client_algo_id)
        return {"msg": "ok"}


def eng(api=None):
    e = engine(api=api or SpyAPI())
    e._seq = 10
    e.book = {}
    rules = type("Rules", (), {
        "round_qty": lambda s, q: f"{q:.2f}",
        "round_price": lambda s, p: f"{p:.6f}",
        "min_notional": 5.0})()
    e.rules = rules
    e.rules_for = lambda sym: rules
    return e


class TestScaleOutAffordability(unittest.TestCase):
    """The auto-enable: it is the arithmetic, not a flag anyone has to flip."""

    def test_zero_when_half_the_position_is_under_the_exchange_minimum(self):
        e = eng()
        p = long_pos(entry=1.0, qty=8.7)          # $8.70 -> halves of $4.35
        self.assertEqual(e.scale_out_qty(p), 0.0)

    def test_splits_once_both_halves_clear_the_minimum(self):
        e = eng()
        p = long_pos(entry=1.0, qty=12.0)         # $12 -> halves of $6
        self.assertEqual(e.scale_out_qty(p), 6.0)

    def test_the_exact_boundary_is_allowed(self):
        e = eng()
        p = long_pos(entry=1.0, qty=10.0)         # $10 -> halves of exactly $5
        self.assertEqual(e.scale_out_qty(p), 5.0)


class TestReplaceProtective(unittest.TestCase):
    def test_places_the_new_order_before_cancelling_the_old_one(self):
        """Cancel-first leaves the position naked if the place then fails."""
        api = SpyAPI()
        e = eng(api)
        p = long_pos()
        self.assertTrue(e.replace_protective(p, "stop", 99.0))
        kinds = [c[0] for c in api.calls if c[0] in ("algo_order", "cancel_algo_order")]
        self.assertEqual(kinds, ["algo_order", "cancel_algo_order"])

    def test_a_failed_placement_leaves_the_old_stop_alone(self):
        from bot.binanceapi import BinanceError

        class Refusing(SpyAPI):
            def algo_order(self, **params):
                raise BinanceError(-2021, "would immediately trigger", "/algoOrder")

        api = Refusing()
        e = eng(api)
        p = long_pos()
        self.assertFalse(e.replace_protective(p, "stop", 99.0))
        self.assertEqual(api.algo_cancelled, [])
        self.assertEqual(p.stop_order_id, "s-1")

    def test_the_new_order_id_is_recorded(self):
        api = SpyAPI()
        e = eng(api)
        p = long_pos()
        e.replace_protective(p, "stop", 99.0)
        self.assertNotEqual(p.stop_order_id, "s-1")
        self.assertEqual(api.algo_placed[0]["reduceOnly"], "true")


class TestApplyPlan(unittest.TestCase):
    def test_an_exit_plan_closes_the_position(self):
        e = eng()
        closed = []
        e.close_position = lambda reason, symbol=None: closed.append((symbol, reason))
        p = long_pos(peak=102.0, ref_level=100.5)
        e.book[p.symbol] = p
        e.apply_plan(p, supervise(p, ok(100.4), cfg()), 100.4)
        self.assertEqual(len(closed), 1)

    def test_a_stop_move_smaller_than_the_threshold_is_not_sent(self):
        """A trail recomputed every tick must not churn two calls a tick."""
        api = SpyAPI()
        e = eng(api)
        p = long_pos()
        from bot.supervise import Plan
        e.apply_plan(p, Plan(stop=98.001), 100.0)
        self.assertEqual(api.algo_placed, [])

    def test_arming_the_runner_marks_the_position(self):
        api = SpyAPI()
        e = eng(api)
        p = long_pos(peak=102.6, qty=12.0)
        plan = supervise(p, ok(102.6), cfg(), scale_out_qty=6.0)
        e.apply_plan(p, plan, 102.6)
        self.assertTrue(p.runner)


if __name__ == "__main__":
    unittest.main()
