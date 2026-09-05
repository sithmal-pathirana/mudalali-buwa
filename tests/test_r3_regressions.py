"""
Round-three findings: the exchange boundary stayed single-symbol while
everything above it went multi-symbol.
"""

import ast
import collections
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.engine import Engine                       # noqa: E402
from bot.stream import OrderUpdate                  # noqa: E402
from test_book import pos                           # noqa: E402
from test_r2_regressions import StubAPI, engine     # noqa: E402


class MultiAPI(StubAPI):
    """Answers per symbol; the single-symbol stub could not catch these."""

    def __init__(self, open_symbols=(), fail_on=()):
        super().__init__()
        self.open = {s: 10.0 for s in open_symbols}
        self.fail_on = set(fail_on)

    def positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        amt = self.open.get(symbol, 0.0)
        return [] if not amt else [{"symbol": symbol, "positionAmt": str(amt),
                                    "entryPrice": "1.0", "unRealizedProfit": "0",
                                    "liquidationPrice": "0.5"}]

    def order(self, **kw):
        from bot.binanceapi import BinanceError
        self.calls.append(("order", kw.get("symbol")))
        if kw.get("symbol") in self.fail_on:
            raise BinanceError(-2011, "rejected", "/order")
        self.open.pop(kw.get("symbol"), None)
        return {"status": "NEW"}

    def touched(self, symbol):
        return any(symbol in [str(x) for x in c] for c in self.calls)


def held(symbols, **kw):
    e = engine(**kw)
    e.book = {s: pos(s) for s in symbols}
    e.strategy = type("S", (), {"name": "x", "mode": "auto"})()
    # The real schedule: snapshot() needs a Progress object, and a fake that
    # returns a string only proves the fake works.
    from bot.targets import TargetSchedule
    e.schedule = TargetSchedule.from_config(
        {"stop_when_reached": False,
         "schedule": [{"from_day": 1, "usd_per_day": 2.0}]})
    e.schedule.start_date = e.state.day
    e.last_prices = {s: 1.0 for s in symbols}
    return e


class TestS1SnapshotRenders(unittest.TestCase):
    """snapshot() raised NameError the moment any position opened, freezing
    the dashboard and /status on the shipped single-symbol config."""

    def test_snapshot_with_no_position(self):
        e = held([])
        self.assertEqual(Engine.snapshot(e)["positions"], [])

    def test_snapshot_with_one_position(self):
        e = held(["AUSDT"])
        snap = Engine.snapshot(e)
        self.assertEqual(len(snap["positions"]), 1)
        self.assertIsNotNone(snap["position"])

    def test_snapshot_with_many_positions(self):
        e = held([f"S{i}USDT" for i in range(25)])
        self.assertEqual(len(Engine.snapshot(e)["positions"]), 25)

    def test_snapshot_has_no_duplicate_position_builder(self):
        """One source of truth: the duplicate is what broke."""
        src = inspect.getsource(Engine.snapshot)
        self.assertNotIn('"to_tp"', src, "snapshot builds position dicts again")


class TestS2OrderUpdatesFindTheirPosition(unittest.TestCase):
    def _fill(self, e, symbol, otype="STOP_MARKET", pnl=-2.5, cid=None):
        Engine.on_order(e, OrderUpdate(
            symbol=symbol, client_order_id=cid or f"s-{symbol}", side="SELL",
            status="FILLED", order_type=otype, last_filled_qty=1.0,
            cumulative_qty=1.0, avg_price=1.0, realized_pnl=pnl, raw={}))

    def test_a_scanner_symbol_fill_is_not_dropped(self):
        e = held(["BTCUSDT", "DOGEUSDT", "SOLUSDT"])
        e.cfg.symbol = "BTCUSDT"
        self._fill(e, "SOLUSDT")
        self.assertNotIn("SOLUSDT", e.book, "slot never freed")
        self.assertAlmostEqual(e.state.realized_today, -2.5,
                               msg="loss never booked")

    def test_closing_one_leaves_the_others(self):
        e = held(["AUSDT", "BUSDT", "CUSDT"])
        self._fill(e, "BUSDT")
        self.assertEqual(sorted(e.book), ["AUSDT", "CUSDT"])

    def test_an_update_for_an_unheld_symbol_is_ignored(self):
        e = held(["AUSDT"])
        self._fill(e, "ZUSDT")
        self.assertEqual(sorted(e.book), ["AUSDT"])
        self.assertEqual(e.state.realized_today, 0.0)

    def test_realized_pnl_accrues_so_the_target_can_fire(self):
        """S2's real damage: check_target_reached could never trigger."""
        e = held(["AUSDT", "BUSDT"])
        self._fill(e, "AUSDT", pnl=1.5)
        self._fill(e, "BUSDT", pnl=1.0)
        self.assertAlmostEqual(e.state.realized_today, 2.5)


