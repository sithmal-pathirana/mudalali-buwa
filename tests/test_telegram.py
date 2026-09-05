"""
Telegram control: authorisation, confirmation, and command queueing.

No network. `_call` and `send` are stubbed, and synthetic Telegram updates are
fed straight into the router.
"""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.telegram_control import CONFIRM_TTL, TelegramControl   # noqa: E402

MINE = "123456"
THEIRS = "999999"

SNAPSHOT = {
    "symbol": "BTCUSDT", "mode": "testnet", "dry_run": True, "equity": 43.0,
    "price": 80_000.0, "realized_today": 1.25, "day": 1, "target": 2.0,
    "target_pct": 62.5, "target_reached": False, "stop_when_reached": True,
    "target_note": "day 1: target $2.00/day", "halted": False, "halt_reason": "",
    "trades_today": 1, "stream_ok": True,
    "position": {"side": "BUY", "qty": 0.0012, "entry": 79_400.0, "stop": 78_200.0,
                 "take_profit": 82_400.0, "unrealized": 0.72,
                 "to_tp": 0.2, "to_sl": 0.1},
}


def message(text, chat=MINE, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


def callback(data, chat=MINE, uid=2):
    return {"update_id": uid,
            "callback_query": {"id": "cb1", "data": data,
                               "message": {"chat": {"id": int(chat)}}}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tc = TelegramControl("token", MINE)
        self.tc.publish(dict(SNAPSHOT))
        self.sent = []
        self.keyboards = []

        def fake_send(text, keyboard=None):
            self.sent.append(text)
            self.keyboards.append(keyboard)

        self.tc.send = fake_send
        self.tc._call = lambda *a, **k: {}      # answerCallbackQuery etc.

    def last(self):
        return self.sent[-1] if self.sent else ""

    def nonce_from_last_keyboard(self):
        return self.keyboards[-1][0][0]["callback_data"].split(":", 1)[1]


class TestAuthorisation(Base):
    """The allowlist is the whole security boundary."""

    def test_message_from_another_chat_is_ignored(self):
        self.tc._handle(message("/status", chat=THEIRS))
        self.assertEqual(self.sent, [], "replied to an unauthorised chat")

    def test_command_from_another_chat_queues_nothing(self):
        self.tc._handle(message("/close", chat=THEIRS))
        self.assertEqual(self.tc.pop_commands(), [])

    def test_callback_from_another_chat_is_ignored(self):
        self.tc._handle(message("/close"))
        nonce = self.nonce_from_last_keyboard()
        self.tc._handle(callback(f"close:{nonce}", chat=THEIRS))
        self.assertEqual(self.tc.pop_commands(), [],
                         "a stranger pressed the confirm button and it worked")

    def test_authorised_chat_is_served(self):
        self.tc._handle(message("/status"))
        self.assertIn("BTCUSDT", self.last())

    def test_chat_id_comparison_is_type_insensitive(self):
        """Telegram sends ints; .env gives strings."""
        self.assertTrue(self.tc._authorised(int(MINE)))
        self.assertTrue(self.tc._authorised(MINE))
        self.assertFalse(self.tc._authorised(int(THEIRS)))


class TestReadOnlyCommands(Base):
    def test_status_reports_position_and_equity(self):
        self.tc._handle(message("/status"))
        out = self.last()
        for expected in ("equity", "43.00", "BUY", "to TP", "to SL"):
            self.assertIn(expected, out)

    def test_status_flags_a_halt(self):
        self.tc.publish(dict(SNAPSHOT, halted=True, halt_reason="daily loss limit"))
        self.tc._handle(message("/status"))
        self.assertIn("HALTED", self.last())

    def test_pnl_renders_progress(self):
        self.tc._handle(message("/pnl"))
        self.assertIn("+1.25", self.last())
        self.assertIn("2.00", self.last())

    def test_help_is_offered_for_unknown_commands(self):
        self.tc._handle(message("/liquidate"))
        self.assertIn("/help", self.last())

    def test_read_only_commands_queue_nothing(self):
        for c in ("/status", "/pnl", "/target", "/help"):
            self.tc._handle(message(c))
        self.assertEqual(self.tc.pop_commands(), [])


class TestConfirmation(Base):
    def test_close_asks_before_acting(self):
        self.tc._handle(message("/close"))
        self.assertEqual(self.tc.pop_commands(), [], "acted without confirmation")
        self.assertIsNotNone(self.keyboards[-1])

    def test_confirmed_close_is_queued(self):
        self.tc._handle(message("/close"))
        self.tc._handle(callback(f"close:{self.nonce_from_last_keyboard()}"))
        self.assertEqual([c.action for c in self.tc.pop_commands()], ["close"])

    def test_cancel_queues_nothing(self):
        self.tc._handle(message("/close"))
        self.tc._handle(callback(f"cancel:{self.nonce_from_last_keyboard()}"))
        self.assertEqual(self.tc.pop_commands(), [])
        self.assertIn("Cancelled", self.last())

    def test_a_nonce_cannot_be_replayed(self):
        self.tc._handle(message("/close"))
        nonce = self.nonce_from_last_keyboard()
        self.tc._handle(callback(f"close:{nonce}"))
        self.tc.pop_commands()
        self.tc._handle(callback(f"close:{nonce}"))
        self.assertEqual(self.tc.pop_commands(), [], "confirmation was replayable")

    def test_expired_confirmation_is_refused(self):
        self.tc._handle(message("/close"))
        nonce = self.nonce_from_last_keyboard()
        self.tc._pending[nonce].created = time.time() - CONFIRM_TTL - 1
        self.tc._handle(callback(f"close:{nonce}"))
        self.assertEqual(self.tc.pop_commands(), [])
        self.assertIn("expired", self.last())

    def test_a_nonce_cannot_be_used_for_a_different_action(self):
        self.tc._handle(message("/halt"))
        nonce = self.nonce_from_last_keyboard()
        self.tc._handle(callback(f"close:{nonce}"))
        self.assertEqual(self.tc.pop_commands(), [])

    def test_unknown_nonce_is_refused(self):
        self.tc._handle(callback("close:not-a-real-nonce"))
        self.assertEqual(self.tc.pop_commands(), [])


class TestGuards(Base):
    def test_close_with_no_position_is_refused_early(self):
        self.tc.publish(dict(SNAPSHOT, position=None))
        self.tc._handle(message("/close"))
        self.assertIn("No open position", self.last())
        self.assertIsNone(self.keyboards[-1])

    def test_resume_when_not_halted_is_refused(self):
        self.tc._handle(message("/resume"))
        self.assertIn("not halted", self.last())

    def test_halt_when_already_halted_is_refused(self):
        self.tc.publish(dict(SNAPSHOT, halted=True, halt_reason="x"))
        self.tc._handle(message("/halt"))
        self.assertIn("Already halted", self.last())

    def test_resume_is_offered_when_halted(self):
        self.tc.publish(dict(SNAPSHOT, halted=True, halt_reason="daily loss limit"))
        self.tc._handle(message("/resume"))
        self.assertIsNotNone(self.keyboards[-1])
        self.tc._handle(callback(f"resume:{self.nonce_from_last_keyboard()}"))
        self.assertEqual([c.action for c in self.tc.pop_commands()], ["resume"])


class TestPlumbing(Base):
    def test_offset_advances_so_updates_are_not_reprocessed(self):
        self.tc._handle(message("/status", uid=41))
        self.assertEqual(self.tc._offset, 42)

    def test_disabled_without_a_chat_id(self):
        self.assertFalse(TelegramControl("token", "").enabled)
        self.assertFalse(TelegramControl("", MINE).enabled)
        self.assertTrue(TelegramControl("token", MINE).enabled)

    def test_group_style_command_suffix_is_handled(self):
        self.tc._handle(message("/status@my_trading_bot"))
        self.assertIn("BTCUSDT", self.last())

    def test_matches_the_dashboard_controller_interface(self):
        """The engine drives both through the same three methods."""
        from bot.dashboard import Dashboard
        for name in ("publish", "pop_commands", "start", "stop"):
            self.assertTrue(hasattr(TelegramControl, name))
            self.assertTrue(hasattr(Dashboard, name))


class TestControlDisabledIsDiscoverable(unittest.TestCase):
    """
    Alerts arriving while commands are ignored looks like a broken bot, and the
    one channel that could explain it is the one switched off. So the engine
    must say it loudly at startup, and `doctor` must answer it directly.
    """

    def test_engine_warns_rather_than_informs(self):
        import inspect

        from bot.engine import Engine
        src = inspect.getsource(Engine.startup)
        block = src.split("not self.cfg.telegram.control")[1][:700]
        self.assertIn("log.warning", block, "still only an INFO line")
        self.assertIn("control: true", block, "does not say how to fix it")

    def test_warning_names_the_config_file(self):
        from bot.config import Config
        cfg = Config.load()
        self.assertTrue(cfg.config_path, "config does not record where it loaded from")
        self.assertTrue(cfg.config_path.endswith(".yaml"))

    def test_doctor_reports_control_surface_state(self):
        src = (ROOT / "run.py").read_text()
        block = src.split("control surfaces")[1][:900]
        self.assertIn("commands ON", block)
        self.assertIn("commands OFF", block)
        self.assertIn("control: true", block)

    def test_missing_chat_id_is_called_out_separately(self):
        """Token without chat id is a different fault from control: false."""
        src = (ROOT / "run.py").read_text()
        block = src.split("control surfaces")[1][:900]
        self.assertIn("TELEGRAM_CHAT_ID missing", block)
        self.assertIn("allowlist", block)


class TestModeVisibility(unittest.TestCase):
    """Every message must answer 'what is it running' without a follow-up."""

    def test_notifier_prefixes_the_mode_line(self):
        from bot.notify import Event, Notifier
        n = Notifier(symbol="BTCUSDT")
        n.context = lambda: "[safe · switcher/auto · testnet · dry-run]"
        seen = []
        n._safe = staticmethod(lambda ch, subj, body: seen.append(subj))
        n.telegram = type("T", (), {"enabled": True, "send": lambda *a: None})()
        n.send(Event.TRADE_OPEN, "body")
        self.assertTrue(seen)
        self.assertIn("switcher/auto", seen[0])

    def test_a_failing_context_never_blocks_the_alert(self):
        from bot.notify import Event, Notifier
        n = Notifier(symbol="BTCUSDT")
        n.context = lambda: 1 / 0
        n.send(Event.TRADE_OPEN, "body")     # must not raise

    def test_engine_mode_line_names_profile_strategy_and_mode(self):
        import inspect
        from bot.engine import Engine
        src = inspect.getsource(Engine.mode_line)
        for term in ("AGGRESSIVE", "safe", "dry-run"):
            self.assertIn(term, src)

    def test_engine_publishes_the_mode_line(self):
        import inspect
        from bot.engine import Engine
        self.assertIn('"mode_line"', inspect.getsource(Engine.snapshot))


class TestExpandedControl(unittest.TestCase):
    def test_help_lists_monitor_and_control_sections(self):
        from bot.telegram_control import HELP
        for cmd in ("/status", "/positions", "/config", "/scan",
                    "/strategy", "/close", "/halt", "/resume"):
            self.assertIn(cmd, HELP)

    def test_close_accepts_a_symbol_argument(self):
        import inspect
        from bot.telegram_control import TelegramControl
        self.assertIn("value=arg.upper()",
                      inspect.getsource(TelegramControl._handle))

    def test_control_surface_still_calls_no_exchange_method(self):
        """The invariant the QA audit enforces; asserted here too."""
        import inspect
        from bot import telegram_control
        src = inspect.getsource(telegram_control)
        for forbidden in ("cancel_all", "usdt_equity", "set_leverage",
                          "user_trades", "all_orders"):
            self.assertNotIn(forbidden, src)
