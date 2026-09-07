"""
Round-five finding, from the live session of 2026-09-07.

One theme: with `realtime: false` -- the supported configuration on this host,
because live futures websockets are silent from it -- nothing ever wrote the
price cache. `on_tick` is its only writer and `on_tick` only runs off the
websocket, so `poll_once` drove `decide()` from klines and left every
price-derived reading with no price at all.

What that looked like on Telegram, with a real AEROUSDT position open:

    AEROUSDT  BUY 17.3
      entry 0.6075  now 79,097.9000     <- BTCUSDT's mark price
      unrealised +0.00
      TP 0%  SL 0%

The reported "now" was the configured symbol's startup mark price, reached
through a fallback in `_position_book` that survived the round-four fix to the
P&L lines beside it. The zeros were the same cache miss taking the other
branch.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.positions import ActivePosition                      # noqa: E402
from test_r2_regressions import StubAPI, engine, _cleanup     # noqa: E402


#: The real fills Binance recorded for the AEROUSDT trade of 2026-09-07, which
#: is the trade whose P&L never reached /pnl. Gross 0.16262, commission
#: 0.01059, net 0.15203 -- and the balance moved 2.44788 -> 2.59991, which is
#: the net to five decimal places.
AERO_FILLS = [
    {"time": 1788807621451, "side": "BUY", "price": "0.6075000", "qty": "17.3",
     "realizedPnl": "0", "commission": "0.00525487", "commissionAsset": "USDT"},
    {"time": 1788809142015, "side": "SELL", "price": "0.6169000", "qty": "0.4",
     "realizedPnl": "0.00376000", "commission": "0.00012338",
     "commissionAsset": "USDT"},
    {"time": 1788809142015, "side": "SELL", "price": "0.6169000", "qty": "16.9",
     "realizedPnl": "0.15886000", "commission": "0.00521280",
     "commissionAsset": "USDT"},
]


class PricingAPI(StubAPI):
    """A stub that can quote a mark price, which StubAPI never needed to."""

    def __init__(self, prices=None, fills=None, **kw):
        super().__init__(**kw)
        self.prices = dict(prices or {})
        self.mark_price_calls = []
        self.fills = list(fills) if fills is not None else list(AERO_FILLS)
        self.user_trades_calls = []

    def user_trades(self, symbol, start_ms=None, limit=1000):
        self.user_trades_calls.append((symbol, start_ms))
        return list(self.fills)

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))

    def mark_price(self, symbol):
        self.mark_price_calls.append(symbol)
        return {"symbol": symbol, "markPrice": str(self.prices[symbol])}

    def mark_prices(self, symbols=None):
        self.mark_price_calls.append(tuple(symbols or ()))
        if symbols is None:
            return dict(self.prices)
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def klines(self, symbol, interval, limit=None, end_ms=None):
        return []


def _aero(symbol="AEROUSDT"):
    return ActivePosition(symbol=symbol, side="BUY", qty=17.3,
                          entry=0.6075, stop=0.5873, take_profit=0.6376,
                          entry_order_id="e-1", stop_order_id="s-1",
                          tag=symbol)


def _held(prices):
    """An engine in polling mode holding AEROUSDT, configured on BTCUSDT."""
    e = engine(api=PricingAPI(prices=prices))
    e.cfg.symbol = "BTCUSDT"
    e.cfg.realtime = False
    e.cfg.poll_seconds = 20
    e.last_price = 79_097.9          # BTCUSDT, fetched once at startup
    e.last_prices = {}               # what polling mode actually leaves behind
    e.book = {"AEROUSDT": _aero()}
    e._dry_pending = {}
    return e


class TestPollingModeHasNoPrices(unittest.TestCase):
    def tearDown(self):
        _cleanup()

    def test_the_position_was_quoted_at_the_other_symbols_price(self):
        """The bug, reproduced: AEROUSDT published at BTCUSDT's 79,097."""
        e = _held({})
        e.last_prices = {}
        row = e._position_book()[0]
        self.assertNotEqual(
            row["price"], 79_097.9,
            "an unpriced position must not borrow the configured symbol's price")
        self.assertEqual(row["price"], 0.0, "zero is how 'no price yet' is said")

    def test_polling_fills_the_price_cache(self):
        e = _held({"BTCUSDT": 79_097.9, "AEROUSDT": 0.6400})
        e.poll_prices()
        self.assertEqual(e.last_prices["AEROUSDT"], 0.6400)
        self.assertEqual(e.last_price, 79_097.9,
                         "the configured symbol still drives last_price")

    def test_polling_asks_for_the_held_symbol_not_just_the_configured_one(self):
        e = _held({"BTCUSDT": 79_097.9, "AEROUSDT": 0.6400})
        e.poll_prices()
        asked = e.api.mark_price_calls[0]
        self.assertIn("AEROUSDT", asked)
        self.assertIn("BTCUSDT", asked)

    def test_unrealised_pnl_is_real_once_polled(self):
        e = _held({"BTCUSDT": 79_097.9, "AEROUSDT": 0.6400})
        e.poll_prices()
        row = e._position_book()[0]
        self.assertEqual(row["price"], 0.6400)
        self.assertAlmostEqual(row["unrealized"], (0.6400 - 0.6075) * 17.3, places=6)
        self.assertGreater(row["to_tp"], 0.0, "distance to TP was stuck at 0%")

    def test_a_resting_entry_is_priced_too(self):
        e = _held({"BTCUSDT": 79_097.9, "AEROUSDT": 0.64, "DOTUSDT": 3.21})
        e.book = {}
        e._dry_pending = {"DOTUSDT": _aero("DOTUSDT")}
        e.poll_prices()
        self.assertEqual(e.last_prices["DOTUSDT"], 3.21)

    def test_a_price_poll_failure_does_not_break_the_loop(self):
        e = _held({"BTCUSDT": 79_097.9})     # AEROUSDT missing from the stub
        e.poll_prices()
        self.assertNotIn("AEROUSDT", e.last_prices)
        self.assertEqual(e._position_book()[0]["price"], 0.0)


