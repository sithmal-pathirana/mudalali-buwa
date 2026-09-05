"""
Broadcast entries to a separate Telegram channel, for sharing.

Deliberately different from the control channel:

  * ONE-WAY. It never reads updates, so nobody in the channel can send the bot
    a command. The control channel keeps its allowlist of one.
  * NO ACCOUNT DETAIL. Position size, equity, P&L and slot counts never leave.
    A signal is a symbol, a direction, and the three prices -- everything a
    reader needs and nothing about your balance.
  * MODE IS ALWAYS STATED. A testnet or dry-run signal says so in the first
    line. Readers acting on paper trades they believed were live is the
    failure mode this exists to prevent.

Honesty note worth keeping in view: no strategy in this repository is
profitable out-of-sample. Publishing its entries is publishing an experiment,
and the footer says so on every message rather than in a pinned post nobody
reads.
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from dataclasses import dataclass

log = logging.getLogger("signals")

API = "https://api.telegram.org"


@dataclass
class SignalChannel:
    token: str = ""
    chat_id: str = ""
    #: appended to every message; readers deserve to know what they are seeing
    disclaimer: str = ("Automated signal. Not advice. Do your own research.")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    # ------------------------------------------------------------ sending
    def _post(self, text: str) -> None:
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "text": text,
             "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(f"{API}/bot{self.token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=10) as r:
            json.loads(r.read())

    def send(self, text: str) -> None:
        """Never let a broadcast failure touch the trading loop."""
        if not self.enabled:
            return
        try:
            self._post(text)
        except Exception as e:
            log.warning("signal broadcast failed: %s", e)

    # ------------------------------------------------------------ formats
    @staticmethod
    def _mode_banner(mode: str, dry_run: bool) -> str:
        if dry_run:
            return "PAPER / DRY RUN -- no real order was placed"
        if mode != "live":
            return "TESTNET -- simulated funds, not a live position"
        return "LIVE"

    def entry(self, symbol: str, side: str, entry: float, stop: float,
              take_profit: float, mode: str, dry_run: bool,
              reason: str = "") -> None:
        rr = ""
        risk = abs(entry - stop)
        reward = abs(take_profit - entry)
        if risk > 0 and reward > 0:
            rr = f"\nRisk/reward   1:{reward / risk:.1f}"
        self.send(
            f"{self._mode_banner(mode, dry_run)}\n"
            f"\n{side} {symbol}\n"
            f"\nEntry         {entry:,.6f}".rstrip("0").rstrip(".")
            + f"\nStop loss     {stop:,.6f}".rstrip("0").rstrip(".")
            + f"\nTake profit   {take_profit:,.6f}".rstrip("0").rstrip(".")
            + rr
            + (f"\n\nSetup: {reason}" if reason else "")
            + f"\n\n{self.disclaimer}")

    def closed(self, symbol: str, exit_price: float, pnl_pct: float,
               outcome: str, mode: str, dry_run: bool) -> None:
        """
        Percentage only -- never the dollar amount, which would leak position
        size and therefore the account.
        """
        self.send(
            f"{self._mode_banner(mode, dry_run)}\n"
            f"\n{symbol} closed -- {outcome}\n"
            f"\nExit          {exit_price:,.6f}".rstrip("0").rstrip(".")
            + f"\nResult        {pnl_pct:+.2f}%"
            + f"\n\n{self.disclaimer}")
