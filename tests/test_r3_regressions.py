"""
Round-three findings: the exchange boundary stayed single-symbol while
everything above it went multi-symbol.
"""

import ast
import collections
import inspect
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from bot.engine import Engine                       # noqa: E402
from bot.stream import OrderUpdate                  # noqa: E402
from test_book import pos                           # noqa: E402
from test_r2_regressions import StubAPI, engine     # noqa: E402


class MultiAPI(StubAPI):
    """Answers per symbol; the single-symbol stub could not catch these."""

    def __init__(self, open_symbols=(), fail_on=()):
        super().__init__()
        self.open = {s: 10.0 for s in open_symbols}
        self.fail_on = set(fail_on)

    def positions(self, symbol=None):
        self.calls.append(("positions", symbol))
        amt = self.open.get(symbol, 0.0)
        return [] if not amt else [{"symbol": symbol, "positionAmt": str(amt),
                                    "entryPrice": "1.0", "unRealizedProfit": "0",
                                    "liquidationPrice": "0.5"}]

    def order(self, **kw):
        from bot.binanceapi import BinanceError
        self.calls.append(("order", kw.get("symbol")))
        if kw.get("symbol") in self.fail_on:
            raise BinanceError(-2011, "rejected", "/order")
        self.open.pop(kw.get("symbol"), None)
        return {"status": "NEW"}

    def touched(self, symbol):
        return any(symbol in [str(x) for x in c] for c in self.calls)


def held(symbols, **kw):
    e = engine(**kw)
    e.book = {s: pos(s) for s in symbols}
    e.strategy = type("S", (), {"name": "x", "mode": "auto"})()
    # The real schedule: snapshot() needs a Progress object, and a fake that
    # returns a string only proves the fake works.
    from bot.targets import TargetSchedule
    e.schedule = TargetSchedule.from_config(
        {"stop_when_reached": False,
         "schedule": [{"from_day": 1, "usd_per_day": 2.0}]})
    e.schedule.start_date = e.state.day
    e.last_prices = {s: 1.0 for s in symbols}
    return e


class TestS1SnapshotRenders(unittest.TestCase):
    """snapshot() raised NameError the moment any position opened, freezing
    the dashboard and /status on the shipped single-symbol config."""

    def test_snapshot_with_no_position(self):
        e = held([])
        self.assertEqual(Engine.snapshot(e)["positions"], [])

    def test_snapshot_with_one_position(self):
        e = held(["AUSDT"])
        snap = Engine.snapshot(e)
        self.assertEqual(len(snap["positions"]), 1)
        self.assertIsNotNone(snap["position"])

    def test_snapshot_with_many_positions(self):
        e = held([f"S{i}USDT" for i in range(25)])
        self.assertEqual(len(Engine.snapshot(e)["positions"]), 25)

    def test_snapshot_has_no_duplicate_position_builder(self):
        """One source of truth: the duplicate is what broke."""
        src = inspect.getsource(Engine.snapshot)
        self.assertNotIn('"to_tp"', src, "snapshot builds position dicts again")


class TestS2OrderUpdatesFindTheirPosition(unittest.TestCase):
    def _fill(self, e, symbol, otype="STOP_MARKET", pnl=-2.5, cid=None):
        Engine.on_order(e, OrderUpdate(
            symbol=symbol, client_order_id=cid or f"s-{symbol}", side="SELL",
            status="FILLED", order_type=otype, last_filled_qty=1.0,
            cumulative_qty=1.0, avg_price=1.0, realized_pnl=pnl, raw={}))

    def test_a_scanner_symbol_fill_is_not_dropped(self):
        e = held(["BTCUSDT", "DOGEUSDT", "SOLUSDT"])
        e.cfg.symbol = "BTCUSDT"
        self._fill(e, "SOLUSDT")
        self.assertNotIn("SOLUSDT", e.book, "slot never freed")
        self.assertAlmostEqual(e.state.realized_today, -2.5,
                               msg="loss never booked")

    def test_closing_one_leaves_the_others(self):
        e = held(["AUSDT", "BUSDT", "CUSDT"])
        self._fill(e, "BUSDT")
        self.assertEqual(sorted(e.book), ["AUSDT", "CUSDT"])

    def test_an_update_for_an_unheld_symbol_is_ignored(self):
        e = held(["AUSDT"])
        self._fill(e, "ZUSDT")
        self.assertEqual(sorted(e.book), ["AUSDT"])
        self.assertEqual(e.state.realized_today, 0.0)

    def test_realized_pnl_accrues_so_the_target_can_fire(self):
        """S2's real damage: check_target_reached could never trigger."""
        e = held(["AUSDT", "BUSDT"])
        self._fill(e, "AUSDT", pnl=1.5)
        self._fill(e, "BUSDT", pnl=1.0)
        self.assertAlmostEqual(e.state.realized_today, 2.5)


