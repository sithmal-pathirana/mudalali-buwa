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
                        Track, ladder_stop, net_pnl, rank_board, stop_price,
                        target_price)
from bot.notify import Event                               # noqa: E402
from test_r2_regressions import StubAPI, engine            # noqa: E402


def tick(sym, pct, price=1.0, qv=50e6, high=None):
    return {"symbol": sym, "priceChangePercent": str(pct),
            "lastPrice": str(price), "quoteVolume": str(qv),
            "highPrice": str(price if high is None else high)}


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
    # The swap guards are off here so each older test isolates its own rule;
    # class SwapGuards turns them on one at a time.
    base = dict(enabled=True, dry_run=dry,
                board=dict(poll_seconds=0),
                entry=dict(confirm_minutes=0, buy_only_if_rising=False,
                           rebuy_cooldown_minutes=0),
                exit=dict(min_hold_minutes=0),
                alerts=dict(milestones_usd=[1.0, 2.0], status_minutes=0))
    for k, v in kw.items():
        if isinstance(v, dict):
            base.setdefault(k, {}).update(v)
        else:
            base[k] = v
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
        b = GainerBoard(GainerConfig(forecast=dict(slope_minutes=10, predict_minutes=15)))
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
        m, e = miner(entry=dict(confirm_minutes=0.5))
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
        m.cfg.entry.max_positions = 3
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
        m, e = miner(dry=False, entry=dict(notional_usdt=100.0))
        e.equity = e.state.day_start_equity = 10.0
        self.assertFalse(m.open("AAAUSDT", 1.0, 40))
        self.assertTrue(any("leverage ceiling" in b for b in bodies(e)))
        self.assertEqual(e.api.orders, [])

    def test_unprofitable_live_gainer_is_closed_through_the_engine(self):
        m, e = miner(dry=False, entry=dict(max_positions=3))
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


class Reentry(unittest.TestCase):
    def stopped_out(self, **kw):
        """Live AAAUSDT, still the leader, closed by its stop at 0.95; 24h high 1.3."""
        m, e = miner(dry=False, **kw)
        m._baselined, m.leader = True, "AAAUSDT"
        m.open("AAAUSDT", 1.0, 40)
        e.book.pop("AAAUSDT")
        e.api.filled_amt = 0.0
        e.api.board = [tick("AAAUSDT", 40, 0.95, high=1.3)]
        m.tick(now=1000)
        return m, e

    def test_stop_out_on_the_leader_arms_at_the_24h_high(self):
        m, e = self.stopped_out()
        self.assertEqual(m.tracks, {})
        self.assertEqual(m.rearm, {"AAAUSDT": 1.3})
        self.assertEqual(len(e.api.orders), 1, "no re-entry below the high")

    def test_buys_again_above_the_high_once(self):
        m, e = self.stopped_out()
        e.api.board = [tick("AAAUSDT", 45, 1.25, high=1.3)]
        m.tick(now=1030)
        self.assertEqual(len(e.api.orders), 1)
        e.api.board = [tick("AAAUSDT", 50, 1.31, high=1.31)]
        m.tick(now=1060)
        self.assertIn("AAAUSDT", m.tracks)
        self.assertEqual(len(e.api.orders), 2)
        self.assertEqual(m.rearm, {})
        self.assertTrue(any("RE-ENTRY: AAAUSDT" in b for b in bodies(e)))

    def test_refused_reentry_is_not_retried_every_poll(self):
        m, e = self.stopped_out()
        e.state.halted = True
        e.api.board = [tick("AAAUSDT", 50, 1.31, high=1.31)]
        m.tick(now=1030)
        e.api.board = [tick("AAAUSDT", 55, 1.40, high=1.40)]
        m.tick(now=1060)
        self.assertEqual(sum("NOT opened" in b for b in bodies(e)), 1)
        self.assertEqual(len(e.api.orders), 1)

    def test_new_leader_clears_the_rearm(self):
        m, e = self.stopped_out()
        e.api.board = [tick("BBBUSDT", 60, 1.0), tick("AAAUSDT", 40, 1.4, high=1.4)]
        m._tradable = set()         # the cached symbol list predates BBBUSDT
        m.tick(now=1030)
        self.assertNotIn("AAAUSDT", m.rearm)
        self.assertIn("BBBUSDT", m.tracks)
        self.assertNotIn("AAAUSDT", m.tracks)

    def test_disabled_does_not_arm(self):
        m, e = self.stopped_out(entry=dict(reentry_on_new_high=False))
        self.assertEqual(m.rearm, {})

    def test_rearm_survives_a_restart(self):
        m, e = self.stopped_out()
        m2 = GainerMiner(e, m.cfg, path=m.path)
        self.assertEqual(m2.rearm, {"AAAUSDT": 1.3})


