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

LAYERS = ["env", "static", "unit", "audit", "portfolio", "cli", "net"]

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

    def no_method_is_defined_twice():
        """
        A slice-based edit once duplicated five engine methods. Python binds
        the LAST definition, so a fix can sit in the file as dead code while a
        stale copy keeps running -- and nothing about that is visible in a diff
        or a test run. Cheap to check, invisible otherwise.
        """
        import ast
        dupes = []
        for path in sorted((ROOT / "bot").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.ClassDef):
                    continue
                seen = set()
                for item in node.body:
                    if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    decorators = {getattr(d, "attr", getattr(d, "id", ""))
                                  for d in item.decorator_list}
                    if decorators & {"setter", "deleter"}:
                        continue
                    if item.name in seen:
                        dupes.append(f"{path.name}:{node.name}.{item.name}")
                    seen.add(item.name)
        return not dupes, "; ".join(dupes) or "no shadowed definitions in bot/"

    check(report, "static", "all sources compile", compiles)
    check(report, "static", "no method is defined twice", no_method_is_defined_twice,
          severity="high")
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

def _rules(symbol="DOGEUSDT"):
    from bot.filters import SymbolRules
    return SymbolRules(symbol, Decimal("0.00001"), Decimal("1"), Decimal("1"),
                       Decimal("9000000"), Decimal("5"), 6, 0)


def _position(symbol, entry=1.00, stop=0.98, tp=1.06, qty=10.0):
    from bot.positions import ActivePosition
    return ActivePosition(symbol, "BUY", entry, stop, tp, qty,
                          f"e-{symbol}", f"s-{symbol}", f"t-{symbol}", tag=f"e-{symbol}")


def _engine(dry_run=False, api=None, position_amt=0.0, active=None, controllers=(),
            book=None, portfolio=False, equity=43.0, symbol="DOGEUSDT"):
    """A live Engine with every collaborator stubbed. No network, no state file."""
    from bot.config import Config, RiskConfig
    from bot.engine import Engine
    from bot.notify import Notifier
    from bot.risk import RiskManager
    from bot.state import State
    from bot.targets import TargetSchedule

    TMP.mkdir(parents=True, exist_ok=True)
    cfg = Config(symbol=symbol, dry_run=dry_run, risk=RiskConfig(),
                 targets={"schedule": [{"from_day": 1, "usd_per_day": 2.0}]})
    e = Engine.__new__(Engine)
    e.cfg = cfg
    e.api = api
    e.state = State(path=TMP / "audit_state.json")
    e.state.day_start_equity = equity
    e.state.schedule_start_date = "2026-01-01"
    e.risk = RiskManager(cfg, e.state)
    e.schedule = TargetSchedule.from_config(cfg.targets)
    e.schedule.start_date = "2026-01-01"
    e.notify = Notifier(symbol=symbol)
    e.rules = _rules(symbol)
    e.equity, e.last_price = equity, 0.09
    e.position_amt = position_amt
    e.stream = None
    e.scanner = None
    e.dashboard = controllers[0] if controllers else None
    e.telegram = controllers[1] if len(controllers) > 1 else None
    e._last_reconcile = e._last_publish = e._last_guard = 0.0
    e._last_heartbeat = time.time()
    e._seq = 0
    e._entry_placed_at = time.time()
    e._flat_reconciles = 0
    e._rules_cache = {}
    e._exchange_info = None
    e._stopping = False
    e._stop_reason = ""
    e.aggressive_profile = None
    e.events = collections.deque(maxlen=40)
    e.bars = []
    e.strategy = type("S", (), {"name": "stub", "mode": "auto",
                                "choices": [], "warmup": 1})()
    e.rules_for = _rules
    if book:
        e.book = {s: _position(s) for s in book}
        e.last_prices = {s: 1.02 for s in book}
    else:
        e.active = active
        e.last_prices = {}
    if portfolio:
        pf = cfg.portfolio
        pf.enabled = True
        pf.portfolio_risk_pct = 6.0
        pf.single_position_cap_pct = 2.0
        pf.stop_distance = 0.02
        pf.hard_cap = 40
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

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        self.calls.append(("set_margin_type", symbol, margin_type))
        return {"msg": "ok"}

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        return {"leverage": leverage}

    def cancel_all(self, s):
        self.calls.append(("cancel_all", s))

    def order(self, **p):
        self.calls.append(("order", p.get("type"), p.get("side")))
        return {"status": "NEW"}