class TestS3ReconcileIsScoped(unittest.TestCase):
    def test_reconciling_one_symbol_keeps_the_others(self):
        e = held(["ADAUSDT", "DOGEUSDT", "SOLUSDT"], api=MultiAPI())
        e.cfg.symbol = "DOGEUSDT"
        Engine.reconcile_position(e, {"position_amt": 0.0, "open_order_ids": set()})
        self.assertEqual(sorted(e.book), ["ADAUSDT", "SOLUSDT"])

    def test_reconcile_position_never_assigns_to_active(self):
        tree = ast.parse(inspect.getsource(Engine.reconcile_position).lstrip())
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "active":
                        self.fail("reconcile_position wipes the whole book")

    def test_book_wide_reconcile_exists_and_uses_two_calls(self):
        src = inspect.getsource(Engine.reconcile_book)
        self.assertIn("self.api.positions()", src)
        self.assertIn("self.api.open_orders()", src)


class TestS4ShutdownCoversEverySymbol(unittest.TestCase):
    def test_every_held_symbol_is_handled(self):
        symbols = ["SOLUSDT", "ADAUSDT", "XRPUSDT"]
        api = MultiAPI(open_symbols=symbols)
        e = held(symbols, api=api)
        e.cfg.symbol = "DOGEUSDT"       # configured symbol is flat
        Engine.cancel_orders_safely(e, keep_protective=True)
        missed = [s for s in symbols if not api.touched(s)]
        self.assertEqual(missed, [], f"never looked at {missed}")

    def test_the_configured_symbol_is_included_too(self):
        api = MultiAPI()
        e = held(["AUSDT"], api=api)
        e.cfg.symbol = "DOGEUSDT"
        Engine.cancel_orders_safely(e, keep_protective=True)
        self.assertTrue(api.touched("DOGEUSDT"))


class TestS5CloseAllCountsTruthfully(unittest.TestCase):
    def test_a_refused_close_is_not_counted(self):
        symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = MultiAPI(open_symbols=symbols, fail_on=["BBBUSDT"])
        e = held(symbols, api=api)
        self.assertEqual(Engine.close_all(e, "qa"), 2)

    def test_the_failure_keeps_its_protection_and_its_tracking(self):
        symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = MultiAPI(open_symbols=symbols, fail_on=["BBBUSDT"])
        e = held(symbols, api=api)
        Engine.close_all(e, "qa")
        self.assertEqual(sorted(e.book), ["BBBUSDT"])
        self.assertIn("FAILED", " ".join(b for _, b in e.sent))

    def test_close_position_returns_a_boolean(self):
        api = MultiAPI(open_symbols=["AUSDT"])
        e = held(["AUSDT"], api=api)
        self.assertIs(Engine.close_position(e, "qa", symbol="AUSDT"), True)
        api2 = MultiAPI(open_symbols=["BUSDT"], fail_on=["BUSDT"])
        e2 = held(["BUSDT"], api=api2)
        self.assertIs(Engine.close_position(e2, "qa", symbol="BUSDT"), False)


class TestNoDuplicateMethods(unittest.TestCase):
    """
    A slice-based edit duplicated five methods, and Python binds the LAST
    definition -- so a fix sat in the file as dead code while the stale copy
    ran. Cheap to assert, expensive to miss.
    """

    def test_no_module_defines_a_method_twice(self):
        for path in sorted((ROOT / "bot").rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef):
                    continue
                names = collections.Counter(
                    n.name for n in node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    # property getter/setter legitimately share a name
                    and not any(isinstance(d, ast.Attribute) and d.attr == "setter"
                                for d in n.decorator_list))
                dupes = {n: c for n, c in names.items() if c > 1}
                self.assertEqual(dupes, {},
                                 f"{path.name}:{node.name} defines {dupes} twice")


