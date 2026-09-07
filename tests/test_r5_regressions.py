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


class PricingAPI(StubAPI):
    """A stub that can quote a mark price, which StubAPI never needed to."""

    def __init__(self, prices=None, **kw):
        super().__init__(**kw)
        self.prices = dict(prices or {})
        self.mark_price_calls = []

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


if __name__ == "__main__":
    unittest.main()
