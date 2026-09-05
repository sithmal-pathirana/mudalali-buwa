"""
Tests for the parts that lose money when they are wrong.

    /usr/bin/python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Config, RiskConfig          # noqa: E402
from bot.filters import SymbolRules                # noqa: E402
from bot.risk import RiskManager                   # noqa: E402
from bot.state import State                        # noqa: E402
from bot.strategies.base import Bar                # noqa: E402
from bot.strategies.trend_atr import TrendATR      # noqa: E402


def btc_rules() -> SymbolRules:
    """The real BTCUSDT perp filters, as returned by exchangeInfo."""
    return SymbolRules("BTCUSDT", Decimal("0.10"), Decimal("0.0001"),
                       Decimal("0.0001"), Decimal("1000"), Decimal("50"), 1, 4)


def cheap_rules() -> SymbolRules:
    """A $5-minimum symbol, e.g. DOGEUSDT."""
    return SymbolRules("DOGEUSDT", Decimal("0.000010"), Decimal("1"),
                       Decimal("1"), Decimal("9000000"), Decimal("5"), 6, 0)


def fresh(tmp: Path, **risk_kw) -> tuple[RiskManager, State]:
    state = State.load(tmp)
    cfg = Config(risk=RiskConfig(**risk_kw))
    return RiskManager(cfg, state), state


class TestSizing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("data/test_state.json")
        self.tmp.unlink(missing_ok=True)

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_43_dollars_cannot_trade_btc_within_risk_budget(self):
        """The headline case: $43 risking 2% cannot legally size a BTC trade."""
        rm, _ = fresh(self.tmp)
        d = rm.size_position(equity=43.0, entry=80_000, stop=78_400, rules=btc_rules())
        self.assertFalse(d.allowed)
        self.assertIn("cheapest legal order", d.reason)

    def test_same_account_can_size_a_cheap_symbol(self):
        rm, _ = fresh(self.tmp)
        d = rm.size_position(equity=43.0, entry=0.09, stop=0.0882, rules=cheap_rules())
        self.assertTrue(d.allowed, d.reason)
        self.assertLessEqual(d.qty_notional, 43.0 * 3)

    def test_size_scales_inversely_with_stop_distance(self):
        rm, _ = fresh(self.tmp)
        tight = rm.size_position(1000.0, 100.0, 99.0, cheap_rules())    # 1% stop
        wide = rm.size_position(1000.0, 100.0, 95.0, cheap_rules())     # 5% stop
        self.assertTrue(tight.allowed and wide.allowed)
        self.assertAlmostEqual(tight.qty_notional / wide.qty_notional, 5.0, places=6)

    def test_leverage_cap_clamps_notional(self):
        rm, _ = fresh(self.tmp, max_leverage=2, risk_per_trade_pct=50.0)
        d = rm.size_position(1000.0, 100.0, 99.9, cheap_rules())
        self.assertTrue(d.allowed)
        self.assertLessEqual(d.qty_notional, 1000.0 * 2 + 1e-9)


class TestGates(unittest.TestCase):
    def setUp(self):
        self.tmp = Path("data/test_state.json")
        self.tmp.unlink(missing_ok=True)

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_daily_loss_limit_halts_and_stays_halted(self):
        rm, state = fresh(self.tmp, daily_loss_limit_pct=5.0)
        state.day_start_equity = 100.0
        self.assertTrue(rm.preflight(96.0))            # -4%, still trading
        self.assertFalse(rm.preflight(94.0))           # -6%, halt
        self.assertTrue(state.halted)
        self.assertFalse(rm.preflight(200.0))          # recovery does not un-halt

    def test_equity_floor_halts(self):
        rm, state = fresh(self.tmp, min_equity_usdt=10.0)
        self.assertFalse(rm.preflight(9.99))
        self.assertTrue(state.halted)

    def test_trade_cap_counts_attempts_not_fills(self):
        """The cap limits activity, so a submitted entry consumes a slot. (QA F15)"""
        rm, state = fresh(self.tmp, max_trades_per_day=2)
        state.day_start_equity = 100.0
        rm.record_attempt(); rm.record_attempt()
        self.assertFalse(rm.preflight(100.0))
        self.assertFalse(state.halted)                 # a cap pauses; it does not halt

    def test_fills_are_counted_separately_from_attempts(self):
        rm, state = fresh(self.tmp, max_trades_per_day=10)
        rm.record_attempt()
        self.assertEqual((state.trades_today, state.total_trades), (1, 0))
        rm.record_fill(1.25)
        self.assertEqual((state.trades_today, state.total_trades), (1, 1))
        self.assertAlmostEqual(state.realized_today, 1.25)

    def test_averaging_down_blocked_but_reversal_allowed(self):
        rm, _ = fresh(self.tmp)
        self.assertFalse(rm.check_add_to_position(current_amt=0.5, side="BUY"))
        self.assertTrue(rm.check_add_to_position(current_amt=0.5, side="SELL"))
        self.assertTrue(rm.check_add_to_position(current_amt=0.0, side="BUY"))


class TestFilters(unittest.TestCase):
    def test_rounding_is_always_down_to_the_tick(self):
        r = btc_rules()
        self.assertEqual(r.round_price(80_000.17), "80000.1")
        self.assertEqual(r.round_qty(0.00019), "0.0001")

    def test_order_below_min_notional_is_refused_not_shrunk(self):
        r = btc_rules()
        self.assertIsNone(r.size_for_notional(40.0, 80_000))     # under $50 minimum
        self.assertIsNotNone(r.size_for_notional(60.0, 80_000))

    def test_min_affordable_uses_the_binding_constraint(self):
        self.assertAlmostEqual(btc_rules().min_affordable_notional(80_000), 50.0)
        self.assertAlmostEqual(btc_rules().min_affordable_notional(800_000), 80.0)  # minQty binds


class TestStrategy(unittest.TestCase):
    def _flat(self, n, price=100.0):
        return [Bar(i, price, price, price, price, 1.0) for i in range(n)]

    def test_no_signal_without_warmup(self):
        s = TrendATR()
        self.assertIsNone(s.on_bars(self._flat(5), 0.0))

    def test_no_signal_while_already_in_a_position(self):
        s = TrendATR()
        bars = self._flat(60)
        bars.append(Bar(60, 100, 130, 100, 130, 1.0))
        self.assertIsNone(s.on_bars(bars, position_amt=0.3))

    def test_breakout_produces_a_stop_on_the_correct_side(self):
        s = TrendATR(min_atr_pct=0.0)
        bars = [Bar(i, 100 + i * 0.1, 101 + i * 0.1, 99 + i * 0.1, 100 + i * 0.1, 1.0)
                for i in range(60)]
        bars.append(Bar(60, 106, 130, 106, 130, 1.0))
        sig = s.on_bars(bars, 0.0)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.side, "BUY")
        self.assertLess(sig.stop, sig.entry)
        self.assertGreater(sig.take_profit, sig.entry)

    def test_dead_market_is_skipped(self):
        """No range means no room to cover fees, so no trade."""
        s = TrendATR(min_atr_pct=1.0)
        bars = self._flat(60)
        bars.append(Bar(60, 100, 100.05, 100, 100.02, 1.0))
        self.assertIsNone(s.on_bars(bars, 0.0))


class TestBacktestHonesty(unittest.TestCase):
    def test_harness_matches_hand_calculation(self):
        from bot.backtest import validate
        self.assertTrue(validate())


if __name__ == "__main__":
    unittest.main(verbosity=2)