class TestS3ReconcileIsScoped(unittest.TestCase):
    def test_reconciling_one_symbol_keeps_the_others(self):
        e = held(["ADAUSDT", "DOGEUSDT", "SOLUSDT"], api=MultiAPI())
        e.cfg.symbol = "DOGEUSDT"
        Engine.reconcile_position(e, {"position_amt": 0.0, "open_order_ids": set()})
        self.assertEqual(sorted(e.book), ["ADAUSDT", "SOLUSDT"])

    def test_reconcile_position_never_assigns_to_active(self):
        tree = ast.parse(inspect.getsource(Engine.reconcile_position).lstrip())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "active":
                        self.fail("reconcile_position wipes the whole book")

    def test_book_wide_reconcile_exists_and_uses_two_calls(self):
        src = inspect.getsource(Engine.reconcile_book)
        self.assertIn("self.api.positions()", src)
        self.assertIn("self.api.open_orders()", src)


class TestS4ShutdownCoversEverySymbol(unittest.TestCase):
    def test_every_held_symbol_is_handled(self):
        symbols = ["SOLUSDT", "ADAUSDT", "XRPUSDT"]
        api = MultiAPI(open_symbols=symbols)
        e = held(symbols, api=api)
        e.cfg.symbol = "DOGEUSDT"       # configured symbol is flat
        Engine.cancel_orders_safely(e, keep_protective=True)
        missed = [s for s in symbols if not api.touched(s)]
        self.assertEqual(missed, [], f"never looked at {missed}")

    def test_the_configured_symbol_is_included_too(self):
        api = MultiAPI()
        e = held(["AUSDT"], api=api)
        e.cfg.symbol = "DOGEUSDT"
        Engine.cancel_orders_safely(e, keep_protective=True)
        self.assertTrue(api.touched("DOGEUSDT"))


class TestS5CloseAllCountsTruthfully(unittest.TestCase):
    def test_a_refused_close_is_not_counted(self):
        symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = MultiAPI(open_symbols=symbols, fail_on=["BBBUSDT"])
        e = held(symbols, api=api)
        self.assertEqual(Engine.close_all(e, "qa"), 2)

    def test_the_failure_keeps_its_protection_and_its_tracking(self):
        symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = MultiAPI(open_symbols=symbols, fail_on=["BBBUSDT"])
        e = held(symbols, api=api)
        Engine.close_all(e, "qa")
        self.assertEqual(sorted(e.book), ["BBBUSDT"])
        self.assertIn("FAILED", " ".join(b for _, b in e.sent))

    def test_close_position_returns_a_boolean(self):
        api = MultiAPI(open_symbols=["AUSDT"])
        e = held(["AUSDT"], api=api)
        self.assertIs(Engine.close_position(e, "qa", symbol="AUSDT"), True)
        api2 = MultiAPI(open_symbols=["BUSDT"], fail_on=["BUSDT"])
        e2 = held(["BUSDT"], api=api2)
        self.assertIs(Engine.close_position(e2, "qa", symbol="BUSDT"), False)


class TestNoDuplicateMethods(unittest.TestCase):
    """
    A slice-based edit duplicated five methods, and Python binds the LAST
    definition -- so a fix sat in the file as dead code while the stale copy
    ran. Cheap to assert, expensive to miss.
    """

    def test_no_module_defines_a_method_twice(self):
        for path in sorted((ROOT / "bot").rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = collections.Counter(
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    # property getter/setter legitimately share a name
                    and not any(isinstance(d, ast.Attribute) and d.attr == "setter"
                                for d in n.decorator_list))
                dupes = {n: c for n, c in names.items() if c > 1}
                self.assertEqual(dupes, {},
                                 f"{path.name}:{node.name} defines {dupes} twice")
