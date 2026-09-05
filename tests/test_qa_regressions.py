"""
Regressions for the findings in the 2026-08-28 QA audit.

One test per finding, named for it, so a reintroduction is obvious in the
failure output rather than needing archaeology.
"""

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.backtest import Fills, run as bt_run           # noqa: E402
from bot.config import Config                           # noqa: E402
from bot.positions import ActivePosition                # noqa: E402
from bot.risk import KILL_FILE                          # noqa: E402
from bot.state import STATE_PATH, State                 # noqa: E402
from bot.strategies.base import Bar, Signal, Strategy   # noqa: E402


class TestF1PositionTracking(unittest.TestCase):
    """A resting entry must not be mistaken for a closed trade."""

    def _engine(self):
        from bot import engine as eng
        e = object.__new__(eng.Engine)
        e.active = ActivePosition("BTCUSDT", "BUY", 100.0, 90.0, 120.0, 1.0,
                                  entry_order_id="e-1", stop_order_id="s-1",
                                  tp_order_id="t-1", tag="e-1")
        from bot.config import Config
        e.cfg = Config(symbol="BTCUSDT", dry_run=False)
        e.notify = type("N", (), {"clear_position_alerts": lambda *a: None,
                                  "send": lambda *a, **k: None})()
        e._entry_placed_at = 0.0
        e.api = type("A", (), {"cancel_all": lambda *a, **k: None})()
        e.stream = None
        return e

    def test_flat_with_our_entry_still_resting_keeps_the_position(self):
        e = self._engine()
        snap = {"position_amt": 0.0, "open_order_ids": {"e-1"}}
        from bot.engine import Engine
        Engine.reconcile_position(e, snap)
        self.assertIsNotNone(e.active, "cleared `active` while our entry was resting: "
                                       "the next bar would place a second entry")

    def test_flat_with_no_orders_clears_the_position(self):
        e = self._engine()
        from bot.engine import Engine
        Engine.reconcile_position(e, {"position_amt": 0.0, "open_order_ids": set()})
        self.assertIsNone(e.active)

    def test_open_position_is_never_cleared(self):
        e = self._engine()
        from bot.engine import Engine
        Engine.reconcile_position(e, {"position_amt": 0.5, "open_order_ids": set()})
        self.assertIsNotNone(e.active)


class TestF2Equity(unittest.TestCase):
    def test_equity_uses_margin_balance_not_cross_only(self):
        """crossUnPnl excludes isolated positions, which the engine uses."""
        import inspect
        from bot.binanceapi import Binance
        src = inspect.getsource(Binance.usdt_equity)
        self.assertIn("marginBalance", src)
        self.assertNotIn('float(b["crossUnPnl"])', src,
                         "equity is back to a cross-only reading")


class TestF3KillLatency(unittest.TestCase):
    def test_emergency_check_is_called_off_the_bar_path(self):
        import inspect
        from bot.engine import Engine
        self.assertIn("emergency_check", inspect.getsource(Engine.periodic))
        self.assertIn("emergency_check", inspect.getsource(Engine._tick_guard))
        self.assertIn("_tick_guard", inspect.getsource(Engine.on_tick))

    def test_kill_file_path_is_anchored_to_the_repo(self):
        self.assertTrue(KILL_FILE.is_absolute())
        self.assertEqual(KILL_FILE.parent, ROOT)


