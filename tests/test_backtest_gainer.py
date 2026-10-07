"""tools/backtest_gainer.py: settings, grids, the trade replay and the account."""

import unittest

from tools import backtest_gainer as bt

B, H, D = bt.B, bt.H, bt.D

BASE = {
    "gainer": {"exit": {"stop_pct": 30, "target_pct": 95, "ladder_enabled": True,
                        "ladder_first_pct": 10, "ladder_step_pct": 10,
                        "unarmed_max_hours": 72, "on_new_leader": "keep"},
               "entry": {"notional_pct_of_equity": 10, "min_notional_usdt": 6,
                         "max_positions": 5, "rebuy_cooldown_minutes": 180,
                         "confirm_minutes": 0, "buy_only_if_rising": False},
               "board": {"leader_check_minutes": 5},
               "sweep": {"enabled": True, "pct": 25, "min_transfer_usdt": 1,
                         "principal_enabled": True, "principal_trigger_x": 2.5}},
    "risk": {"max_leverage": 2, "min_equity_usdt": 25},
}


class SettingsTest(unittest.TestCase):
    def test_override_is_built_by_the_bot_config(self):
        s = bt.Settings(BASE, {"gainer.filters.max_rsi_1h": 85})
        self.assertEqual(s.g.filters.max_rsi_1h, 85)
        self.assertEqual(s.g.exit.target_pct, 95)

    def test_unknown_key_is_refused(self):
        with self.assertRaises(SystemExit):
            bt.Settings(BASE, {"gainer.filters.max_rsi": 85})
        with self.assertRaises(SystemExit):
            bt.Settings(BASE, {"gainer.exit.ladder_mode": "zigzag"})

    def test_grid_is_every_combination(self):
        specs = bt.grid_specs({"vary": {"gainer.filters.max_rsi_1h": [0, 80, 85],
                                        "gainer.exit.ladder_step_pct": [5, 10]}}, {})
        self.assertEqual(len(specs), 6)
        runs = bt.build_runs(BASE, specs)
        # live first; the combination equal to live (0, 10) is dropped
        self.assertEqual(runs[0]["id"], "live")
        self.assertEqual(len(runs), 6)