class TestClockOffsetMeasurement(unittest.TestCase):
    """
    Reported drift crept 207 -> 768 ms across a day of QA runs, which looked
    like a failing clock. Most of it was measurement error: the offset was read
    as `server - local_before`, so about half the round trip counted as drift,
    and the correction then pushed timestamps into the future -- rejected just
    as readily as stale ones.
    """

    def _api(self, server_time, latency_ms):
        from bot.binanceapi import Binance
        api = Binance(testnet=True)
        clock = {"t": 1_000_000.0}

        def fake_request(method, path, params=None, signed=False):
            clock["t"] += latency_ms / 1000.0      # the round trip elapses
            return {"serverTime": server_time}

        api._request = fake_request
        import bot.binanceapi as mod
        self._real_time = mod.time.time
        mod.time.time = lambda: clock["t"]
        self.addCleanup(setattr, mod.time, "time", self._real_time)
        return api

    def test_round_trip_is_not_counted_as_drift(self):
        """Clocks agree exactly; a 400 ms round trip must not look like drift."""
        api = self._api(server_time=1_000_000_200, latency_ms=400)
        offset = api.sync_clock()
        self.assertLess(abs(offset), 50,
                        f"a perfectly synced clock reported {offset} ms of drift")

    def test_records_the_round_trip(self):
        api = self._api(server_time=1_000_000_200, latency_ms=400)
        api.sync_clock()
        self.assertAlmostEqual(api.last_rtt_ms, 400, delta=5)

    def test_a_genuine_offset_is_still_detected(self):
        api = self._api(server_time=1_000_002_200, latency_ms=400)
        self.assertAlmostEqual(api.sync_clock(), 2000, delta=50)

    def test_thresholds_sit_below_the_recv_window(self):
        from bot.binanceapi import Binance, RECV_WINDOW
        self.assertLess(Binance.DRIFT_WARN_MS, Binance.DRIFT_DANGER_MS)
        self.assertLess(Binance.DRIFT_DANGER_MS, RECV_WINDOW)

    def test_sync_time_is_recorded_for_the_resync_clock(self):
        api = self._api(server_time=1_000_000_200, latency_ms=100)
        api.sync_clock()
        self.assertGreater(api.last_sync, 0)


class TestPeriodicResync(unittest.TestCase):
    def test_engine_resyncs_on_a_schedule_not_only_at_startup(self):
        src = inspect.getsource(Engine.periodic)
        self.assertIn("resync_clock_if_due", src)

    def test_resync_is_skipped_while_recent(self):
        e = held([])
        e.api = type("A", (), {"last_sync": __import__("time").time(),
                               "_offset_ms": 0, "DRIFT_WARN_MS": 500,
                               "sync_clock": lambda s: self.fail("resynced too soon")})()
        Engine.resync_clock_if_due(e)

    def test_offset_is_published_for_the_control_surfaces(self):
        e = held(["AUSDT"])
        snap = Engine.snapshot(e)
        self.assertIn("clock_offset_ms", snap)
        self.assertIn("clock_rtt_ms", snap)


class TestClockCheckCannotBreakTheLoop(unittest.TestCase):
    """
    resync_clock_if_due runs inside periodic(), which is the reconciliation
    path. Reaching for attributes the API object may not have made a clock
    check able to stop the bot noticing that a position closed.
    """

    def test_an_api_without_clock_support_is_tolerated(self):
        e = held([])
        e.api = object()
        Engine.resync_clock_if_due(e)        # must not raise

    def test_a_failing_sync_does_not_propagate(self):
        e = held([])

        def boom():
            raise RuntimeError("network gone")

        e.api = type("A", (), {"sync_clock": staticmethod(boom),
                               "last_sync": 0, "_offset_ms": 0})()
        Engine.resync_clock_if_due(e)        # must not raise

    def test_periodic_survives_a_broken_clock_api(self):
        api = MultiAPI()
        e = held(["AUSDT"], api=api)
        e.api = api
        e._last_reconcile = 0                # force the periodic body to run
        Engine.periodic(e)                   # must not raise


