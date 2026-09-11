"""
Regressions for the AKEUSDT halt and the unbooked manual closes of 2026-09-11.

1. A /scan at 05:13 cached 15m bars ending at the 04:45 close. The 05:15
   bar-close cycle reused them (the scan was under rescan_seconds old), priced
   a short at 0.013395 with the market at 0.0120, and its take-profit at
   0.012320 was already crossed. Binance answered -2021, and the bot halted
   with a message blaming the stop.
2. Closing from Telegram released the position without booking it. FFUSDT
   (+0.19) and AKEUSDT (+3.09) never reached realized_today, and total_trades
   stayed 0 because polling mode never reaches record_fill.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import bot.engine as engine_mod                                # noqa: E402
from bot.binanceapi import BinanceError                        # noqa: E402
from bot.engine import Engine                                  # noqa: E402
from bot.scanner import Candidate, ScanConfig, Scanner, ScanResult  # noqa: E402
from bot.strategies.base import Bar, Signal                    # noqa: E402
from test_r2_regressions import StubAPI, _cleanup, engine      # noqa: E402
from test_r5_regressions import AERO_FILLS, PricingAPI, _aero  # noqa: E402

M15 = 900_000
BAR_0445 = 1789101900000          # 2026-09-11 04:45 UTC
AT_0513 = 1789103595.0            # the manual /scan
AT_0515 = 1789103706.0            # the bar-close cycle that halted


def _scanner_with_bar(open_time):
    s = Scanner(api=None, cfg=ScanConfig(interval="15m"))
    bar = Bar(open_time, 0.0146, 0.0146, 0.0132, 0.013395, 1.0)
    s.last = ScanResult(ranked=[Candidate("AKEUSDT", 0.013395, 0, 0, 0, 0,
                                          bars=[bar])])
    return s


class TestCachedScanBarsGoStale(unittest.TestCase):
    def test_fresh_within_the_same_bar(self):
        self.assertFalse(_scanner_with_bar(BAR_0445).stale(now=AT_0513))

    def test_stale_once_the_next_bar_closes(self):
        self.assertTrue(_scanner_with_bar(BAR_0445).stale(now=AT_0515))

    def test_current_bar_is_not_stale(self):
        self.assertFalse(_scanner_with_bar(BAR_0445 + M15).stale(now=AT_0515))

    def test_no_result_is_not_stale(self):
        self.assertFalse(Scanner(api=None).stale(now=AT_0515))


class TestPortfolioRescansStaleBars(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_a_stale_scan_is_refreshed_before_trading(self):
        e = engine()
        e.cfg.portfolio.enabled = True
        scans = []
        e.scanner = type("S", (), {
            "due": lambda s: False,
            "stale": lambda s: True,
            "last": ScanResult(),
            "scan": lambda s, **kw: scans.append(kw)})()
        e.portfolio_cycle()
        self.assertEqual(len(scans), 1,
                         "a scan older than the last bar close was traded from")


class _PlaceAPI(StubAPI):
    def __init__(self, mark=None, fail_type=None, **kw):
        super().__init__(**kw)
        self.mark = mark
        self.fail_type = fail_type

    def mark_price(self, symbol):
        if self.mark is None:
            raise BinanceError(-1001, "unavailable", "/premiumIndex")
        return {"symbol": symbol, "markPrice": str(self.mark)}

    def algo_order(self, **kw):
        if kw.get("type") == self.fail_type:
            self.calls.append(("algo_order", kw.get("side"), kw.get("type"), "x"))
            raise BinanceError(-2021, "Order would immediately trigger.", "/algoOrder")
        return super().algo_order(**kw)


def _placing(api):
    e = engine(api=api)
    e.rules = type("R", (), {
        "size_for_notional": lambda s, n, p: ("834", f"{p:.7f}"),
        "round_price": lambda s, p: f"{p:.7f}",
        "round_qty": lambda s, q: f"{q:.0f}"})()
    e._seq = 0
    e._entry_placed_at = 0.0
    e._prepared = None
    e.signals = None
    return e


AKE_SHORT = Signal("SELL", entry=0.013395, stop=0.014111, take_profit=0.012320)


class TestCrossedLevelsAreSkippedNotHalted(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_the_akeusdt_signal_is_skipped(self):
        api = _PlaceAPI(mark=0.0120)
        e = _placing(api)
        e.place(AKE_SHORT, 8.54, "n")
        self.assertFalse([c for c in api.calls if c[0] in ("order", "algo_order")],
                         "an order was sent for a take-profit already crossed")
        self.assertFalse(e.state.halted)

    def test_a_crossed_stop_is_skipped_too(self):
        api = _PlaceAPI(mark=0.0145)
        e = _placing(api)
        e.place(AKE_SHORT, 8.54, "n")
        self.assertFalse([c for c in api.calls if c[0] == "order"])

    def test_a_valid_signal_still_trades(self):
        api = _PlaceAPI(mark=0.0134)
        e = _placing(api)
        e.place(AKE_SHORT, 8.54, "n")
        self.assertIn(e.cfg.symbol, e.book)

    def test_the_halt_still_backs_it_up_and_names_the_leg(self):
        """No mark price: the check steps aside and the halt still fires."""
        api = _PlaceAPI(mark=None, fail_type="TAKE_PROFIT_MARKET")
        e = _placing(api)
        e.place(AKE_SHORT, 8.54, "n")
        self.assertTrue(e.state.halted)
        self.assertTrue(api.cancelled_everything)
        self.assertIn("take-profit", e.state.halt_reason)


class _CloseAPI(PricingAPI):
    """Holds AEROUSDT; its closing fill appears only after a first read."""

    def __init__(self, order_id=None, late_fill=None, **kw):
        super().__init__(position_amt=17.3, **kw)
        self.order_id = order_id
        self.late_fill = late_fill

    def order(self, **kw):
        super().order(**kw)
        return {"status": "NEW", "orderId": self.order_id}

    def user_trades(self, symbol, start_ms=None, limit=1000):
        rows = super().user_trades(symbol, start_ms, limit)
        if self.late_fill is not None and len(self.user_trades_calls) > 1:
            rows.append(self.late_fill)
        return rows


def _holding(api):
    e = engine(api=api)
    e.cfg.realtime = False
    e.book = {"AEROUSDT": _aero()}
    e.book["AEROUSDT"].opened_ms = 1788807621000
    e._seq = 0
    e.state.realized_today = 0.0
    e.state.total_trades = 0
    return e


class TestManualCloseIsBooked(unittest.TestCase):
    def setUp(self):
        self._wait = engine_mod.FILL_WAIT_SECONDS
        engine_mod.FILL_WAIT_SECONDS = 0

    def tearDown(self):
        engine_mod.FILL_WAIT_SECONDS = self._wait
        _cleanup()

    def test_telegram_close_reaches_realized_today(self):
        e = _holding(_CloseAPI())
        self.assertTrue(Engine.close_position(e, "closed manually (via telegram)",
                                              symbol="AEROUSDT"))
        self.assertAlmostEqual(e.state.realized_today, 0.15202895, places=6)
        self.assertNotIn("AEROUSDT", e.book)

    def test_it_counts_as_a_trade(self):
        e = _holding(_CloseAPI())
        Engine.close_position(e, "manual", symbol="AEROUSDT")
        self.assertEqual(e.state.total_trades, 1)

    def test_the_alert_carries_the_result(self):
        e = _holding(_CloseAPI())
        Engine.close_position(e, "manual", symbol="AEROUSDT")
        self.assertTrue([b for _, b in e.sent if "+0.15 USDT" in b])

    def test_it_waits_for_the_closing_fill(self):
        late = dict(AERO_FILLS[2], orderId=42, realizedPnl="1.0", commission="0")
        e = _holding(_CloseAPI(order_id=42, late_fill=late))
        Engine.close_position(e, "manual", symbol="AEROUSDT")
        self.assertAlmostEqual(e.state.realized_today, 1.15202895, places=6)

    def test_a_position_already_flat_is_booked_too(self):
        api = _CloseAPI()
        api.position_amt = 0.0
        e = _holding(api)
        Engine.close_position(e, "manual", symbol="AEROUSDT")
        self.assertAlmostEqual(e.state.realized_today, 0.15202895, places=6)

    def test_websocket_mode_does_not_count_the_trade_twice(self):
        e = _holding(_CloseAPI())
        e.cfg.realtime = True          # on_order already counted the entry
        Engine.close_position(e, "manual", symbol="AEROUSDT")
        self.assertEqual(e.state.total_trades, 0)

    def test_an_unreadable_exchange_still_frees_the_slot(self):
        api = _CloseAPI()

        def boom(*a, **kw):
            raise BinanceError(-1001, "disconnected", "/userTrades")

        api.user_trades = boom
        e = _holding(api)
        self.assertTrue(Engine.close_position(e, "manual", symbol="AEROUSDT"))
        self.assertEqual(e.state.realized_today, 0.0)
        self.assertNotIn("AEROUSDT", e.book)


if __name__ == "__main__":
    unittest.main()
