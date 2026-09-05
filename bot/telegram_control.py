"""
Telegram as a control channel: monitor and act on the bot from your phone.

Why this exists alongside the web dashboard: it is **outbound only**. The bot
long-polls Telegram, so the host needs no inbound port, no tunnel, and nothing
opened in a cloud security list. On a public-IP VPS that is a materially better
posture than exposing an endpoint that can close trades.

Security model, in order of importance:

  1. ALLOWLIST. Only the configured chat id may issue commands. Anyone can
     find a bot by username and message it, so every other sender is refused
     and logged. This is the whole security boundary -- get it wrong and the
     internet can close your trades.
  2. CONFIRMATION. Destructive actions (close, halt, resume) return an inline
     keyboard; nothing executes until the button is pressed, and the pending
     action expires after CONFIRM_TTL seconds.
  3. NO EXECUTION HERE. Like the dashboard, this thread never calls the
     exchange. It pushes onto the same Command queue the engine drains in its
     own loop, so order placement stays single-threaded.

The bot token is a credential equivalent to the dashboard token: whoever holds
it can read your alerts. Keep it in .env.
"""

from __future__ import annotations

import json
import logging
import queue
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .dashboard import Command

log = logging.getLogger("telegram")

API = "https://api.telegram.org"
# The whole stop path must fit inside systemd's TimeoutStopSec, or SIGKILL
# lands before orders are cancelled. Budget: poll 20s + socket slack 5s, and
# stop() waits only JOIN_TIMEOUT because the thread is a daemon. (QA R3)
POLL_TIMEOUT = 20          # long-poll; Telegram holds the connection open
SOCKET_SLACK = 5
JOIN_TIMEOUT = 5
CONFIRM_TTL = 60           # seconds a pending confirmation stays valid
BACKOFF_MAX = 60

HELP = """Commands

/status  — equity, day target, position, market regime
/strategy — show how the strategy is being chosen
/strategy auto|trend_atr|mean_reversion|none — override it
/pnl     — today's realised P&L and progress
/target  — the schedule and what it asks of the account
/close   — close the open position (confirm)
/halt    — stop opening new trades (confirm)
/resume  — clear a halt (confirm)
/help    — this message

Monitoring is read-only. The three actions ask for confirmation first."""


@dataclass
class Pending:
    action: str
    created: float = field(default_factory=time.time)
    value: str = ""

    @property
    def expired(self) -> bool:
        return time.time() - self.created > CONFIRM_TTL


