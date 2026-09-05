#!/usr/bin/env python3
"""
The regression harness. One command, run it before every deploy.

    /usr/bin/python3 tools/qa.py                 everything
    /usr/bin/python3 tools/qa.py --offline       skip anything touching the network
    /usr/bin/python3 tools/qa.py --quick         skip the slow market-data checks
    /usr/bin/python3 tools/qa.py --only audit    run one layer
    /usr/bin/python3 tools/qa.py -v              show output from failing checks

Six layers, cheapest first, so a broken environment fails in two seconds
rather than four minutes:

    env      interpreter, dependencies, imports
    static   every module compiles; config and state load
    unit     the project's own suite (tests/)
    audit    behavioural probes for defects the suite does NOT cover
    cli      every documented command's exit code
    net      live endpoints, market data, dashboard over real HTTP

The `audit` layer is the point of this file. Unit tests check the code the
author thought about; these probes drive the engine against a stubbed exchange
and assert the safety properties the README promises. A green suite with a red
audit layer means the tests and the documentation disagree.

Exit code is 0 only when nothing failed. Skips do not fail the run.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import json
import pathlib
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.request
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PY = sys.executable
TMP = ROOT / "data" / "qa_tmp"

LAYERS = ["env", "static", "unit", "audit", "cli", "net"]

# ---------------------------------------------------------------- reporting
PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
COLOR = {PASS: "\033[32m", FAIL: "\033[31m", SKIP: "\033[33m"}
RESET = "\033[0m"


class Report:
    def __init__(self, verbose: bool):
        self.rows: list[tuple[str, str, str, str, str]] = []
        self.verbose = verbose
        self._tty = sys.stdout.isatty()

    def add(self, layer, name, status, note="", detail="", severity=""):
        self.rows.append((layer, name, status, note, severity))
        tag = f"{COLOR[status]}{status}{RESET}" if self._tty else status
        sev = f" [{severity}]" if severity and status == FAIL else ""
        print(f"  {tag}  {name}{sev}" + (f"  — {note}" if note else ""))
        if detail and (self.verbose or status == FAIL):
            for line in detail.rstrip().splitlines():
                print(f"          {line}")

    def counts(self, status):
        return sum(1 for r in self.rows if r[2] == status)

    def summary(self) -> int:
        failed = [r for r in self.rows if r[2] == FAIL]
        print("\n" + "=" * 72)
        print(f"  {self.counts(PASS)} passed   {len(failed)} failed   "
              f"{self.counts(SKIP)} skipped")
        if failed:
            print("\n  FAILED:")
            for layer, name, _, note, sev in failed:
                print(f"    [{layer}] {name}" + (f"  ({sev})" if sev else ""))
                if note:
                    print(f"        {note}")
        print("=" * 72 + "\n")
        return 1 if failed else 0



def _env_has(name: str) -> bool:
    """True when `name` is set in the environment or in the repo's .env."""
    import os
    if os.environ.get(name):
        return True
    env = ROOT / ".env"
    if not env.exists():
        return False
    for line in env.read_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == name and v.strip().strip("'\""):
            return True
    return False

def header(text: str) -> None:
    print(f"\n{text}\n" + "-" * 72)


def check(report, layer, name, fn, severity=""):
    """Run fn(); it returns (ok, note) or raises. Exceptions are failures."""
    try:
        ok, note = fn()
    except Exception:
        report.add(layer, name, FAIL, "raised an exception",
                   traceback.format_exc(), severity)
        return
    report.add(layer, name, PASS if ok else FAIL, note, severity=severity)


