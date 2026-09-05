"""
Alerting: Telegram and email.

Two rules shape this module:
  * an alert must never be able to kill the bot -- every send is wrapped
  * an alert must never spam -- proximity warnings fire once per threshold
    crossing per position, not once per price tick

Configure whichever channels you want in .env; unconfigured ones no-op.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from enum import Enum

log = logging.getLogger("notify")


class Event(Enum):
    STARTUP = "startup"
    TRADE_OPEN = "trade opened"
    APPROACH_TP = "approaching take-profit"
    APPROACH_SL = "approaching stop-loss"
    TP_HIT = "take-profit hit"
    SL_HIT = "stop-loss hit"
    TARGET_REACHED = "daily target reached"
    TARGET_RAISED = "daily target raised"
    HALT = "HALTED"
    DAILY_SUMMARY = "daily summary"
    ERROR = "error"


# Events important enough to email; the rest go to Telegram only, so your
# inbox does not become a tick feed.
EMAIL_EVENTS = {Event.TRADE_OPEN, Event.APPROACH_TP, Event.APPROACH_SL,
                Event.TP_HIT, Event.SL_HIT, Event.TARGET_REACHED,
                Event.TARGET_RAISED, Event.HALT, Event.DAILY_SUMMARY}

ICON = {
    Event.STARTUP: "[ * ]", Event.TRADE_OPEN: "[ > ]",
    Event.APPROACH_TP: "[ ~ ]", Event.APPROACH_SL: "[ ! ]",
    Event.TP_HIT: "[ + ]", Event.SL_HIT: "[ - ]",
    Event.TARGET_REACHED: "[ = ]", Event.TARGET_RAISED: "[ ^ ]",
    Event.HALT: "[!!!]", Event.DAILY_SUMMARY: "[ i ]", Event.ERROR: "[ x ]",
}


@dataclass
class TelegramChannel:
    token: str = ""
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, subject: str, body: str) -> None:
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": f"{subject}\n{body}"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self.token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=8) as r:
            json.loads(r.read())


@dataclass
class EmailChannel:
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    to: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.host and self.user and self.password and self.to)

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.user
        msg["To"] = self.to
        msg.set_content(body)
        ctx = ssl.create_default_context()
        if self.port == 465:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=15, context=ctx) as s:
                s.login(self.user, self.password)
                s.send_message(msg)
        else:
            with smtplib.SMTP(self.host, self.port, timeout=15) as s:
                s.starttls(context=ctx)
                s.login(self.user, self.password)
                s.send_message(msg)


@dataclass
class Notifier:
    telegram: TelegramChannel = field(default_factory=TelegramChannel)
    email: EmailChannel = field(default_factory=EmailChannel)
    symbol: str = ""
    #: returns a short "mode · strategy" string, shown on EVERY message so a
    #: glance at any alert answers "what is it running right now"
    context: object = None
    listener: object = None          # called (event, subject, body) for every alert
    _fired: set = field(default_factory=set, repr=False)

    @classmethod
    def from_config(cls, cfg) -> "Notifier":
        return cls(
            telegram=TelegramChannel(cfg.telegram_token, cfg.telegram_chat_id),
            email=EmailChannel(cfg.smtp_host, cfg.smtp_port, cfg.smtp_user,
                               cfg.smtp_password, cfg.alert_email),
            symbol=cfg.symbol,
        )

    @property
    def channels(self) -> list[str]:
        out = []
        if self.telegram.enabled:
            out.append("telegram")
        if self.email.enabled:
            out.append(f"email->{self.email.to}")
        return out or ["none configured (log only)"]

    # ------------------------------------------------------------------ send
    def send(self, event: Event, body: str, dedupe_key: str | None = None) -> None:
        """
        dedupe_key suppresses repeats: pass something that identifies the
        *state*, e.g. "approach_sl:<order id>", so a 90%-of-the-way-to-stop
        warning is sent once rather than every two seconds.
        """
        if dedupe_key is not None:
            if dedupe_key in self._fired:
                return
            self._fired.add(dedupe_key)
            # Non-position keys (stale:, halt:, raise:) are never cleared by
            # clear_position_alerts, so cap the set on a long-lived process.
            # (QA F19)
            if len(self._fired) > 512:
                for stale in list(self._fired)[:256]:
                    self._fired.discard(stale)

        tag = ""
        if self.context is not None:
            try:
                tag = self.context() or ""
            except Exception:
                tag = ""
        subject = (f"{ICON.get(event, '[ ? ]')} {self.symbol} {event.value}"
                   + (f"\n{tag}" if tag else ""))
        log.info("ALERT %s | %s", subject, body.replace("\n", " | "))

        if self.listener is not None:
            try:
                self.listener(event, subject, body)
            except Exception as e:
                log.warning("alert listener failed: %s", e)

        if self.telegram.enabled:
            self._safe(self.telegram, subject, body)
        if self.email.enabled and event in EMAIL_EVENTS:
            self._safe(self.email, subject, body)

    @staticmethod
    def _safe(channel, subject: str, body: str) -> None:
        try:
            channel.send(subject, body)
        except Exception as e:                    # never let alerting stop trading
            log.warning("%s send failed: %s", type(channel).__name__, e)

    def clear_position_alerts(self, tag: str) -> None:
        """Called when a position closes, so the next one can alert again."""
        self._fired = {k for k in self._fired if not k.endswith(tag)}

    def test(self) -> None:
        self.send(Event.STARTUP,
                  "If you are reading this, alerting works.\n"
                  f"Channels: {', '.join(self.channels)}")