class SwapGuards(unittest.TestCase):
    """Two fading coins trading first place must not be bought and sold in turn."""

    def led_by_aaa(self, **kw):
        m, e = miner(**kw)
        m._baselined, m.leader = True, "AAAUSDT"
        return m, e

    def test_a_new_leader_must_hold_first_place_for_confirm_minutes(self):
        m, e = self.led_by_aaa(entry=dict(confirm_minutes=5))
        e.api.board = [tick("BBBUSDT", 50), tick("AAAUSDT", 40)]
        m.tick(now=1000)
        m.tick(now=1240)
        self.assertEqual(m.leader, "AAAUSDT", "confirmed after 4 minutes")
        m.tick(now=1300)
        self.assertEqual(m.leader, "BBBUSDT")
        self.assertIn("BBBUSDT", m.tracks)

    def test_a_fading_leader_is_watched_then_bought_when_it_rises(self):
        m, e = self.led_by_aaa(entry=dict(buy_only_if_rising=True))
        e.api.board = [tick("AAAUSDT", 60), tick("BBBUSDT", 50)]
        m.tick(now=1000)
        e.api.board = [tick("BBBUSDT", 45), tick("AAAUSDT", 40)]
        m.tick(now=1100)
        self.assertNotIn("BBBUSDT", m.tracks)
        self.assertEqual(m.waiting, "BBBUSDT")
        e.api.board = [tick("BBBUSDT", 49), tick("AAAUSDT", 38)]
        m.tick(now=1200)
        self.assertNotIn("BBBUSDT", m.tracks, "bought while still under its start")
        e.api.board = [tick("BBBUSDT", 60), tick("AAAUSDT", 37)]
        m.tick(now=1300)
        self.assertIn("BBBUSDT", m.tracks)
        self.assertEqual(sum("not bought yet" in b for b in bodies(e)), 1,
                         "the wait should be announced once, not every poll")

    def test_unknown_trend_is_not_read_as_rising(self):
        m, e = self.led_by_aaa(entry=dict(buy_only_if_rising=True))
        e.api.board = [tick("BBBUSDT", 50), tick("AAAUSDT", 40)]
        m.tick(now=1000)
        self.assertEqual(m.tracks, {})

    def test_a_coin_sold_recently_is_not_bought_back(self):
        m, e = self.led_by_aaa(entry=dict(rebuy_cooldown_minutes=60))
        m.closed_at["BBBUSDT"] = 1000
        e.api.board = [tick("BBBUSDT", 50), tick("AAAUSDT", 40)]
        m.tick(now=1100)
        self.assertEqual(m.tracks, {})
        self.assertTrue(any("rebuy_cooldown_minutes" in b for b in bodies(e)))
        m.tick(now=1000 + 3600 + 1)
        self.assertIn("BBBUSDT", m.tracks, "not bought once the cooldown ran out")

    def test_reentry_on_a_new_high_ignores_the_cooldown(self):
        m, e = miner(dry=False, entry=dict(rebuy_cooldown_minutes=60))
        m._baselined, m.leader = True, "AAAUSDT"
        m.open("AAAUSDT", 1.0, 40)
        e.book.pop("AAAUSDT")
        e.api.filled_amt = 0.0
        e.api.board = [tick("AAAUSDT", 40, 0.95, high=1.3)]
        m.tick(now=1000)
        e.api.board = [tick("AAAUSDT", 50, 1.31, high=1.31)]
        m.tick(now=1030)
        self.assertIn("AAAUSDT", m.tracks)

    def test_a_young_position_is_not_swapped_out(self):
        m, e = self.led_by_aaa(exit=dict(min_hold_minutes=15))
        m.tracks["CCCUSDT"] = Track("CCCUSDT", 1.0, 10, 0.95, 1.2, 900, paper=True)
        e.api.board = [tick("BBBUSDT", 50, 1.0), tick("CCCUSDT", 30, 0.99)]
        m.tick(now=1000)
        self.assertIn("CCCUSDT", m.tracks)
        self.assertTrue(any("CCCUSDT HELD" in b for b in bodies(e)))

    def test_an_old_losing_position_is_still_swapped_out(self):
        m, e = self.led_by_aaa(exit=dict(min_hold_minutes=15))
        m.tracks["CCCUSDT"] = Track("CCCUSDT", 1.0, 10, 0.95, 1.2, 0, paper=True)
        e.api.board = [tick("BBBUSDT", 50, 1.0), tick("CCCUSDT", 30, 0.99)]
        m.tick(now=1000)
        self.assertNotIn("CCCUSDT", m.tracks)
        self.assertEqual(m.closed_at["CCCUSDT"], 1000)

    def test_keep_never_sells_on_a_new_leader(self):
        m, e = self.led_by_aaa(exit=dict(on_new_leader="keep"))
        m.tracks["CCCUSDT"] = Track("CCCUSDT", 1.0, 10, 0.95, 1.2, 0, paper=True)
        e.api.board = [tick("BBBUSDT", 50, 1.0), tick("CCCUSDT", 30, 0.97)]
        m.tick(now=1000)
        self.assertIn("CCCUSDT", m.tracks)

    def test_an_unknown_on_new_leader_is_refused(self):
        with self.assertRaises(ValueError):
            GainerConfig(exit=dict(on_new_leader="sometimes"))

    def test_an_unknown_key_in_a_group_names_the_group(self):
        with self.assertRaises(TypeError) as ctx:
            GainerConfig(entry=dict(confirm_polls=2))
        self.assertIn("gainer.entry", str(ctx.exception))

    def test_closes_survive_a_restart(self):
        m, e = self.led_by_aaa()
        m.tracks["CCCUSDT"] = Track("CCCUSDT", 1.0, 10, 0.95, 1.2, 0, paper=True)
        m.close("CCCUSDT", 0.99, "test", now=1000)
        m2 = GainerMiner(e, m.cfg, path=m.path)
        self.assertEqual(m2.closed_at, {"CCCUSDT": 1000})

    def test_the_2026_09_15_swap_loop_trades_each_coin_once(self):
        """AAA and BBB swap #1 every 30s for 5 minutes, prices flat."""
        m, e = miner(entry=dict(rebuy_cooldown_minutes=60))
        e.api.board = [tick("AAAUSDT", 60), tick("BBBUSDT", 59)]
        m.tick(now=0)                                   # baseline AAA
        for i in range(1, 11):
            first, second = ("BBBUSDT", "AAAUSDT") if i % 2 else ("AAAUSDT", "BBBUSDT")
            e.api.board = [tick(first, 60 + i), tick(second, 59 + i)]
            m.tick(now=30 * i)
        opened = sum("OPENED" in b for b in bodies(e))
        self.assertEqual(opened, 2, "without the cooldown this opened 10 times")


