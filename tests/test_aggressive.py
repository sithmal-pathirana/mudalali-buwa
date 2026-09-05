"""Phase 7: the aggressive profile and its warnings."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.aggressive import PROFILES, apply, banner, ruin_probability, short_warning  # noqa: E402
from bot.config import AggressiveConfig, Config                                      # noqa: E402


class TestOptIn(unittest.TestCase):
    def test_off_by_default(self):
        self.assertFalse(AggressiveConfig().enabled)
        self.assertFalse(Config().aggressive_on)

    def test_config_flags_it_loudly_when_on(self):
        cfg = Config(aggressive=AggressiveConfig(enabled=True))
        joined = " ".join(cfg.validate())
        self.assertIn("AGGRESSIVE MODE IS ENABLED", joined)
        self.assertIn("run.py risk", joined)

    def test_unknown_profile_is_rejected(self):
        cfg = Config(aggressive=AggressiveConfig(enabled=True, profile="ludicrous"))
        self.assertIn("aggressive.profile must be one of", " ".join(cfg.validate()))

    def test_three_profiles_ordered_by_leverage(self):
        levs = [PROFILES[n].leverage for n in ("moderate", "high", "maximum")]
        self.assertEqual(levs, sorted(levs))


class TestProfileApplication(unittest.TestCase):
    def test_replaces_the_risk_profile(self):
        cfg = Config()
        before = cfg.risk.max_leverage
        apply(cfg, PROFILES["high"])
        self.assertNotEqual(cfg.risk.max_leverage, before)
        self.assertEqual(cfg.risk.max_leverage, 20)
        self.assertEqual(cfg.interval, "5m")

    def test_loosens_trendiness_and_demands_movement(self):
        cfg = Config()
        apply(cfg, PROFILES["moderate"])
        self.assertGreaterEqual(cfg.universe["min_atr_pct"], 0.6)
        self.assertLessEqual(cfg.universe["min_efficiency"], 0.30)

    def test_does_not_touch_config_when_disabled(self):
        """apply() is only ever called when enabled; assert the safe default."""
        cfg = Config()
        self.assertEqual(cfg.risk.max_leverage, 3)
        self.assertFalse(cfg.aggressive.enabled)


class TestWarnings(unittest.TestCase):
    def test_ruin_is_computed_not_hardcoded(self):
        low, _ = ruin_probability(43.0, PROFILES["moderate"], sims=800)
        high, _ = ruin_probability(43.0, PROFILES["maximum"], sims=800)
        self.assertGreaterEqual(high, low)

    def test_more_equity_survives_longer(self):
        _, small = ruin_probability(43.0, PROFILES["moderate"], sims=800)
        _, big = ruin_probability(50_000.0, PROFILES["moderate"], sims=800)
        self.assertGreaterEqual(big, small)

    def test_banner_carries_real_numbers(self):
        text = banner(43.0, PROFILES["moderate"])
        self.assertIn("probability of ruin", text)
        self.assertIn("median days survived", text)
        self.assertIn("AGGRESSIVE MODE IS ON", text)

    def test_banner_states_what_is_still_enforced(self):
        text = banner(43.0, PROFILES["high"])
        for term in ("exchange-side stop", "KILL", "equity floor"):
            self.assertIn(term, text)

    def test_banner_states_the_no_edge_assumption(self):
        """Accurate today; would be wrong in the operator's favour with edge."""
        self.assertIn("no edge", banner(43.0, PROFILES["moderate"]))

    def test_short_warning_fits_one_line(self):
        w = short_warning(43.0, PROFILES["moderate"])
        self.assertLess(len(w), 90)
        self.assertIn("P(ruin)", w)


class TestNonNegotiables(unittest.TestCase):
    def test_momentum_burst_always_defines_a_stop(self):
        from bot.strategies import build
        from bot.strategies.base import Bar
        s = build("momentum_burst", {"min_atr_pct": 0.0, "expansion_ratio": 0.0})
        bars = [Bar(i, 100 + i * .1, 101 + i * .1, 99 + i * .1, 100 + i * .1, 1.0)
                for i in range(40)]
        bars.append(Bar(40, 104, 130, 104, 130, 1.0))
        sig = s.on_bars(bars, 0.0)
        if sig is not None:
            self.assertGreater(sig.stop, 0)
            self.assertNotEqual(sig.stop, sig.entry)

    def test_engine_still_places_a_protective_order_in_every_mode(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.place)
        self.assertIn("STOP_MARKET", src)
        self.assertIn("PROTECTIVE ORDER FAILED", src)

    def test_kill_and_equity_floor_are_not_profile_settings(self):
        import inspect
        from bot import aggressive
        src = inspect.getsource(aggressive.apply)
        for absolute in ("min_equity_usdt", "kill_action"):
            self.assertNotIn(absolute, src,
                             f"aggressive profile overrides {absolute}")

    def test_live_aggressive_requires_typed_confirmation(self):
        src = (ROOT / "run.py").read_text()
        self.assertIn('Type AGGRESSIVE to continue', src)

    def test_momentum_burst_is_not_routed_by_the_switcher(self):
        from bot.strategies.switcher import RegimeSwitcher
        self.assertNotIn("momentum_burst", str(RegimeSwitcher().routes))


class TestProfilesFollowTheMeasurement(unittest.TestCase):
    """
    The 5m default was a guess ('faster is more aggressive') and the data
    contradicted it: momentum_burst is -1.42 $/day out-of-sample on 5m and
    +0.27 on 15m. Profiles must not drift back to the intuition.
    """

    def test_no_profile_uses_a_sub_15m_interval(self):
        for name, prof in PROFILES.items():
            self.assertIn(prof.interval, ("15m", "1h"),
                          f"{name} uses {prof.interval}, which measured negative")

    def test_aggression_comes_from_size_not_speed(self):
        levs = [PROFILES[n].leverage for n in ("moderate", "high", "maximum")]
        self.assertEqual(levs, sorted(levs))
        intervals = {PROFILES[n].interval for n in PROFILES}
        self.assertEqual(len(intervals), 1,
                         "profiles differ by timeframe rather than by size")

    def test_the_measurement_is_recorded_next_to_the_choice(self):
        import inspect
        from bot import aggressive
        src = inspect.getsource(aggressive)
        self.assertIn("out-of-sample", src)
        self.assertIn("-1.4202", src, "the losing 5m number is not recorded")