class TelegramControl:
    """Mirrors the Dashboard interface: publish(), pop_commands(), start(), stop()."""

    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = str(chat_id)
        self.commands: queue.Queue[Command] = queue.Queue()
        self._snapshot: dict = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._offset = 0
        self._pending: dict[str, Pending] = {}
        self.connected = threading.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ----------------------------------------------------------------- data
    def publish(self, snapshot: dict) -> None:
        with self._lock:
            self._snapshot = snapshot

    def read(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def pop_commands(self) -> list[Command]:
        out = []
        while True:
            try:
                out.append(self.commands.get_nowait())
            except queue.Empty:
                return out

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if not self.enabled:
            log.info("telegram control disabled (no token or chat id)")
            return
        self._thread = threading.Thread(target=self._run, name="telegram", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            # Daemon thread: an in-flight long poll dies with the process, so
            # there is no reason to block shutdown waiting for it.
            self._thread.join(timeout=JOIN_TIMEOUT)

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            try:
                updates = self._get_updates()
                self.connected.set()
                backoff = 1
                for u in updates:
                    self._handle(u)
            except Exception as e:
                self.connected.clear()
                # 409 means another process is polling the same token.
                if "409" in str(e):
                    log.error("another instance is polling this Telegram token. "
                              "Only one bot process may use a token at a time.")
                log.warning("telegram poll failed (%s); retrying in %ds", e, backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)

    # ------------------------------------------------------------------ http
    def _call(self, method: str, params: dict, timeout: int = 15):
        data = urllib.parse.urlencode(
            {k: (json.dumps(v) if isinstance(v, (dict, list)) else v)
             for k, v in params.items()}).encode()
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"{e.code} {e.read()[:200]!r}") from None
        if not payload.get("ok"):
            raise RuntimeError(payload.get("description", "unknown telegram error"))
        return payload["result"]

    def _get_updates(self) -> list:
        return self._call("getUpdates", {
            "offset": self._offset,
            "timeout": POLL_TIMEOUT,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=POLL_TIMEOUT + SOCKET_SLACK)

    def send(self, text: str, keyboard: list | None = None) -> None:
        params = {"chat_id": self.chat_id, "text": text}
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self._call("sendMessage", params)
        except Exception as e:
            log.warning("telegram send failed: %s", e)

    # -------------------------------------------------------------- routing
    def _authorised(self, chat_id) -> bool:
        """The entire security boundary. Anyone can message a public bot."""
        if str(chat_id) == self.chat_id:
            return True
        log.warning("ignoring telegram message from unauthorised chat %s", chat_id)
        return False

    def _handle(self, update: dict) -> None:
        self._offset = max(self._offset, update["update_id"] + 1)

        if "callback_query" in update:
            self._handle_callback(update["callback_query"])
            return

        msg = update.get("message") or {}
        chat = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip()
        if not text or chat is None:
            return
        if not self._authorised(chat):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].lower() if len(parts) > 1 else ""
        handlers = {
            "/start": lambda: self.send(HELP),
            "/help": lambda: self.send(HELP),
            "/status": self._send_status,
            "/pnl": self._send_pnl,
            "/target": self._send_target,
            "/strategy": lambda: self._strategy(arg),
            "/close": lambda: self._ask("close", "Close the open position at market?"),
            "/halt": lambda: self._ask("halt", "Stop opening new trades?"),
            "/resume": lambda: self._ask("resume", "Clear the halt and resume trading?"),
        }
        handler = handlers.get(cmd)
        if handler is None:
            self.send(f"Unknown command {cmd}. Send /help.")
            return
        handler()

    def _ask(self, action: str, question: str) -> None:
        """Destructive actions are two-step: ask, then act on the button press."""
        snap = self.read()
        if action == "close" and not snap.get("position"):
            self.send("No open position to close.")
            return
        if action == "resume" and not snap.get("halted"):
            self.send("The bot is not halted.")
            return
        if action == "halt" and snap.get("halted"):
            self.send("Already halted.")
            return

        self._sweep_pending()
        nonce = secrets.token_urlsafe(8)
        self._pending[nonce] = Pending(action)
        detail = ""
        if action == "close" and snap.get("position"):
            p = snap["position"]
            detail = (f"\n\n{p['side']} {p['qty']:g} @ {p['entry']:,.4f}\n"
                      f"unrealised {p['unrealized']:+.2f} USDT")
        elif action == "resume":
            detail = f"\n\nHalt reason:\n{snap.get('halt_reason', '')}"
        self.send(f"{question}{detail}\n\nExpires in {CONFIRM_TTL}s.",
                  keyboard=[[{"text": f"Yes, {action}", "callback_data": f"{action}:{nonce}"},
                             {"text": "Cancel", "callback_data": f"cancel:{nonce}"}]])

    def _sweep_pending(self) -> None:
        """Drop expired confirmations. Called from both sides, so repeatedly
        sending /close without ever pressing a button cannot grow the dict.
        (QA R8)"""
        for k, v in list(self._pending.items()):
            if v.expired:
                del self._pending[k]

    def _handle_callback(self, cq: dict) -> None:
        chat = ((cq.get("message") or {}).get("chat") or {}).get("id")
        if not self._authorised(chat):
            return
        data = cq.get("data", "")
        try:
            self._call("answerCallbackQuery", {"callback_query_id": cq["id"]})
        except Exception:
            pass

        action, _, nonce = data.partition(":")
        pending = self._pending.pop(nonce, None)

        self._sweep_pending()

        if action == "cancel":
            self.send("Cancelled.")
            return
        if pending is None or pending.expired or pending.action != action:
            self.send("That confirmation has expired. Send the command again.")
            return

        self.commands.put(Command(action, note="via telegram",
                                  value=getattr(pending, "value", "")))
        log.info("telegram command queued: %s", action)
        self.send(f"{action} queued. You will get a confirmation when it completes.")

    # -------------------------------------------------------------- messages
    def _send_status(self) -> None:
        s = self.read()
        if not s:
            self.send("No data yet -- the bot may still be starting.")
            return
        lines = [
            f"{s.get('symbol', '?')} · {s.get('mode', '?')}"
            + (" · dry run" if s.get("dry_run") else ""),
            "",
            f"equity      ${s.get('equity', 0):,.2f}",
            f"price       {s.get('price', 0):,.4f}",
            f"today       {s.get('realized_today', 0):+.2f} USDT",
            f"trades      {s.get('trades_today', 0)} today",
            f"stream      {'live' if s.get('stream_ok') else 'DOWN'}",
        ]
        if s.get("regime"):
            lines += ["", f"regime      {s['regime']}"]
        if s.get("halted"):
            lines += ["", f"HALTED: {s.get('halt_reason', '')}"]
        p = s.get("position")
        if p:
            lines += [
                "",
                f"{p['side']} {p['qty']:g} @ {p['entry']:,.4f}",
                f"unrealised  {p['unrealized']:+.2f} USDT",
                f"to TP       {p['to_tp']*100:5.1f}%  ({p['take_profit']:,.4f})",
                f"to SL       {p['to_sl']*100:5.1f}%  ({p['stop']:,.4f})",
            ]
        else:
            lines += ["", "flat"]
        self.send("\n".join(lines))

    def _send_pnl(self) -> None:
        s = self.read()
        realized = s.get("realized_today", 0.0)
        target = s.get("target", 0.0)
        pct = s.get("target_pct", 0.0)
        filled = max(0, min(20, int(pct / 100 * 20)))
        bar = "#" * filled + "." * (20 - filled)
        msg = (f"day {s.get('day', '?')}\n"
               f"[{bar}] {pct:.0f}%\n"
               f"{realized:+.2f} of ${target:.2f} target")
        if s.get("target_reached"):
            msg += ("\n\nTarget banked. "
                    + ("No further trades today." if s.get("stop_when_reached")
                       else "Still trading."))
        self.send(msg)

    def _strategy(self, arg: str) -> None:
        s = self.read()
        mode = s.get("strategy_mode", "fixed")
        choices = s.get("strategy_choices") or []
        if not arg:
            msg = [f"selection: {mode}"]
            if s.get("regime"):
                msg.append(f"reading:   {s['regime']}")
            if choices:
                msg += ["", "change with:", "  " + "  ".join(f"/strategy {c}" for c in choices)]
            else:
                msg.append("this bot runs a single fixed strategy")
            self.send("\n".join(msg))
            return
        if choices and arg not in choices:
            self.send(f"unknown option {arg!r}. Choose from: {', '.join(choices)}")
            return
        # Switching how trades are chosen is a state change, so it confirms.
        self._sweep_pending()
        nonce = secrets.token_urlsafe(8)
        self._pending[nonce] = Pending("strategy")
        self._pending[nonce].value = arg
        self.send(f"Set strategy selection to {arg}?\n\ncurrently: {mode}\n"
                  f"Expires in {CONFIRM_TTL}s.",
                  keyboard=[[{"text": f"Yes, {arg}", "callback_data": f"strategy:{nonce}"},
                             {"text": "Cancel", "callback_data": f"cancel:{nonce}"}]])

    def _send_target(self) -> None:
        s = self.read()
        self.send(s.get("target_note") or "No schedule loaded yet.")
