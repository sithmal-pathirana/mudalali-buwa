"""Phase 1: allocation and the portfolio-level risk gate."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import Config, PortfolioConfig, RiskConfig     # noqa: E402
from bot.portfolio import allocate                             # noqa: E402
from bot.positions import ActivePosition                       # noqa: E402
from bot.risk import RiskManager                               # noqa: E402
from bot.state import State                                    # noqa: E402


def pos(symbol, entry=100.0, qty=1.0):
    return ActivePosition(symbol, "BUY", entry, entry * 0.98, entry * 1.03, qty,
                          entry_order_id=f"e-{symbol}", tag=f"e-{symbol}")


def manager(**risk_kw):
    tmp = ROOT / "data" / "test_portfolio_state.json"
    tmp.unlink(missing_ok=True)
    cfg = Config(risk=RiskConfig(**risk_kw))
    return RiskManager(cfg, State(path=tmp)), cfg


class TestAllocation(unittest.TestCase):
    def test_one_eligible_gets_the_single_position_cap_not_the_budget(self):
        a = allocate(43.0, eligible=1, portfolio_risk_pct=6.0,
                     single_position_cap_pct=2.0)
        self.assertEqual(a.slots, 1)
        self.assertAlmostEqual(a.per_position_risk_pct, 2.0)
        self.assertAlmostEqual(a.total_risk_pct, 2.0,
                               msg="a lone qualifier took the whole portfolio budget")

    def test_budget_is_shared_once_it_is_full(self):
        a = allocate(43.0, eligible=10)
        self.assertEqual(a.slots, 10)
        self.assertAlmostEqual(a.total_risk_pct, 6.0)

    def test_total_risk_never_exceeds_the_cap(self):
        for n in range(1, 101):
            a = allocate(43.0, n, portfolio_risk_pct=6.0)
            self.assertLessEqual(round(a.total_risk_pct, 6), 6.0, f"{n} eligible")

    def test_hundred_eligible_is_trimmed_to_what_is_affordable(self):
        a = allocate(43.0, eligible=100)
        self.assertEqual(a.slots, 25)
        self.assertEqual(a.limited_by, "minimum order size")
        self.assertGreaterEqual(round(a.notional_per_position, 6), 5.0)

    def test_every_position_clears_the_exchange_minimum(self):
        for equity in (15, 43, 100, 1000, 20000):
            for n in (1, 5, 40, 100):
                a = allocate(float(equity), n)
                if a.slots:
                    self.assertGreaterEqual(round(a.notional_per_position, 6), 5.0)

    def test_leverage_ceiling_is_respected(self):
        for equity in (43, 500):
            a = allocate(float(equity), 100, max_leverage=3.0)
            self.assertLessEqual(round(a.deployable, 6), equity * 3.0 + 1e-6)

    def test_bigger_account_opens_more(self):
        small = allocate(43.0, 100).slots
        big = allocate(500.0, 100).slots
        self.assertGreater(big, small)

    def test_nothing_eligible_allocates_nothing(self):
        self.assertEqual(allocate(43.0, 0).slots, 0)

    def test_tiny_account_that_cannot_fund_one_position(self):
        a = allocate(1.0, 5, min_notional=5.0)
        self.assertEqual(a.slots, 0)
        self.assertIn("cannot fund", a.limited_by)

    def test_hard_cap_is_reported_as_the_limit(self):
        a = allocate(5000.0, 100, hard_cap=40)
        self.assertEqual(a.slots, 40)
        self.assertEqual(a.limited_by, "hard cap")


class TestPortfolioGate(unittest.TestCase):
    def _cfg(self, **kw):
        c = PortfolioConfig(enabled=True, portfolio_risk_pct=6.0)
        c.resolved_slots = kw.get("slots", 5)
        c.resolved_risk_pct = kw.get("risk", 1.2)
        return c

    def test_disabled_allows_one_position_only(self):
        rm, _ = manager()
        off = PortfolioConfig(enabled=False)
        self.assertTrue(rm.check_portfolio({}, 43.0, 40.0, "AUSDT", off))
        held = {"AUSDT": pos("AUSDT")}
        self.assertFalse(rm.check_portfolio(held, 43.0, 40.0, "BUSDT", off))

    def test_never_doubles_up_on_one_symbol(self):
        rm, _ = manager()
        held = {"AUSDT": pos("AUSDT")}
        d = rm.check_portfolio(held, 43.0, 10.0, "AUSDT", self._cfg())
        self.assertFalse(d)
        self.assertIn("one position per symbol", d.reason)

    def test_refuses_once_slots_are_full(self):
        rm, _ = manager()
        held = {f"S{i}USDT": pos(f"S{i}USDT", qty=0.05) for i in range(5)}
        d = rm.check_portfolio(held, 43.0, 5.0, "NEWUSDT", self._cfg(slots=5))
        self.assertFalse(d)
        self.assertIn("slots in use", d.reason)

    def test_refuses_a_breach_of_total_leverage(self):
        rm, _ = manager(max_leverage=3)
        held = {"AUSDT": pos("AUSDT", entry=100.0, qty=1.2)}   # $120 of $129
        d = rm.check_portfolio(held, 43.0, 40.0, "BUSDT", self._cfg())
        self.assertFalse(d)
        self.assertIn("leverage ceiling", d.reason)

    def test_refuses_a_breach_of_total_risk(self):
        rm, _ = manager()
        cfg = self._cfg(slots=99, risk=2.0)
        held = {f"S{i}USDT": pos(f"S{i}USDT", qty=0.01) for i in range(3)}
        d = rm.check_portfolio(held, 43.0, 1.0, "NEWUSDT", cfg)
        self.assertFalse(d)
        self.assertIn("cap", d.reason)

    def test_allows_a_legitimate_addition(self):
        rm, _ = manager()
        held = {"AUSDT": pos("AUSDT", entry=100.0, qty=0.05)}
        d = rm.check_portfolio(held, 43.0, 5.0, "BUSDT", self._cfg())
        self.assertTrue(d, d.reason)
        self.assertIn("slot 2", d.reason)

    def test_gate_sits_above_per_trade_sizing_not_instead_of_it(self):
        """Both must be consulted; the portfolio gate cannot approve a bad size."""
        import inspect
        src = inspect.getsource(RiskManager)
        self.assertIn("def size_position", src)
        self.assertIn("def check_portfolio", src)