# ==================================================================== env
def layer_env(report, args):
    header("ENV")

    def interpreter():
        v = sys.version_info
        ok = v >= (3, 10)
        return ok, f"{sys.executable} — Python {v.major}.{v.minor}.{v.micro}"

    def ssl_works():
        import ssl
        return True, f"{ssl.OPENSSL_VERSION}"

    def deps():
        missing, found = [], []
        for mod in ("yaml", "websockets"):
            try:
                m = __import__(mod)
                found.append(f"{mod} {getattr(m, '__version__', '?')}")
            except ImportError:
                missing.append(mod)
        if missing:
            return False, f"missing: {', '.join(missing)} — pip install -r requirements.txt"
        return True, ", ".join(found)

    def imports():
        mods = ["bot.config", "bot.engine", "bot.risk", "bot.filters", "bot.state",
                "bot.dashboard", "bot.telegram_control", "bot.notify", "bot.stream",
                "bot.backtest", "bot.targets", "bot.positions", "bot.strategies"]
        for m in mods:
            __import__(m)
        return True, f"{len(mods)} modules import cleanly"

    check(report, "env", "interpreter is 3.10+", interpreter)
    check(report, "env", "ssl is usable (Binance is https-only)", ssl_works)
    check(report, "env", "third-party dependencies present", deps)
    check(report, "env", "every bot module imports", imports)


# ================================================================= static
def layer_static(report, args):
    header("STATIC")

    def compiles():
        r = subprocess.run([PY, "-m", "compileall", "-q", "bot", "tools", "tests",
                            "run.py"], cwd=ROOT, capture_output=True, text=True)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:200] or "all files compile"

    def config_loads():
        from bot.config import Config
        cfg = Config.load(ROOT / "config.yaml")
        if cfg.unknown_keys:
            return False, f"config.yaml has unrecognised keys: {cfg.unknown_keys}"
        return True, (f"mode={cfg.mode} dry_run={cfg.dry_run} symbol={cfg.symbol} "
                      f"strategy={cfg.strategy}")

    def shipped_config_is_safe():
        """The repo must never ship armed. This is the last line of defence."""
        from bot.config import Config
        cfg = Config.load(ROOT / "config.yaml")
        problems = []
        if not cfg.dry_run:
            problems.append("dry_run is FALSE in the committed config")
        if cfg.mode == "live":
            problems.append("mode is LIVE in the committed config")
        if cfg.risk.max_leverage > 5:
            problems.append(f"max_leverage={cfg.risk.max_leverage}")
        if cfg.risk.allow_averaging_down:
            problems.append("allow_averaging_down is True")
        if cfg.dashboard.enabled and cfg.dashboard.host not in ("127.0.0.1", "localhost"):
            problems.append(f"dashboard bound to {cfg.dashboard.host}")
        return not problems, "; ".join(problems) or "ships in dry-run testnet, limits sane"

    def state_round_trips():
        from bot.state import State
        TMP.mkdir(parents=True, exist_ok=True)
        p = TMP / "state_roundtrip.json"
        p.unlink(missing_ok=True)
        s = State(path=p)
        s.realized_today = 1.23
        s.save()
        again = State.load(p)
        p.unlink(missing_ok=True)
        return abs(again.realized_today - 1.23) < 1e-9, "save/load preserves the day's P&L"

    def requirements_match_imports():
        req = (ROOT / "requirements.txt").read_text().lower()
        needed = {"pyyaml": "yaml", "websockets": "websockets"}
        missing = [pkg for pkg in needed if pkg not in req]
        return not missing, (f"undeclared: {missing}" if missing
                             else "declares PyYAML and websockets")

    check(report, "static", "all sources compile", compiles)
    check(report, "static", "config.yaml parses with no unknown keys", config_loads)
    check(report, "static", "committed config is not armed", shipped_config_is_safe,
          severity="blocker")
    check(report, "static", "state file round-trips", state_round_trips)
    check(report, "static", "requirements.txt covers real imports", requirements_match_imports)


