"""
Regressions for the round-two QA findings (R1-R8).

The theme of round two was that halting and shutting down had become the same
code path, so the safest-looking button did the least safe thing. These tests
pin the separation.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import AlertConfig, Config, DashboardConfig, RiskConfig, TelegramConfig  # noqa: E402
from bot.engine import Engine                                  # noqa: E402
from bot.positions import ActivePosition                       # noqa: E402
from bot.strategies.base import Signal                         # noqa: E402


class StubAPI:
    """Records calls instead of making them."""

    DRIFT_WARN_MS = 500
    DRIFT_DANGER_MS = 900

    def __init__(self, position_amt=0.0, open_ids=()):
        self.calls = []
        self.position_amt = position_amt
        self.open_ids = list(open_ids)
        self._offset_ms = 0
        self.last_rtt_ms = 0
        # Recent, so periodic() does not try to resync against a stub.
        self.last_sync = __import__("time").time()

    def sync_clock(self):
        self.calls.append(("sync_clock", None))
        return 0

    def positions(self, symbol):
        self.calls.append(("positions", symbol))
        if self.position_amt == 0.0:
            return []
        return [{"positionAmt": str(self.position_amt), "entryPrice": "0.09",
                 "unRealizedProfit": "0", "liquidationPrice": "0"}]

    def open_orders(self, symbol=None):
        self.calls.append(("open_orders", symbol))
        return [{"clientOrderId": i, "side": "SELL", "type": "STOP_MARKET",
                 "origQty": "1", "price": "0"} for i in self.open_ids]

    def usdt_equity(self):
        self.calls.append(("usdt_equity", None))
        return 5000.0

    def set_margin_type(self, symbol, margin_type="ISOLATED"):
        self.calls.append(("set_margin_type", symbol, margin_type))
        return {"msg": "ok"}

    def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", symbol, leverage))
        return {"leverage": leverage}

    def cancel_all(self, symbol):
        self.calls.append(("cancel_all", symbol))

    def cancel_order(self, symbol, cid):
        self.calls.append(("cancel_order", symbol, cid))

    def order(self, **kw):
        self.calls.append(("order", kw.get("side"), kw.get("type"),
                           kw.get("reduceOnly")))
        return {"status": "NEW"}

    @property
    def cancelled_everything(self):
        return any(c[0] == "cancel_all" for c in self.calls)

    @property
    def reduce_only_closes(self):
        return [c for c in self.calls if c[0] == "order" and c[3] == "true"]


def position():
    return ActivePosition("DOGEUSDT", "BUY", 0.09, 0.0882, 0.0954, 100.0,
                          entry_order_id="e-1", stop_order_id="s-1",
                          tp_order_id="t-1", tag="e-1")


def engine(api=None, dry_run=False, active=None, halted=False,
           kill_action="flatten", position_amt=0.0):
    e = object.__new__(Engine)
    e.cfg = Config(dry_run=dry_run, symbol="DOGEUSDT",
                   risk=RiskConfig(kill_action=kill_action),
                   alerts=AlertConfig(), dashboard=DashboardConfig(),
                   telegram=TelegramConfig())
    e.api = api or StubAPI()
    e.active = active
    e.position_amt = position_amt
    e.rules = type("Rules", (), {
        "round_qty": lambda s, q: f"{q:.0f}",
        "round_price": lambda s, p: f"{p:.6f}"})()
    e.sent = []
    # The real State, pointed at a scratch file. A hand-rolled fake had to grow
    # a new method every time the engine touched one, and each gap surfaced as
    # an AttributeError in an unrelated test rather than a real finding.
    from bot.state import State
    st = State(path=ROOT / "data" / "test_r2_state.json")
    st.halted = halted
    st.halt_reason = "test" if halted else ""
    st.day_start_equity = 5000.0
    e.state = st

    from bot.targets import TargetSchedule
    e.schedule = TargetSchedule.from_config(
        {"stop_when_reached": True,
         "schedule": [{"from_day": 1, "usd_per_day": 2.0}]})
    e.schedule.start_date = st.day

    # A real RiskManager, like the real State above: hand-rolled fakes had to
    # grow a method every time the engine touched one.
    from bot.risk import RiskManager
    e.risk = RiskManager(e.cfg, st)
    e.equity = 5000.0
    e.last_price = 0.09
    e.events = __import__("collections").deque(maxlen=40)
    e.notify = type("N", (), {
        "send": lambda s, ev, body, dedupe_key=None: e.sent.append((ev, body)),
        "clear_position_alerts": lambda s, tag: None})()
    e.stream = None
    e.dashboard = None
    e.telegram = None
    e._stopping = False
    e._stop_reason = ""
    return e


def _cleanup():
    (ROOT / "data" / "test_r2_state.json").unlink(missing_ok=True)


class TestR1HaltIsNotShutdown(unittest.TestCase):
    def tearDown(self):
        _cleanup()


    def test_halt_leaves_protective_orders_on_the_book(self):
        api = StubAPI(position_amt=100.0, open_ids=["s-1", "t-1"])
        e = engine(api=api, active=position(), halted=True, position_amt=100.0)
        stop_new_trades = e.emergency_check()
        self.assertTrue(stop_new_trades)
        self.assertFalse(api.cancelled_everything,
                         "halting cancelled the protective stop")

    def test_halt_does_not_stop_the_process(self):
        e = engine(active=position(), halted=True, position_amt=100.0)
        e.emergency_check()
        self.assertFalse(e._stopping, "halting exited; it must only pause trading")

    def test_shutdown_with_a_position_open_keeps_the_stop(self):
        api = StubAPI(position_amt=100.0, open_ids=["e-1", "s-1"])
        e = engine(api=api, active=position(), position_amt=100.0)
        e.shutdown(reason="interrupted")
        self.assertFalse(api.cancelled_everything,
                         "shutdown stripped the stop from an open position")
        self.assertIn(("cancel_order", "DOGEUSDT", "e-1"), api.calls,
                      "the resting entry should still be pulled")

    def test_shutdown_when_flat_cancels_everything(self):
        api = StubAPI(position_amt=0.0)
        e = engine(api=api, active=None)
        e.shutdown(reason="interrupted")
        self.assertTrue(api.cancelled_everything)


class TestR2KillSwitch(unittest.TestCase):
    def test_flatten_closes_before_cancelling(self):
        api = StubAPI(position_amt=100.0, open_ids=["s-1"])
        e = engine(api=api, active=position(), position_amt=100.0,
                   kill_action="flatten")
        e.trigger_kill("kill file")
        self.assertTrue(api.reduce_only_closes,
                        "KILL cancelled orders without closing the position")
        order_idx = next(i for i, c in enumerate(api.calls) if c[0] == "order")
        cancel_idx = next(i for i, c in enumerate(api.calls) if c[0] == "cancel_all")
        self.assertLess(order_idx, cancel_idx,
                        "the position must be closed BEFORE the stop is cancelled")

    def test_protect_leaves_the_position_and_its_stop(self):
        api = StubAPI(position_amt=100.0, open_ids=["s-1"])
        e = engine(api=api, active=position(), position_amt=100.0,
                   kill_action="protect")
        e.trigger_kill("kill file")
        self.assertEqual(api.reduce_only_closes, [])
        self.assertFalse(api.cancelled_everything)

    def test_kill_is_idempotent(self):
        api = StubAPI(position_amt=100.0)
        e = engine(api=api, active=position(), position_amt=100.0)
        e.trigger_kill("first")
        n = len(api.calls)
        e.trigger_kill("second")
        self.assertEqual(len(api.calls), n, "kill ran twice")

    def test_kill_sets_the_stopping_flag_rather_than_raising(self):
        """An exception here would be swallowed by the loop's `except Exception`."""
        e = engine(active=position(), position_amt=100.0)
        e.trigger_kill("kill file")           # must not raise
        self.assertTrue(e._stopping)

    def test_kill_action_is_validated(self):
        cfg = Config(risk=RiskConfig(kill_action="ignore"))
        self.assertTrue(any("kill_action" in p for p in cfg.validate()))