class _MultiAPI:
    """
    A stubbed exchange that knows about MORE THAN ONE symbol.

    The single-symbol stub cannot catch portfolio defects, because every
    question it is asked has the same answer whichever symbol you name.
    """

    def __init__(self, open_symbols=(), open_orders=None, fail_on=()):
        self.calls = []
        self.open_symbols = dict(open_symbols) if isinstance(open_symbols, dict) \
            else {s: 10.0 for s in open_symbols}
        self._orders = dict(open_orders or {})
        self.fail_on = set(fail_on)

    def positions(self, symbol=None):
        amt = self.open_symbols.get(symbol, 0.0)
        if not amt:
            return []
        return [{"symbol": symbol, "positionAmt": str(amt), "entryPrice": "1.0",
                 "unRealizedProfit": "0.0", "liquidationPrice": "0.5"}]

    def open_orders(self, symbol=None):
        return [{"clientOrderId": i, "side": "SELL", "type": "STOP_MARKET",
                 "origQty": "10", "price": "0"}
                for i in self._orders.get(symbol, [])]

    def usdt_equity(self):
        return 43.0

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        self.calls.append(("set_margin_type", symbol, margin_type))
        return {"msg": "ok"}

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        return {"leverage": leverage}

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))
        self._orders.pop(symbol, None)

    def cancel_order(self, symbol, order_id):
        self.calls.append(("cancel_order", symbol, order_id))

    def order(self, **p):
        symbol = p.get("symbol")
        if symbol in self.fail_on:
            from bot.binanceapi import BinanceError
            raise BinanceError(-1001, "exchange refused", "/order")
        self.calls.append(("order", symbol, p.get("type")))
        if p.get("reduceOnly"):
            self.open_symbols.pop(symbol, None)
        return {"status": "NEW"}

    def touched(self, symbol):
        return any(symbol in [str(x) for x in c] for c in self.calls)


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
        e.notify.send = lambda ev, body, **kw: sent.append(body)
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

    # -- A13 --------------------------------------------------------------
    def the_snapshot_renders_with_a_position_open():
        """
        publish() calls snapshot() every loop. If it raises, run() logs and
        carries on -- so the bot keeps trading while the dashboard and
        Telegram freeze at the last state they managed to render. The one
        moment you need the monitor is the one moment it would be stale, so
        this is asserted by CALLING it, never by reading its source.
        """
        for book in ([], ["DOGEUSDT"], [f"S{i:02d}USDT" for i in range(25)]):
            for priced in (True, False):
                e = _engine(book=book or None, portfolio=bool(book))
                if not priced:
                    e.last_prices = {}       # the stream has not ticked yet
                try:
                    snap = e.snapshot()
                except Exception as exc:
                    return False, (f"snapshot() raised {type(exc).__name__}: {exc} "
                                   f"with {len(book)} position(s) open"
                                   + ("" if priced else " and no price yet"))
                if len(snap.get("positions", [])) != len(book):
                    return False, (f"{len(book)} open but snapshot reported "
                                   f"{len(snap.get('positions', []))}")
        return True, "flat, one, and 25 positions -- priced and unpriced"

    probes = [
        ("snapshot renders with a position open",
         the_snapshot_renders_with_a_position_open, "blocker"),
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


# ============================================================== portfolio
# Scenario coverage for scan-wide/hold-many. Every probe is offline: a stubbed
# exchange that answers per symbol, so a single-symbol assumption in the engine
# shows up as a failure rather than as a coincidence.

def layer_portfolio(report, args):
    header("PORTFOLIO  (scanner, allocation, position book, aggressive mode)")
    from bot.portfolio import allocate, auto_slots
    from bot.scanner import Candidate, ScanResult
    from bot.strategies.base import Bar, Signal

    # ---------------------------------------------------------- allocation
    def risk_is_conserved_at_every_account_size():
        """
        The plan's Phase 1 gate: total risk conserved and no slot below the
        exchange minimum, from $20 to $20,000.
        """
        bad = []
        for equity in (20, 43, 100, 500, 2000, 20000):
            for eligible in (1, 2, 3, 10, 25, 40, 100):
                a = allocate(equity, eligible, portfolio_risk_pct=6.0,
                             single_position_cap_pct=2.0, max_leverage=3.0,
                             stop_distance=0.02, min_notional=5.0, hard_cap=40)
                if not a.slots:
                    continue
                if a.notional_per_position < 5.0 - 1e-9:
                    bad.append(f"${equity}/{eligible}: slot below minimum")
                if a.total_risk_pct > 6.0 + 1e-9:
                    bad.append(f"${equity}/{eligible}: {a.total_risk_pct:.2f}% risk")
                if a.deployable > equity * 3.0 + 1e-6:
                    bad.append(f"${equity}/{eligible}: over leverage")
                if a.slots > 40:
                    bad.append(f"${equity}/{eligible}: over hard cap")
        return not bad, "; ".join(bad[:3]) or "42 combinations, no breach"

    def a_lone_qualifier_gets_the_single_position_cap():
        """
        "Only one coin qualified" must not become the riskiest day of the
        month. One eligible takes the single-position cap, not the whole
        portfolio budget.
        """
        one = allocate(43.0, 1, portfolio_risk_pct=6.0, single_position_cap_pct=2.0)
        if abs(one.per_position_risk_pct - 2.0) > 1e-9:
            return False, f"a lone qualifier risked {one.per_position_risk_pct:.2f}%"
        three = allocate(43.0, 3, portfolio_risk_pct=6.0, single_position_cap_pct=2.0)
        return abs(three.total_risk_pct - 6.0) < 1e-9, (
            f"1 eligible -> {one.per_position_risk_pct:.2f}%, "
            f"3 -> {three.total_risk_pct:.2f}% total")

    def slots_scale_with_equity():
        small, _ = auto_slots(20.0, 6.0, 3.0)
        mid, _ = auto_slots(43.0, 6.0, 3.0)
        big, _ = auto_slots(100.0, 6.0, 3.0)
        ok = 0 < small < mid <= big
        return ok, f"$20 -> {small} slots, $43 -> {mid}, $100 -> {big}"

    # ------------------------------------------------------------ scanning
    class _Scanner:
        def __init__(self, n):
            self.last = ScanResult()
            for i in range(n):
                c = Candidate(f"SYM{i:03d}USDT", 1.0, 5e8, 0.9, 1.0, 5.0)
                c.score = 1.0 - i / 1000.0
                c.bars = [Bar(j, 1.0, 1.01, 0.99, 1.0, 1.0) for j in range(60)]
                self.last.ranked.append(c)

        def due(self):
            return False

        def scan(self, **k):
            return self.last

    class _Signalling:
        name, warmup, mode, choices = "stub", 1, "auto", []

        def on_bars(self, bars, amt):
            p = bars[-1].close
            return Signal("BUY", entry=p, stop=p * 0.98, take_profit=p * 1.06)

    def _portfolio_engine(eligible, equity=43.0, dry_run=True, api=None):
        e = _engine(dry_run=dry_run, api=api or _MultiAPI(), portfolio=True,
                    equity=equity)
        e.strategy = _Signalling()
        e.scanner = _Scanner(eligible)
        return e

    def scan_fills_exactly_the_affordable_number_of_slots():
        """Phase 4 gate: 0 eligible opens nothing, 1 opens one, 100 opens what fits."""
        results = {}
        for n in (0, 1, 100):
            e = _portfolio_engine(n)
            e.portfolio_cycle()
            results[n] = (e.cfg.portfolio.resolved_slots,
                          e.cfg.portfolio.resolved_risk_pct)
        problems = []
        if results[0][0] != 0:
            problems.append("0 eligible allocated slots")
        if results[1][0] != 1 or abs(results[1][1] - 2.0) > 1e-9:
            problems.append(f"1 eligible -> {results[1]}")
        if results[100][0] != 25:
            problems.append(f"100 eligible -> {results[100][0]} slots, expected 25")
        return not problems, "; ".join(problems) or (
            f"0->0, 1->1 at 2.00%, 100->{results[100][0]} at {results[100][1]:.2f}%")

    def the_same_symbol_is_never_opened_twice():
        e = _portfolio_engine(5, dry_run=False)
        e.portfolio_cycle()
        first = set(e.book)
        e.scanner._last = None
        e.portfolio_cycle()
        dupes = [s for s in e.book if list(e.book).count(s) > 1]
        return set(e.book) == first and not dupes, (
            f"{len(first)} held after the first cycle, {len(e.book)} after the second")

    def the_portfolio_gate_refuses_a_breach():
        from bot.risk import RiskManager
        e = _engine(portfolio=True, book=[f"S{i}USDT" for i in range(3)])
        pf = e.cfg.portfolio
        pf.resolved_slots = 3
        pf.resolved_risk_pct = 2.0
        rm: RiskManager = e.risk
        checks = {
            "slots exhausted": rm.check_portfolio(e.book, 43.0, 5.0, "NEWUSDT", pf),
            "duplicate symbol": rm.check_portfolio(e.book, 43.0, 5.0, "S1USDT", pf),
            "over leverage": rm.check_portfolio({}, 43.0, 10_000.0, "NEWUSDT", pf),
        }
        allowed = [k for k, v in checks.items() if v.allowed]
        return not allowed, f"gate allowed: {allowed}" if allowed else \
            "slot, duplicate and leverage breaches all refused"

    def portfolio_off_still_means_one_position():
        e = _engine(book=["AAAUSDT"])
        pf = e.cfg.portfolio
        pf.enabled = False
        d = e.risk.check_portfolio(e.book, 43.0, 5.0, "BBBUSDT", pf)
        return not d.allowed, d.reason or "a second position was allowed"

    # ------------------------------------------------- the exchange boundary
    def an_order_update_finds_its_own_position():
        """
        A stop filling on a scanner-opened symbol must book the P&L and free
        the slot. If order updates are filtered on the single configured
        symbol, every position the scanner opened is invisible to fills.
        """
        from bot.stream import OrderUpdate
        e = _engine(book=["DOGEUSDT", "SOLUSDT", "ADAUSDT"], portfolio=True,
                    api=_MultiAPI())
        e.on_order(OrderUpdate(symbol="SOLUSDT", client_order_id="s-SOLUSDT",
                               side="SELL", status="FILLED",
                               order_type="STOP_MARKET", last_filled_qty=10.0,
                               cumulative_qty=10.0, avg_price=0.98,
                               realized_pnl=-2.5, raw={}))
        if "SOLUSDT" in e.book:
            return False, ("a stop fill on a held symbol was ignored: the slot is "
                           "still occupied and the loss was never booked")
        if abs(e.state.realized_today + 2.5) > 1e-9:
            return False, (f"P&L not credited: realized_today is "
                           f"{e.state.realized_today}, expected -2.50")
        return True, "fills are routed to the position they belong to"

    def reconciling_one_symbol_does_not_wipe_the_book():
        """
        The engine's own comment warns that `self.active = None` replaces the
        whole book. Reconciling the configured symbol must not drop tracking
        for twenty-four positions that are still open on the exchange.
        """
        e = _engine(book=["DOGEUSDT", "SOLUSDT", "ADAUSDT"], portfolio=True,
                    api=_MultiAPI(open_symbols=["SOLUSDT", "ADAUSDT"]))
        e.reconcile_position({"position_amt": 0.0, "open_order_ids": set(),
                              "equity": 43.0})
        survived = sorted(e.book)
        if set(survived) < {"SOLUSDT", "ADAUSDT"}:
            return False, (f"reconciling DOGEUSDT alone left {survived}; SOLUSDT and "
                           f"ADAUSDT are still open on the exchange but untracked")
        return True, f"still tracking {len(survived)} position(s)"

    def shutdown_cancels_orders_on_every_held_symbol():
        """
        Resting entries must not survive a stop. With the configured symbol
        flat and the scanner's symbols open, a single-symbol cancel path
        leaves every one of them on the book.
        """
        held = ["SOLUSDT", "ADAUSDT", "XRPUSDT"]
        api = _MultiAPI(open_symbols=held,
                        open_orders={s: [f"e-{s}"] for s in held})
        e = _engine(book=held, portfolio=True, api=api)
        e.cancel_orders_safely(keep_protective=True)
        missed = [s for s in held if not api.touched(s)]
        return not missed, (f"never looked at {missed}" if missed
                            else "every held symbol was handled")

    def close_all_reports_the_truth():
        """A failed close must not be counted as a close."""
        held = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = _MultiAPI(open_symbols=held, fail_on=["BBBUSDT"])
        e = _engine(book=held, portfolio=True, api=api)
        closed = e.close_all("qa")
        if closed != 2:
            return False, (f"reported {closed} closed, but BBBUSDT failed and is "
                           f"still tracked as {sorted(e.book)}")
        return True, "2 of 3 closed, the failure reported as a failure"

    def a_failed_close_keeps_its_protection():
        """Phase 5 gate: a mid-sequence failure leaves the rest protected."""
        held = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
        api = _MultiAPI(open_symbols=held, fail_on=["BBBUSDT"])
        e = _engine(book=held, portfolio=True, api=api)
        alerts = []
        e.notify.send = lambda ev, body, **kw: alerts.append((ev.name, body))
        e.close_all("qa")
        cancelled_bbb = ("cancel_all", "BBBUSDT") in api.calls
        loud = any(a[0] == "ERROR" for a in alerts)
        if cancelled_bbb:
            return False, "the failed position had its protective orders cancelled"
        if "BBBUSDT" not in e.book:
            return False, "the failed position was dropped from tracking"
        return loud, "failure kept its stop and tracking" if loud else \
            "failure was protected but never alerted"

    def the_target_flattens_the_whole_book():
        """Decision 3: reaching the target closes everything, not just stands down."""
        e = _engine(dry_run=True, book=["AAAUSDT", "BBBUSDT"], portfolio=True,
                    api=_MultiAPI())
        e.state.realized_today = 2.50
        stopped = e.check_target_reached()
        return stopped and not e.book, (
            f"stop_trading={stopped}, book still {sorted(e.book)}")

    def kill_honours_its_configured_action():
        """flatten closes every position first; protect leaves them all alone."""
        import bot.engine as engine_mod
        import bot.risk as risk
        TMP.mkdir(parents=True, exist_ok=True)
        kill = TMP / "KILL"
        original = risk.KILL_FILE
        problems = []
        for action, expect_closes in (("flatten", 3), ("protect", 0)):
            held = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
            api = _MultiAPI(open_symbols=held)
            e = _engine(book=held, portfolio=True, api=api)
            e.cfg.risk.kill_action = action
            kill.write_text("")
            risk.KILL_FILE = engine_mod.KILL_FILE = kill
            try:
                e.trigger_kill("qa")
            except Exception as exc:
                problems.append(f"{action} raised {type(exc).__name__}: {exc}")
                continue
            finally:
                risk.KILL_FILE = engine_mod.KILL_FILE = original
                kill.unlink(missing_ok=True)
            closes = len([c for c in api.calls if c[0] == "order"])
            if closes != expect_closes:
                problems.append(f"{action}: {closes} market closes, "
                                f"expected {expect_closes}")
        return not problems, "; ".join(problems) or \
            "flatten closed all 3, protect closed none"

    # ------------------------------------------------------------- streaming
    def the_stream_tracks_the_held_set():
        from bot.stream import MarketStream
        s = MarketStream(["BTCUSDT"], "15m", testnet=True)
        s.add_symbol("SOLUSDT")
        s.add_symbol("ADAUSDT")
        s.add_symbol("SOLUSDT")                    # duplicate
        after_add = len(s.symbols)
        s.remove_symbol("SOLUSDT")
        url = s._url()
        if after_add != 3:
            return False, f"adding produced {after_add} subscriptions, expected 3"
        if "solusdt" in url:
            return False, "removed symbol is still in the subscription URL"
        if "btcusdt" not in url or "adausdt" not in url:
            return False, "removing one symbol dropped the others"
        return True, f"{len(s.symbols)} subscribed, URL rebuilt correctly"

    # ------------------------------------------------------------ aggressive
    def aggressive_off_changes_nothing():
        from bot.config import Config
        cfg = Config.load(ROOT / "config.yaml")
        if cfg.aggressive.enabled:
            return False, "aggressive mode is ENABLED in the committed config"
        problems = []
        if cfg.risk.max_leverage > 5:
            problems.append(f"leverage {cfg.risk.max_leverage}x")
        if cfg.risk.risk_per_trade_pct > 2.0:
            problems.append(f"risk/trade {cfg.risk.risk_per_trade_pct}%")
        return not problems, "; ".join(problems) or (
            f"off; safe profile intact ({cfg.risk.max_leverage}x, "
            f"{cfg.risk.risk_per_trade_pct}%/trade)")

    def aggressive_on_replaces_the_profile_and_warns():
        from bot.aggressive import PROFILES, apply, short_warning
        from bot.config import Config
        problems = []
        for name, profile in PROFILES.items():
            cfg = Config.load(ROOT / "config.yaml")
            apply(cfg, profile)
            if cfg.risk.max_leverage != profile.leverage:
                problems.append(f"{name}: leverage not applied")
            if cfg.portfolio.portfolio_risk_pct != profile.portfolio_risk_pct:
                problems.append(f"{name}: portfolio risk not applied")
            warning = short_warning(43.0, profile)
            if "P(ruin)" not in warning or "AGGRESSIVE" not in warning:
                problems.append(f"{name}: warning missing its computed number")
        return not problems, "; ".join(problems) or \
            f"{len(PROFILES)} profiles apply and each warns with a live P(ruin)"

    def aggressive_cannot_disable_the_hard_invariants():
        """
        The plan: an exchange-side stop, the KILL file and the equity floor
        survive every profile. Aggressive means bigger, never unprotected.
        """
        from bot.aggressive import PROFILES, apply
        from bot.config import Config
        problems = []
        for name, profile in PROFILES.items():
            cfg = Config.load(ROOT / "config.yaml")
            apply(cfg, profile)
            if cfg.risk.min_equity_usdt <= 0:
                problems.append(f"{name}: equity floor removed")
            if cfg.risk.kill_action not in ("flatten", "protect"):
                problems.append(f"{name}: kill action is {cfg.risk.kill_action!r}")
        src = (ROOT / "bot" / "engine.py").read_text()
        if "STOP_MARKET" not in src:
            problems.append("no unconditional stop order in place()")
        return not problems, "; ".join(problems) or \
            "equity floor, KILL and the exchange-side stop survive every profile"

    def the_ruin_warning_is_computed_not_hardcoded():
        """Two different profiles must not produce the same number by accident."""
        from bot.aggressive import PROFILES, ruin_probability
        mod = ruin_probability(43.0, PROFILES["moderate"], sims=400, horizon=180)
        mx = ruin_probability(43.0, PROFILES["maximum"], sims=400, horizon=180)
        rich = ruin_probability(50_000.0, PROFILES["moderate"], sims=400, horizon=180)
        if mod[1] == mx[1] and mod[1] == rich[1]:
            return False, "the model returns the same life for every input"
        return True, (f"moderate {mod[0]*100:.0f}%/{mod[1]:.0f}d, "
                      f"maximum {mx[0]*100:.0f}%/{mx[1]:.0f}d at $43")

    # ------------------------------------------- robustness of the S2/S4/S5 fixes
    def an_unheld_symbol_is_ignored_not_crashed():
        """The user stream carries the whole account, including symbols we
        never opened and ones we released a moment ago."""
        from bot.stream import OrderUpdate
        e = _engine(book=["AAAUSDT"], portfolio=True, api=_MultiAPI())
        before = (dict(e.book), e.state.realized_today)
        e.on_order(OrderUpdate(symbol="ZZZUSDT", client_order_id="s-ZZZUSDT",
                               side="SELL", status="FILLED",
                               order_type="STOP_MARKET", last_filled_qty=10.0,
                               cumulative_qty=10.0, avg_price=1.0,
                               realized_pnl=-99.0, raw={}))
        after = (dict(e.book), e.state.realized_today)
        return before == after, (
            f"book and P&L unchanged (realized {after[1]})" if before == after
            else "an update for a symbol we do not hold changed state")

    def a_replayed_fill_is_not_booked_twice():
        """Websocket redelivery must not double-count a realised loss."""
        from bot.stream import OrderUpdate

        def stop(symbol):
            return OrderUpdate(symbol=symbol, client_order_id=f"s-{symbol}",
                               side="SELL", status="FILLED",
                               order_type="STOP_MARKET", last_filled_qty=10.0,
                               cumulative_qty=10.0, avg_price=0.98,
                               realized_pnl=-2.0, raw={})

        e = _engine(book=["AAAUSDT", "BBBUSDT"], portfolio=True, api=_MultiAPI())
        e.on_order(stop("AAAUSDT"))
        once = e.state.realized_today
        e.on_order(stop("AAAUSDT"))
        twice = e.state.realized_today
        return abs(once - twice) < 1e-9 and abs(once + 2.0) < 1e-9, (
            f"realized_today {once} -> {twice} after the replay")

    def one_symbol_failing_does_not_abort_the_rest():
        """A read failure on one symbol must not strand the other 24."""
        from bot.binanceapi import BinanceError
        held = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

        class Partial(_MultiAPI):
            def positions(self, symbol=None):
                if symbol == "BBBUSDT":
                    raise BinanceError(-1001, "read failed", "/positionRisk")
                return super().positions(symbol)

        api = Partial(open_symbols=held,
                      open_orders={s: [f"e-{s}"] for s in held})
        e = _engine(book=held, portfolio=True, api=api)
        e.cancel_orders_safely(keep_protective=False)
        touched = {c[1] for c in api.calls}
        return {"AAAUSDT", "CCCUSDT"} <= touched, (
            f"handled {sorted(touched)} despite one symbol failing to read")

    def close_all_survives_an_exception_not_just_a_refusal():
        """S5 made close_position return False. An unexpected raise must also
        be counted as a failure, not silently as a close."""
        held = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]

        class Exploding(_MultiAPI):
            def positions(self, symbol=None):
                if symbol == "BBBUSDT":
                    raise RuntimeError("boom")
                return super().positions(symbol)

        e = _engine(book=held, portfolio=True, api=Exploding(open_symbols=held))
        alerts = []
        e.notify.send = lambda ev, body, **kw: alerts.append(ev.name)
        closed = e.close_all("qa")
        return closed == 2 and "BBBUSDT" in e.book and "ERROR" in alerts, (
            f"closed={closed}, still tracked {sorted(e.book)}, "
            f"alerted={'ERROR' in alerts}")

    def releasing_one_position_keeps_the_others_dedupe():
        """Alert de-duplication is per position; releasing one must not reset
        the thresholds already fired for another."""
        e = _engine(book=["AAAUSDT", "BBBUSDT"], portfolio=True, api=_MultiAPI())
        e.notify._fired = {"sl:e-AAAUSDT", "sl:e-BBBUSDT"}
        e.release("AAAUSDT")
        return e.notify._fired == {"sl:e-BBBUSDT"}, (
            f"remaining keys {sorted(e.notify._fired)}")

    probes = [
        ("risk is conserved at every account size",
         risk_is_conserved_at_every_account_size, "blocker"),
        ("a lone qualifier gets the single-position cap",
         a_lone_qualifier_gets_the_single_position_cap, "high"),
        ("slots scale with equity", slots_scale_with_equity, "medium"),
        ("scan fills exactly the affordable number of slots",
         scan_fills_exactly_the_affordable_number_of_slots, "high"),
        ("the same symbol is never opened twice",
         the_same_symbol_is_never_opened_twice, "blocker"),
        ("the portfolio gate refuses a breach",
         the_portfolio_gate_refuses_a_breach, "blocker"),
        ("portfolio off still means one position",
         portfolio_off_still_means_one_position, "high"),
        ("an order update finds its own position",
         an_order_update_finds_its_own_position, "blocker"),
        ("reconciling one symbol does not wipe the book",
         reconciling_one_symbol_does_not_wipe_the_book, "blocker"),
        ("shutdown cancels orders on every held symbol",
         shutdown_cancels_orders_on_every_held_symbol, "high"),
        ("close_all reports the truth", close_all_reports_the_truth, "medium"),
        ("a failed close keeps its protection",
         a_failed_close_keeps_its_protection, "blocker"),
        ("the target flattens the whole book",
         the_target_flattens_the_whole_book, "high"),
        ("KILL honours its configured action",
         kill_honours_its_configured_action, "blocker"),
        ("the stream tracks the held set", the_stream_tracks_the_held_set, "high"),
        ("aggressive off changes nothing", aggressive_off_changes_nothing, "blocker"),
        ("aggressive on replaces the profile and warns",
         aggressive_on_replaces_the_profile_and_warns, "high"),
        ("aggressive cannot disable the hard invariants",
         aggressive_cannot_disable_the_hard_invariants, "blocker"),
        ("the ruin warning is computed, not hardcoded",
         the_ruin_warning_is_computed_not_hardcoded, "medium"),
        ("an unheld symbol is ignored, not crashed",
         an_unheld_symbol_is_ignored_not_crashed, "high"),
        ("a replayed fill is not booked twice",
         a_replayed_fill_is_not_booked_twice, "blocker"),
        ("one symbol failing does not abort the rest",
         one_symbol_failing_does_not_abort_the_rest, "high"),
        ("close_all survives an exception, not just a refusal",
         close_all_survives_an_exception_not_just_a_refusal, "high"),
        ("releasing one position keeps the others' de-duplication",
         releasing_one_position_keeps_the_others_dedupe, "medium"),
    ]
    for name, fn, sev in probes:
        check(report, "portfolio", name, fn, severity=sev)


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
               "audit": layer_audit, "portfolio": layer_portfolio,
               "cli": layer_cli, "net": layer_net}
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

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        self.calls.append(("set_margin_type", symbol, margin_type))
        return {"msg": "ok"}

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        return {"leverage": leverage}
