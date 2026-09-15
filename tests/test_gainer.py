"""
Gainer mining: bot/gainer.py.

The board, the arithmetic and the leader-change rules are tested without an
exchange. The live path is tested for what can lose money: that a filled
market entry gets its stop and take-profit, that a failed stop flattens the
position, that an unprofitable old gainer is closed while a profitable one is
kept, and that the supervisor stands aside for gainer positions.
"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.binanceapi import BinanceError                    # noqa: E402
from bot.gainer import (GainerBoard, GainerConfig, GainerMiner,  # noqa: E402
                        Track, net_pnl, rank_board, stop_price, target_price)
from bot.notify import Event                               # noqa: E402
from test_r2_regressions import StubAPI, engine            # noqa: E402


def tick(sym, pct, price=1.0, qv=50e6):
    return {"symbol": sym, "priceChangePercent": str(pct),
            "lastPrice": str(price), "quoteVolume": str(qv)}


class Rules:
    symbol = "X"

    def round_qty(self, q):
        return f"{float(q):.0f}"

    def round_price(self, p):
        return f"{float(p):.6f}"

    def size_for_notional(self, notional, price):
        qty = int(notional / price) + 1
        return f"{qty}", f"{price:.6f}"

    def min_affordable_notional(self, price):
        return 5.0


class BoardAPI(StubAPI):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.board = []
        self.orders = []
        self.algo = []
        self.fail_algo = False
        self.filled_amt = 0.0

    def ticker_24hr(self, symbol=None):
        return self.board

    def exchange_info(self):
        return {"symbols": [{"symbol": t["symbol"], "status": "TRADING",
                             "contractType": "PERPETUAL", "quoteAsset": "USDT",
                             "baseAsset": t["symbol"][:-4]} for t in self.board]}

    def order(self, **p):
        self.orders.append(p)
        if p.get("type") == "MARKET" and not p.get("reduceOnly"):
            self.filled_amt = float(p["quantity"])
        return {"orderId": 1}

    def algo_order(self, **p):
        if self.fail_algo:
            raise BinanceError(-2021, "would trigger immediately", "/fapi/v1/algoOrder")
        self.algo.append(p)
        return {"algoId": 1}

    def positions(self, symbol=None):
        if self.filled_amt:
            return [{"positionAmt": str(self.filled_amt), "entryPrice": "1.0",
                     "unRealizedProfit": "0", "liquidationPrice": "0"}]
        return []

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))

    def set_leverage(self, symbol, leverage):
        return {}


def miner(dry=True, api=None, **kw):
    e = engine(api=api or BoardAPI())
    e._book = {}
    e._seq = 0
    e._prepared = {"AAAUSDT", "BBBUSDT", "CCCUSDT"}
    e.equity = 50.0
    # engine() anchors the day at 5000; at 50 the preflight would read a 99%
    # daily loss and refuse every open for the wrong reason.
    e.state.day_start_equity = e.equity
    e.rules_for = lambda s: Rules()
    closed = []

    def close_position(reason, symbol=None):
        closed.append(symbol)
        e.book.pop(symbol, None)
        return True
    e.close_position = close_position
    e.closed = closed
    base = dict(enabled=True, dry_run=dry, poll_seconds=0, confirm_polls=1,
                milestones_usd=[1.0, 2.0], status_minutes=0)
    base.update(kw)
    path = Path(tempfile.mkdtemp()) / "gainer.json"
    m = GainerMiner(e, GainerConfig(**base), path=path)
    e.gainer = m
    return m, e


def bodies(e):
    return [b for _, b in e.sent]


class Arithmetic(unittest.TestCase):
    def test_target_nets_the_dollar_amount_after_costs(self):
        tp = target_price(1.0, 10.0, 2.0, 0.15)
        self.assertAlmostEqual(net_pnl(1.0, 10.0, tp, 0.15), 2.0)

    def test_stop_is_below_entry(self):
        self.assertAlmostEqual(stop_price(2.0, 5.0), 1.9)

    def test_rank_board_filters_volume_and_orders_by_change(self):
        rows = rank_board([tick("AAAUSDT", 10), tick("BBBUSDT", 30),
                           tick("THINUSDT", 90, qv=1e5)], set(), 10e6)
        self.assertEqual([r.symbol for r in rows], ["BBBUSDT", "AAAUSDT"])
        self.assertEqual(rows[0].rank, 1)


class Board(unittest.TestCase):
    def test_forecast_names_the_closing_challenger(self):
        b = GainerBoard(GainerConfig(slope_minutes=10, predict_minutes=15))
        b.update(rank_board([tick("AAAUSDT", 40), tick("BBBUSDT", 20)], set(), 0), 0)
        b.update(rank_board([tick("AAAUSDT", 39), tick("BBBUSDT", 35)], set(), 0), 600)
        fc = b.forecast(600)
        self.assertEqual(fc.leader, "AAAUSDT")
        self.assertEqual(fc.leader_trend, "fading")
        self.assertEqual(fc.challenger, "BBBUSDT")
        self.assertGreater(fc.eta_minutes, 0)

    def test_no_history_means_unknown_not_a_guess(self):
        b = GainerBoard(GainerConfig())
        b.update(rank_board([tick("AAAUSDT", 40), tick("BBBUSDT", 20)], set(), 0), 0)
        fc = b.forecast(0)
        self.assertEqual(fc.leader_trend, "unknown")
        self.assertEqual(fc.challenger, "")


class LeaderChange(unittest.TestCase):
    def test_boot_leader_is_a_baseline_not_a_trade(self):
        m, e = miner()
        m.engine.api.board = [tick("AAAUSDT", 40)]
        m.tick(now=1000)
        self.assertEqual(m.leader, "AAAUSDT")
        self.assertEqual(m.tracks, {})

    def test_new_leader_opens_a_paper_position_and_says_so(self):
        m, e = miner()
        e.api.board = [tick("AAAUSDT", 40), tick("BBBUSDT", 20)]
        m.tick(now=1000)
        e.api.board = [tick("BBBUSDT", 45), tick("AAAUSDT", 40)]
        m.tick(now=1030)
        self.assertIn("BBBUSDT", m.tracks)
        self.assertTrue(any("NEW TOP GAINER: BBBUSDT" in b for b in bodies(e)))
        self.assertTrue(any("PAPER OPENED BBBUSDT" in b for b in bodies(e)))
        self.assertEqual(e.api.orders, [], "paper mode must send nothing")

    def test_confirmation_ignores_a_one_poll_flicker(self):
        m, e = miner(confirm_polls=2)
        e.api.board = [tick("AAAUSDT", 40), tick("BBBUSDT", 20)]
        m.tick(now=1000)
        e.api.board = [tick("BBBUSDT", 41), tick("AAAUSDT", 40)]
        m.tick(now=1030)
        e.api.board = [tick("AAAUSDT", 42), tick("BBBUSDT", 41)]
        m.tick(now=1060)
        self.assertEqual(m.tracks, {})

    def test_old_gainer_kept_if_profitable_closed_if_not(self):
        m, e = miner()
        m._baselined, m.leader = True, "AAAUSDT"
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.95, 1.2, 0, paper=True)
        m.tracks["CCCUSDT"] = Track("CCCUSDT", 1.0, 10, 0.95, 1.2, 0, paper=True)
        e.api.board = [tick("BBBUSDT", 50, 1.0), tick("AAAUSDT", 40, 1.05),
                       tick("CCCUSDT", 30, 0.99)]
        m.max_positions = 3
        m.cfg.max_positions = 3
        m.tick(now=1000)
        self.assertIn("AAAUSDT", m.tracks)
        self.assertNotIn("CCCUSDT", m.tracks)
        self.assertIn("BBBUSDT", m.tracks)
        self.assertTrue(any("AAAUSDT KEPT" in b for b in bodies(e)))
        self.assertTrue(any("PAPER CLOSED CCCUSDT" in b for b in bodies(e)))

    def test_milestones_fire_once_each(self):
        m, e = miner()
        m._baselined, m.leader = True, "AAAUSDT"
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.5, 2.0, 0, paper=True)
        e.api.board = [tick("AAAUSDT", 40, 1.15)]
        m.tick(now=1000)
        m.tick(now=1030)
        hits = [b for b in bodies(e) if "milestone" in b]
        self.assertEqual(len(hits), 1)
        self.assertIn("$1 milestone", hits[0])

    def test_paper_take_profit_closes(self):
        m, e = miner()
        m._baselined, m.leader = True, "AAAUSDT"
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.9, 1.2, 0, paper=True)
        e.api.board = [tick("AAAUSDT", 40, 1.25)]
        m.tick(now=1000)
        self.assertEqual(m.tracks, {})
        self.assertTrue(any("take-profit hit" in b for b in bodies(e)))


class LiveOrders(unittest.TestCase):
    def test_filled_entry_gets_stop_and_take_profit(self):
        m, e = miner(dry=False)
        self.assertTrue(m.open("AAAUSDT", 1.0, 40))
        self.assertEqual(e.api.orders[0]["type"], "MARKET")
        kinds = [a["type"] for a in e.api.algo]
        self.assertEqual(kinds, ["STOP_MARKET", "TAKE_PROFIT_MARKET"])
        self.assertTrue(all(a["reduceOnly"] == "true" for a in e.api.algo))
        pos = e.book["AAAUSDT"]
        self.assertEqual(pos.strategy, "gainer")
        self.assertTrue(pos.filled)

    def test_failed_stop_flattens_the_position(self):
        api = BoardAPI()
        api.fail_algo = True
        m, e = miner(dry=False, api=api)
        self.assertFalse(m.open("AAAUSDT", 1.0, 40))
        closes = [o for o in api.orders if o.get("reduceOnly") == "true"]
        self.assertEqual(len(closes), 1)
        self.assertNotIn("AAAUSDT", e.book)
        self.assertNotIn("AAAUSDT", m.tracks)

    def test_halted_bot_does_not_open(self):
        m, e = miner(dry=False)
        e.state.halted = True
        self.assertFalse(m.open("AAAUSDT", 1.0, 40))
        self.assertEqual(e.api.orders, [])

    def test_leverage_ceiling_counts_the_whole_book(self):
        m, e = miner(dry=False, notional_usdt=100.0)
        e.equity = e.state.day_start_equity = 10.0
        self.assertFalse(m.open("AAAUSDT", 1.0, 40))
        self.assertTrue(any("leverage ceiling" in b for b in bodies(e)))
        self.assertEqual(e.api.orders, [])

    def test_unprofitable_live_gainer_is_closed_through_the_engine(self):
        m, e = miner(dry=False, max_positions=3)
        m._baselined, m.leader = True, "AAAUSDT"
        m.open("AAAUSDT", 1.0, 40)
        e.api.board = [tick("BBBUSDT", 50, 1.0), tick("AAAUSDT", 40, 0.98)]
        m.tick(now=1000)
        self.assertIn("AAAUSDT", e.closed)
        self.assertNotIn("AAAUSDT", m.tracks)

    def test_supervisor_stands_aside_for_gainer_positions(self):
        m, e = miner(dry=False)
        m.open("AAAUSDT", 1.0, 40)
        from bot.supervise import SuperviseConfig
        e.cfg.supervise = SuperviseConfig(enabled=True)
        e.held_bars = lambda s: (_ for _ in ()).throw(AssertionError("supervised"))
        e.supervise_position(e.book["AAAUSDT"], 0.5)

    def test_restore_remarks_adopted_positions(self):
        m, e = miner(dry=False)
        m.open("AAAUSDT", 1.0, 40)
        e.book["AAAUSDT"].strategy = ""       # as adopt_open_positions leaves it
        m2 = GainerMiner(e, m.cfg, path=m.path)
        m2.restore()
        self.assertEqual(e.book["AAAUSDT"].strategy, "gainer")
        self.assertIn("AAAUSDT", m2.tracks)


class Config(unittest.TestCase):
    def test_config_yaml_loads_the_gainer_section(self):
        from bot.config import Config as C
        cfg = C.load(ROOT / "config.yaml")
        self.assertIsInstance(cfg.gainer, GainerConfig)
        self.assertEqual(cfg.gainer.target_usd, 2.0)
        self.assertGreater(cfg.gainer.notional_usdt, 5.0, "must clear the $5 minimum")

    def test_event_exists(self):
        self.assertEqual(Event.GAINER.value, "gainer mining")


if __name__ == "__main__":
    unittest.main()
