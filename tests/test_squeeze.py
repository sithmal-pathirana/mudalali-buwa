"""Squeeze mode (bot/squeeze.py): buy the coins whose shorts are most crowded."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.squeeze import (SqueezeConfig, SqueezeTrader, last_settlement_ms,  # noqa: E402
                         per_8h, pick)
from test_gainer import BoardAPI, Rules, bodies, tick                       # noqa: E402
from test_r2_regressions import engine                                      # noqa: E402

H = 3600
SETTLED = 1790208000            # 2026-09-24 16:00:00 UTC, a settlement


class FundingAPI(BoardAPI):
    def __init__(self, rates=None, intervals=None, **kw):
        super().__init__(**kw)
        self.rates = rates or {}
        self.intervals = intervals or {}
        self.rate_calls = 0

    def funding_rates_since(self, start_ms, limit=1000):
        self.rate_calls += 1
        return [{"symbol": s, "fundingRate": str(r), "fundingTime": SETTLED * 1000}
                for s, r in self.rates.items()]

    def funding_info(self):
        return [{"symbol": s, "fundingIntervalHours": h} for s, h in self.intervals.items()]


def trader(dry=True, api=None, **kw):
    e = engine(api=api or FundingAPI())
    e._book = {}
    e._seq = 0
    e._prepared = {"AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"}
    e.equity = 100.0
    e.state.day_start_equity = e.equity
    e.rules_for = lambda s: Rules()
    closed = []

    def close_position(reason, symbol=None):
        closed.append((symbol, reason))
        e.book.pop(symbol, None)
        return True
    e.close_position = close_position
    e.closed = closed
    base = dict(enabled=True, dry_run=dry, board=dict(poll_seconds=0),
                alerts=dict(status_minutes=0))
    for k, v in kw.items():
        if isinstance(v, dict):
            base.setdefault(k, {}).update(v)
        else:
            base[k] = v
    path = Path(tempfile.mkdtemp()) / "squeeze.json"
    t = SqueezeTrader(e, SqueezeConfig(**base), path=path)
    e.squeeze = t
    return t, e


def board(*syms, qv=50e6, price=1.0):
    return [tick(s, 0, price, qv=qv) for s in syms]


class Arithmetic(unittest.TestCase):
    def test_rates_are_scaled_to_eight_hours(self):
        self.assertAlmostEqual(per_8h(-0.0005, 4), -0.001)
        self.assertAlmostEqual(per_8h(-0.001, 8), -0.001)
        self.assertAlmostEqual(per_8h(-0.0001, 1), -0.0008)

    def test_last_settlement(self):
        self.assertEqual(last_settlement_ms(SETTLED + 5 * H), SETTLED * 1000)
        self.assertEqual(last_settlement_ms(SETTLED - 1), (SETTLED - 8 * H) * 1000)

    def test_pick_orders_filters_and_caps(self):
        rates = {"AAAUSDT": -0.30, "BBBUSDT": -0.12, "CCCUSDT": -0.05,
                 "DDDUSDT": -0.50, "EEEUSDT": -0.20}
        vols = {"AAAUSDT": 50e6, "BBBUSDT": 50e6, "CCCUSDT": 50e6,
                "DDDUSDT": 5e6, "EEEUSDT": 50e6}
        tradable = set(rates)
        got = pick(rates, vols, {"EEEUSDT"}, tradable, -0.10, 20e6, 3)
        # DDD too thin, EEE held, CCC not negative enough
        self.assertEqual([s for s, _ in got], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(len(pick(rates, vols, set(), tradable, -0.10, 0, 1)), 1)


class Rounds(unittest.TestCase):
    def api(self, **rates):
        a = FundingAPI(rates=rates)
        a.board = board(*rates)
        return a

    def test_buys_the_most_crowded_after_settlement(self):
        t, e = trader(api=self.api(AAAUSDT=-0.003, BBBUSDT=-0.0012, CCCUSDT=-0.0002),
                      entry=dict(max_new_per_round=3))
        t.tick(now=SETTLED + 180)
        self.assertEqual(sorted(t.tracks), ["AAAUSDT", "BBBUSDT"])
        self.assertTrue(any("SQUEEZE ROUND" in b for b in bodies(e)))

    def test_waits_for_the_settlement_to_publish(self):
        t, e = trader(api=self.api(AAAUSDT=-0.003))
        t.tick(now=SETTLED + 30)
        self.assertEqual(t.tracks, {})
        self.assertEqual(e.api.rate_calls, 0)

    def test_acts_once_per_settlement(self):
        t, e = trader(api=self.api(AAAUSDT=-0.003, BBBUSDT=-0.003),
                      entry=dict(max_new_per_round=1))
        t.tick(now=SETTLED + 180)
        t.tick(now=SETTLED + 600)
        self.assertEqual(len(t.tracks), 1)
        self.assertEqual(e.api.rate_calls, 1)

    def test_a_late_restart_does_not_buy_the_round_again(self):
        t, e = trader(api=self.api(AAAUSDT=-0.003))
        t.tick(now=SETTLED + 2 * H)
        self.assertEqual(t.tracks, {})

    def test_four_hour_contracts_are_compared_per_eight_hours(self):
        a = self.api(AAAUSDT=-0.0006)            # -0.06% per 4h = -0.12% per 8h
        a.intervals = {"AAAUSDT": 4}
        t, e = trader(api=a)
        t.tick(now=SETTLED + 180)
        self.assertIn("AAAUSDT", t.tracks)

    def test_respects_max_positions(self):
        t, e = trader(api=self.api(AAAUSDT=-0.003, BBBUSDT=-0.003, CCCUSDT=-0.003),
                      entry=dict(max_positions=2, max_new_per_round=3))
        t.tick(now=SETTLED + 180)
        self.assertEqual(len(t.tracks), 2)


class Exits(unittest.TestCase):
    def test_live_entry_gets_a_stop_and_no_take_profit(self):
        a = FundingAPI(rates={"AAAUSDT": -0.003})
        a.board = board("AAAUSDT")
        t, e = trader(dry=False, api=a)
        self.assertTrue(t.open("AAAUSDT", 1.0, -0.3))
        self.assertEqual([x["type"] for x in e.api.algo], ["STOP_MARKET"])
        pos = e.book["AAAUSDT"]
        self.assertEqual(pos.strategy, "squeeze")
        self.assertTrue(pos.no_target)
        self.assertAlmostEqual(t.tracks["AAAUSDT"].stop, 0.80)

    def test_closed_after_the_hold_time(self):
        a = FundingAPI()
        a.board = board("AAAUSDT", price=1.05)
        t, e = trader(api=a)
        t.open("AAAUSDT", 1.0, -0.3)
        t.tracks["AAAUSDT"].opened_at = 1000.0
        t.tick(now=1000.0 + 71 * H)
        self.assertIn("AAAUSDT", t.tracks)
        t._last_poll = 0
        t.tick(now=1000.0 + 72 * H)
        self.assertEqual(t.tracks, {})
        self.assertTrue(any("time limit" in b for b in bodies(e)))

    def test_paper_stop_closes(self):
        a = FundingAPI()
        a.board = board("AAAUSDT", price=0.79)
        t, e = trader(api=a)
        t.open("AAAUSDT", 1.0, -0.3)
        t.tick(now=time.time())
        self.assertEqual(t.tracks, {})

    def test_supervisor_and_sweep_route_to_squeeze(self):
        a = FundingAPI()
        t, e = trader(dry=False, api=a, sweep=dict(enabled=True, pct=25))
        e.api.testnet = True          # transfers are simulated
        from bot.positions import ActivePosition
        pos = ActivePosition(symbol="AAAUSDT", side="BUY", entry=1.0, stop=0.8,
                             take_profit=0.0, qty=10, entry_order_id="q-1",
                             strategy="squeeze")
        e.sweep_gainer_profit(pos, 8.0)
        self.assertAlmostEqual(t.swept_total, 2.0)


class Config(unittest.TestCase):
    def test_config_yaml_loads_the_squeeze_section(self):
        from bot.config import Config as C
        cfg = C.load(ROOT / "config.yaml")
        self.assertIsInstance(cfg.squeeze, SqueezeConfig)
        self.assertLess(cfg.squeeze.entry.funding_at_most_pct, 0)

    def test_unknown_key_names_the_group(self):
        with self.assertRaises(TypeError) as ctx:
            SqueezeConfig(entry=dict(nope=1))
        self.assertIn("squeeze.entry", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