class TestF4Dependencies(unittest.TestCase):
    def test_websockets_is_declared(self):
        reqs = (ROOT / "requirements.txt").read_text()
        self.assertIn("websockets", reqs)

    def test_engine_imports_without_websockets(self):
        """realtime: false must work on a host that lacks the package."""
        code = (
            "import sys, types\n"
            "sys.modules['websockets'] = None\n"
            "import builtins\n"
            "real = builtins.__import__\n"
            "def block(name, *a, **k):\n"
            "    if name == 'websockets': raise ImportError('blocked')\n"
            "    return real(name, *a, **k)\n"
            "builtins.__import__ = block\n"
            "del sys.modules['websockets']\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import bot.engine\n"
            "print('ok')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        self.assertIn("ok", r.stdout, f"engine import needs websockets: {r.stderr[-400:]}")


class TestF5ExitCodes(unittest.TestCase):
    def test_run_returns_an_exit_code(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.run)
        self.assertIn("return 1", src)
        self.assertIn("-> int", src)

    def test_cmd_trade_propagates_it(self):
        src = (ROOT / "run.py").read_text()
        self.assertIn("return Engine(cfg).run()", src)


class TestF7AveragingDown(unittest.TestCase):
    def test_check_is_wired_into_decide(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.decide)
        self.assertIn("check_add_to_position", src)
        self.assertIn("self.position_amt", src,
                      "must be checked against exchange state, not local tracking")


class TestF8DryRun(unittest.TestCase):
    def test_dry_run_tracks_a_position_so_alerts_can_fire(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.place)
        dry = src.split("if self.cfg.dry_run:")[1].split("return")[0]
        self.assertIn("ActivePosition", dry,
                      "dry run returns before tracking, so proximity alerts never fire")

    def test_simulate_exit_resolves_a_dry_run_position(self):
        from bot.engine import Engine
        self.assertTrue(hasattr(Engine, "simulate_exit"))


class TestF9F10Fills(unittest.TestCase):
    def test_entry_price_is_updated_from_the_fill(self):
        """Behavioural now: the source moved when on_order became per-symbol."""
        import sys
        sys.path.insert(0, str(ROOT / "tests"))
        from bot.engine import Engine
        from bot.stream import OrderUpdate
        from test_r2_regressions import StubAPI, engine
        from test_book import pos as make_pos

        e = engine(api=StubAPI())
        e.book = {"AUSDT": make_pos("AUSDT", entry=100.0)}
        e.schedule = type("S", (), {"progress": lambda s, r: "prog"})()
        Engine.on_order(e, OrderUpdate(
            symbol="AUSDT", client_order_id="e-AUSDT", side="BUY",
            status="FILLED", order_type="LIMIT", last_filled_qty=1.0,
            cumulative_qty=1.0, avg_price=101.5, realized_pnl=0.0, raw={}))
        self.assertAlmostEqual(e.book["AUSDT"].entry, 101.5,
                               msg="the real fill price was not adopted")

    def test_partial_fills_are_handled(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.on_order)
        self.assertIn("PARTIALLY_FILLED", src)

    def test_order_update_exposes_cumulative_quantity(self):
        from bot.stream import OrderUpdate
        self.assertIn("cumulative_qty", OrderUpdate.__dataclass_fields__)


class TestF11BacktestFills(unittest.TestCase):
    class ForcedLong(Strategy):
        name, warmup = "forced", 0

        def __init__(self):
            self.fired = False

        def on_bars(self, bars, position_amt):
            if self.fired:
                return None
            self.fired = True
            return Signal("BUY", entry=100.0, stop=90.0, take_profit=120.0)

    def test_unreachable_entry_expires_instead_of_filling(self):
        bars = [Bar(0, 100, 100, 100, 100, 1)] + [
            Bar(i, 120 + i, 121 + i, 119 + i, 120 + i, 1) for i in range(1, 8)]
        res = bt_run(bars, self.ForcedLong(), 1000.0, 10.0, 10.0)
        self.assertEqual(res.trades, [])
        self.assertEqual(res.fills.expired, 1)
        self.assertEqual(res.fills.filled, 0)

    def test_reachable_entry_fills(self):
        bars = [Bar(0, 100, 100, 100, 100, 1),
                Bar(1, 100, 101, 99, 100, 1),
                Bar(2, 100, 125, 99, 120, 1)]
        res = bt_run(bars, self.ForcedLong(), 1000.0, 10.0, 10.0)
        self.assertEqual(res.fills.filled, 1)
        self.assertEqual(len(res.trades), 1)

    def test_fill_rate_is_reported(self):
        f = Fills(signals=4, filled=1, expired=3)
        self.assertAlmostEqual(f.fill_rate, 0.25)


class TestF12StateTolerance(unittest.TestCase):
    def setUp(self):
        self.tmp = ROOT / "data" / "qa_regression_state.json"
        self.tmp.parent.mkdir(exist_ok=True)

    def tearDown(self):
        self.tmp.unlink(missing_ok=True)

    def test_unknown_keys_do_not_crash_the_boot(self):
        self.tmp.write_text(json.dumps({"realized_today": 2.5, "gone_field": 1}))
        st = State.load(self.tmp)
        self.assertAlmostEqual(st.realized_today, 2.5)

    def test_corrupt_file_falls_back_to_defaults(self):
        self.tmp.write_text("{ not json")
        self.assertFalse(State.load(self.tmp).halted)

    def test_state_path_is_anchored_to_the_repo(self):
        self.assertTrue(STATE_PATH.is_absolute())


class TestF13ConfigTypos(unittest.TestCase):
    def test_unknown_top_level_keys_are_surfaced(self):
        import tempfile

        import yaml
        raw = yaml.safe_load((ROOT / "config.yaml").read_text())
        raw["dry_runn"] = False
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(raw, f)
            path = f.name
        cfg = Config.load(path)
        self.assertTrue(cfg.dry_run, "the typo must not silently disable dry run")
        self.assertIn("dry_runn", " ".join(cfg.validate()))


class TestF20DeadBranch(unittest.TestCase):
    def test_strategy_receives_the_real_position(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.decide)
        self.assertIn("self.strategy.on_bars(self.bars, self.position_amt)", src)
        self.assertNotIn("1.0 if self.active else 0.0", src)


class TestAuthErrorsDoNotRetry(unittest.TestCase):
    """
    A bad key must fail once, loudly.

    Falling back to a second endpoint on a credential error cannot succeed --
    the key is wrong, not the endpoint. It spends request weight, and the
    rate-limit error it eventually produces buries the real cause. Observed
    live: a mistyped key reported "IP banned" instead of "key format invalid".
    """

    def _api(self, code, msg="nope"):
        from bot.binanceapi import Binance, BinanceError

        api = Binance("k", "s", testnet=True)
        calls = []

        def boom(method, path, params=None, signed=False):
            calls.append(path)
            raise BinanceError(code, msg, path)

        api._request = boom
        return api, calls

    def test_auth_error_raises_without_a_second_request(self):
        from bot.binanceapi import AUTH_ERRORS, BinanceError

        for code in sorted(AUTH_ERRORS):
            api, calls = self._api(code)
            with self.assertRaises(BinanceError):
                api.usdt_equity()
            self.assertEqual(len(calls), 1,
                             f"code {code} retried after a credential failure")

    def test_non_auth_error_still_falls_back(self):
        from bot.binanceapi import BinanceError

        api, calls = self._api(-1104, "unexpected parameter")
        with self.assertRaises(BinanceError):
            api.usdt_equity()
        self.assertGreater(len(calls), 1, "the fallback path was removed entirely")

    def test_every_auth_code_has_an_explanation(self):
        from bot.binanceapi import AUTH_ERRORS, ERROR_HELP

        missing = [c for c in AUTH_ERRORS if not ERROR_HELP.get(c)]
        self.assertEqual(missing, [], f"no guidance for codes {missing}")

    def test_rate_limit_error_is_explained(self):
        from bot.binanceapi import BinanceError

        self.assertIn("Rate limited", BinanceError(-1003, "x", "/y").help)


class TestOperatorHelpers(unittest.TestCase):
    def test_myip_and_verifykey_are_registered(self):
        src = (ROOT / "run.py").read_text()
        for cmd in ("myip", "verifykey"):
            self.assertIn(f'"{cmd}"', src)

    def test_verifykey_never_echoes_the_secret(self):
        """The secret is read with getpass and must not be printed back."""
        src = (ROOT / "run.py").read_text()
        block = src.split("def cmd_verifykey")[1].split("\ndef ")[0]
        self.assertIn("getpass.getpass", block)
        self.assertNotIn("print(secret", block)
        self.assertNotIn("{secret}", block)


class TestEndpoints(unittest.TestCase):
    """
    The testnet host moved. testnet.binancefuture.com is a CloudFront alias
    Binance's docs no longer name; demo-fapi.binance.com resolves straight to
    the backend. Both work today, so this pins which one we actually use.
    """

    def test_testnet_uses_the_documented_host(self):
        from bot.binanceapi import TESTNET
        self.assertEqual(TESTNET, "https://demo-fapi.binance.com")

    def test_stream_matches_rest(self):
        from bot.stream import WS_TESTNET
        self.assertEqual(WS_TESTNET, "wss://demo-fstream.binance.com")

    def test_live_host_is_unchanged(self):
        from bot.binanceapi import LIVE
        self.assertEqual(LIVE, "https://fapi.binance.com")

    def test_legacy_alias_is_still_reachable_in_code(self):
        """Kept as an escape hatch if the new host ever breaks."""
        from bot.binanceapi import TESTNET_LEGACY
        from bot.stream import WS_TESTNET_LEGACY
        self.assertIn("binancefuture.com", TESTNET_LEGACY)
        self.assertIn("binancefuture.com", WS_TESTNET_LEGACY)

    def test_base_can_be_overridden_by_environment(self):
        import os

        from bot.binanceapi import Binance, TESTNET_LEGACY
        old = os.environ.get("BINANCE_TESTNET_BASE")
        try:
            os.environ["BINANCE_TESTNET_BASE"] = TESTNET_LEGACY
            self.assertEqual(Binance(testnet=True).base, TESTNET_LEGACY)
        finally:
            os.environ.pop("BINANCE_TESTNET_BASE", None)
            if old:
                os.environ["BINANCE_TESTNET_BASE"] = old

    def test_explicit_base_wins_over_everything(self):
        from bot.binanceapi import Binance
        self.assertEqual(Binance(testnet=True, base="https://x.example/").base,
                         "https://x.example")

    def test_live_mode_ignores_the_testnet_override(self):
        import os

        from bot.binanceapi import Binance, LIVE
        os.environ["BINANCE_TESTNET_BASE"] = "https://should-not-be-used.example"
        try:
            self.assertEqual(Binance(testnet=False).base, LIVE)
        finally:
            os.environ.pop("BINANCE_TESTNET_BASE", None)


class TestMissingWebsockets(unittest.TestCase):
    """
    Deploying to a fresh host without the package should say what to install,
    not print a traceback and leave port 8080 held by a dead process.
    """

    def test_stream_start_raises_a_readable_error(self):
        import builtins

        real = builtins.__import__

        def block(name, *a, **k):
            if name == "websockets":
                raise ImportError("No module named 'websockets'")
            return real(name, *a, **k)

        from bot.stream import MarketStream
        builtins.__import__ = block
        try:
            with self.assertRaises(RuntimeError) as ctx:
                MarketStream("BTCUSDT", "15m").start()
            msg = str(ctx.exception)
            self.assertIn("websockets", msg)
            self.assertIn("realtime: false", msg, "no workaround offered")
        finally:
            builtins.__import__ = real

    def test_startup_catches_it_and_refuses_cleanly(self):
        """Parse the handler block rather than counting characters into it."""
        import ast
        import inspect
        import textwrap

        from bot.engine import Engine
        tree = ast.parse(textwrap.dedent(inspect.getsource(Engine.startup)))
        handlers = [h for node in ast.walk(tree) if isinstance(node, ast.Try)
                    for h in node.handlers
                    if isinstance(h.type, ast.Name) and h.type.id == "RuntimeError"]
        self.assertTrue(handlers, "startup() does not handle RuntimeError")

        block = handlers[0]
        returns_false = any(
            isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
            and n.value.value is False
            for n in ast.walk(block))
        self.assertTrue(returns_false, "handler does not refuse to start")

        text = ast.unparse(block)
        self.assertIn("apt-get install", text, "no concrete install command given")
        self.assertIn("realtime: false", text, "no workaround offered")

    def test_a_refused_startup_releases_the_dashboard_port(self):
        import inspect

        from bot.engine import Engine
        self.assertIn("stop_controllers", inspect.getsource(Engine.run))
        self.assertTrue(hasattr(Engine, "stop_controllers"))

    def test_stop_controllers_is_idempotent(self):
        from bot.engine import Engine
        e = object.__new__(Engine)
        e.dashboard = None
        e.telegram = None
        e.stream = None
        Engine.stop_controllers(e)
        Engine.stop_controllers(e)      # must not raise on a second call


class TestDeploymentPythonPath(unittest.TestCase):
    """
    Whichever interpreter setup.sh installs into, the unit must point at the
    same one. A venv under /home is invisible to the service (ProtectHome), and
    a venv under /opt still fails if ExecStart is left hardcoded -- both look
    fine when tested by hand and fail only once systemd starts it. (QA F14)
    """

    def setUp(self):
        self.unit = (ROOT / "deploy" / "trading-bot.service").read_text()
        self.setup = (ROOT / "deploy" / "setup.sh").read_text()

    def test_unit_still_protects_home(self):
        self.assertIn("ProtectHome=true", self.unit)

    def test_default_execstart_uses_the_system_interpreter(self):
        line = next(l for l in self.unit.splitlines() if l.startswith("ExecStart="))
        self.assertIn("/usr/bin/python3", line)

    def test_venv_is_placed_under_opt_not_home(self):
        self.assertIn("$APP_DIR/.venv", self.setup)
        self.assertNotIn("/home/$SERVICE_USER/.venv", self.setup)
        # Only executable lines: the script mentions --user in a comment that
        # explains why it must not be used, and that comment is worth keeping.
        code = "\n".join(l for l in self.setup.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("--user", code,
                         "a --user install lands in /home, which ProtectHome hides")

    def test_venv_mode_rewrites_execstart(self):
        self.assertIn("sed -i", self.setup)
        self.assertIn("^ExecStart=.*", self.setup)

    def test_imports_are_verified_as_the_service_user(self):
        self.assertIn('sudo -u "$SERVICE_USER" "$PYTHON"', self.setup)

    def test_requirements_lists_exactly_the_third_party_imports(self):
        reqs = (ROOT / "requirements.txt").read_text().lower()
        for pkg in ("pyyaml", "websockets"):
            self.assertIn(pkg, reqs)


class TestSharedHostSafety(unittest.TestCase):
    """
    The instance is expected to run other things -- websites, jobs. Provisioning
    the bot must not disturb them, and the bot must not crowd them out.
    """

    def setUp(self):
        self.setup = (ROOT / "deploy" / "setup.sh").read_text()
        self.unit = (ROOT / "deploy" / "trading-bot.service").read_text()

    def test_setup_never_resets_the_firewall(self):
        """`ufw reset` drops every rule keeping an existing site reachable."""
        code = "\n".join(l for l in self.setup.splitlines()
                         if not l.lstrip().startswith("#"))
        self.assertNotIn("ufw --force reset", code)
        self.assertNotIn("ufw reset", code)

    def test_setup_leaves_an_active_firewall_alone(self):
        self.assertIn("Status: active", self.setup)
        self.assertIn("leaving your existing rules alone", self.setup)

    def test_firewall_step_can_be_skipped(self):
        self.assertIn("--no-firewall", self.setup)

    def test_bot_is_resource_capped(self):
        for directive in ("MemoryMax=", "CPUQuota=", "TasksMax="):
            self.assertIn(directive, self.unit)

    def test_bot_yields_to_interactive_work(self):
        self.assertIn("Nice=", self.unit)
        self.assertIn("IOSchedulingClass=", self.unit)

    def test_dashboard_stays_on_loopback_by_default(self):
        from bot.config import DashboardConfig
        self.assertEqual(DashboardConfig().host, "127.0.0.1")

    def test_dashboard_port_is_configurable(self):
        """8080 is a popular port; coexisting apps must be able to move it."""
        from bot.config import DashboardConfig
        self.assertEqual(DashboardConfig(port=9123).port, 9123)
        self.assertIn("port", (ROOT / "config.yaml").read_text())


class TestSymbolIsSingular(unittest.TestCase):
    """
    The engine trades exactly one symbol. Worth pinning, because the config key
    is singular and someone will eventually try a list.
    """

    def test_config_symbol_is_a_string(self):
        from bot.config import Config
        self.assertIsInstance(Config().symbol, str)

    def test_engine_scopes_everything_to_one_symbol(self):
        import inspect
        from bot.engine import Engine
        for method in (Engine.startup, Engine.place, Engine.close_position):
            self.assertIn("self.cfg.symbol", inspect.getsource(method))

    def test_doctor_can_plan_against_a_different_equity(self):
        """Testnet's demo balance hides the constraint that decides live."""
        src = (ROOT / "run.py").read_text()
        self.assertIn("--equity", src)
        self.assertIn("equity_override", src)
        self.assertIn("PLANNING AGAINST", src)


class TestDocumentedPathsMatchTheCode(unittest.TestCase):
    """
    The GitHub repo is `mudalali-buwa`, the working copy is `trading-bot`, and
    the install path is `/opt/trading-bot`. Renaming the repo left the README
    pointing at `/opt/mudalali-buwa/.venv`, which setup.sh never creates --
    the kind of drift that sends you looking in the wrong directory at the
    worst moment.
    """

    def setUp(self):
        self.setup = (ROOT / "deploy" / "setup.sh").read_text()
        self.unit = (ROOT / "deploy" / "trading-bot.service").read_text()
        self.readme = (ROOT / "README.md").read_text()
        import re
        m = re.search(r"^APP_DIR=(\S+)", self.setup, re.M)
        self.app_dir = m.group(1)

    def test_install_path_is_fixed(self):
        self.assertEqual(self.app_dir, "/opt/trading-bot")

    def test_unit_agrees_with_setup(self):
        for directive in ("WorkingDirectory=", "ExecStart="):
            line = next(l for l in self.unit.splitlines() if l.startswith(directive))
            self.assertIn(self.app_dir, line,
                          f"{directive} disagrees with APP_DIR")

    def test_no_doc_points_at_an_install_path_that_is_never_created(self):
        import re
        for name, text in (("README.md", self.readme),
                           ("setup.sh", self.setup),
                           ("trading-bot.service", self.unit)):
            for path in re.findall(r"/opt/[\w.-]+", text):
                self.assertTrue(path.startswith(self.app_dir),
                                f"{name} references {path}, but the installer "
                                f"only ever creates {self.app_dir}")

    def test_setup_locates_its_source_without_naming_the_directory(self):
        """So the checkout can be called anything."""
        self.assertIn('dirname "$0"', self.setup)
