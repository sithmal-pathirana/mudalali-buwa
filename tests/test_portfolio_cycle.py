"""
Phase 4 gate: the scanner drives entries.

100 eligible opens exactly the affordable number, 1 eligible opens one at the
capped size, 0 opens nothing -- all against a stubbed exchange.
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.config import PortfolioConfig                  # noqa: E402
from bot.engine import Engine                           # noqa: E402
from bot.filters import SymbolRules                     # noqa: E402
from bot.scanner import Candidate, ScanResult           # noqa: E402
from bot.strategies.base import Bar, Signal             # noqa: E402
from test_r2_regressions import StubAPI, engine         # noqa: E402


def cheap_rules(sym):
    return SymbolRules(sym, Decimal("0.000010"), Decimal("1"), Decimal("1"),
                       Decimal("9000000"), Decimal("5"), 6, 0)


def bars(n=60, price=0.09):
    return [Bar(i, price, price * 1.01, price * 0.99, price, 1.0) for i in range(n)]


class AlwaysSignals:
    """Every candidate produces a valid long with a 2% stop."""
    name, warmup = "always", 1

    def on_bars(self, b, position_amt):
        px = b[-1].close
        return Signal("BUY", entry=px, stop=px * 0.98, take_profit=px * 1.03)


class NeverSignals:
    name, warmup = "never", 1

    def on_bars(self, b, position_amt):
        return None


class FakeScanner:
    def __init__(self, n):
        self.last = ScanResult(considered=100)
        self.last.ranked = [
            Candidate(f"C{i}USDT", 0.09, 100e6, 0.5, 1.0, 5.0,
                      score=1.0 - i / 1000, bars=bars())
            for i in range(n)]
        self.cfg = type("C", (), {"max_symbols": 100, "rescan_seconds": 3600})()
        self.scans = 0

    def due(self):
        return False

    def scan(self, **kw):
        self.scans += 1
        return self.last


def portfolio_engine(eligible, equity=43.0, strategy=None, **pf_kw):
    e = engine(api=StubAPI())
    e.equity = equity
    e.strategy = strategy or AlwaysSignals()
    e.scanner = FakeScanner(eligible)
    e.rules_for = cheap_rules
    e.cfg.portfolio = PortfolioConfig(enabled=True, portfolio_risk_pct=6.0,
                                      single_position_cap_pct=2.0, **pf_kw)
    # dry_run False against StubAPI: orders are recorded, never sent, and the
    # book fills immediately. Dry-run mode deliberately rests entries instead,
    # which is a separate path with its own tests.
    e.cfg.dry_run = False
    e._seq = 0
    e.risk.cfg = e.cfg.risk      # point it at this test's risk settings
    return e


class TestPhase4Gate(unittest.TestCase):
    def test_zero_eligible_opens_nothing(self):
        e = portfolio_engine(0)
        self.assertEqual(Engine.portfolio_cycle(e), 0)
        self.assertEqual(e.book, {})

    def test_one_eligible_opens_one_at_the_single_position_cap(self):
        e = portfolio_engine(1)
        self.assertEqual(Engine.portfolio_cycle(e), 1)
        self.assertEqual(len(e.book), 1)
        self.assertAlmostEqual(e.cfg.portfolio.resolved_risk_pct, 2.0)

    def test_hundred_eligible_opens_only_what_is_affordable(self):
        e = portfolio_engine(100)
        opened = Engine.portfolio_cycle(e)
        self.assertEqual(opened, 25, "should open the 25 the account can fund")
        self.assertEqual(len(e.book), 25)

    def test_ten_eligible_shares_the_budget(self):
        e = portfolio_engine(10)
        Engine.portfolio_cycle(e)
        self.assertEqual(len(e.book), 10)
        self.assertAlmostEqual(e.cfg.portfolio.resolved_risk_pct, 0.6, places=4)

    def test_a_bigger_account_opens_more(self):
        small = portfolio_engine(100, equity=43.0)
        big = portfolio_engine(100, equity=500.0)
        Engine.portfolio_cycle(small)
        Engine.portfolio_cycle(big)
        self.assertGreater(len(big.book), len(small.book))

    def test_no_signal_means_no_position(self):
        e = portfolio_engine(50, strategy=NeverSignals())
        self.assertEqual(Engine.portfolio_cycle(e), 0)

    def test_never_opens_a_symbol_twice(self):
        e = portfolio_engine(5)
        Engine.portfolio_cycle(e)
        held = set(e.book)
        Engine.portfolio_cycle(e)
        self.assertEqual(set(e.book), held, "re-opened symbols it already held")

    def test_fills_only_the_free_slots(self):
        e = portfolio_engine(25)
        Engine.portfolio_cycle(e)
        self.assertEqual(len(e.book), 25)
        before = len(e.book)
        self.assertEqual(Engine.portfolio_cycle(e), 0)
        self.assertEqual(len(e.book), before)

    def test_every_position_gets_a_stop(self):
        """The portfolio layer decides how many, never whether there is a stop."""
        e = portfolio_engine(10)
        Engine.portfolio_cycle(e)
        for p in e.book.values():
            self.assertGreater(p.stop, 0)
            self.assertLess(p.stop, p.entry, "long position stop is above entry")

    def test_total_risk_stays_within_the_cap(self):
        e = portfolio_engine(100)
        Engine.portfolio_cycle(e)
        total = len(e.book) * e.cfg.portfolio.resolved_risk_pct
        self.assertLessEqual(round(total, 6), 6.0)

    def test_disabled_portfolio_opens_nothing_here(self):
        e = portfolio_engine(50)
        e.cfg.portfolio.enabled = False
        self.assertEqual(Engine.portfolio_cycle(e), 0)


class TestPhase5CloseAll(unittest.TestCase):
    """Target and KILL must flatten the book, not merely stop opening."""

    def _held(self, n=3, **kw):
        e = portfolio_engine(n, **kw)
        Engine.portfolio_cycle(e)
        return e

    def test_target_reached_closes_every_position(self):
        e = self._held(3)
        self.assertEqual(len(e.book), 3)
        e.state.realized_today = 5.0          # target is $2
        self.assertTrue(Engine.check_target_reached(e))
        self.assertEqual(e.book, {}, "target reached but positions stayed open")

    def test_target_not_reached_leaves_positions_alone(self):
        e = self._held(3)
        e.state.realized_today = 0.5
        Engine.check_target_reached(e)
        self.assertEqual(len(e.book), 3)

    def test_kill_with_flatten_closes_the_whole_book(self):
        e = self._held(3, )
        e.cfg.risk.kill_action = "flatten"
        Engine.trigger_kill(e, "kill file")
        self.assertEqual(e.book, {})

    def test_kill_with_protect_leaves_the_book_intact(self):
        e = self._held(3)
        e.cfg.risk.kill_action = "protect"
        Engine.trigger_kill(e, "kill file")
        self.assertEqual(len(e.book), 3,
                         "protect must leave positions and stops in place")


class TestPhase6Commands(unittest.TestCase):
    from bot.dashboard import Command

    def _held(self, n=3):
        e = portfolio_engine(n)
        Engine.portfolio_cycle(e)
        e.dashboard = None
        e.telegram = None
        return e

    def _run(self, e, value):
        from bot.dashboard import Command

        class Q:
            def __init__(self, c): self.c = [c]
            def pop_commands(self): out, self.c = self.c, []; return out
            def publish(self, snap): pass
            def stop(self): pass
        e.dashboard = Q(Command("close", note="test", value=value))
        Engine.process_commands(e)

    def test_close_all_closes_everything(self):
        e = self._held(3)
        self._run(e, "all")
        self.assertEqual(e.book, {})

    def test_empty_value_also_means_all(self):
        e = self._held(3)
        self._run(e, "")
        self.assertEqual(e.book, {})

    def test_close_one_symbol_leaves_the_others(self):
        e = self._held(3)
        target = sorted(e.book)[1]
        self._run(e, target)
        self.assertNotIn(target, e.book)
        self.assertEqual(len(e.book), 2)

    def test_unknown_symbol_closes_nothing_and_says_so(self):
        e = self._held(3)
        self._run(e, "NOPEUSDT")
        self.assertEqual(len(e.book), 3)
        self.assertIn("Not holding", " ".join(b for _, b in e.sent))

    def test_dashboard_renders_a_position_table(self):
        html = (ROOT / "bot" / "dashboard.html").read_text()
        self.assertIn("book.length > 1", html)
        self.assertIn("total unrealised", html)
        self.assertIn("Close all ", html)


class TestEverySymbolIsPrepared(unittest.TestCase):
    """
    Margin mode and leverage were set at startup for cfg.symbol alone, so every
    scanner-opened symbol inherited the ACCOUNT DEFAULT -- typically cross
    margin at 20x on Binance futures. Cross is the dangerous half: isolated
    caps a position's loss at its own margin, cross lets one position reach the
    whole balance and take the rest of the book with it.
    """

    def _prepared_for(self, api):
        return {c[1] for c in api.calls if c[0] == "set_margin_type"}

    def test_each_opened_symbol_gets_isolated_margin(self):
        e = portfolio_engine(5)
        Engine.portfolio_cycle(e)
        prepared = self._prepared_for(e.api)
        self.assertEqual(prepared, set(e.book),
                         "some positions were opened on the account defaults")

    def test_leverage_matches_the_configured_cap(self):
        e = portfolio_engine(3)
        Engine.portfolio_cycle(e)
        levs = {c[2] for c in e.api.calls if c[0] == "set_leverage"}
        self.assertEqual(levs, {e.cfg.risk.max_leverage})

    def test_margin_type_is_isolated_not_cross(self):
        e = portfolio_engine(3)
        Engine.portfolio_cycle(e)
        modes = {c[2] for c in e.api.calls if c[0] == "set_margin_type"}
        self.assertEqual(modes, {"ISOLATED"})

    def test_preparation_happens_once_per_symbol(self):
        e = portfolio_engine(3)
        Engine.portfolio_cycle(e)
        first = len([c for c in e.api.calls if c[0] == "set_leverage"])
        Engine.portfolio_cycle(e)
        again = len([c for c in e.api.calls if c[0] == "set_leverage"])
        self.assertEqual(first, again, "re-prepared symbols it already holds")

    def test_a_symbol_that_cannot_be_prepared_is_not_traded(self):
        """Better to skip it than trade it on whatever the account defaults to."""
        from bot.binanceapi import BinanceError

        e = portfolio_engine(3)
        real = e.api.set_leverage

        def refuse(symbol, leverage):
            raise BinanceError(-4028, "leverage not modifiable", "/leverage")

        e.api.set_leverage = refuse
        Engine.portfolio_cycle(e)
        self.assertEqual(e.book, {}, "traded a symbol it could not configure")
        e.api.set_leverage = real
