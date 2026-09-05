"""Universe scanning and concurrency capacity."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.portfolio import capacity                              # noqa: E402
from bot.scanner import Candidate, ScanConfig, Scanner          # noqa: E402


def cand(sym="XUSDT", vol=100e6, er=0.5, atr=1.0, minn=5.0):
    return Candidate(sym, 1.0, vol, er, atr, minn)


class TestScoring(unittest.TestCase):
    def setUp(self):
        self.s = Scanner(api=None, cfg=ScanConfig())

    def test_trending_scores_above_choppy(self):
        hot = self.s.score(cand(er=0.8), 100.0)
        cold = self.s.score(cand(er=0.35), 100.0)
        self.assertTrue(hot.ok and cold.ok)
        self.assertGreater(hot.score, cold.score)

    def test_hard_filters_exclude_rather_than_downweight(self):
        """A wildly illiquid coin must not score its way in on trendiness."""
        c = self.s.score(cand(er=0.99, vol=1e6), 100.0)
        self.assertFalse(c.ok)
        self.assertIn("illiquid", c.rejected)

    def test_too_quiet_is_rejected(self):
        self.assertIn("too quiet", self.s.score(cand(atr=0.05), 100.0).rejected)

    def test_too_violent_is_rejected(self):
        self.assertIn("too violent", self.s.score(cand(atr=40.0), 100.0).rejected)

    def test_not_trending_is_rejected(self):
        self.assertIn("not trending", self.s.score(cand(er=0.10), 100.0).rejected)

    def test_unaffordable_symbol_is_rejected(self):
        c = self.s.score(cand(minn=50.0), risk_budget_notional=43.0)
        self.assertFalse(c.ok)
        self.assertIn("exceeds risk budget", c.rejected)

    def test_affordable_symbol_passes_the_same_budget(self):
        self.assertTrue(self.s.score(cand(minn=5.0), 43.0).ok)

    def test_zero_budget_skips_the_affordability_check(self):
        """Used when planning without an account."""
        self.assertTrue(self.s.score(cand(minn=5000.0), 0.0).ok)

    def test_volatility_has_diminishing_returns(self):
        """
        Concavity shows in equal ABSOLUTE steps. Comparing 1->2 against 2->4
        tests equal ratios, and a log curve gives those roughly equal
        increments by construction -- which says nothing about concavity.
        """
        a = self.s.score(cand(atr=1.0), 100.0).score
        b = self.s.score(cand(atr=2.0), 100.0).score
        c = self.s.score(cand(atr=3.0), 100.0).score
        self.assertGreater(b - a, c - b, "ATR term is not concave")

    def test_more_volatility_still_scores_higher(self):
        """Concave, but still monotonic up to the hard ceiling."""
        scores = [self.s.score(cand(atr=v), 100.0).score for v in (0.5, 1.0, 3.0, 8.0)]
        self.assertEqual(scores, sorted(scores))


class TestCapacity(unittest.TestCase):
    def test_portfolio_risk_binds_before_leverage(self):
        c = capacity(43.0, per_trade_risk_pct=2.0, portfolio_risk_pct=6.0,
                     max_leverage=3, requested=10)
        self.assertEqual(c.max_concurrent, 3)
        self.assertEqual(c.limited_by, "portfolio risk")

    def test_smaller_per_trade_risk_allows_more_slots(self):
        big = capacity(43.0, 2.0, 6.0, 3, 10).max_concurrent
        small = capacity(43.0, 0.5, 6.0, 3, 10).max_concurrent
        self.assertGreater(small, big)

    def test_never_exceeds_what_was_requested(self):
        self.assertEqual(capacity(5000.0, 0.1, 20.0, 5, 2).max_concurrent, 2)

    def test_slots_stay_above_the_minimum_order(self):
        c = capacity(43.0, 0.5, 6.0, 3, 10)
        self.assertGreaterEqual(c.notional_per_slot, 5.0)

    def test_zero_equity_yields_no_slots(self):
        self.assertEqual(capacity(0.0, 2.0, 6.0, 3, 5).max_concurrent, 0)

    def test_explanation_names_every_limit(self):
        c = capacity(43.0, 2.0, 6.0, 3, 10)
        for term in ("portfolio risk", "leverage", "minimum order size"):
            self.assertIn(term, c.detail)


class TestNoImportCycle(unittest.TestCase):
    def test_regime_does_not_import_strategies_at_runtime(self):
        """scanner -> regime -> strategies -> switcher -> regime was a cycle."""
        import subprocess
        r = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, {str(ROOT)!r}); "
             "import bot.scanner, bot.regime, bot.strategies, bot.engine; print('ok')"],
            capture_output=True, text=True)
        self.assertIn("ok", r.stdout, r.stderr[-400:])


class TestAutoScaling(unittest.TestCase):
    """
    Slot count must scale with equity in BOTH directions -- a $43 account and a
    $4,300 account should each fill their own capacity, not inherit a number
    tuned for the other.
    """

    def _slots(self, equity, cap=6.0, lev=3.0):
        from bot.portfolio import auto_slots
        return auto_slots(equity, cap, lev)

    def test_more_equity_never_means_fewer_slots(self):
        counts = [self._slots(e)[0] for e in (20, 43, 75, 100, 500, 5000)]
        self.assertEqual(counts, sorted(counts))

    def test_small_account_is_bounded_by_the_exchange_minimum(self):
        slots, per_trade = self._slots(20.0)
        notional = 20.0 * per_trade / 100 / 0.02
        self.assertGreaterEqual(notional, 5.0)
        self.assertGreater(slots, 1, "a $20 account should still hold several")

    def test_forty_three_dollars_supports_far_more_than_three(self):
        """The old fixed 2%/6% split gave 3; deriving per-trade risk gives many."""
        slots, _ = self._slots(43.0)
        self.assertGreaterEqual(slots, 20)

    def test_every_slot_clears_the_minimum_order(self):
        for equity in (15, 43, 100, 1000):
            slots, per_trade = self._slots(equity)
            if not slots:
                continue
            notional = equity * per_trade / 100 / 0.02
            self.assertGreaterEqual(round(notional, 6), 5.0,
                                    f"${equity} slots are below the minimum")

    def test_total_notional_respects_leverage(self):
        for equity in (43, 100, 1000):
            slots, per_trade = self._slots(equity, lev=3.0)
            deployed = slots * equity * per_trade / 100 / 0.02
            self.assertLessEqual(round(deployed, 6), equity * 3.0 + 1e-6)

    def test_portfolio_risk_is_conserved_however_many_slots(self):
        for equity in (43, 1000):
            slots, per_trade = self._slots(equity, cap=6.0)
            self.assertAlmostEqual(slots * per_trade, 6.0, places=6)

    def test_hard_cap_is_respected(self):
        from bot.portfolio import auto_slots
        slots, _ = auto_slots(50_000.0, 6.0, 3.0, hard_cap=12)
        self.assertLessEqual(slots, 12)

    def test_zero_equity_yields_nothing(self):
        self.assertEqual(self._slots(0.0)[0], 0)


class TestPortfolioConfig(unittest.TestCase):
    def test_multi_position_is_opt_in(self):
        from bot.config import PortfolioConfig
        self.assertFalse(PortfolioConfig().enabled,
                         "multi-position must not switch on silently")

    def test_defaults_to_auto(self):
        from bot.config import PortfolioConfig
        self.assertEqual(PortfolioConfig().max_concurrent, "auto")

    def test_section_is_built_into_its_dataclass(self):
        from bot.config import Config, PortfolioConfig
        self.assertIsInstance(Config.load().portfolio, PortfolioConfig)

    def test_universe_section_reaches_the_scanner(self):
        from bot.config import Config
        from bot.scanner import ScanConfig
        cfg = Config.load()
        sc = ScanConfig(**cfg.universe)
        self.assertGreaterEqual(sc.max_symbols, 100)