# =================================================================== unit
def layer_unit(report, args):
    header("UNIT")
    t0 = time.time()
    r = subprocess.run([PY, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=ROOT, capture_output=True, text=True)
    out = r.stderr + r.stdout
    ran = "?"
    for line in out.splitlines():
        if line.startswith("Ran "):
            ran = line.split()[1]
    report.add("unit", f"project suite ({ran} tests)",
               PASS if r.returncode == 0 else FAIL,
               f"{time.time() - t0:.1f}s",
               detail="" if r.returncode == 0 else out[-3000:])


# ================================================================== audit
# Probes for defects the project's own suite does not cover. Each one drives
# real engine code against a stub; none of them touch the network.

def _engine(dry_run=False, api=None, position_amt=0.0, active=None, controllers=()):
    """A live Engine with every collaborator stubbed. No network, no state file."""
    from bot.config import Config, RiskConfig
    from bot.engine import Engine
    from bot.filters import SymbolRules
    from bot.notify import Notifier
    from bot.risk import RiskManager
    from bot.state import State
    from bot.targets import TargetSchedule

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = Config(symbol="DOGEUSDT", dry_run=dry_run, risk=RiskConfig(),
                 targets={"schedule": [{"from_day": 1, "usd_per_day": 2.0}]})
    e = Engine.__new__(Engine)
    e.cfg = cfg
    e.api = api
    e.state = State(path=TMP / "audit_state.json")
    e.state.day_start_equity = 43.0
    e.state.schedule_start_date = "2026-01-01"
    e.risk = RiskManager(cfg, e.state)
    e.schedule = TargetSchedule.from_config(cfg.targets)
    e.schedule.start_date = "2026-01-01"
    e.notify = Notifier(symbol="DOGEUSDT")
    e.rules = SymbolRules("DOGEUSDT", Decimal("0.00001"), Decimal("1"), Decimal("1"),
                          Decimal("9000000"), Decimal("5"), 6, 0)
    e.equity, e.last_price = 43.0, 0.09
    e.position_amt = position_amt
    e.stream = None
    e.dashboard = controllers[0] if controllers else None
    e.telegram = controllers[1] if len(controllers) > 1 else None
    e._last_reconcile = e._last_publish = e._last_guard = 0.0
    e._last_heartbeat = time.time()
    e._seq = 0
    e._entry_placed_at = time.time()
    e.events = collections.deque(maxlen=40)
    e.bars = []
    e.active = active
    return e


class _StubAPI:
    """Records every call instead of making it."""

    def __init__(self, position_amt=0.0, open_ids=()):
        self.calls = []
        self._amt = position_amt
        self._open = list(open_ids)

    def positions(self, s=None):
        if self._amt == 0.0:
            return []
        return [{"positionAmt": str(self._amt), "entryPrice": "0.09",
                 "unRealizedProfit": "-1.0", "liquidationPrice": "0.05"}]

    def open_orders(self, s=None):
        return [{"clientOrderId": i, "side": "SELL", "type": "STOP_MARKET",
                 "origQty": "100", "price": "0"} for i in self._open]

    def usdt_equity(self):
        return 42.0

    def cancel_all(self, s):
        self.calls.append(("cancel_all", s))

    def order(self, **p):
        self.calls.append(("order", p.get("type"), p.get("side")))
        return {"status": "NEW"}


class _StubController:
    """Stands in for the dashboard or Telegram: queues commands, records stop()."""

    def __init__(self, actions=()):
        from bot.dashboard import Command
        self.queued = [Command(a, note="qa") for a in actions]
        self.stopped = False

    def publish(self, snap):
        pass

    def pop_commands(self):
        out, self.queued = self.queued, []
        return out

    def stop(self):
        self.stopped = True


def layer_audit(report, args):
    header("AUDIT  (probes the unit suite does not cover)")
    from bot.positions import ActivePosition
    from bot.stream import OrderUpdate
    from bot.strategies.base import Signal

    def open_pos():
        return ActivePosition("DOGEUSDT", "BUY", 0.09, 0.0882, 0.0954, 100,
                              "e-1", "s-1", "t-1", tag="e-1")

    # -- A1 ---------------------------------------------------------------
    def halt_keeps_the_stop():
        """
        README: 'Halt bot (stops new trades, leaves open positions alone)' and
        'A naked position is the one state it refuses to sit in.'

        A halt while a position is open must NOT cancel the protective stop.
        """
        api = _StubAPI(position_amt=100.0, open_ids=["s-1", "t-1"])
        ctl = _StubController(["halt"])
        e = _engine(api=api, position_amt=100.0, active=open_pos(), controllers=(ctl,))
        e.process_commands()                      # the Halt button
        try:
            e.periodic()                          # next reconcile
        except KeyboardInterrupt:
            e.shutdown()
        cancelled = [c for c in api.calls if c[0] == "cancel_all"]
        if cancelled:
            return False, ("halting cancelled the protective stop and exited, "
                           "leaving the open position with no stop")
        return True, "protective orders survive a halt"

    # -- A2 ---------------------------------------------------------------
    def kill_file_keeps_the_stop():
        """Same property for the KILL file: stop trading, do not strip the stop."""
        import bot.engine as engine_mod
        import bot.risk as risk
        TMP.mkdir(parents=True, exist_ok=True)
        kill = TMP / "KILL"
        kill.write_text("")
        # engine does `from .risk import KILL_FILE`, so the name must be
        # rebound in BOTH modules or the probe silently tests nothing.
        original = risk.KILL_FILE
        risk.KILL_FILE = engine_mod.KILL_FILE = kill
        try:
            api = _StubAPI(position_amt=100.0, open_ids=["s-1", "t-1"])
            e = _engine(api=api, position_amt=100.0, active=open_pos())
            try:
                e.periodic()
            except KeyboardInterrupt:
                e.shutdown()
            cancelled = [c for c in api.calls if c[0] == "cancel_all"]
            reduce_only = [c for c in api.calls if c[0] == "order"]
            if cancelled and not reduce_only:
                return False, ("KILL cancelled the stop and exited without closing "
                               "the position — it is left naked")
            return True, "KILL does not strand a naked position"
        finally:
            risk.KILL_FILE = engine_mod.KILL_FILE = original
            kill.unlink(missing_ok=True)

    # -- A3 ---------------------------------------------------------------
    def stale_protective_order_does_not_strand_tracking():
        """
        Flat account, entry long gone, one protective order still listed.
        The engine must not hold `active` forever — that silently stops it
        trading with no halt, no alert and nothing in the logs.
        """
        e = _engine(api=_StubAPI(), active=open_pos())
        e.reconcile_position({"position_amt": 0.0, "open_order_ids": {"s-1"}})
        if e.active is not None:
            return False, ("flat + a lingering protective order leaves `active` set "
                           "with no expiry path; expire_stale_entry only handles "
                           "the entry order")
        return True, "tracking is released"

    # -- A4 ---------------------------------------------------------------
    def dry_run_models_the_limit_fill():
        """
        Entries are LIMIT GTC. The backtester was taught this (F11); dry run
        was not, so it books trades and P&L that the live bot would never get.
        """
        e = _engine(dry_run=True, api=_StubAPI())
        e.place(Signal("BUY", entry=0.0900, stop=0.0882, take_profit=0.0954), 20.0, "n")
        if e.active is not None and e.active.entry == 0.09:
            return False, ("dry run opens instantly at the asked limit price; it "
                           "never models an entry that does not fill")
        return True, "dry-run entries model fill"

    # -- A5 ---------------------------------------------------------------
    def fill_alert_reports_real_slippage():
        """The 'entry filled at X (asked Y)' message must show the true ask."""
        e = _engine(api=_StubAPI(), active=ActivePosition(
            "DOGEUSDT", "BUY", 0.0900, 0.0882, 0.0954, 100, "e-1", "s-1", tag="e-1"))
        sent = []
        e.notify.send = lambda ev, body, dedupe_key=None: sent.append(body)
        e.on_order(OrderUpdate(symbol="DOGEUSDT", client_order_id="e-1", side="BUY",
                               status="FILLED", order_type="LIMIT",
                               last_filled_qty=100.0, cumulative_qty=100.0,
                               avg_price=0.0915, realized_pnl=0.0, raw={}))
        line = sent[0].splitlines()[0] if sent else ""
        if "0.0915" in line and line.count("0.0915") > 1:
            return False, f"asked price is overwritten before it is printed: {line!r}"
        return True, "slippage is visible in the fill alert"

    # -- A6 ---------------------------------------------------------------
    def shutdown_fits_the_systemd_stop_timeout():
        """
        systemctl stop sends SIGINT; shutdown() must finish cancelling orders
        before TimeoutStopSec, or systemd SIGKILLs it mid-way and the orders
        stay on the book.
        """
        import re
        import bot.telegram_control as tg
        unit = (ROOT / "deploy" / "trading-bot.service").read_text()
        m = re.search(r"TimeoutStopSec=(\d+)", unit)
        if not m:
            return False, "TimeoutStopSec is not set in the unit file"
        budget = int(m.group(1))
        # shutdown() stops controllers BEFORE cancelling orders.
        worst = tg.POLL_TIMEOUT + 5
        if worst >= budget:
            return False, (f"telegram stop() can block {worst}s before orders are "
                           f"cancelled, but TimeoutStopSec={budget}s — systemd "
                           f"SIGKILLs first and open orders survive the stop")
        return True, f"controller stop {worst}s < TimeoutStopSec {budget}s"

    # -- A7 ---------------------------------------------------------------
    def commands_never_touch_the_exchange_thread():
        """Both control surfaces must only enqueue; the engine executes."""
        import inspect
        from bot import dashboard, telegram_control
        bad = []
        for mod in (dashboard, telegram_control):
            src = inspect.getsource(mod)
            for needle in (".order(", "cancel_all(", "positions(", "usdt_equity("):
                if needle in src:
                    bad.append(f"{mod.__name__} calls {needle}")
        return not bad, "; ".join(bad) or "control surfaces only enqueue"

    # -- A8 ---------------------------------------------------------------
    def telegram_rejects_every_other_chat():
        """The allowlist is the entire security boundary for Telegram."""
        from bot.telegram_control import TelegramControl
        tc = TelegramControl("t", "111")
        tc.send = lambda *a, **k: None
        for chat in ("222", 222, "", "1111", "11"):
            tc._handle({"update_id": 1, "message": {"chat": {"id": chat},
                                                    "text": "/close"}})
        tc._handle({"update_id": 2, "callback_query": {
            "id": "x", "data": "close:whatever",
            "message": {"chat": {"id": "222"}}}})
        n = len(tc.pop_commands())
        return n == 0, f"{n} commands accepted from unauthorised chats"

    # -- A9 ---------------------------------------------------------------
    def telegram_confirmation_cannot_be_replayed():
        from bot.telegram_control import TelegramControl
        tc = TelegramControl("t", "111")
        tc.send = lambda *a, **k: None
        tc._call = lambda *a, **k: {}
        tc.publish({"position": {"side": "BUY", "qty": 1, "entry": 1.0,
                                 "unrealized": 0.0}})
        tc._handle({"update_id": 1, "message": {"chat": {"id": "111"},
                                                "text": "/close"}})
        nonce = next(iter(tc._pending))
        cb = {"id": "1", "data": f"close:{nonce}", "message": {"chat": {"id": "111"}}}
        tc._handle_callback(cb)
        tc._handle_callback(cb)                      # replay
        actions = [c.action for c in tc.pop_commands()]
        return actions == ["close"], f"replay produced {actions}"

    # -- A10 --------------------------------------------------------------
    def every_state_change_requires_confirmation():
        """
        Resume clears a daily-loss-limit halt, so it is the most dangerous of
        the three. Driven over loopback rather than read out of the source, so
        this cannot pass because the gate merely looks right.
        """
        from bot.dashboard import Dashboard, generate_token
        token = generate_token()
        dash = Dashboard(token, "127.0.0.1", 8232)
        dash.start()
        try:
            def post(path, body):
                req = urllib.request.Request(
                    f"http://127.0.0.1:8232{path}",
                    data=json.dumps(body).encode(), method="POST")
                req.add_header("Authorization", "Bearer " + token)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            unguarded = [a for a in ("close", "halt", "resume")
                         if post(f"/api/{a}", {}) == 202]
            guarded = [a for a in ("close", "halt", "resume")
                       if post(f"/api/{a}", {"confirm": True}) == 202]
            if unguarded:
                return False, f"accepted with no confirmation: {', '.join(unguarded)}"
            if len(guarded) != 3:
                return False, f"confirmed requests were refused: only {guarded} queued"
            return True, "close, halt and resume all require confirm"
        finally:
            dash.stop()

    # -- A11 --------------------------------------------------------------
    def risk_refuses_an_unaffordable_symbol():
        """The headline property: $43 cannot legally size a BTC trade."""
        from bot.config import Config, RiskConfig
        from bot.filters import SymbolRules
        from bot.risk import RiskManager
        from bot.state import State
        rules = SymbolRules("BTCUSDT", Decimal("0.10"), Decimal("0.0001"),
                            Decimal("0.0001"), Decimal("1000"), Decimal("50"), 1, 4)
        rm = RiskManager(Config(risk=RiskConfig()), State(path=TMP / "risk.json"))
        d = rm.size_position(43.0, 80_000, 78_400, rules)
        return not d.allowed, d.reason[:90] if d.allowed else "refused, as it must be"

    # -- A12 --------------------------------------------------------------
    def a_position_is_never_opened_without_a_stop():
        """If the protective order fails, the entry must be cancelled."""
        from bot.binanceapi import BinanceError

        class FailsOnStop(_StubAPI):
            def order(self, **p):
                self.calls.append(("order", p.get("type"), p.get("side")))
                if p.get("type") in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                    raise BinanceError(-2021, "would immediately trigger", "/order")
                return {"status": "NEW"}

        api = FailsOnStop()
        e = _engine(api=api)
        try:
            e.place(Signal("BUY", entry=0.0900, stop=0.0882, take_profit=0.0954), 20.0, "n")
        except KeyboardInterrupt:
            pass
        cancelled = any(c[0] == "cancel_all" for c in api.calls)
        return (cancelled and e.state.halted and e.active is None), (
            f"cancelled={cancelled} halted={e.state.halted} active={e.active is not None}")

    probes = [
        ("halt leaves the protective stop in place", halt_keeps_the_stop, "blocker"),
        ("KILL does not leave a naked position", kill_file_keeps_the_stop, "blocker"),
        ("stale protective order does not strand tracking",
         stale_protective_order_does_not_strand_tracking, "medium"),
        ("dry run models an unfilled limit entry", dry_run_models_the_limit_fill, "medium"),
        ("fill alert shows real slippage", fill_alert_reports_real_slippage, "low"),
        ("shutdown fits TimeoutStopSec", shutdown_fits_the_systemd_stop_timeout, "high"),
        ("control surfaces never call the exchange",
         commands_never_touch_the_exchange_thread, "blocker"),
        ("telegram rejects every other chat", telegram_rejects_every_other_chat, "blocker"),
        ("telegram confirmations cannot be replayed",
         telegram_confirmation_cannot_be_replayed, "high"),
        ("every state change requires confirmation",
         every_state_change_requires_confirmation, "high"),
        ("risk refuses an unaffordable symbol", risk_refuses_an_unaffordable_symbol, "blocker"),
        ("entry is cancelled when the stop cannot be placed",
         a_position_is_never_opened_without_a_stop, "blocker"),
    ]
    for name, fn, sev in probes:
        check(report, "audit", name, fn, severity=sev)


# ==================================================================== cli
def layer_cli(report, args):
    header("CLI  (exit codes for every documented command)")

    def run(cmd, expect, timeout=240, needs_net=False):
        if needs_net and args.offline:
            return None, "skipped (--offline)"
        r = subprocess.run([PY, "run.py"] + cmd, cwd=ROOT,
                           capture_output=True, text=True, timeout=timeout)
        ok = r.returncode == expect
        return ok, f"exit={r.returncode} (expected {expect})"

    cases = [
        ("check", ["check"], 0, False),
        ("project", ["project"], 0, False),
        ("backtest --validate", ["backtest", "--validate"], 0, False),
        ("dashboard without --demo refuses", ["dashboard"], 1, False),
        # Only meaningful without a token: once TELEGRAM_TOKEN is configured --
        # which is the correct end state -- `telegram` does real work and exits
        # 0. Asserting a refusal regardless would make a properly configured
        # bot fail its own QA, which is how a harness teaches you to ignore it.
        ("telegram refuses without a token / works with one", ["telegram"],
         0 if _env_has("TELEGRAM_TOKEN") else 1, False),
        ("bad subcommand is rejected", ["nonsense"], 2, False),
        ("doctor", ["doctor"], 0, True),
        ("netcheck", ["netcheck"], 0, True),
    ]
    for name, cmd, expect, net in cases:
        if net and args.offline:
            report.add("cli", name, SKIP, "network layer disabled")
            continue
        if args.quick and net:
            report.add("cli", name, SKIP, "--quick")
            continue
        try:
            ok, note = run(cmd, expect, needs_net=net)
            report.add("cli", name, PASS if ok else FAIL, note)
        except subprocess.TimeoutExpired:
            report.add("cli", name, FAIL, "timed out")


# ==================================================================== net
def layer_net(report, args):
    header("NET  (live endpoints and real HTTP)")
    if args.offline:
        report.add("net", "all network checks", SKIP, "--offline")
        return

    def rest(url, label):
        def fn():
            with urllib.request.urlopen(url, timeout=15) as r:
                r.read(64)
            return True, label
        return fn

    check(report, "net", "Binance futures REST (live)",
          rest("https://fapi.binance.com/fapi/v1/time", "reachable"))
    check(report, "net", "Binance futures REST (testnet)",
          rest("https://testnet.binancefuture.com/fapi/v1/time", "reachable"))
    check(report, "net", "Telegram API", rest("https://api.telegram.org", "reachable"))

    def clock_drift():
        from bot.binanceapi import Binance
        drift = Binance(testnet=True).sync_clock()
        return abs(drift) < 1000, f"{drift} ms (signed requests fail past ~1000 ms)"

    def symbol_is_tradable():
        from bot.binanceapi import Binance
        from bot.config import Config
        from bot.filters import SymbolRules
        cfg = Config.load(ROOT / "config.yaml")
        api = Binance(testnet=cfg.testnet)
        rules = SymbolRules.from_exchange_info(api.exchange_info(), cfg.symbol)
        price = float(api.mark_price(cfg.symbol)["markPrice"])
        return True, rules.describe(price)

    check(report, "net", "clock drift is within recvWindow", clock_drift)
    check(report, "net", "configured symbol resolves with filters", symbol_is_tradable)

    # ---- dashboard end to end over real HTTP
    def dashboard_e2e():
        from bot.dashboard import Dashboard, generate_token
        token = generate_token()
        dash = Dashboard(token, "127.0.0.1", 8231)
        dash.start()
        try:
            base = "http://127.0.0.1:8231"

            def call(path, method="GET", body=None, tok=token):
                data = json.dumps(body).encode() if body is not None else None
                req = urllib.request.Request(base + path, data=data, method=method)
                if tok:
                    req.add_header("Authorization", "Bearer " + tok)
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status
                except urllib.error.HTTPError as e:
                    return e.code

            failures = []
            if call("/api/state", tok=None) != 401:
                failures.append("missing token was not rejected")
            if call("/api/state", tok="wrong") != 401:
                failures.append("wrong token was not rejected")
            if call("/api/state") != 200:
                failures.append("valid token was rejected")
            if call("/healthz", tok=None) != 200:
                failures.append("/healthz needs no auth")
            if call("/healthz", method="HEAD", tok=None) != 200:
                failures.append("HEAD /healthz is not answered")
            if call("/api/close", "POST", {}) != 400:
                failures.append("close without confirm was accepted")
            if call("/api/close", "POST", {"confirm": True}) != 202:
                failures.append("confirmed close was not queued")
            if call("/api/resume", "POST", {}) == 202:
                failures.append("resume was accepted with no confirmation")
            if call("/api/liquidate", "POST", {"confirm": True}) != 404:
                failures.append("unknown route did not 404")
            if [c.action for c in dash.pop_commands()] != ["close"]:
                failures.append("command queue did not match the accepted requests")
            return not failures, "; ".join(failures) or "auth, confirm gates and routes hold"
        finally:
            dash.stop()

    check(report, "net", "dashboard over real HTTP", dashboard_e2e, severity="high")

    # ---- market data + strategy edge
    if args.quick:
        report.add("net", "backtest on live history", SKIP, "--quick")
        report.add("net", "multi-symbol sweep", SKIP, "--quick")
        return

    def backtest_runs():
        r = subprocess.run([PY, "run.py", "backtest"], cwd=ROOT,
                           capture_output=True, text=True, timeout=300)
        return r.returncode == 0, "ran against live klines"

    check(report, "net", "backtest on live history", backtest_runs)

    if not args.full:
        report.add("net", "multi-symbol sweep", SKIP, "use --full (about 3 minutes)")
        return

    def sweep_runs():
        r = subprocess.run([PY, "tools/sweep.py"], cwd=ROOT,
                           capture_output=True, text=True, timeout=600)
        verdict = [ln.strip() for ln in r.stdout.splitlines() if "cells profitable" in ln]
        return r.returncode == 0, verdict[0] if verdict else "completed"

    check(report, "net", "multi-symbol sweep", sweep_runs)


# =================================================================== main
def main() -> int:
    p = argparse.ArgumentParser(
        description="Regression harness. Run before every deploy.")
    p.add_argument("--offline", action="store_true",
                   help="skip everything that touches the network")
    p.add_argument("--quick", action="store_true",
                   help="skip slow market-data checks")
    p.add_argument("--full", action="store_true",
                   help="include the multi-symbol sweep (~3 min)")
    p.add_argument("--only", choices=LAYERS, action="append",
                   help="run only these layers (repeatable)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="show output from passing checks too")
    args = p.parse_args()

    layers = args.only or LAYERS
    report = Report(args.verbose)

    print("=" * 72)
    print("  trading-bot regression harness")
    print(f"  {ROOT}")
    print(f"  layers: {', '.join(layers)}"
          + ("   [offline]" if args.offline else "")
          + ("   [quick]" if args.quick else ""))
    print("=" * 72)

    t0 = time.time()
    runners = {"env": layer_env, "static": layer_static, "unit": layer_unit,
               "audit": layer_audit, "cli": layer_cli, "net": layer_net}
    for name in layers:
        runners[name](report, args)

    print(f"\n  finished in {time.time() - t0:.1f}s")
    code = report.summary()
    with contextlib.suppress(OSError):
        for f in TMP.glob("*"):
            f.unlink()
        TMP.rmdir()
    return code


if __name__ == "__main__":
    sys.exit(main())