class OutcomeTest(unittest.TestCase):
    ek_default = bt.Settings(BASE, {}).exit_key(0.0)

    def outcome(self, bars, ek=None, t=B):
        """bars: prices (flat bars) or (open, high, low, close) tuples."""
        rows = [b if isinstance(b, tuple) else (b, b, b, b) for b in bars]
        d = ([i * B for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
             [r[2] for r in rows], [r[3] for r in rows], [1e6] * len(rows))
        o = bt.Oracle.__new__(bt.Oracle)
        return o._outcome("X", d, t, ek or self.ek_default, {"ok": True}, {})

    def test_stop_fills_at_the_stop(self):
        r, _, why = self.outcome([1, 1, (0.9, 0.95, 0.6, 0.8)])
        self.assertEqual(why, "stop")
        self.assertAlmostEqual(r, -0.30)

    def test_stop_gap_fills_at_the_open(self):
        r, _, why = self.outcome([1, 1, 0.6])
        self.assertEqual(why, "stop")
        self.assertAlmostEqual(r, -0.40)

    def test_take_profit(self):
        r, _, why = self.outcome([1, 1, (1.5, 2.1, 1.5, 2.0)])
        self.assertEqual(why, "take-profit")
        self.assertAlmostEqual(r, 0.95)

    def test_stop_before_take_profit_in_one_bar(self):
        _, _, why = self.outcome([1, 1, (1.0, 2.1, 0.6, 1.0)])
        self.assertEqual(why, "stop")

    def test_ladder_locks_entry_then_steps(self):
        r, _, why = self.outcome([1, 1, (1.0, 1.1, 1.0, 1.05), (1.05, 1.05, 0.9, 0.9)])
        self.assertEqual(why, "ladder stop at entry")
        self.assertAlmostEqual(r, 0.0)
        r, _, why = self.outcome([1, 1, (1.0, 1.35, 1.0, 1.3), (1.3, 1.3, 1.0, 1.0)])
        self.assertEqual(why, "ladder stop in profit")
        self.assertAlmostEqual(r, 0.2)

    def test_trail_keeps_its_distance(self):
        ek = bt.Settings(BASE, {"gainer.exit.ladder_mode": "trail"}).exit_key(0.0)
        r, _, why = self.outcome([1, 1, (1.0, 1.35, 1.0, 1.3), (1.3, 1.3, 0.5, 0.5)], ek)
        self.assertAlmostEqual(r, 0.0)           # -30% stop + 3 steps of 10% = entry

    def test_unarmed_time_limit(self):
        prices = [1] * (72 * 12 + 5)
        r, _, why = self.outcome(prices[:2] + [(1, 1.02, 1, 1.02)] * (72 * 12 + 3))
        self.assertEqual(why, "time limit")
        self.assertAlmostEqual(r, 0.02)

    def test_partial_take(self):
        ek = bt.Settings(BASE, {"gainer.exit.partial_take_pct": 50}).exit_key(0.0)
        r, _, why = self.outcome([1, 1, (1.0, 1.6, 1.0, 1.5), (1.5, 1.5, 1.0, 1.0)], ek)
        # half sold at +50%; a +60% peak puts the ladder at +50%, where the rest goes
        self.assertAlmostEqual(r, 0.5 * 0.5 + 0.5 * 0.5)


class ShortOutcomeTest(unittest.TestCase):
    ek = bt.Settings(BASE, {"test.losers_target_pct": 50}).losers_exit_key(0.0)

    def outcome(self, bars):
        rows = [b if isinstance(b, tuple) else (b, b, b, b) for b in bars]
        d = ([i * B for i in range(len(rows))], [r[0] for r in rows], [r[1] for r in rows],
             [r[2] for r in rows], [r[3] for r in rows], [1e6] * len(rows))
        return bt.Oracle._outcome_short(d, B, self.ek, {"ok": True})

    def test_stop_is_above(self):
        r, _, why = self.outcome([1, 1, (1.0, 1.35, 1.0, 1.3)])
        self.assertEqual(why, "stop")
        self.assertAlmostEqual(r, -0.30)

    def test_take_profit_is_below(self):
        r, _, why = self.outcome([1, 1, (0.6, 0.6, 0.45, 0.5)])
        self.assertEqual(why, "take-profit")
        self.assertAlmostEqual(r, 0.5)

    def test_ladder_follows_the_fall(self):
        # falls 35%: the stop moves to 20% below entry, then the bounce takes it out there
        r, _, why = self.outcome([1, 1, (1.0, 1.0, 0.65, 0.7), (0.7, 0.9, 0.7, 0.9)])
        self.assertEqual(why, "ladder stop in profit")
        self.assertAlmostEqual(r, 0.2)


class FakeOracle:
    def __init__(self, outs):
        self.outs = outs

    def feature(self, sym, t):
        return {"ok": True, "price": 1.0, "rsi": 50, "atr": 3, "age": None,
                "btc24": 0, "btc1": 0}

    def outcome(self, ek, sym, t):
        return self.outs[sym]

    def first_rising(self, *a):
        return None

    def mark(self, sym, t_entry, t):
        return 0.9                               # every held position is down 10%


class AccountTest(unittest.TestCase):
    def run_account(self, outs, events, overrides=None):
        s = bt.Settings(BASE, overrides or {})
        a = bt.Account(s, FakeOracle(outs), events, 0, 40 * D, 0.0, (50.0, 50.0), {})
        return a.run()

    def test_win_is_swept_and_profit_counts_funding(self):
        res = self.run_account({"A": (1.0, 2 * H, "take-profit")}, [(H, "A", 3 * H)])
        # $6 minimum trade doubles: +$6, 25% = $1.50 to Funding
        self.assertEqual(res["trades"], 1)
        self.assertAlmostEqual(res["of_which_profit_sweeps"], 1.5)
        self.assertAlmostEqual(res["real_profit"], 6.0)

    def test_max_positions(self):
        outs = {s: (0.0, 10 * D, "time limit") for s in "ABC"}
        events = [(H * (i + 1), s, H * (i + 2)) for i, s in enumerate("ABC")]
        res = self.run_account(outs, events, {"gainer.entry.max_positions": 2})
        self.assertEqual(res["trades"], 2)
        self.assertEqual(res["skipped"], {"max_positions full": 1})

    def test_filter_skips(self):
        res = self.run_account({"A": (1.0, 2 * H, "x")}, [(H, "A", 3 * H)],
                               {"gainer.filters.max_rsi_1h": 40})
        self.assertEqual(res["trades"], 0)
        self.assertEqual(res["skipped"], {"filter: max_rsi_1h": 1})

    def test_cooldown_retry_while_still_leader(self):
        outs = {"A": (-0.1, 2 * H, "stop")}
        # A closes at 2h, leads again at 3h (inside the 3h cooldown) and stays #1 to 8h
        events = [(H, "A", 2 * H), (3 * H, "A", 8 * H)]
        res = self.run_account(outs, events)
        self.assertEqual(res["trades"], 2)       # bought again once the cooldown ended


class PumpChecksTest(unittest.TestCase):
    def run_account(self, feats, overrides):
        class F(FakeOracle):
            def feature(self, sym, t):
                return {**FakeOracle.feature(self, sym, t), **feats}
        s = bt.Settings(BASE, overrides)
        return bt.Account(s, F({"A": (0.1, 2 * H, "x")}), [(H, "A", 3 * H)],
                          0, 40 * D, 0.0, (50.0, 50.0), {}).run()

    def test_skips_above_a_max(self):
        res = self.run_account({"vol_surge": 20}, {"test.max_volume_surge_x": 10})
        self.assertEqual(res["skipped"], {"test: max_volume_surge_x": 1})

    def test_skips_below_a_min(self):
        res = self.run_account({"vol_trend": 0.2}, {"test.min_volume_trend": 0.5})
        self.assertEqual(res["skipped"], {"test: min_volume_trend": 1})

    def test_unknown_measurement_passes(self):
        res = self.run_account({}, {"test.max_funding_pct": 0.05})
        self.assertEqual(res["trades"], 1)

    def test_off_by_default(self):
        res = self.run_account({"vol_surge": 99, "below_high": 50}, {})
        self.assertEqual(res["trades"], 1)

    def test_futures_only_coin_skipped_when_spot_required(self):
        res = self.run_account({"has_spot": 0.0}, {"test.require_spot": 1})
        self.assertEqual(res["skipped"], {"test: require_spot": 1})
        res = self.run_account({"has_spot": 1.0}, {"test.require_spot": 1})
        self.assertEqual(res["trades"], 1)

    def test_spot_lead(self):
        res = self.run_account({"spot_lead": -0.1}, {"test.min_spot_lead": 0.0001})
        self.assertEqual(res["skipped"], {"test: min_spot_lead": 1})
        res = self.run_account({"spot_lead": -0.1}, {"test.min_spot_lead": -0.2})
        self.assertEqual(res["trades"], 1)


class WhenFullTest(unittest.TestCase):
    def run_account(self, overrides):
        outs = {s: (0.0, 10 * D, "time limit") for s in "AB"}
        events = [(H, "A", 2 * H), (5 * H, "B", 6 * H)]
        s = bt.Settings(BASE, {"gainer.entry.max_positions": 1, **overrides})
        return bt.Account(s, FakeOracle(outs), events, 0, 40 * D, 0.0, (50.0, 50.0), {}).run()

    def test_refuse_is_the_bot(self):
        res = self.run_account({})
        self.assertEqual(res["trades"], 1)
        self.assertEqual(res["skipped"], {"max_positions full": 1})

    def test_replace_sells_the_held_one_at_its_mark(self):
        res = self.run_account({"test.when_full": "replace_worst"})
        self.assertEqual(res["trades"], 2)
        self.assertEqual(res["exit_reasons"]["replaced by a new leader"], 1)
        self.assertAlmostEqual(res["trading_pnl"], -0.6)      # -10% of $6

    def test_min_hold_protects_young_positions(self):
        res = self.run_account({"test.when_full": "replace_oldest",
                                "test.replace_min_hold_hours": 12})
        self.assertEqual(res["trades"], 1)

    def test_replace_losing_keeps_a_winner(self):
        class Up(FakeOracle):
            def mark(self, *a):
                return 1.2
        outs = {s: (0.0, 10 * D, "time limit") for s in "AB"}
        s = bt.Settings(BASE, {"gainer.entry.max_positions": 1,
                               "test.when_full": "replace_losing"})
        res = bt.Account(s, Up(outs), [(H, "A", 2 * H), (5 * H, "B", 6 * H)],
                         0, 40 * D, 0.0, (50.0, 50.0), {}).run()
        self.assertEqual(res["trades"], 1)

    def test_unknown_test_setting_is_refused(self):
        with self.assertRaises(SystemExit):
            bt.Settings(BASE, {"test.when_ful": "refuse"})


class Round2Test(unittest.TestCase):
    def test_combines_only_winners(self):
        def run(group, ov, y1, y2):
            return dict(group=group, overrides=ov, results={
                "year1": {"real_profit": y1}, "year2": {"real_profit": y2},
                "2y": {"real_profit": y1 + y2}})
        runs = [run("", {}, 0, 0),
                run("a", {"a": 1}, 5, 5), run("a", {"a": 2}, 6, 6),
                run("b", {"b": 1}, 1, 1), run("c", {"c": 1}, 9, -1)]
        specs, info = bt.round2_specs(runs, {"keep_per_setting": 2, "max_runs": 100},
                                      lambda m: None)
        self.assertEqual(info["groups"], ["a", "b"])
        self.assertEqual(sorted(tuple(sorted(s["overrides"].items())) for s in specs),
                         [(("a", 1), ("b", 1)), (("a", 2), ("b", 1))])


if __name__ == "__main__":
    unittest.main()
