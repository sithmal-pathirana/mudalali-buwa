"""
The trailing stop: an ATR-derived TRAILING_STOP_MARKET in place of the fixed
STOP_MARKET, gated behind aggressive.trailing_atr_mult (0 in the safe
profile).

Three layers, tested separately:
  * ActivePosition.update_trailing_stop -- the pure, exchange-side-agnostic
    ratchet math, mirrored locally for display only.
  * aggressive.apply -- the profile value actually reaches cfg.risk.
  * Engine.place -- the right order TYPE and callbackRate go to the API, and
    the fixed stop is untouched when trailing is off or there is no
    volatility reading to size it from.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.aggressive import PROFILES, apply             # noqa: E402
from bot.config import Config                           # noqa: E402
from bot.positions import ActivePosition                # noqa: E402
from bot.strategies.base import Signal                   # noqa: E402
from test_r2_regressions import StubAPI, engine          # noqa: E402


def pos(side="BUY", entry=100.0, stop=98.0, tp=106.0, trailing_pct=2.0):
    return ActivePosition(symbol="DOGEUSDT", side=side, entry=entry, stop=stop,
                          take_profit=tp, qty=1.0, entry_order_id="e-1",
                          trailing_pct=trailing_pct)


class TestUpdateTrailingStop(unittest.TestCase):
    def test_off_by_default_never_moves_the_stop(self):
        p = pos(trailing_pct=0.0)
        p.update_trailing_stop(110.0)
        self.assertEqual(p.stop, 98.0)

    def test_long_stop_ratchets_up_as_price_makes_new_highs(self):
        p = pos(side="BUY", entry=100.0, stop=98.0, trailing_pct=2.0)
        p.update_trailing_stop(110.0)
        self.assertAlmostEqual(p.stop, 110.0 * 0.98)

    def test_long_stop_never_moves_down_on_a_pullback(self):
        p = pos(side="BUY", entry=100.0, stop=98.0, trailing_pct=2.0)
        p.update_trailing_stop(110.0)
        tightened = p.stop
        p.update_trailing_stop(105.0)      # pulled back, but no new high
        self.assertEqual(p.stop, tightened, "stop loosened on a pullback")

    def test_short_stop_ratchets_down_as_price_makes_new_lows(self):
        p = pos(side="SELL", entry=100.0, stop=102.0, trailing_pct=2.0)
        p.update_trailing_stop(90.0)
        self.assertAlmostEqual(p.stop, 90.0 * 1.02)

    def test_short_stop_never_moves_up_on_a_bounce(self):
        p = pos(side="SELL", entry=100.0, stop=102.0, trailing_pct=2.0)
        p.update_trailing_stop(90.0)
        tightened = p.stop
        p.update_trailing_stop(95.0)       # bounced, but no new low
        self.assertEqual(p.stop, tightened, "stop loosened on a bounce")

    def test_adverse_move_before_any_favourable_one_keeps_the_original_stop(self):
        """Mirrors Binance: the trail starts from the entry-time reference, so
        a position that goes straight against the entry is no worse off than
        it would have been with the fixed stop it replaced."""
        p = pos(side="BUY", entry=100.0, stop=98.0, trailing_pct=2.0)
        p.update_trailing_stop(99.0)       # never made a new high
        self.assertEqual(p.stop, 98.0)


class TestAggressiveWiring(unittest.TestCase):
    def test_trailing_atr_mult_reaches_risk_config(self):
        for name, profile in PROFILES.items():
            cfg = Config()
            apply(cfg, profile)
            self.assertEqual(cfg.risk.trailing_atr_mult, profile.trailing_atr_mult,
                            f"{name} profile's trailing_atr_mult was not applied")

    def test_safe_profile_never_calls_apply_so_trailing_stays_off(self):
        self.assertEqual(Config().risk.trailing_atr_mult, 0.0)


class TestPlaceChoosesOrderType(unittest.TestCase):
    """dry-run: cheapest way to check the sizing/clamping math without a
    live order round-trip."""

    def _dry(self, trailing_atr_mult=0.0):
        e = engine(dry_run=True, api=StubAPI())
        e.cfg.risk.trailing_atr_mult = trailing_atr_mult
        e.rules = type("R", (), {
            "size_for_notional": lambda s, n, p: ("100", "0.090000"),
            "round_price": lambda s, p: f"{p:.6f}",
            "round_qty": lambda s, q: f"{q:.0f}"})()
        e.risk = type("RM", (), {"record_attempt": lambda s: None,
                                 "record_fill": lambda s, p=0.0: None})()
        e._seq = 0
        e._entry_placed_at = 0.0
        e._dry_pending = None
        return e

    def test_no_trailing_configured_keeps_the_fixed_stop(self):
        e = self._dry(trailing_atr_mult=0.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=2.0)
        got = e._dry_pending[e.cfg.symbol]
        self.assertEqual(got.trailing_pct, 0.0)

    def test_no_volatility_reading_keeps_the_fixed_stop(self):
        """atr_pct == 0 means the caller has nothing to size a trail from
        (e.g. too few bars) -- must not divide-by-zero into a bogus trail."""
        e = self._dry(trailing_atr_mult=2.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=0.0)
        got = e._dry_pending[e.cfg.symbol]
        self.assertEqual(got.trailing_pct, 0.0)

    def test_trailing_configured_computes_a_callback_rate(self):
        # ATR-derived trail (1.0 * 3.0 = 3.0%) is wider than the 2% stop, so
        # it is what gets sent.
        e = self._dry(trailing_atr_mult=1.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=3.0)
        got = e._dry_pending[e.cfg.symbol]
        self.assertEqual(got.trailing_pct, 3.0)

    def test_callback_never_sits_inside_the_strategy_stop(self):
        """
        The trailing order REPLACES the fixed stop, and Binance trails from the
        price prevailing when it lands -- the entry, on a position that never
        goes into profit. So a callbackRate under the stop distance is not a
        tighter trail, it is a narrower stop. Both JUPUSDT trades on
        2026-09-08 were cut at 0.68% and 0.93% adverse against a planned 1.72%
        stop, inside the noise the ATR stop was sized to sit outside of.
        """
        # entry 0.09, stop 0.0882 -> the strategy asked for 2.0%
        e = self._dry(trailing_atr_mult=1.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=0.5)                          # 1.0 * 0.5 = 0.5%, inside the stop
        got = e._dry_pending[e.cfg.symbol]
        self.assertGreaterEqual(got.trailing_pct, 2.0)

    def test_rounding_to_one_decimal_only_ever_widens(self):
        """Binance takes one decimal; rounding must not shave the callback
        back inside the stop distance."""
        # entry 0.09, stop 0.088429 -> 1.745%, which must not round to 1.7
        e = self._dry(trailing_atr_mult=1.0)
        e.place(Signal("BUY", entry=0.09, stop=0.088429, take_profit=0.0954), 20.0, "n",
               atr_pct=0.5)
        got = e._dry_pending[e.cfg.symbol]
        self.assertEqual(got.trailing_pct, 1.8)

    def test_a_stop_wider_than_the_cap_keeps_the_fixed_stop(self):
        """Binance caps callbackRate at 5%. A stop wider than that cannot be
        expressed as a trail, so the fixed STOP_MARKET must survive rather
        than the exit being silently tightened to 5%."""
        e = self._dry(trailing_atr_mult=10.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=8.0)                          # 10 * 8 = 80%, way over
        got = e._dry_pending[e.cfg.symbol]
        self.assertEqual(got.trailing_pct, 0.0)

    def test_callback_respects_the_binance_floor(self):
        e = self._dry(trailing_atr_mult=0.01)
        # A stop this tight (0.0899 on 0.09 = 0.011%) leaves the 0.1% Binance
        # minimum as the binding term.
        e.place(Signal("BUY", entry=0.09, stop=0.0899, take_profit=0.0954), 20.0, "n",
                atr_pct=0.5)
        got = e._dry_pending[e.cfg.symbol]
        self.assertGreaterEqual(got.trailing_pct, 0.1)


class TestPlaceSendsTheRightAlgoOrder(unittest.TestCase):
    """Live path: the actual shape of the call Binance receives."""

    def _live(self, trailing_atr_mult=0.0):
        api = StubAPI()
        e = engine(api=api, dry_run=False)
        e.cfg.risk.trailing_atr_mult = trailing_atr_mult
        e.rules = type("R", (), {
            "size_for_notional": lambda s, n, p: ("100", "0.090000"),
            "round_price": lambda s, p: f"{p:.6f}",
            "round_qty": lambda s, q: f"{q:.0f}"})()
        e.risk = type("RM", (), {"record_attempt": lambda s: None,
                                 "record_fill": lambda s, p=0.0: None})()
        e.signals = None
        e._seq = 0
        return e, api

    def test_trailing_off_sends_a_fixed_stop_market(self):
        e, api = self._live(trailing_atr_mult=0.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=2.0)
        stop_calls = [c for c in api.calls if c[0] == "algo_order" and c[2] == "STOP_MARKET"]
        self.assertEqual(len(stop_calls), 1)
        self.assertFalse(any(c[2] == "TRAILING_STOP_MARKET" for c in api.calls))
        self.assertEqual(e.book[e.cfg.symbol].trailing_pct, 0.0)

    def test_trailing_on_sends_a_trailing_stop_market(self):
        e, api = self._live(trailing_atr_mult=1.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=2.0)
        trail_calls = [c for c in api.calls if c[0] == "algo_order" and c[2] == "TRAILING_STOP_MARKET"]
        self.assertEqual(len(trail_calls), 1)
        self.assertFalse(any(c[2] == "STOP_MARKET" for c in api.calls))
        self.assertEqual(e.book[e.cfg.symbol].trailing_pct, 2.0)

    def test_take_profit_is_unaffected_by_trailing(self):
        e, api = self._live(trailing_atr_mult=1.0)
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n",
               atr_pct=2.0)
        tp_calls = [c for c in api.calls if c[0] == "algo_order" and c[2] == "TAKE_PROFIT_MARKET"]
        self.assertEqual(len(tp_calls), 1)


if __name__ == "__main__":
    unittest.main()
