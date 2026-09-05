"""Phase 3: the position book, and close-all."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.engine import Engine                       # noqa: E402
from bot.positions import ActivePosition            # noqa: E402
from test_r2_regressions import StubAPI, engine     # noqa: E402


def pos(sym, entry=100.0, qty=1.0):
    return ActivePosition(sym, "BUY", entry, entry * .98, entry * 1.03, qty,
                          entry_order_id=f"e-{sym}", stop_order_id=f"s-{sym}",
                          tp_order_id=f"t-{sym}", tag=f"e-{sym}")


class TestBookIsTheStorage(unittest.TestCase):
    def test_active_reads_through_to_the_book(self):
        e = engine()
        e.book = {"AUSDT": pos("AUSDT")}
        self.assertIsNotNone(e.active)
        self.assertEqual(e.active.symbol, "AUSDT")

    def test_setting_active_writes_the_book(self):
        """Every existing call site assigns to .active; that must still work."""
        e = engine()
        e.active = pos("BUSDT")
        self.assertEqual(list(e.book), ["BUSDT"])

    def test_clearing_active_empties_the_book(self):
        e = engine()
        e.active = pos("BUSDT")
        e.active = None
        self.assertEqual(e.book, {})

    def test_empty_book_reads_as_no_position(self):
        self.assertIsNone(engine().active)

    def test_position_for_finds_by_symbol(self):
        e = engine()
        e.book = {"AUSDT": pos("AUSDT"), "BUSDT": pos("BUSDT")}
        self.assertEqual(e.position_for("BUSDT").symbol, "BUSDT")
        self.assertIsNone(e.position_for("CUSDT"))

    def test_release_frees_one_slot_only(self):
        e = engine()
        e.book = {"AUSDT": pos("AUSDT"), "BUSDT": pos("BUSDT")}
        e.release("AUSDT")
        self.assertEqual(list(e.book), ["BUSDT"])

    def test_release_of_an_unheld_symbol_is_harmless(self):
        engine().release("NOPEUSDT")


class TestPerSymbolPricing(unittest.TestCase):
    def test_each_position_is_valued_at_its_own_price(self):
        e = engine()
        e.book = {"AUSDT": pos("AUSDT", entry=100.0),
                  "BUSDT": pos("BUSDT", entry=50.0)}
        e.last_prices = {"AUSDT": 110.0, "BUSDT": 45.0}
        rows = {r["symbol"]: r for r in Engine._position_book(e)}
        self.assertGreater(rows["AUSDT"]["unrealized"], 0)
        self.assertLess(rows["BUSDT"]["unrealized"], 0,
                        "positions were valued against one shared price")

    def test_tick_for_an_unheld_symbol_only_caches_the_price(self):
        from bot.stream import Tick
        e = engine()
        e.book = {"AUSDT": pos("AUSDT")}
        e.cfg.dry_run = False
        e._last_guard = 9e9        # skip the guard
        Engine.on_tick(e, Tick("ZUSDT", 123.0, 0))
        self.assertEqual(e.last_prices["ZUSDT"], 123.0)


class TestCloseAll(unittest.TestCase):
    def test_closes_every_position(self):
        api = StubAPI(position_amt=1.0)
        e = engine(api=api)
        e.book = {s: pos(s) for s in ("AUSDT", "BUSDT", "CUSDT")}
        closed = Engine.close_all(e, "target reached")
        self.assertEqual(closed, 3)
        self.assertEqual(e.book, {})

    def test_a_mid_sequence_failure_leaves_the_rest_protected(self):
        class Flaky(StubAPI):
            def order(self, **kw):
                if kw.get("symbol") == "BUSDT":
                    raise RuntimeError("exchange said no")
                return super().order(**kw)

        api = Flaky(position_amt=1.0)
        e = engine(api=api)
        e.book = {s: pos(s) for s in ("AUSDT", "BUSDT", "CUSDT")}
        Engine.close_all(e, "target reached")
        # BUSDT could not be closed, so it must still be tracked and still
        # hold its stop -- never silently dropped.
        self.assertIn("BUSDT", e.book)
        self.assertNotIn("AUSDT", e.book)
        body = " ".join(b for _, b in e.sent)
        self.assertIn("FAILED", body)
        self.assertIn("still hold their stops", body)

    def test_dry_run_closes_locally_and_sends_nothing(self):
        api = StubAPI()
        e = engine(api=api, dry_run=True)
        e.book = {s: pos(s) for s in ("AUSDT", "BUSDT")}
        Engine.close_all(e, "target")
        self.assertEqual(e.book, {})
        self.assertEqual([c for c in api.calls if c[0] == "order"], [])


class TestClosingOneNeverDropsTheOthers(unittest.TestCase):
    """
    `self.active = None` routes through the property setter and replaces the
    WHOLE book. Used inside close_position it meant closing one of twenty-five
    positions silently dropped tracking for the other twenty-four while they
    were still open on the exchange. Only release(symbol) is safe there.
    """

    def test_closing_one_leaves_the_rest_tracked(self):
        e = engine(api=StubAPI(position_amt=1.0))
        e.book = {s: pos(s) for s in ("AUSDT", "BUSDT", "CUSDT")}
        Engine.close_position(e, "manual", symbol="BUSDT")
        self.assertEqual(sorted(e.book), ["AUSDT", "CUSDT"])

    def test_dry_run_close_also_releases_only_one(self):
        e = engine(api=StubAPI(), dry_run=True)
        e.book = {s: pos(s) for s in ("AUSDT", "BUSDT")}
        Engine.close_position(e, "manual", symbol="AUSDT")
        self.assertEqual(list(e.book), ["BUSDT"])

    def test_close_position_never_assigns_to_active(self):
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(Engine.close_position)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if (isinstance(t, ast.Attribute) and t.attr == "active"):
                        self.fail("close_position assigns to .active, "
                                  "which wipes the whole book")

    def test_book_is_not_shared_between_instances(self):
        a, b = engine(), engine()
        a.book["LEAK"] = pos("LEAK")
        self.assertNotIn("LEAK", b.book)
        self.assertNotIn("LEAK", Engine().book if False else {})

    def test_bare_engine_gets_its_own_book(self):
        x = object.__new__(Engine)
        y = object.__new__(Engine)
        x.book["A"] = 1
        self.assertEqual(y.book, {}, "book is a shared class-level dict")