class TestConfigAndScanDiagnostics(unittest.TestCase):
    """
    Reported: portfolio enabled in config.yaml, but /config said off and /scan
    did nothing. The config parsed correctly -- the running bot had loaded a
    different file, and neither command said which. /scan also could not
    distinguish "off" from "on, waiting for the first bar close".
    """

    def _engine(self, cfg):
        from bot.targets import TargetSchedule
        e = object.__new__(Engine)
        e.cfg = cfg
        e.scanner = None
        e.schedule = TargetSchedule.from_config(cfg.targets or {})
        e.schedule.start_date = "2026-09-05"
        return e

    def _cfg(self, **kw):
        from bot.config import Config, PortfolioConfig
        c = Config(**kw)
        if "portfolio" not in kw:
            c.portfolio = PortfolioConfig()
        return c

    def test_config_names_the_file_it_loaded(self):
        from bot.config import Config
        cfg = Config.load()
        summary = Engine._config_summary(self._engine(cfg))
        self.assertIn("config file", summary)
        self.assertTrue(summary["config file"].endswith(".yaml"))

    def test_config_file_is_the_first_line(self):
        """It is the answer to 'my edit did not apply', so it leads."""
        from bot.config import Config
        summary = Engine._config_summary(self._engine(Config.load()))
        self.assertEqual(next(iter(summary)), "config file")

    def test_portfolio_flag_is_reported_accurately(self):
        from bot.config import PortfolioConfig
        off = self._cfg(portfolio=PortfolioConfig(enabled=False))
        on = self._cfg(portfolio=PortfolioConfig(enabled=True))
        self.assertEqual(Engine._config_summary(self._engine(off))["portfolio"], "off")
        self.assertEqual(Engine._config_summary(self._engine(on))["portfolio"], "on")

    def test_scan_says_portfolio_is_off_rather_than_nothing(self):
        from bot.config import PortfolioConfig
        e = self._engine(self._cfg(portfolio=PortfolioConfig(enabled=False)))
        state = Engine._scan_summary(e)["state"]
        self.assertIn("portfolio mode is off", state)
        self.assertIn("portfolio.enabled: true", state)

    def test_scan_distinguishes_on_but_not_yet_scanned(self):
        from bot.config import PortfolioConfig
        e = self._engine(self._cfg(portfolio=PortfolioConfig(enabled=True)))
        e.scanner = type("S", (), {"last": None,
                                   "cfg": type("C", (), {"rescan_seconds": 300})()})()
        state = Engine._scan_summary(e)["state"]
        self.assertIn("no scan yet", state)
        self.assertNotIn("off", state)

    def test_a_real_result_carries_no_state_message(self):
        from bot.config import PortfolioConfig
        from bot.scanner import Candidate, ScanResult
        e = self._engine(self._cfg(portfolio=PortfolioConfig(enabled=True)))
        res = ScanResult(considered=100)
        res.ranked = [Candidate("AUSDT", 1.0, 1e8, 0.5, 1.0, 5.0, score=0.9)]
        e.scanner = type("S", (), {"last": res,
                                   "cfg": type("C", (), {"rescan_seconds": 300})()})()
        summary = Engine._scan_summary(e)
        self.assertNotIn("state", summary)
        self.assertEqual(summary["passed"], 1)

    def test_startup_scans_once_immediately(self):
        """Waiting for the first bar close looked exactly like a broken scanner."""
        src = inspect.getsource(Engine.startup)
        block = src.split("portfolio mode ON")[1][:800]
        self.assertIn("self.scanner.scan(", block)
        self.assertIn("initial scan failed", block,
                      "a failed initial scan must not stop startup")


class TestForcedScan(unittest.TestCase):
    """
    /scan reports the last result. Asking for a fresh one has to go through the
    command queue: the Telegram thread never calls the exchange, and a scan is
    ~101 REST calls that would block the poller for a minute.
    """

    class Queue:
        def __init__(self, cmd=None):
            self.c = [cmd] if cmd else []

        def pop_commands(self):
            out, self.c = self.c, []
            return out

        def publish(self, snap):
            pass

        def stop(self):
            pass

    def _engine(self, enabled=True, last=None):
        from bot.config import PortfolioConfig
        e = held([])
        e.cfg.portfolio = PortfolioConfig(enabled=enabled)
        e.equity = 43.0
        e.rules_for = lambda s: None
        e.scanner = None
        if enabled:
            e.scanner = type("S", (), {
                "last": last,
                "cfg": type("C", (), {"max_symbols": 100, "rescan_seconds": 300})(),
                "scan": lambda self_, **kw: last})()
        return e

    def test_telegram_queues_rather_than_scanning_itself(self):
        import inspect
        from bot import telegram_control
        src = inspect.getsource(telegram_control.TelegramControl._scan)
        self.assertIn('Command("scan"', src)
        self.assertNotIn("klines", src)
        self.assertNotIn("ticker_24hr", src)

    def test_engine_executes_a_queued_scan(self):
        import inspect
        from bot.engine import Engine
        self.assertIn('cmd.action == "scan"',
                      inspect.getsource(Engine.process_commands))

    def test_refuses_when_portfolio_is_off(self):
        e = self._engine(enabled=False)
        msg = Engine.run_scan_now(e)
        self.assertIn("Portfolio mode is off", msg)

    def test_rate_limited_against_spamming(self):
        import time as t
        from bot.scanner import ScanResult
        recent = ScanResult(considered=100)
        recent.scanned_at = t.time()
        e = self._engine(last=recent)
        msg = Engine.run_scan_now(e)
        self.assertIn("limited to once every", msg)

    def test_an_old_result_does_not_block_a_rescan(self):
        import time as t
        from bot.scanner import ScanResult
        old = ScanResult(considered=100)
        old.scanned_at = t.time() - 3600
        e = self._engine(last=old)
        msg = Engine.run_scan_now(e)
        self.assertNotIn("limited to once every", msg)

    def test_a_failing_scan_reports_rather_than_raises(self):
        from bot.config import PortfolioConfig
        e = held([])
        e.cfg.portfolio = PortfolioConfig(enabled=True)
        e.equity = 43.0
        e.rules_for = lambda s: None

        def boom(**kw):
            raise RuntimeError("exchange unreachable")

        e.scanner = type("S", (), {
            "last": None,
            "cfg": type("C", (), {"max_symbols": 100, "rescan_seconds": 300})(),
            "scan": staticmethod(boom)})()
        msg = Engine.run_scan_now(e)
        self.assertIn("scan failed", msg)

    def test_help_documents_both_forms(self):
        from bot.telegram_control import HELP
        self.assertIn("/scan now", HELP)