class TestR3ShutdownBudget(unittest.TestCase):
    def test_controller_stop_fits_inside_the_systemd_timeout(self):
        from bot import telegram_control as tg
        unit = (ROOT / "deploy" / "trading-bot.service").read_text()
        timeout = int(next(l for l in unit.splitlines()
                           if l.startswith("TimeoutStopSec=")).split("=")[1])
        self.assertLess(tg.JOIN_TIMEOUT, timeout)
        self.assertLess(tg.POLL_TIMEOUT + tg.SOCKET_SLACK, timeout)

    def test_exchange_work_happens_before_controllers_are_stopped(self):
        import inspect
        src = inspect.getsource(Engine.shutdown)
        self.assertLess(src.index("cancel_orders_safely"), src.index("c.stop()"),
                        "controllers are stopped before orders are cancelled")


class TestR4DryRunFills(unittest.TestCase):
    def _dry(self):
        e = engine(dry_run=True, api=StubAPI())
        e.rules = type("R", (), {
            "size_for_notional": lambda s, n, p: ("100", "0.090000"),
            "round_price": lambda s, p: f"{p:.6f}",
            "round_qty": lambda s, q: f"{q:.0f}"})()
        e.risk = type("RM", (), {"record_attempt": lambda s: None,
                                 "record_fill": lambda s, p=0.0: None})()
        e._seq = 0
        e._entry_placed_at = 0.0
        e._dry_pending = None
        return e

    def test_entry_rests_instead_of_filling_instantly(self):
        e = self._dry()
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n")
        self.assertIsNone(e.active, "dry run booked an instant fill at the asked price")
        self.assertIsNotNone(e._dry_pending)

    def test_entry_fills_only_when_price_reaches_it(self):
        e = self._dry()
        e.place(Signal("BUY", entry=0.09, stop=0.0882, take_profit=0.0954), 20.0, "n")
        e.simulate_entry(0.0925)            # above a BUY limit: no fill
        self.assertIsNone(e.active)
        e.simulate_entry(0.0899)            # trades through: fills
        self.assertIsNotNone(e.active)

    def test_short_entry_fills_on_the_other_side(self):
        e = self._dry()
        e.rules.size_for_notional = lambda n, p: ("100", "0.090000")
        e.place(Signal("SELL", entry=0.09, stop=0.0918, take_profit=0.0846), 20.0, "n")
        e.simulate_entry(0.0880)
        self.assertIsNone(e.active)
        e.simulate_entry(0.0901)
        self.assertIsNotNone(e.active)


