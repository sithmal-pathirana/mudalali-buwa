"""
Round-four findings, all from one live portfolio-mode session.

The theme: the engine kept one set of single-symbol variables -- one bar
series, one price, one anchor for the day -- while the scanner filled the book
with other coins. Every finding here is a place where a second symbol, or a
second basis for equity, was written into a slot that only had room for one.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.config import PortfolioConfig                        # noqa: E402
from bot.engine import Engine                                 # noqa: E402
from bot.notify import Event                                  # noqa: E402
from bot.positions import ActivePosition                      # noqa: E402
from bot.scanner import Candidate, ScanResult                 # noqa: E402
from bot.strategies.base import Signal                        # noqa: E402
from bot.stream import BarClosed, Tick                        # noqa: E402
from test_r2_regressions import StubAPI, engine, _cleanup     # noqa: E402


class TestTheEquityCapCannotFakeADrawdown(unittest.TestCase):
    """
    Live halt, 2026-09-05: "daily loss limit hit: -98.00% (limit 5.00%)".

    Nothing lost anything. `risk.equity_cap_usdt: 100` was added to the config
    part-way through a day whose opening equity had already been recorded as
    the testnet's real 5,000. The limit then compared an uncapped anchor with a
    capped reading -- 100 against 5,000 -- and halted on the next bar close.
    """

    def tearDown(self):
        _cleanup()

    def _capped(self, cap=100.0, day_start=5000.0):
        e = engine()
        e.cfg.risk.equity_cap_usdt = cap
        e.state.day_start_equity = day_start
        e.equity = e.effective_equity(5000.0)
        return e

    def test_the_stale_anchor_is_what_halted_it(self):
        e = self._capped()
        self.assertFalse(e.risk.preflight(e.equity),
                         "this is the bug being fixed; it must reproduce")

    def test_rebasing_puts_both_sides_on_one_basis(self):
        e = self._capped()
        e.rebase_day_start_equity()
        self.assertEqual(e.state.day_start_equity, 100.0)
        self.assertTrue(e.risk.preflight(e.equity),
                        "a cap the user typed is not a 98% loss")

    def test_a_real_loss_under_the_cap_still_halts(self):
        e = self._capped()
        e.rebase_day_start_equity()
        self.assertFalse(e.risk.preflight(90.0), "the limit must still bite")

    def test_an_uncapped_account_is_left_alone(self):
        e = self._capped(cap=0.0)
        e.rebase_day_start_equity()
        self.assertEqual(e.state.day_start_equity, 5000.0)

    def test_the_anchor_is_never_raised(self):
        """Raising the cap mid-day must not invent headroom for losses."""
        e = self._capped(cap=5000.0, day_start=100.0)
        e.rebase_day_start_equity()
        self.assertEqual(e.state.day_start_equity, 100.0)


class TestDryRunLossesCountAgainstTheLimit(unittest.TestCase):
    """
    The other half of the same limit: simulated fills never touch the exchange
    balance, so the equity feed reported a flat day no matter what the
    simulation lost, and the daily loss limit was inert in the mode the README
    tells you to run for days.
    """

    def tearDown(self):
        _cleanup()

    def test_a_simulated_loss_trips_the_limit(self):
        e = engine(dry_run=True)
        e.state.day_start_equity = 100.0
        e.state.realized_today = -6.0          # limit is 5%
        self.assertFalse(e.risk.preflight(100.0))
        self.assertTrue(e.state.halted)

    def test_a_simulated_loss_inside_the_limit_does_not(self):
        e = engine(dry_run=True)
        e.state.day_start_equity = 100.0
        e.state.realized_today = -2.0
        self.assertTrue(e.risk.preflight(100.0))

    def test_a_winning_day_is_not_a_drawdown(self):
        e = engine(dry_run=True)
        e.state.day_start_equity = 100.0
        e.state.realized_today = 24.49
        self.assertTrue(e.risk.preflight(100.0))


class TestOneSeriesOneSymbol(unittest.TestCase):
    """
    Every held symbol streams its own klines on the shared socket. on_bar took
    them all: the configured symbol's series became a blend of markets, and
    decide() ran once per subscribed symbol -- which is why three identical
    "limit entry resting" alerts arrived inside one second.
    """

    def tearDown(self):
        _cleanup()

    def _engine(self):
        e = engine()
        e.bars = []
        e.strategy = type("St", (), {"warmup": 50})()
        e.decided = 0
        e.decide = lambda: setattr(e, "decided", e.decided + 1)
        return e

    def _bar(self, symbol, close):
        return BarClosed(symbol=symbol, interval="15m", open_time=1, open=close,
                         high=close, low=close, close=close, volume=1.0)

    def test_another_symbols_bar_is_not_appended(self):
        e = self._engine()
        e.on_bar(self._bar("ENAUSDT", 0.1873))
        self.assertEqual(e.bars, [], "a held coin's chart entered DOGEUSDT's series")

    def test_another_symbols_bar_does_not_run_the_cycle(self):
        e = self._engine()
        for sym in ("ENAUSDT", "UNIUSDT", "ARBUSDT"):
            e.on_bar(self._bar(sym, 1.0))
        self.assertEqual(e.decided, 0, "one bar close, one decision")

    def test_the_configured_symbols_bar_is_taken(self):
        e = self._engine()
        e.on_bar(self._bar("DOGEUSDT", 0.09))
        self.assertEqual(len(e.bars), 1)
        self.assertEqual(e.decided, 1)


class TestThePublishedPriceIsTheConfiguredSymbols(unittest.TestCase):
    """
    /status and the heartbeat print `last_price` beside the configured symbol's
    name. Any ticking coin could write to it, so a BTCUSDT summary was sent
    quoting 0.1946 -- ARBUSDT's price.
    """

    def tearDown(self):
        _cleanup()

    def _engine(self):
        e = engine()
        e.last_prices = {}
        e.last_price = 79_000.0
        e._tick_guard = lambda: False
        return e

    def test_another_symbols_tick_does_not_move_it(self):
        e = self._engine()
        e.on_tick(Tick(symbol="ARBUSDT", mark_price=0.1946, event_time=1))
        self.assertEqual(e.last_price, 79_000.0)

    def test_but_it_is_still_cached_for_that_symbol(self):
        e = self._engine()
        e.on_tick(Tick(symbol="ARBUSDT", mark_price=0.1946, event_time=1))
        self.assertEqual(e.last_prices["ARBUSDT"], 0.1946)

    def test_its_own_tick_does_move_it(self):
        e = self._engine()
        e.on_tick(Tick(symbol="DOGEUSDT", mark_price=0.0901, event_time=1))
        self.assertEqual(e.last_price, 0.0901)


class TestARestingEntryHoldsItsSlot(unittest.TestCase):
    """
    In dry run a placed entry rests in `_dry_pending`, not in `book`. The
    portfolio loop only consulted `book`, so the next cycle re-entered the same
    symbol: duplicate alerts, the earlier entry silently overwritten, and the
    slot and leverage caps measured against a book that under-reported what was
    already committed.
    """

    def tearDown(self):
        _cleanup()

    def _engine(self, pending=()):
        e = engine(dry_run=True)
        e.equity = 100.0
        e.cfg.portfolio = PortfolioConfig(enabled=True, hard_cap=4)
        e.book = {}
        e._dry_pending = {
            s: ActivePosition(s, "BUY", 1.0, 0.98, 1.04, 10.0,
                              entry_order_id=s, tag=s)
            for s in pending}
        e.scanner = type("S", (), {
            "due": lambda s: False,
            "last": ScanResult(ranked=[
                Candidate(symbol="ARBUSDT", price=1.0, quote_volume=1e7,
                          efficiency=0.5, atr_pct=1.0, min_notional=5.0,
                          score=0.8, bars=[]),
                Candidate(symbol="UNIUSDT", price=1.0, quote_volume=1e7,
                          efficiency=0.5, atr_pct=1.0, min_notional=5.0,
                          score=0.7, bars=[]),
            ], considered=100)})()
        e.strategy = type("St", (), {
            "on_bars": lambda s, bars, amt: Signal("BUY", 1.0, 0.98,
                                                   "test", 1.04)})()
        e.rules_for = lambda sym: type("R", (), {
            "symbol": sym,
            "min_affordable_notional": lambda s, px: 5.0})()
        e.placed = []
        e.place = lambda sig, notional, note, symbol=None, atr_pct=0.0: e.placed.append(symbol)
        e.stream = None
        return e

    def test_a_resting_entry_is_not_placed_again(self):
        e = self._engine(pending=["ARBUSDT"])
        e.portfolio_cycle()
        self.assertNotIn("ARBUSDT", e.placed,
                         "the same entry was placed twice")

    def test_the_other_candidates_are_still_taken(self):
        e = self._engine(pending=["ARBUSDT"])
        e.portfolio_cycle()
        self.assertIn("UNIUSDT", e.placed)

    def test_resting_entries_consume_the_free_slots(self):
        e = self._engine(pending=["ARBUSDT", "UNIUSDT"])
        e.cfg.portfolio.hard_cap = 2
        e.portfolio_cycle()
        self.assertEqual(e.placed, [], "resting entries did not count as held")

    def test_nothing_pending_still_trades(self):
        e = self._engine()
        e.portfolio_cycle()
        self.assertEqual(e.placed[:1], ["ARBUSDT"])


class TestAlertsNameTheSymbolTheyAreAbout(unittest.TestCase):
    """
    Notifier.send has taken a per-message `symbol` since round three, but the
    portfolio paths never passed it -- so a whole session of ENAUSDT, UNIUSDT
    and EIGENUSDT trades arrived titled "BTCUSDT trade opened".
    """

    def tearDown(self):
        _cleanup()

    def _engine(self):
        e = engine(dry_run=True)
        e.sent_kw = []
        e.notify = type("N", (), {
            "send": lambda s, ev, body, **kw: e.sent_kw.append((ev, kw.get("symbol"))),
            "clear_position_alerts": lambda s, tag: None})()
        e.risk = type("R", (), {"record_fill": lambda s, p=0.0: None})()
        e.last_prices = {}
        e._entry_placed_at = 0.0
        e._dry_pending = {}
        e.book = {}
        return e

    def test_a_dry_run_fill_names_its_own_symbol(self):
        e = self._engine()
        e._dry_pending["ENAUSDT"] = ActivePosition(
            "ENAUSDT", "BUY", 0.1873, 0.1818, 0.1954, 100.0,
            entry_order_id="d-1", tag="d-1")
        e.last_prices["ENAUSDT"] = 0.1872
        e.simulate_entry(0.1872, "ENAUSDT")
        self.assertIn((Event.TRADE_OPEN, "ENAUSDT"), e.sent_kw)

    def test_a_simulated_close_names_its_own_symbol(self):
        e = self._engine()
        e.book["ENAUSDT"] = ActivePosition(
            "ENAUSDT", "BUY", 0.1873, 0.1818, 0.1954, 100.0,
            entry_order_id="d-1", tag="d-1")
        e.release = lambda sym: e.book.pop(sym, None)
        e.check_target_reached = lambda: False
        e.simulate_exit(0.1818, "ENAUSDT")
        self.assertIn((Event.SL_HIT, "ENAUSDT"), e.sent_kw)

    def test_a_proximity_warning_names_its_own_symbol(self):
        e = self._engine()
        e.cfg.dry_run = False
        e.book["ENAUSDT"] = ActivePosition(
            "ENAUSDT", "BUY", 0.1873, 0.1818, 0.1954, 100.0,
            entry_order_id="d-1", tag="d-1")
        e._tick_guard = lambda: False
        e.on_tick(Tick(symbol="ENAUSDT", mark_price=0.1828, event_time=1))
        self.assertIn((Event.APPROACH_SL, "ENAUSDT"), e.sent_kw)


class TestScanMessagesAreNotADailySummary(unittest.TestCase):
    def test_the_scan_event_exists_and_is_not_emailed(self):
        from bot.notify import EMAIL_EVENTS, ICON
        self.assertIn(Event.SCAN, ICON)
        self.assertNotIn(Event.SCAN, EMAIL_EVENTS,
                         "a scan is chatter; it does not belong in an inbox")

    def test_the_engine_sends_scans_as_scans(self):
        import inspect
        src = inspect.getsource(Engine.run_scan_now)
        self.assertNotIn("Event.DAILY_SUMMARY", src)
        self.assertIn("Event.SCAN", src)


if __name__ == "__main__":
    unittest.main()
