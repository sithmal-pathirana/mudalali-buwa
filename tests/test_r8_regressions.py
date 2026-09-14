"""
2026-09-13: a /close released a position whose limit entry had never filled,
left that entry resting, and it filled two hours later into an untracked
KAVAUSDT long with no stop. The logs said "position=0.0" throughout, and every
alert about the account was titled BTCUSDT.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.binanceapi import BinanceError                        # noqa: E402
from bot.config import Config                                  # noqa: E402
from bot.engine import Engine                                  # noqa: E402
from bot.notify import Event, Notifier                         # noqa: E402
from bot.positions import ActivePosition                       # noqa: E402
from test_r2_regressions import StubAPI, _cleanup, engine      # noqa: E402


def _kava():
    return ActivePosition("KAVAUSDT", "BUY", 0.07553, 0.07441, 0.07719, 529.4,
                          entry_order_id="e-17", stop_order_id="s-17",
                          tp_order_id="t-17", tag="e-17")


def _resting_kava(api):
    e = engine(api=api)
    e.cfg.realtime = False
    e.book = {"KAVAUSDT": _kava()}
    e._seq = 0
    return e


class TestCloseOnAFlatSymbolClearsWhatIsResting(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_resting_entry_is_cancelled_before_release(self):
        api = StubAPI()                       # flat: the entry never filled
        e = _resting_kava(api)
        self.assertTrue(Engine.close_position(e, "manual", symbol="KAVAUSDT"))
        self.assertIn(("cancel_all", "KAVAUSDT"), api.calls)
        self.assertNotIn("KAVAUSDT", e.book)

    def test_a_failed_cancel_still_releases_and_says_so(self):
        api = StubAPI()

        def refuse(symbol):
            raise BinanceError(-1001, "disconnected", "/fapi/v1/allOpenOrders")
        api.cancel_all = refuse
        e = _resting_kava(api)
        Engine.close_position(e, "manual", symbol="KAVAUSDT")
        self.assertNotIn("KAVAUSDT", e.book)
        self.assertTrue([b for ev, b in e.sent
                         if ev == Event.ERROR and "KAVAUSDT" in b])


class TestUntrackedPositionsAreReported(unittest.TestCase):
    LIVE = {
        "KAVAUSDT": {"positionAmt": "529.4", "entryPrice": "0.07553",
                     "unRealizedProfit": "-0.70"},
        "AUSDT": {"positionAmt": "0", "entryPrice": "0"},
    }

    def tearDown(self):
        _cleanup()

    def test_a_position_missing_from_the_book_raises_an_error_alert(self):
        e = engine()
        e.book = {}
        Engine.warn_untracked(e, self.LIVE)
        errors = [b for ev, b in e.sent if ev == Event.ERROR]
        self.assertEqual(len(errors), 1)
        self.assertIn("KAVAUSDT", errors[0])
        self.assertIn("NOT tracking", errors[0])

    def test_a_tracked_position_is_quiet(self):
        e = engine()
        e.book = {"KAVAUSDT": _kava()}
        Engine.warn_untracked(e, self.LIVE)
        self.assertEqual([b for ev, b in e.sent if ev == Event.ERROR], [])


class TestAccountAlertsAreNotTitledWithTheFeedSymbol(unittest.TestCase):
    def test_portfolio_mode_titles_general_alerts_bot(self):
        cfg = Config(symbol="BTCUSDT")
        cfg.portfolio.enabled = True
        self.assertEqual(Notifier.from_config(cfg).symbol, "bot")

    def test_single_symbol_mode_keeps_the_symbol(self):
        cfg = Config(symbol="BTCUSDT")
        cfg.portfolio.enabled = False
        self.assertEqual(Notifier.from_config(cfg).symbol, "BTCUSDT")


if __name__ == "__main__":
    unittest.main()


class TestAFillBetweenReconcileReadsKeepsProtection(unittest.TestCase):
    """
    2026-09-14: LITUSDT's entry filled between reconcile_book's positions()
    and open_orders() reads. The bot saw flat + entry gone + stop listed,
    cancelled the stop and take-profit, and released a live long.
    """

    SNAP = {"position_amt": 0.0, "open_order_ids": {"s-17", "t-17"}}

    def tearDown(self):
        _cleanup()

    def test_a_size_on_the_recheck_keeps_the_orders_and_confirms_the_fill(self):
        api = StubAPI(position_amt=529.4, open_ids=("s-17", "t-17"))
        e = _resting_kava(api)
        e.reconcile_position(dict(self.SNAP), symbol="KAVAUSDT")
        self.assertIn("KAVAUSDT", e.book)
        self.assertTrue(e.book["KAVAUSDT"].filled)
        self.assertNotIn(("cancel_all", "KAVAUSDT"), api.calls)

    def test_a_failed_recheck_cancels_nothing(self):
        api = StubAPI(open_ids=("s-17", "t-17"))

        def refuse(symbol=None):
            raise BinanceError(-1001, "disconnected", "/fapi/v2/positionRisk")
        api.positions = refuse
        e = _resting_kava(api)
        e.reconcile_position(dict(self.SNAP), symbol="KAVAUSDT")
        self.assertIn("KAVAUSDT", e.book)
        self.assertNotIn(("cancel_all", "KAVAUSDT"), api.calls)

    def test_still_flat_on_the_recheck_releases_as_before(self):
        api = StubAPI(open_ids=("s-17", "t-17"))
        e = _resting_kava(api)
        e.reconcile_position(dict(self.SNAP), symbol="KAVAUSDT")
        self.assertNotIn("KAVAUSDT", e.book)
        self.assertIn(("cancel_all", "KAVAUSDT"), api.calls)
