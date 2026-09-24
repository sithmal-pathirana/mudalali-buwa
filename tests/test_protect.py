"""
The protection watchdog (bot/protect.py) and the STGUSDT halt of 2026-09-19.

An STGUSDT buy priced off a 0.1566 bar close was sent with the market at
0.1523. The limit filled at once, its stop at 0.1513 was refused with -2021,
and the halt path cancelled ORDERS, reported "Nothing is open", and left 71
STG long with no stop for four hours.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.binanceapi import BinanceError                        # noqa: E402
from bot.positions import RISK_UNKNOWN, ActivePosition         # noqa: E402
from bot.protect import ProtectConfig, plan_protection, stop_room  # noqa: E402
from bot.strategies.base import Signal                         # noqa: E402
from bot.supervise import SuperviseConfig                      # noqa: E402
from test_r2_regressions import _cleanup, engine               # noqa: E402

STG_SIGNAL = Signal("BUY", entry=0.1566, stop=0.1513, take_profit=0.1644)


# ----------------------------------------------------------------- pure logic
class TestStopRoom(unittest.TestCase):
    def test_stg_had_a_fifth_of_its_stop_left(self):
        room = stop_room(long=True, entry=0.1566, stop=0.1513, mark=0.1523)
        self.assertAlmostEqual(room, 0.1887, places=3)

    def test_a_market_above_a_long_limit_leaves_the_full_distance(self):
        self.assertEqual(stop_room(long=True, entry=1.0, stop=0.9, mark=1.05), 1.0)

    def test_short_mirrors(self):
        # Short at 1.00, stop 1.10, market already up at 1.08: 20% left.
        self.assertAlmostEqual(
            stop_room(long=False, entry=1.0, stop=1.1, mark=1.08), 0.2)

    def test_passed_stop_is_zero_or_less(self):
        self.assertLessEqual(stop_room(long=True, entry=1.0, stop=0.9, mark=0.89), 0)


class TestPlanProtection(unittest.TestCase):
    def plan(self, **kw):
        base = dict(long=True, mark=1.0, atr=0.02, efficiency=0.5,
                    has_stop=False, has_target=False)
        base.update(kw)
        return plan_protection(**base)

    def test_planned_levels_are_restored_when_still_valid(self):
        p = self.plan(planned_stop=0.95, planned_target=1.08)
        self.assertEqual((p.stop, p.target), (0.95, 1.08))
        self.assertFalse(p.close_now)

    def test_a_passed_planned_stop_closes_instead_of_widening(self):
        p = self.plan(mark=0.94, planned_stop=0.95, planned_target=1.08)
        self.assertTrue(p.close_now)
        self.assertIsNone(p.stop)

    def test_no_plan_is_rebuilt_from_atr(self):
        p = self.plan()
        self.assertAlmostEqual(p.stop, 1.0 - 2.0 * 0.02)
        self.assertAlmostEqual(p.target, 1.0 + 3.0 * 0.02)

    def test_a_choppy_market_gets_a_nearer_target(self):
        p = self.plan(efficiency=0.1)
        self.assertAlmostEqual(p.target, 1.0 + 1.5 * 0.02)
        self.assertIn("choppy", p.why)

    def test_unknown_efficiency_is_not_called_choppy(self):
        p = self.plan(efficiency=-1.0)
        self.assertAlmostEqual(p.target, 1.0 + 3.0 * 0.02)

    def test_short_mirrors(self):
        p = self.plan(long=False)
        self.assertAlmostEqual(p.stop, 1.0 + 0.04)
        self.assertAlmostEqual(p.target, 1.0 - 0.06)

    def test_a_planned_stop_too_close_to_the_mark_is_rebuilt(self):
        """The STG case: 0.1513 under a 0.1515 mark is 0.13% -- one tick of
        noise, or a -2021 before it is even placed."""
        p = self.plan(mark=0.1515, atr=0.002, planned_stop=0.1513)
        self.assertFalse(p.close_now)
        self.assertAlmostEqual(p.stop, 0.1515 - 0.004)
        self.assertIn("too close", p.why)

    def test_no_bars_falls_back_to_percent(self):
        p = self.plan(atr=0.0)
        self.assertAlmostEqual(p.stop, 0.97)
        self.assertAlmostEqual(p.target, 1.045)   # 3% x (3.0 / 2.0)

    def test_tiny_atr_still_respects_the_minimum_gap(self):
        p = self.plan(atr=0.0001)
        self.assertAlmostEqual(p.stop, 0.995)
        self.assertAlmostEqual(p.target, 1.003)

    def test_only_the_missing_leg_is_planned(self):
        p = self.plan(has_stop=True)
        self.assertIsNone(p.stop)
        self.assertIsNotNone(p.target)


class TestConfig(unittest.TestCase):
    def test_groups_arrive_from_yaml_as_dicts(self):
        c = SuperviseConfig(protect={"stop": {"atr_mult": 2.5},
                                     "failure": {"close_after_attempts": 0}})
        self.assertIsInstance(c.protect, ProtectConfig)
        self.assertEqual(c.protect.stop.atr_mult, 2.5)
        self.assertEqual(c.protect.failure.close_after_attempts, 0)
        self.assertTrue(c.protect.watch.enabled)

    def test_a_bad_key_names_its_group(self):
        with self.assertRaises(TypeError) as ctx:
            SuperviseConfig(protect={"stop": {"atr_mul": 2.5}})
        self.assertIn("supervise.protect.stop", str(ctx.exception))

    def test_the_shipped_config_loads(self):
        from bot.config import Config
        cfg = Config.load(overlay=False)
        self.assertTrue(cfg.supervise.protect.watch.enabled)
        self.assertEqual(cfg.supervise.protect.entry.min_stop_room_frac, 0.5)


# ------------------------------------------------------------------ the engine
class ExchangeAPI:
    """A small exchange: positions, open orders, mark prices. Stops can be
    made to fail a number of times."""

    def __init__(self, positions=None, orders=(), marks=None, stop_failures=0,
                 fill_on_entry=None):
        self.pos = dict(positions or {})          # symbol -> (amt, entry)
        self.orders = list(orders)                # dicts with symbol/type/...
        self.marks = dict(marks or {})
        self.stop_failures = stop_failures
        self.fill_on_entry = fill_on_entry        # (amt, fill price) or None
        self.calls = []
        self.last_sync = __import__("time").time()

    def positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        return [{"symbol": s, "positionAmt": str(a), "entryPrice": str(p),
                 "unRealizedProfit": "0", "updateTime": 0}
                for s, (a, p) in self.pos.items()
                if a and (symbol is None or s == symbol)]

    def open_orders(self, symbol=None):
        return [o for o in self.orders if o["type"] == "LIMIT"
                and (symbol is None or o["symbol"] == symbol)]

    def open_algo_orders(self, symbol=None):
        return [o for o in self.orders if o["type"] != "LIMIT"
                and (symbol is None or o["symbol"] == symbol)]

    def mark_price(self, symbol):
        if symbol not in self.marks:
            raise BinanceError(-1001, "unavailable", "/premiumIndex")
        return {"symbol": symbol, "markPrice": str(self.marks[symbol])}

    def order(self, **kw):
        self.calls.append(("order", kw.get("symbol"), kw.get("type"), kw.get("price")))
        if kw.get("type") == "LIMIT" and self.fill_on_entry:
            self.pos[kw["symbol"]] = self.fill_on_entry
        return {"status": "NEW"}

    def algo_order(self, **kw):
        self.calls.append(("algo_order", kw.get("symbol"), kw.get("type"),
                           kw.get("triggerPrice")))
        if kw.get("type") == "STOP_MARKET" and self.stop_failures > 0:
            self.stop_failures -= 1
            raise BinanceError(-2021, "Order would immediately trigger.", "/algoOrder")
        self.orders.append({"symbol": kw["symbol"], "type": kw["type"],
                            "clientOrderId": kw.get("clientAlgoId", ""),
                            "stopPrice": kw.get("triggerPrice")})
        return {"algoStatus": "NEW"}

    def cancel_algo_order(self, cid):
        self.calls.append(("cancel_algo_order", cid))

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))
        self.orders = [o for o in self.orders if o["symbol"] != symbol]

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        return {}

    def set_leverage(self, symbol, leverage):
        return {}

    def user_trades(self, symbol, start_ms=None, limit=1000):
        return []

    def placed(self, kind):
        return [c for c in self.calls if c[0] == "algo_order" and c[2] == kind]


def _engine(api, atr=0.002, er=0.5):
    e = engine(api=api)
    e.rules = type("R", (), {
        "size_for_notional": lambda s, n, p: ("71", f"{p:.4f}"),
        "round_price": lambda s, p: f"{p:.6f}",
        "round_qty": lambda s, q: f"{q:g}"})()
    e.rules_for = lambda symbol: e.rules
    e.book = {}
    e._seq = 0
    e._entry_placed_at = 0.0
    e._prepared = set()
    e.signals = None
    e.prepare_symbol = lambda symbol: True
    e.market_reading = lambda symbol: (atr, er)
    e.closed = []

    def close(reason, symbol=None):
        e.closed.append((symbol, reason))
        api.pos.pop(symbol, None)
        e.book.pop(symbol, None)
        return True
    e.close_position = close
    return e


def _tracked(symbol="STGUSDT", stop=0.1513, tp=0.1644, entry=0.1523):
    return ActivePosition(symbol, "BUY", entry, stop, tp, 71.0,
                          entry_order_id="e-1", stop_order_id="s-1",
                          tp_order_id="t-1", tag="e-1", filled=True,
                          initial_stop=stop, initial_risk=abs(entry - stop))


class TestTheStgEntryIsSkipped(unittest.TestCase):
    """Root cause: the signal was priced 2.7% above where it could fill."""

    def tearDown(self):
        _cleanup()

    def test_no_order_is_sent(self):
        api = ExchangeAPI(marks={"STGUSDT": 0.1523})
        e = _engine(api)
        e.place(STG_SIGNAL, 5.36, "n", symbol="STGUSDT")
        self.assertFalse([c for c in api.calls if c[0] in ("order", "algo_order")])
        self.assertNotIn("STGUSDT", e.book)
        self.assertFalse(e.state.halted)

    def test_the_same_signal_trades_when_the_market_has_not_moved(self):
        api = ExchangeAPI(marks={"STGUSDT": 0.1566})
        e = _engine(api)
        e.place(STG_SIGNAL, 5.36, "n", symbol="STGUSDT")
        self.assertIn("STGUSDT", e.book)

    def test_zero_turns_the_check_off(self):
        api = ExchangeAPI(marks={"STGUSDT": 0.1523})
        e = _engine(api)
        e.cfg.supervise.protect.entry.min_stop_room_frac = 0.0
        e.place(STG_SIGNAL, 5.36, "n", symbol="STGUSDT")
        self.assertTrue([c for c in api.calls if c[0] == "order"])


class TestAFilledEntryWhoseStopIsRefused(unittest.TestCase):
    """The halt path: the entry filled before the stop was refused."""

    def tearDown(self):
        _cleanup()

    def _run(self, mark, stop_failures=1):
        api = ExchangeAPI(marks={"STGUSDT": mark}, stop_failures=stop_failures,
                          fill_on_entry=(71.0, 0.1523))
        e = _engine(api)
        e.protective_levels_crossed = lambda *a, **k: ""    # the race got past it
        e.place(STG_SIGNAL, 5.36, "n", symbol="STGUSDT")
        return api, e

    def test_it_is_tracked_and_protected_not_left_naked(self):
        api, e = self._run(mark=0.1530)
        self.assertFalse(e.state.halted)
        self.assertIn("STGUSDT", e.book)
        stops = api.placed("STOP_MARKET")
        self.assertEqual(len(stops), 2, "the refused stop was not placed again")
        # 0.1513 is 1.1% under a 0.1530 mark: the planned stop still stands.
        self.assertEqual(stops[-1][3], "0.151300")
        self.assertTrue(api.placed("TAKE_PROFIT_MARKET"))
        self.assertFalse([b for _, b in e.sent if "Nothing is open" in b])

    def test_past_the_planned_stop_it_is_closed(self):
        api, e = self._run(mark=0.1510)
        self.assertEqual(e.closed and e.closed[0][0], "STGUSDT")
        self.assertFalse(e.state.halted)

    def test_it_is_closed_when_no_stop_can_be_placed_at_all(self):
        api, e = self._run(mark=0.1530, stop_failures=99)
        self.assertEqual([s for s, _ in e.closed], ["STGUSDT"])
        self.assertFalse(e.state.halted)

    def test_nothing_filled_is_a_skip_with_a_true_message(self):
        api = ExchangeAPI(marks={"STGUSDT": 0.1530}, stop_failures=1)
        e = _engine(api)
        e.protective_levels_crossed = lambda *a, **k: ""
        e.place(STG_SIGNAL, 5.36, "n", symbol="STGUSDT")
        self.assertFalse(e.state.halted)
        self.assertNotIn("STGUSDT", e.book)
        self.assertTrue([b for _, b in e.sent if "nothing is open" in b])


class TestTheWatchdog(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_an_untracked_naked_position_is_adopted_and_protected(self):
        """Exactly what sat on the exchange from 00:00 to 04:15."""
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api, atr=0.0025, er=0.5)
        e.reconcile_book()
        self.assertIn("STGUSDT", e.book)
        pos = e.book["STGUSDT"]
        self.assertAlmostEqual(pos.stop, 0.1540 - 2 * 0.0025)
        self.assertAlmostEqual(pos.take_profit, 0.1540 + 3 * 0.0025)
        self.assertEqual(len(api.placed("STOP_MARKET")), 1)
        self.assertEqual(len(api.placed("TAKE_PROFIT_MARKET")), 1)
        # 1R was unknown on adoption; the stop just placed defines it now,
        # so the supervisor manages the position instead of standing down.
        self.assertAlmostEqual(pos.initial_risk, 2 * 0.0025)

    def test_it_runs_while_halted(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        e.state.halted = True
        e.reconcile_book()
        self.assertTrue(api.placed("STOP_MARKET"))

    def test_a_protected_position_is_left_alone(self):
        api = ExchangeAPI(
            positions={"STGUSDT": (71.0, 0.1523)}, marks={"STGUSDT": 0.1540},
            orders=[{"symbol": "STGUSDT", "type": "STOP_MARKET",
                     "clientOrderId": "s-1", "stopPrice": "0.1513"},
                    {"symbol": "STGUSDT", "type": "TAKE_PROFIT_MARKET",
                     "clientOrderId": "t-1", "stopPrice": "0.1644"}])
        e = _engine(api)
        e.book["STGUSDT"] = _tracked()
        e.reconcile_book()
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])

    def test_a_trailing_stop_counts_as_a_stop(self):
        api = ExchangeAPI(
            positions={"STGUSDT": (71.0, 0.1523)}, marks={"STGUSDT": 0.1540},
            orders=[{"symbol": "STGUSDT", "type": "TRAILING_STOP_MARKET",
                     "clientOrderId": "s-1", "stopPrice": "0"}])
        e = _engine(api)
        e.book["STGUSDT"] = _tracked()
        e.reconcile_book()
        self.assertFalse(api.placed("STOP_MARKET"))
        self.assertEqual(len(api.placed("TAKE_PROFIT_MARKET")), 1)

    def test_only_a_missing_take_profit_is_put_back_at_its_planned_level(self):
        api = ExchangeAPI(
            positions={"STGUSDT": (71.0, 0.1523)}, marks={"STGUSDT": 0.1540},
            orders=[{"symbol": "STGUSDT", "type": "STOP_MARKET",
                     "clientOrderId": "s-1", "stopPrice": "0.1513"}])
        e = _engine(api)
        e.book["STGUSDT"] = _tracked()
        e.reconcile_book()
        self.assertFalse(api.placed("STOP_MARKET"))
        self.assertEqual(api.placed("TAKE_PROFIT_MARKET")[0][3], "0.164400")

    def test_a_deliberate_no_target_position_keeps_no_target(self):
        """The gainer ladder runs on its stop alone; no TP is put back."""
        api = ExchangeAPI(
            positions={"STGUSDT": (71.0, 0.1523)}, marks={"STGUSDT": 0.1540},
            orders=[{"symbol": "STGUSDT", "type": "STOP_MARKET",
                     "clientOrderId": "s-1", "stopPrice": "0.1513"}])
        e = _engine(api)
        pos = _tracked()
        pos.no_target = True
        e.book["STGUSDT"] = pos
        e.reconcile_book()
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])

    def test_a_no_target_position_still_gets_a_missing_stop(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        pos = _tracked()
        pos.no_target = True
        e.book["STGUSDT"] = pos
        e.reconcile_book()
        self.assertEqual(len(api.placed("STOP_MARKET")), 1)
        self.assertFalse(api.placed("TAKE_PROFIT_MARKET"))

    def test_repeated_stop_failures_close_the_position(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540}, stop_failures=99)
        e = _engine(api)
        e.book["STGUSDT"] = _tracked()
        for _ in range(2):
            e.reconcile_book()
            self.assertFalse(e.closed)
        e.reconcile_book()
        self.assertEqual([s for s, _ in e.closed], ["STGUSDT"])

    def test_zero_attempts_means_alert_only(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540}, stop_failures=99)
        e = _engine(api)
        e.cfg.supervise.protect.failure.close_after_attempts = 0
        e.book["STGUSDT"] = _tracked()
        for _ in range(5):
            e.reconcile_book()
        self.assertFalse(e.closed)

    def test_a_stop_that_just_triggered_is_not_replaced(self):
        """positions() is read before the orders: a stop that fires in between
        looks like an open position with no stop. The re-read sees it flat."""
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        e.book["STGUSDT"] = _tracked()
        live = {r["symbol"]: r for r in api.positions()}
        api.pos.clear()                             # the stop fired
        e.protect_positions(live, [])
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])

    def test_untracked_is_only_reported_when_adoption_is_off(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        e.cfg.supervise.protect.watch.adopt_untracked = False
        e.reconcile_book()
        self.assertNotIn("STGUSDT", e.book)
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])
        self.assertTrue([b for _, b in e.sent if "NOT tracking" in b])

    def test_off_means_off(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        e.cfg.supervise.protect.watch.enabled = False
        e.reconcile_book()
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])

    def test_dry_run_touches_nothing(self):
        api = ExchangeAPI(positions={"STGUSDT": (71.0, 0.1523)},
                          marks={"STGUSDT": 0.1540})
        e = _engine(api)
        e.cfg.dry_run = True
        e.protect_positions({r["symbol"]: r for r in api.positions()}, [])
        self.assertFalse([c for c in api.calls if c[0] == "algo_order"])

    def test_an_adopted_break_even_stop_keeps_its_unknown_risk(self):
        """Only a MISSING stop redefines 1R. An adopted position that already
        has a stop is untouched, RISK_UNKNOWN included."""
        api = ExchangeAPI(
            positions={"STGUSDT": (71.0, 0.1523)}, marks={"STGUSDT": 0.1540},
            orders=[{"symbol": "STGUSDT", "type": "STOP_MARKET",
                     "clientOrderId": "s-9", "stopPrice": "0.1524"},
                    {"symbol": "STGUSDT", "type": "TAKE_PROFIT_MARKET",
                     "clientOrderId": "t-9", "stopPrice": "0.1644"}])
        e = _engine(api)
        e.reconcile_book()
        self.assertEqual(e.book["STGUSDT"].initial_risk, RISK_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