LADDER = dict(exit=dict(ladder_enabled=True, target_usd=0, stop_pct=30,
                        ladder_first_pct=10, ladder_step_pct=10))


class Ladder(unittest.TestCase):
    """exit.ladder_*: no take-profit, a wide stop, walked up as the coin runs."""

    def held(self, dry=True, api=None, **kw):
        m, e = miner(dry=dry, api=api, **{**LADDER, **kw})
        m._baselined, m.leader = True, "AAAUSDT"
        return m, e

    def test_the_three_step_shapes(self):
        self.assertIsNone(ladder_stop(1.0, 1.099, 10, 10))
        self.assertAlmostEqual(ladder_stop(1.0, 1.10, 10, 10), 1.0)
        self.assertAlmostEqual(ladder_stop(1.0, 1.25, 10, 10), 1.10)
        # first 10, step 20: entry until +40, then one 20% step below
        self.assertAlmostEqual(ladder_stop(1.0, 1.39, 10, 20), 1.0)
        self.assertAlmostEqual(ladder_stop(1.0, 1.40, 10, 20), 1.20)
        self.assertAlmostEqual(ladder_stop(1.0, 1.65, 10, 20), 1.40)
        # first 20, step 20: nothing until +20
        self.assertIsNone(ladder_stop(1.0, 1.15, 20, 20))

    def test_opens_with_a_stop_and_no_take_profit(self):
        m, e = miner(dry=False, **LADDER)
        self.assertTrue(m.open("AAAUSDT", 1.0, 40))
        self.assertEqual([a["type"] for a in e.api.algo], ["STOP_MARKET"])
        pos = e.book["AAAUSDT"]
        self.assertTrue(pos.no_target)
        self.assertEqual(pos.take_profit, 0.0)
        self.assertAlmostEqual(m.tracks["AAAUSDT"].stop, 0.70)

    def test_without_the_ladder_a_zero_target_is_refused(self):
        m, e = miner(dry=False, exit=dict(target_usd=0))
        self.assertFalse(m.open("AAAUSDT", 1.0, 40))
        self.assertEqual(e.api.orders, [])

    def test_paper_stop_walks_up_and_closes_in_profit(self):
        m, e = self.held()
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.70, 0.0, 0, paper=True, peak=1.0)
        for i, px in enumerate((1.05, 1.12, 1.26)):
            e.api.board = [tick("AAAUSDT", 40, px)]
            m.tick(now=1000 + i)
        self.assertAlmostEqual(m.tracks["AAAUSDT"].stop, 1.10)
        e.api.board = [tick("AAAUSDT", 40, 1.09)]
        m.tick(now=2000)
        self.assertEqual(m.tracks, {})
        self.assertTrue(any("ladder stop hit" in b for b in bodies(e)))

    def test_no_take_profit_means_a_big_move_is_not_sold(self):
        m, e = self.held()
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.70, 0.0, 0, paper=True, peak=1.0)
        e.api.board = [tick("AAAUSDT", 90, 3.0)]
        m.tick(now=1000)
        self.assertIn("AAAUSDT", m.tracks)
        self.assertAlmostEqual(m.tracks["AAAUSDT"].stop, 2.9)

    def test_live_stop_is_moved_on_the_exchange(self):
        m, e = self.held(dry=False)
        self.assertTrue(m.open("AAAUSDT", 1.0, 40))
        moved = []
        e.replace_protective = lambda pos, leg, level, qty=None: moved.append((leg, level)) or True
        e.api.board = [tick("AAAUSDT", 45, 1.12)]
        m.tick(now=time_now() + 60)
        self.assertEqual(moved, [("stop", 1.0)])
        self.assertAlmostEqual(e.book["AAAUSDT"].stop, 1.0)

    def test_a_refused_move_keeps_the_old_stop_and_retries(self):
        m, e = self.held(dry=False)
        self.assertTrue(m.open("AAAUSDT", 1.0, 40))
        e.replace_protective = lambda pos, leg, level, qty=None: False
        e.api.board = [tick("AAAUSDT", 45, 1.12)]
        m.tick(now=time_now() + 60)
        self.assertAlmostEqual(m.tracks["AAAUSDT"].stop, 0.70)

    def test_unarmed_time_limit_closes_a_drifting_coin(self):
        m, e = self.held(exit=dict(**LADDER["exit"], unarmed_max_hours=72))
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 0.70, 0.0, 0, paper=True, peak=1.0)
        e.api.board = [tick("AAAUSDT", 40, 0.95)]
        m.tick(now=71 * 3600)
        self.assertIn("AAAUSDT", m.tracks)
        m.tick(now=72 * 3600)
        self.assertEqual(m.tracks, {})
        self.assertTrue(any("time limit" in b for b in bodies(e)))

    def test_armed_positions_ignore_the_time_limit(self):
        m, e = self.held(exit=dict(**LADDER["exit"], unarmed_max_hours=72))
        m.tracks["AAAUSDT"] = Track("AAAUSDT", 1.0, 10, 1.0, 0.0, 0, paper=True, peak=1.15)
        e.api.board = [tick("AAAUSDT", 40, 1.05)]
        m.tick(now=500 * 3600)
        self.assertIn("AAAUSDT", m.tracks)

    def test_old_state_files_without_a_peak_still_load(self):
        t = Track("AAAUSDT", 1.0, 10, 0.7, 0.0, 0)
        self.assertEqual(t.peak, 0.0)


