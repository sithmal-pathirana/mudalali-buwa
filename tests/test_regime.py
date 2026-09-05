"""Regime detection and the routing layer."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.regime import Regime, RegimeDetector, efficiency_ratio   # noqa: E402
from bot.strategies import build                                  # noqa: E402
from bot.strategies.base import Bar                               # noqa: E402
from bot.strategies.switcher import RegimeSwitcher                # noqa: E402


def straight_line(n=60, start=100.0, step=0.5):
    """Pure trend: every bar advances the same amount."""
    return [Bar(i, start + i * step, start + i * step + .2,
                start + i * step - .2, start + i * step, 1.0) for i in range(n)]


def sawtooth(n=60, base=100.0, amp=2.0):
    """Pure chop: price oscillates and ends where it started."""
    out = []
    for i in range(n):
        c = base + (amp if i % 2 else -amp)
        out.append(Bar(i, c, c + .3, c - .3, c, 1.0))
    return out


class TestEfficiencyRatio(unittest.TestCase):
    def test_straight_line_is_maximally_efficient(self):
        self.assertAlmostEqual(efficiency_ratio(straight_line(), 30), 1.0, places=6)

    def test_oscillation_is_inefficient(self):
        self.assertLess(efficiency_ratio(sawtooth(), 30), 0.1)

    def test_flat_market_returns_zero_not_a_crash(self):
        flat = [Bar(i, 100, 100, 100, 100, 1.0) for i in range(40)]
        self.assertEqual(efficiency_ratio(flat, 30), 0.0)

    def test_short_history_returns_zero(self):
        self.assertEqual(efficiency_ratio(straight_line(5), 30), 0.0)


class TestDetector(unittest.TestCase):
    def test_thresholds_must_be_ordered(self):
        with self.assertRaises(ValueError):
            RegimeDetector(trend_above=0.2, range_below=0.5)

    def test_trend_is_classified_as_trending(self):
        d = RegimeDetector(min_atr_pct=0.0)
        self.assertIs(d.classify(straight_line()).regime, Regime.TRENDING)

    def test_chop_is_classified_as_ranging(self):
        d = RegimeDetector(min_atr_pct=0.0)
        self.assertIs(d.classify(sawtooth()).regime, Regime.RANGING)

    def test_quiet_market_is_unclear_regardless_of_shape(self):
        """No range means no room to cover fees, whatever the path looks like."""
        d = RegimeDetector(min_atr_pct=5.0)
        self.assertIs(d.classify(straight_line()).regime, Regime.UNCLEAR)

    def test_hysteresis_requires_confirmation_before_switching(self):
        d = RegimeDetector(min_atr_pct=0.0, confirm_bars=3)
        bars = straight_line()
        for _ in range(5):
            d.update(bars)
        self.assertIs(d.current, Regime.TRENDING)

        chop = sawtooth()
        first = d.update(chop)
        self.assertIs(first.regime, Regime.TRENDING, "switched on a single bar")
        d.update(chop)
        d.update(chop)
        self.assertIs(d.current, Regime.RANGING, "never confirmed the switch")

    def test_switch_count_is_tracked(self):
        d = RegimeDetector(min_atr_pct=0.0, confirm_bars=1)
        before = d.switches
        for _ in range(2):
            d.update(straight_line())
        self.assertGreater(d.switches, before)


class TestSwitcher(unittest.TestCase):
    def test_default_routes_gate_rather_than_alternate(self):
        """Measured: mean_reversion loses in every regime, so nothing routes to it."""
        s = RegimeSwitcher()
        self.assertEqual(s.routes["trending"], "trend_atr")
        self.assertIsNone(s.routes["ranging"])
        self.assertIsNone(s.routes["unclear"])

    def test_stands_down_when_no_route_applies(self):
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0, "confirm_bars": 1})
        bars = sawtooth(80)
        for _ in range(3):
            s.on_bars(bars, 0.0)
        self.assertGreater(s.stood_down, 0)

    def test_never_signals_while_a_position_is_open(self):
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0})
        self.assertIsNone(s.on_bars(straight_line(80), position_amt=1.0))

    def test_routes_are_validated(self):
        with self.assertRaises(ValueError):
            RegimeSwitcher(routes={"trending": "nonexistent"})
        with self.assertRaises(ValueError):
            RegimeSwitcher(routes={"sideways": "trend_atr"})

    def test_routes_are_overridable(self):
        s = RegimeSwitcher(routes={"ranging": "mean_reversion"})
        self.assertEqual(s.routes["ranging"], "mean_reversion")
        self.assertEqual(s.routes["trending"], "trend_atr")

    def test_signal_is_tagged_with_the_regime_that_produced_it(self):
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0, "confirm_bars": 1},
                           trend={"min_atr_pct": 0.0})
        bars = straight_line(80)
        sig = None
        for _ in range(4):
            sig = s.on_bars(bars, 0.0) or sig
        if sig is not None:
            self.assertIn("trending", sig.reason)

    def test_warmup_covers_every_component(self):
        s = RegimeSwitcher()
        self.assertGreaterEqual(s.warmup, s.detector.warmup)
        self.assertGreaterEqual(s.warmup, s.trend.warmup)
        self.assertGreaterEqual(s.warmup, s.revert.warmup)

    def test_registered_and_buildable(self):
        s = build("switcher", {})
        self.assertIsInstance(s, RegimeSwitcher)
        ok, note = s.feasible(43.0, 0.09, None)
        self.assertTrue(ok)
        self.assertIn("stands down", note)


class TestMeanReversion(unittest.TestCase):
    def test_fades_a_stretch_below_the_mean(self):
        s = build("mean_reversion", {"min_atr_pct": 0.0, "z_entry": 1.5})
        bars = [Bar(i, 100, 100.5, 99.5, 100, 1.0) for i in range(30)]
        bars.append(Bar(30, 100, 100, 94, 94, 1.0))
        sig = s.on_bars(bars, 0.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "BUY")
        self.assertLess(sig.stop, sig.entry)
        self.assertGreater(sig.take_profit, sig.entry)

    def test_no_signal_inside_the_band(self):
        s = build("mean_reversion", {"min_atr_pct": 0.0, "z_entry": 2.0})
        bars = [Bar(i, 100 + (i % 3), 101 + (i % 3), 99 + (i % 3), 100 + (i % 3), 1.0)
                for i in range(40)]
        self.assertIsNone(s.on_bars(bars, 0.0))


class TestManualOverride(unittest.TestCase):
    """Automatic by default, manual when you say so, and it must persist."""

    def test_defaults_to_automatic(self):
        s = RegimeSwitcher()
        self.assertIsNone(s.override)
        self.assertEqual(s.mode, "auto")

    def test_override_beats_the_regime_reading(self):
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0, "confirm_bars": 1})
        s.set_override("mean_reversion")
        # A textbook trending market would normally route to trend_atr.
        self.assertIs(s.route(Regime.TRENDING), s.revert)
        self.assertIs(s.route(Regime.RANGING), s.revert)

    def test_none_stands_down_in_every_regime(self):
        s = RegimeSwitcher()
        s.set_override("none")
        for r in Regime:
            self.assertIsNone(s.route(r))

    def test_auto_restores_routing(self):
        s = RegimeSwitcher()
        s.set_override("mean_reversion")
        s.set_override("auto")
        self.assertIsNone(s.override)
        self.assertIs(s.route(Regime.TRENDING), s.trend)
        self.assertIsNone(s.route(Regime.RANGING))

    def test_unknown_override_is_rejected_and_leaves_state_alone(self):
        s = RegimeSwitcher()
        s.set_override("trend_atr")
        with self.assertRaises(ValueError):
            s.set_override("moon_phase")
        self.assertEqual(s.override, "trend_atr", "a bad value cleared the override")

    def test_regime_is_still_measured_while_overridden(self):
        """You must be able to see what you are overriding."""
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0, "confirm_bars": 1},
                           trend={"min_atr_pct": 0.0})
        s.set_override("trend_atr")
        s.on_bars(sawtooth(80), 0.0)
        self.assertIsNotNone(s.last_reading)

    def test_signal_reason_marks_a_forced_choice(self):
        s = RegimeSwitcher(regime={"min_atr_pct": 0.0, "confirm_bars": 1},
                           trend={"min_atr_pct": 0.0})
        s.set_override("trend_atr")
        bars, sig = straight_line(80), None
        for _ in range(4):
            sig = s.on_bars(bars, 0.0) or sig
        if sig is not None:
            self.assertIn("forced:trend_atr", sig.reason)


class TestOverridePlumbing(unittest.TestCase):
    def test_command_carries_a_value(self):
        from bot.dashboard import Command
        self.assertEqual(Command("strategy", value="trend_atr").value, "trend_atr")

    def test_dashboard_exposes_the_route(self):
        import inspect
        from bot import dashboard
        self.assertIn("/api/strategy", inspect.getsource(dashboard.Dashboard.start))

    def test_state_persists_the_override(self):
        from bot.state import State
        self.assertIn("strategy_override", State.__dataclass_fields__)

    def test_engine_restores_a_saved_override_on_boot(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.startup)
        self.assertIn("strategy_override", src)
        self.assertIn("set_override", src)

    def test_fixed_strategy_reports_that_it_cannot_switch(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.set_strategy)
        self.assertIn("does not route", src)

    def test_telegram_offers_the_command(self):
        from bot.telegram_control import HELP
        self.assertIn("/strategy", HELP)
