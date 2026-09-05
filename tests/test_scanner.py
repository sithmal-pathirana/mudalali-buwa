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