class TestR5StaleOrders(unittest.TestCase):
    def test_flat_with_a_stale_protective_order_releases_tracking(self):
        api = StubAPI()
        e = engine(api=api, active=position())
        e.reconcile_position({"position_amt": 0.0, "open_order_ids": {"s-1"}})
        self.assertIsNone(e.active, "a lingering stop parked the bot indefinitely")
        self.assertTrue(api.cancelled_everything)

    def test_flat_with_a_resting_entry_keeps_tracking(self):
        e = engine(api=StubAPI(), active=position())
        e._entry_placed_at = 0.0
        e.reconcile_position({"position_amt": 0.0, "open_order_ids": {"e-1"}})
        self.assertIsNotNone(e.active, "a resting entry must not be forgotten (F1)")

    def test_open_position_is_never_released(self):
        e = engine(api=StubAPI(position_amt=100.0), active=position())
        e.reconcile_position({"position_amt": 100.0, "open_order_ids": {"s-1"}})
        self.assertIsNotNone(e.active)


class TestR6Slippage(unittest.TestCase):
    def test_alert_reports_the_price_actually_asked(self):
        from bot.stream import OrderUpdate
        e = engine(api=StubAPI(), active=position(), position_amt=100.0)
        e.risk = type("RM", (), {"record_fill": lambda s, p=0.0: None})()
        e.schedule = type("S", (), {"progress": lambda s, r: "prog"})()
        upd = OrderUpdate(symbol="DOGEUSDT", client_order_id="e-1", side="BUY",
                          status="FILLED", order_type="LIMIT", last_filled_qty=100.0,
                          cumulative_qty=100.0, avg_price=0.0915,
                          realized_pnl=0.0, raw={})
        e.on_order(upd)
        body = " ".join(b for _, b in e.sent)
        self.assertIn("asked 0.0900", body,
                      "the alert printed the fill price as the asked price")
        self.assertIn("0.0915", body)