class TestFeedStateIsHonest(unittest.TestCase):
    """
    /status reported "stream DOWN" whenever realtime was off. The stream is
    off by configuration on this host; calling that DOWN sends the reader
    hunting for a fault that does not exist.
    """

    def tearDown(self):
        _cleanup()

    def test_polling_is_not_reported_as_down(self):
        e = _held({"BTCUSDT": 79_097.9, "AEROUSDT": 0.64})
        e.poll_prices()
        self.assertIn("polling", e._feed_state())
        self.assertNotIn("DOWN", e._feed_state())

    def test_polling_that_stopped_returning_prices_says_so(self):
        e = _held({"BTCUSDT": 79_097.9})
        e._last_price_poll = __import__("time").time() - 600
        self.assertIn("STALLED", e._feed_state())

    def test_a_dead_websocket_is_still_down(self):
        e = _held({})
        e.cfg.realtime = True
        e.stream = None
        self.assertEqual(e._feed_state(), "DOWN")


class TestExchangeSideCloseIsBooked(unittest.TestCase):
    """
    The AEROUSDT trade of 2026-09-07 closed on its take-profit while the bot
    was in polling mode. Equity went 2.4479 -> 2.5999, and realized_today
    stayed at 0.00 -- because realised P&L was booked in exactly one place,
    on_order, which is fed by the user-data websocket that polling mode does
    not have. /pnl read 0% of target with the money already in the account.
    """

    def tearDown(self):
        _cleanup()

    def _closed(self, fills=None):
        """AEROUSDT tracked in the book, flat on the exchange, no orders left."""
        e = _held({"BTCUSDT": 79_097.9})
        if fills is not None:
            e.api.fills = list(fills)
        e.book["AEROUSDT"].opened_ms = 1788807621000
        e.state.realized_today = 0.0
        return e

    def _reconcile_flat(self, e):
        e.reconcile_position({"position_amt": 0.0, "open_order_ids": set()},
                             symbol="AEROUSDT")

    def test_the_close_reaches_realized_today(self):
        e = self._closed()
        self._reconcile_flat(e)
        self.assertAlmostEqual(e.state.realized_today, 0.15202895, places=6)

    def test_it_matches_what_the_balance_actually_did(self):
        e = self._closed()
        self._reconcile_flat(e)
        moved = 2.59991362 - 2.44788467
        self.assertAlmostEqual(e.state.realized_today, moved, places=5,
                               msg="booked P&L must equal the balance change")

    def test_commission_is_not_ignored(self):
        e = self._closed()
        self._reconcile_flat(e)
        gross = 0.00376 + 0.15886
        self.assertLess(e.state.realized_today, gross,
                        "gross realizedPnl overstates what the account gained")

    def test_the_user_is_told_the_trade_closed(self):
        e = self._closed()
        self._reconcile_flat(e)
        self.assertTrue([b for _, b in e.sent if "closed" in b],
                        "an exchange-side close sent no alert at all")

    def test_the_slot_is_still_freed(self):
        e = self._closed()
        self._reconcile_flat(e)
        self.assertNotIn("AEROUSDT", e.book)

    def test_only_fills_from_this_position_are_counted(self):
        """An earlier trade on the same symbol must not be booked again."""
        earlier = dict(AERO_FILLS[2], time=1788700000000,
                       realizedPnl="99.0", commission="0")
        e = self._closed(fills=[earlier] + AERO_FILLS)
        self._reconcile_flat(e)
        self.assertAlmostEqual(e.state.realized_today, 0.15202895, places=6)

    def test_fees_paid_in_bnb_are_not_subtracted_from_usdt(self):
        fills = [dict(AERO_FILLS[1], commission="0.5", commissionAsset="BNB")]
        e = self._closed(fills=fills)
        self._reconcile_flat(e)
        self.assertAlmostEqual(e.state.realized_today, 0.00376, places=6)

    def test_an_unreachable_exchange_books_nothing_rather_than_guessing(self):
        e = self._closed()

        def boom(*a, **kw):
            from bot.binanceapi import BinanceError
            raise BinanceError(-1001, "disconnected", "/userTrades")

        e.api.user_trades = boom
        self._reconcile_flat(e)
        self.assertEqual(e.state.realized_today, 0.0)
        self.assertNotIn("AEROUSDT", e.book, "the slot must still be freed")

    def test_a_crash_while_booking_still_frees_the_slot(self):
        """
        With one slot configured, a position left in the book stops the bot
        trading at all. Bookkeeping must never be able to cause that.
        """
        e = self._closed()

        def boom(*a, **kw):
            raise RuntimeError("something nobody predicted")

        e.api.user_trades = boom
        self._reconcile_flat(e)
        self.assertNotIn("AEROUSDT", e.book)

    def test_a_stale_protective_order_close_is_booked_too(self):
        e = self._closed()
        e.reconcile_position(
            {"position_amt": 0.0, "open_order_ids": {"s-1"}}, symbol="AEROUSDT")
        self.assertAlmostEqual(e.state.realized_today, 0.15202895, places=6)


if __name__ == "__main__":
    unittest.main()
