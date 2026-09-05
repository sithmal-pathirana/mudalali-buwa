"""
The signal channel. Public by design, so what it must NOT say matters more
than what it says.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.signals import SignalChannel   # noqa: E402


def channel():
    ch = SignalChannel(token="t", chat_id="c")
    ch.sent = []
    ch._post = ch.sent.append
    return ch


class TestNeverLeaksTheAccount(unittest.TestCase):
    """A signal is a symbol, a direction and three prices. Nothing else."""

    def _entry(self, **kw):
        ch = channel()
        ch.entry("DOGEUSDT", "BUY", 0.0862, 0.0845, 0.0895,
                 mode=kw.get("mode", "live"), dry_run=kw.get("dry_run", False),
                 reason=kw.get("reason", ""))
        return ch.sent[0]

    def test_entry_carries_the_prices(self):
        msg = self._entry()
        for part in ("BUY DOGEUSDT", "Entry", "Stop loss", "Take profit"):
            self.assertIn(part, msg)

    def test_entry_never_mentions_size_or_balance(self):
        msg = self._entry().lower()
        for leak in ("qty", "quantity", "equity", "balance", "usdt notional",
                     "risking", "slot"):
            self.assertNotIn(leak, msg, f"signal leaked {leak}")

    def test_close_reports_percent_not_dollars(self):
        """
        Percent only. Checking for the substring "USDT" would match the SYMBOL
        (DOGEUSDT), so look for a currency amount instead -- that is the thing
        that would actually leak position size.
        """
        import re
        ch = channel()
        ch.closed("DOGEUSDT", 0.0895, 3.83, "take-profit", "live", False)
        msg = ch.sent[0]
        self.assertIn("+3.83%", msg)
        amounts = re.findall(r"[+-]?\d[\d,.]*\s*(?:USDT|usdt)\b", msg)
        self.assertEqual(amounts, [], f"a currency amount leaked: {amounts}")
        self.assertNotIn("$", msg)

    def test_source_never_touches_account_state(self):
        import inspect
        src = inspect.getsource(SignalChannel)
        for forbidden in ("usdt_equity", "realized_today", "positions(",
                          "qty", "notional"):
            self.assertNotIn(forbidden, src)


class TestModeIsAlwaysStated(unittest.TestCase):
    """Readers acting on paper trades they believed were live is the failure
    this exists to prevent."""

    def _banner(self, mode, dry_run):
        ch = channel()
        ch.entry("X", "BUY", 1.0, 0.9, 1.2, mode=mode, dry_run=dry_run)
        return ch.sent[0].splitlines()[0]

    def test_dry_run_says_paper(self):
        self.assertIn("PAPER", self._banner("testnet", True))

    def test_testnet_says_testnet(self):
        self.assertIn("TESTNET", self._banner("testnet", False))

    def test_live_says_live(self):
        self.assertEqual(self._banner("live", False), "LIVE")

    def test_the_banner_is_the_first_line(self):
        ch = channel()
        ch.entry("X", "BUY", 1.0, 0.9, 1.2, mode="testnet", dry_run=True)
        self.assertTrue(ch.sent[0].startswith("PAPER"))

    def test_every_message_carries_the_disclaimer(self):
        ch = channel()
        ch.entry("X", "BUY", 1.0, 0.9, 1.2, mode="live", dry_run=False)
        ch.closed("X", 1.2, 20.0, "take-profit", "live", False)
        for msg in ch.sent:
            self.assertIn("Not advice", msg)


class TestOneWay(unittest.TestCase):
    def test_it_never_reads_updates(self):
        import inspect
        src = inspect.getsource(SignalChannel)
        for reading in ("getUpdates", "_handle", "commands", "callback"):
            self.assertNotIn(reading, src,
                             "the signal channel must not accept input")

    def test_disabled_without_a_chat_id(self):
        self.assertFalse(SignalChannel(token="t").enabled)
        self.assertFalse(SignalChannel(chat_id="c").enabled)
        self.assertTrue(SignalChannel(token="t", chat_id="c").enabled)

    def test_a_broadcast_failure_never_raises(self):
        ch = SignalChannel(token="t", chat_id="c")

        def boom(text):
            raise RuntimeError("telegram down")

        ch._post = boom
        ch.entry("X", "BUY", 1.0, 0.9, 1.2, mode="live", dry_run=False)

    def test_disabled_channel_sends_nothing(self):
        ch = SignalChannel()
        ch.sent = []
        ch._post = ch.sent.append
        ch.entry("X", "BUY", 1.0, 0.9, 1.2, mode="live", dry_run=False)
        self.assertEqual(ch.sent, [])


class TestConfigOverlay(unittest.TestCase):
    """config.local.yaml keeps deployment settings out of git's way."""

    def _load(self, base_text, local_text=None):
        import tempfile
        from bot.config import Config
        d = Path(tempfile.mkdtemp())
        (d / "config.yaml").write_text(base_text)
        if local_text is not None:
            (d / "config.local.yaml").write_text(local_text)
        return Config.load(d / "config.yaml")

    def test_overlay_wins(self):
        cfg = self._load("mode: testnet\ndry_run: true\n", "dry_run: false\n")
        self.assertFalse(cfg.dry_run)

    def test_nested_merge_keeps_unset_siblings(self):
        cfg = self._load("risk:\n  max_leverage: 3\n  daily_loss_limit_pct: 5.0\n",
                         "risk:\n  max_leverage: 10\n")
        self.assertEqual(cfg.risk.max_leverage, 10)
        self.assertEqual(cfg.risk.daily_loss_limit_pct, 5.0,
                         "the overlay wiped a sibling it never mentioned")

    def test_absent_overlay_is_fine(self):
        self.assertEqual(self._load("mode: testnet\n").mode, "testnet")

    def test_config_path_names_both_files(self):
        cfg = self._load("mode: testnet\n", "mode: live\n")
        self.assertIn("config.local.yaml", cfg.config_path)

    def test_the_override_is_gitignored(self):
        self.assertIn("config.local.yaml", (ROOT / ".gitignore").read_text())