class TestDryRunIsNeverPresentedAsReal(unittest.TestCase):
    """
    A live report showed "+24.49 realized_today" beside "0 fills, equity
    unchanged". Both were correct -- dry run books simulated P&L locally and
    sends nothing -- but the report never said so, and read as money made.
    """

    def _dry(self, **kw):
        from bot import report as rp
        data = {"symbol": "DOGEUSDT", "days": 1, "generated": "x", "equity": 5000.0,
                "mode": "testnet", "dry_run": True,
                "state": {"realized_today": 24.49, "trades_today": 10,
                          "total_trades": 4}}
        data.update(kw)
        return rp.render(data)

    def test_dry_run_is_announced_before_any_number(self):
        out = self._dry()
        self.assertIn("DRY RUN", out)
        self.assertLess(out.index("DRY RUN"), out.index("EQUITY NOW"))

    def test_local_pnl_is_labelled_simulated(self):
        out = self._dry()
        self.assertIn("SIMULATED", out)
        self.assertIn("P&L today (SIMULATED)", out)

    def test_it_says_the_balance_has_not_moved(self):
        self.assertIn("has not moved", self._dry())

    def test_reconciliation_is_skipped_with_a_reason(self):
        out = self._dry()
        self.assertIn("RECONCILIATION  skipped", out)
        self.assertIn("dry_run: false", out)

    def test_a_live_report_carries_no_simulated_labels(self):
        from bot import report as rp
        out = rp.render({"symbol": "X", "days": 1, "generated": "x",
                         "equity": 100.0, "mode": "testnet", "dry_run": False,
                         "state": {"total_trades": 0}})
        self.assertNotIn("SIMULATED", out)
        self.assertNotIn("DRY RUN", out)

    def test_the_two_trade_counters_are_explained(self):
        out = self._dry()
        self.assertIn("entries submitted", out)
        self.assertIn("entries that filled", out)


class TestEquityCap(unittest.TestCase):
    """
    Testnet hands out 5,000 USDT. Sizing against it rehearses an account you do
    not have, and hides the minimum-order constraint that governs a small one.
    """

    def _engine(self, cap):
        from bot.config import Config, RiskConfig
        e = object.__new__(Engine)
        e.cfg = Config(risk=RiskConfig(equity_cap_usdt=cap))
        return e

    def test_zero_means_use_the_real_balance(self):
        self.assertEqual(Engine.effective_equity(self._engine(0.0), 5000.0), 5000.0)

    def test_a_cap_is_applied(self):
        self.assertEqual(Engine.effective_equity(self._engine(100.0), 5000.0), 100.0)

    def test_a_cap_never_inflates_a_smaller_balance(self):
        """If the account really holds $43, the cap must not pretend it is $100."""
        self.assertEqual(Engine.effective_equity(self._engine(100.0), 43.0), 43.0)

    def test_the_cap_changes_position_sizing(self):
        from bot.portfolio import allocate
        big = allocate(5000.0, 100).notional_per_position
        small = allocate(100.0, 100).notional_per_position
        self.assertGreater(big, small * 10)

    def test_config_reports_when_equity_is_capped(self):
        src = inspect.getsource(Engine._config_summary)
        self.assertIn("capped from", src)

    def test_snapshot_exposes_both_figures(self):
        src = inspect.getsource(Engine.snapshot)
        self.assertIn('"actual_equity"', src)
        self.assertIn('"equity_capped"', src)