class TestR7R8Hygiene(unittest.TestCase):
    def test_telegram_control_is_opt_in(self):
        self.assertFalse(TelegramConfig().control,
                         "a control surface must be opted into, not inherited")

    def test_pending_confirmations_are_swept_when_asking(self):
        import inspect
        from bot.telegram_control import TelegramControl
        self.assertIn("_sweep_pending", inspect.getsource(TelegramControl._ask))

    def test_qa_reports_are_gitignored(self):
        self.assertIn("QA", (ROOT / ".gitignore").read_text())


class TestReconcileCadence(unittest.TestCase):
    """
    startup() reconciles, so the periodic loop must not immediately reconcile
    again. Left at 0.0, `_last_reconcile` made every start issue two signed
    account reads seconds apart -- visible in the logs and wasteful of the
    request weight that matters most.
    """

    def test_startup_primes_the_periodic_clock(self):
        import inspect

        from bot.engine import Engine
        src = inspect.getsource(Engine.startup)
        self.assertIn("self._last_reconcile = time.time()", src)

    def test_periodic_is_a_noop_immediately_after_startup(self):
        import time

        from bot.engine import Engine
        e = engine(api=StubAPI())
        e._last_reconcile = time.time()
        before = len(e.api.calls)
        Engine.periodic(e)
        self.assertEqual(len(e.api.calls), before,
                         "reconciled again right after startup already did")

    def test_periodic_does_run_once_the_interval_has_passed(self):
        import time

        from bot.engine import Engine
        e = engine(api=StubAPI())
        e.state.day_start_equity = 5000.0
        e._last_reconcile = time.time() - 3600
        Engine.periodic(e)
        self.assertGreater(len(e.api.calls), 0, "periodic never reconciles at all")


class TestForegroundWarning(unittest.TestCase):
    """
    A bot started from an SSH session dies at logout. The symptom -- "it was
    running, now Telegram is silent" -- points nowhere useful, so the warning
    has to appear at start, and only when it applies.
    """

    class _TTY:
        def isatty(self):
            return True

        def write(self, *a):
            pass

        def flush(self):
            pass

    def _run(self, env, tty=True):
        import io
        import os
        import sys

        from bot.engine import Engine
        saved_env = {k: os.environ.get(k) for k in ("INVOCATION_ID", "JOURNAL_STREAM")}
        saved_out = sys.stdout
        try:
            for k in saved_env:
                os.environ.pop(k, None)
            os.environ.update(env)
            sys.stdout = self._TTY() if tty else io.StringIO()
            return Engine.warn_if_foreground()
        finally:
            sys.stdout = saved_out
            for k, v in saved_env.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_warns_in_an_interactive_terminal(self):
        self.assertTrue(self._run({}))

    def test_silent_under_systemd(self):
        self.assertFalse(self._run({"INVOCATION_ID": "abc"}))
        self.assertFalse(self._run({"JOURNAL_STREAM": "8:123"}))

    def test_silent_when_output_is_redirected(self):
        """nohup or a redirect means the user already chose to detach."""
        self.assertFalse(self._run({}, tty=False))

    def test_startup_calls_it(self):
        import inspect

        from bot.engine import Engine
        self.assertIn("warn_if_foreground", inspect.getsource(Engine.startup))


class TestStartupAlertStatesCommandAvailability(unittest.TestCase):
    def test_startup_message_says_whether_commands_work(self):
        import inspect

        from bot.engine import Engine
        src = inspect.getsource(Engine.startup)
        self.assertIn("Commands are ON", src)
        self.assertIn("Commands are OFF", src)
        self.assertIn("telegram.control", src,
                      "the message must name the setting to change")