class Sweep(unittest.TestCase):
    """gainer.sweep: 25% of each winning trade to the Funding wallet."""

    class LiveAPI(BoardAPI):
        testnet = False

        def __init__(self, fail=None, **kw):
            super().__init__(**kw)
            self.transfers = []
            self.fail = fail

        def universal_transfer(self, asset, amount, type_="UMFUTURE_FUNDING"):
            if self.fail:
                raise BinanceError(-1002, self.fail, "/sapi/v1/asset/transfer")
            self.transfers.append((asset, amount, type_))
            return {"tranId": 1}

    def live(self, **kw):
        api = self.LiveAPI(**kw)
        return miner(dry=False, api=api, sweep=dict(enabled=True, pct=25, min_transfer_usdt=1.0))

    def test_a_winning_trade_moves_a_quarter_of_its_profit(self):
        m, e = self.live()
        m.sweep_profit("AAAUSDT", 8.0)
        self.assertEqual(e.api.transfers, [("USDT", "2.00", "UMFUTURE_FUNDING")])
        self.assertAlmostEqual(m.swept_total, 2.0)

    def test_losses_move_nothing(self):
        m, e = self.live()
        m.sweep_profit("AAAUSDT", -5.0)
        m.sweep_profit("AAAUSDT", 0.0)
        self.assertEqual(e.api.transfers, [])

    def test_small_amounts_wait_until_they_reach_the_minimum(self):
        m, e = self.live()
        m.sweep_profit("AAAUSDT", 2.0)            # 0.50 pending
        self.assertEqual(e.api.transfers, [])
        m.sweep_profit("BBBUSDT", 2.4)            # 0.50 + 0.60 = 1.10
        self.assertEqual(e.api.transfers, [("USDT", "1.10", "UMFUTURE_FUNDING")])
        self.assertAlmostEqual(m.sweep_pending, 0.0)

    def test_a_failed_transfer_stays_pending_and_says_so_once(self):
        m, e = self.live(fail="This API key has no permission")
        m.sweep_profit("AAAUSDT", 8.0)
        m.sweep_profit("BBBUSDT", 4.0)
        self.assertAlmostEqual(m.sweep_pending, 3.0)
        self.assertEqual(m.swept_total, 0.0)
        self.assertEqual(sum("SWEEP FAILED" in b for b in bodies(e)), 1)

    def test_testnet_simulates_and_sends_nothing(self):
        api = self.LiveAPI()
        api.testnet = True
        m, e = miner(dry=False, api=api, sweep=dict(enabled=True))
        m.sweep_profit("AAAUSDT", 8.0)
        self.assertEqual(api.transfers, [])
        self.assertAlmostEqual(m.swept_total, 2.0)
        self.assertTrue(any("nothing sent" in b for b in bodies(e)))

    def test_off_by_default(self):
        m, e = miner(dry=False, api=self.LiveAPI())
        m.sweep_profit("AAAUSDT", 8.0)
        self.assertEqual(e.api.transfers, [])

    def test_totals_survive_a_restart(self):
        m, e = self.live()
        m.sweep_profit("AAAUSDT", 8.0)
        m.sweep_profit("BBBUSDT", 1.0)
        again = GainerMiner(e, m.cfg, path=m.path)
        self.assertAlmostEqual(again.swept_total, 2.0)
        self.assertAlmostEqual(again.sweep_pending, 0.25)

    def test_the_engine_sweeps_only_gainer_closes(self):
        m, e = self.live()
        from bot.positions import ActivePosition
        pos = ActivePosition(symbol="AAAUSDT", side="BUY", entry=1.0, stop=0.7,
                             take_profit=0.0, qty=10, entry_order_id="g-1")
        e.sweep_gainer_profit(pos, 8.0)
        self.assertEqual(e.api.transfers, [])
        pos.strategy = "gainer"
        e.sweep_gainer_profit(pos, 8.0)
        self.assertEqual(len(e.api.transfers), 1)


def time_now():
    import time
    return time.time()


class Config(unittest.TestCase):
    def test_config_yaml_loads_the_gainer_section(self):
        from bot.config import Config as C
        cfg = C.load(ROOT / "config.yaml")
        self.assertIsInstance(cfg.gainer, GainerConfig)
        self.assertGreater(cfg.gainer.exit.target_usd, 0.0)
        self.assertGreater(cfg.gainer.entry.notional_usdt, 5.0,
                           "must clear the $5 minimum")
        self.assertIn(cfg.gainer.exit.on_new_leader, ("close_if_losing", "keep"))

    def test_event_exists(self):
        self.assertEqual(Event.GAINER.value, "gainer mining")


if __name__ == "__main__":
    unittest.main()
