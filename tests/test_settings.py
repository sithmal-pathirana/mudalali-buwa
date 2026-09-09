"""
Editing config.yaml from a control surface.

The three things that can go badly wrong, tested directly:

  * writing the WRONG line -- `enabled:` appears under both `aggressive:` and
    `portfolio:`, so a line-based writer that ignores which section is open
    silently toggles the wrong feature;
  * losing the COMMENTS -- config.yaml is where the reasoning behind every
    number lives, and a yaml.safe_dump() round-trip deletes all of it;
  * accepting a value that should have been refused, from what is ultimately
    an unauthenticated-looking text field on a phone.
"""

import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml                                                     # noqa: E402

from bot.settings import (EDITABLE, current_value, format_value,  # noqa: E402
                          parse_value, write_setting)

SAMPLE = """\
# The bot's settings.
mode: live             # testnet | live
dry_run: false         # send nothing
strategy: switcher     # switcher | trend_atr

risk:
  max_leverage: 3            # ruin is 62% at 2x
  risk_per_trade_pct: 20.0
  min_equity_usdt: 1.70

aggressive:
  enabled: true
  profile: maximum         # moderate | high | maximum

portfolio:
  # Scanning is what picks the symbol.
  enabled: true
  stop_distance: 0.07

universe:
  min_efficiency: 0.20
  rescan_seconds: 300
"""


class ConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "config.yaml"
        self.path.write_text(SAMPLE)

    def reload(self) -> dict:
        return yaml.safe_load(self.path.read_text())


class TestWritesTheRightLine(ConfigFileTest):
    def test_a_duplicated_key_resolves_by_section(self):
        """`enabled:` exists under both aggressive and portfolio."""
        write_setting("portfolio.enabled", False, self.path)
        raw = self.reload()
        self.assertFalse(raw["portfolio"]["enabled"])
        self.assertTrue(raw["aggressive"]["enabled"], "wrote the wrong section")

    def test_the_other_duplicate_resolves_too(self):
        write_setting("aggressive.enabled", False, self.path)
        raw = self.reload()
        self.assertFalse(raw["aggressive"]["enabled"])
        self.assertTrue(raw["portfolio"]["enabled"], "wrote the wrong section")

    def test_top_level_key(self):
        write_setting("strategy", "trend_atr", self.path)
        self.assertEqual(self.reload()["strategy"], "trend_atr")

    def test_returns_the_edit_it_actually_made(self):
        old, new = write_setting("risk.max_leverage", 5, self.path)
        self.assertEqual((old, new), ("3", "5"))

    def test_a_key_not_in_the_file_is_refused_not_appended(self):
        """An appended key usually lands in the wrong block, where it parses
        cleanly and does nothing -- worse than a visible failure."""
        (self.path).write_text("mode: live\n")
        with self.assertRaises(KeyError):
            write_setting("risk.max_leverage", 5, self.path)

    def test_an_undeclared_setting_is_refused(self):
        with self.assertRaises(KeyError):
            write_setting("risk.kill_action", "protect", self.path)


class TestPreservesTheFile(ConfigFileTest):
    def test_comments_survive(self):
        before = self.path.read_text()
        write_setting("risk.max_leverage", 7, self.path)
        after = self.path.read_text()
        self.assertEqual(before.count("#"), after.count("#"))
        self.assertIn("# ruin is 62% at 2x", after)
        self.assertIn("# The bot's settings.", after)

    def test_the_trailing_comment_on_the_edited_line_survives(self):
        write_setting("aggressive.profile", "high", self.path)
        line = [ln for ln in self.path.read_text().splitlines()
                if ln.strip().startswith("profile:")][0]
        self.assertIn("moderate | high | maximum", line)
        self.assertIn("profile: high", line)

    def test_nothing_else_changes(self):
        before = self.path.read_text().splitlines()
        write_setting("universe.min_efficiency", 0.15, self.path)
        after = self.path.read_text().splitlines()
        self.assertEqual(len(before), len(after))
        differing = [i for i, (b, a) in enumerate(zip(before, after)) if b != a]
        self.assertEqual(len(differing), 1, "more than one line changed")

    def test_indentation_survives(self):
        write_setting("risk.risk_per_trade_pct", 8.0, self.path)
        line = [ln for ln in self.path.read_text().splitlines()
                if "risk_per_trade_pct" in ln][0]
        self.assertTrue(line.startswith("  risk_per_trade_pct:"), line)

    def test_a_backup_is_written_alongside(self):
        write_setting("risk.max_leverage", 6, self.path)
        backup = self.path.parent / "data" / "config.yaml.bak"
        self.assertTrue(backup.exists())
        self.assertEqual(yaml.safe_load(backup.read_text())["risk"]["max_leverage"], 3)


class TestValidation(unittest.TestCase):
    def test_bool_forms(self):
        s = EDITABLE["aggressive.enabled"]
        for text in ("true", "yes", "on", "1", "TRUE"):
            self.assertIs(parse_value(s, text), True, text)
        for text in ("false", "no", "off", "0"):
            self.assertIs(parse_value(s, text), False, text)
        with self.assertRaises(ValueError):
            parse_value(s, "maybe")

    def test_choice_is_constrained(self):
        s = EDITABLE["aggressive.profile"]
        self.assertEqual(parse_value(s, "HIGH"), "high")
        with self.assertRaises(ValueError):
            parse_value(s, "insane")

    def test_numeric_bounds_are_enforced(self):
        s = EDITABLE["risk.max_leverage"]
        self.assertEqual(parse_value(s, "3"), 3)
        with self.assertRaises(ValueError):
            parse_value(s, "0")
        with self.assertRaises(ValueError):
            parse_value(s, "125")

    def test_int_settings_reject_fractions(self):
        with self.assertRaises(ValueError):
            parse_value(EDITABLE["risk.max_leverage"], "3.5")

    def test_non_numeric_is_refused(self):
        with self.assertRaises(ValueError):
            parse_value(EDITABLE["risk.risk_per_trade_pct"], "lots")

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            parse_value(EDITABLE["risk.max_leverage"], "   ")

    def test_arming_the_bot_is_not_editable_from_a_control_surface(self):
        """mode and dry_run decide whether real money moves; they need a shell
        and the CONFIRM_LIVE gate, not a chat message."""
        for key in ("mode", "dry_run", "api_key", "api_secret",
                    "risk.kill_action"):
            self.assertNotIn(key, EDITABLE)


class TestFormatting(unittest.TestCase):
    def test_bools_are_yaml_not_python(self):
        self.assertEqual(format_value(True), "true")
        self.assertEqual(format_value(False), "false")

    def test_floats_do_not_go_scientific(self):
        self.assertEqual(format_value(0.07), "0.07")
        self.assertEqual(format_value(20.0), "20")
        self.assertEqual(format_value(0.0001), "0.0001")


class TestCurrentValue(unittest.TestCase):
    def test_reads_dataclass_and_dict_sections(self):
        from bot.config import Config
        cfg = Config()
        self.assertEqual(current_value(cfg, "risk.max_leverage"),
                         cfg.risk.max_leverage)
        self.assertEqual(current_value(cfg, "strategy"), cfg.strategy)
        cfg.universe = {"min_efficiency": 0.25}
        self.assertEqual(current_value(cfg, "universe.min_efficiency"), 0.25)

    def test_every_editable_key_resolves_against_a_default_config(self):
        """A key that cannot be read is a key /set would display as '?'."""
        from bot.config import Config
        cfg = Config.load(ROOT / "config.yaml", overlay=False)
        for key in EDITABLE:
            self.assertIsNotNone(current_value(cfg, key),
                                 f"{key} does not resolve against config.yaml")


if __name__ == "__main__":
    unittest.main()


sys.path.insert(0, str(ROOT / "tests"))

from bot.dashboard import Command                              # noqa: E402
from test_r2_regressions import engine                         # noqa: E402
import bot.engine as engine_mod                                # noqa: E402


class FakeController:
    """Stands in for Telegram/the dashboard: hands the engine queued commands."""

    def __init__(self, *commands):
        self._commands = list(commands)

    def pop_commands(self):
        out, self._commands = self._commands, []
        return out


class EngineCommandTest(ConfigFileTest):
    def build(self):
        e = engine()
        e.cfg.config_path = str(self.path)
        # Never the real data/restarts.json: a test that spends the restart
        # budget would otherwise leave the running bot unable to restart.
        e.restart_ledger = Path(self.dir.name) / "restarts.json"
        return e

    def run_command(self, e, command):
        # `controllers` is a read-only property over dashboard + telegram, so
        # the stub goes in as one of those rather than replacing the list.
        e.telegram = FakeController(command)
        e.process_commands()


class TestEngineAppliesSettings(EngineCommandTest):
    def test_a_set_command_writes_the_file(self):
        e = self.build()
        self.run_command(e, Command("set", value="risk.max_leverage=5"))
        self.assertEqual(self.reload()["risk"]["max_leverage"], 5)

    def test_the_reply_says_it_is_not_live_until_a_restart(self):
        """The risk block is rewritten by aggressive.apply() at startup, so
        there is no honest way to apply one of these in place."""
        e = self.build()
        self.run_command(e, Command("set", value="risk.max_leverage=5"))
        body = " ".join(b for _, b in e.sent)
        self.assertIn("restart", body.lower())

    def test_an_undeclared_key_changes_nothing(self):
        e = self.build()
        before = self.path.read_text()
        self.run_command(e, Command("set", value="risk.kill_action=protect"))
        self.assertEqual(self.path.read_text(), before)
        self.assertIn("not an editable setting", " ".join(b for _, b in e.sent))

    def test_an_out_of_range_value_changes_nothing(self):
        e = self.build()
        self.run_command(e, Command("set", value="risk.max_leverage=999"))
        self.assertEqual(self.reload()["risk"]["max_leverage"], 3)
        self.assertIn("Refused", " ".join(b for _, b in e.sent))

    def test_a_config_path_naming_an_overlay_still_writes_the_tracked_file(self):
        """cfg.config_path is a REPORTING string and may carry
        ' + config.local.yaml'; the writer must not treat that as a filename."""
        e = self.build()
        e.cfg.config_path = f"{self.path} + config.local.yaml"
        self.run_command(e, Command("set", value="risk.max_leverage=5"))
        self.assertEqual(self.reload()["risk"]["max_leverage"], 5)


class TestEngineProcessControl(EngineCommandTest):
    def test_restart_exits_non_zero_so_systemd_brings_it_back(self):
        e = self.build()
        self.run_command(e, Command("restart", note="via telegram"))
        self.assertTrue(e._stopping)
        self.assertEqual(e._exit_code, e.RESTART_EXIT_CODE)
        self.assertNotEqual(e._exit_code, 0, "Restart=on-failure needs non-zero")

    def test_stop_exits_zero_so_systemd_leaves_it_down(self):
        e = self.build()
        self.run_command(e, Command("stop", note="via telegram"))
        self.assertTrue(e._stopping)
        self.assertEqual(e._exit_code, 0)

    def test_both_still_cancel_resting_orders_on_the_way_out(self):
        """trigger_kill() has already done its exchange work; these have not,
        so shutdown() must not be told the exchange is already handled."""
        for action in ("restart", "stop"):
            e = self.build()
            self.run_command(e, Command(action))
            self.assertFalse(e._stop_exchange_done, action)

    def test_stop_says_it_needs_a_shell_to_come_back(self):
        e = self.build()
        self.run_command(e, Command("stop"))
        self.assertIn("systemctl start", " ".join(b for _, b in e.sent))


class TestRestartBudget(EngineCommandTest):
    """
    systemd allows StartLimitBurst starts per StartLimitIntervalSec and puts
    the unit in `failed` once that runs out -- recoverable only with
    `systemctl reset-failed`, from a shell. A Telegram-only operator has no
    shell, so /restart must run out of budget before systemd does.
    """

    def restart(self, e):
        self.run_command(e, Command("restart", note="via telegram"))

    def test_a_restart_within_budget_is_taken(self):
        e = self.build()
        self.restart(e)
        self.assertTrue(e._stopping)

    def test_the_budget_leaves_starts_for_crash_recovery(self):
        self.assertLess(engine_mod.RESTART_BUDGET, engine_mod.START_LIMIT_BURST,
                        "a crash after the last /restart must still be able "
                        "to bring the bot back")

    def test_restarts_beyond_the_budget_are_refused(self):
        ledger = Path(self.dir.name) / "restarts.json"
        now = time.time()
        ledger.write_text(json.dumps([now] * engine_mod.RESTART_BUDGET))
        e = self.build()
        self.restart(e)
        self.assertFalse(e._stopping, "spending the last start strands the bot")
        self.assertIn("refused", " ".join(b for _, b in e.sent).lower())

    def test_a_refused_restart_says_how_long_to_wait(self):
        ledger = Path(self.dir.name) / "restarts.json"
        ledger.write_text(json.dumps([time.time()] * engine_mod.RESTART_BUDGET))
        e = self.build()
        self.restart(e)
        self.assertRegex(" ".join(b for _, b in e.sent), r"\d+s")

    def test_a_refused_restart_leaves_the_bot_trading(self):
        ledger = Path(self.dir.name) / "restarts.json"
        ledger.write_text(json.dumps([time.time()] * engine_mod.RESTART_BUDGET))
        e = self.build()
        self.restart(e)
        self.assertEqual(e._exit_code, 0)
        self.assertTrue(e._stop_exchange_done)

    def test_the_budget_frees_up_once_the_window_passes(self):
        ledger = Path(self.dir.name) / "restarts.json"
        stale = time.time() - engine_mod.START_LIMIT_INTERVAL - 1
        ledger.write_text(json.dumps([stale] * engine_mod.START_LIMIT_BURST))
        e = self.build()
        self.restart(e)
        self.assertTrue(e._stopping, "expired starts no longer count")

    def test_each_restart_is_charged_to_the_ledger(self):
        ledger = Path(self.dir.name) / "restarts.json"
        e = self.build()
        self.restart(e)
        self.assertEqual(len(json.loads(ledger.read_text())), 1)

    def test_a_corrupt_ledger_does_not_block_restarting(self):
        """Losing the count spends budget the bot did not know it had, which
        is survivable; raising here would take down the command loop."""
        ledger = Path(self.dir.name) / "restarts.json"
        ledger.write_text("{not json")
        e = self.build()
        self.restart(e)
        self.assertTrue(e._stopping)

    def test_stop_is_not_rate_limited(self):
        """/stop exits 0, which systemd does not count as a start at all."""
        ledger = Path(self.dir.name) / "restarts.json"
        ledger.write_text(json.dumps([time.time()] * engine_mod.START_LIMIT_BURST))
        e = self.build()
        self.run_command(e, Command("stop"))
        self.assertTrue(e._stopping)


class TestTheUnitFileAgrees(unittest.TestCase):
    """The budget is only real if it matches the unit systemd is running."""

    def setUp(self):
        self.unit = (ROOT / "deploy" / "trading-bot.service").read_text()

    def directive(self, name: str) -> int:
        m = re.search(rf"^{name}=(\d+)", self.unit, re.M)
        self.assertIsNotNone(m, f"{name} is not set in the unit file")
        return int(m.group(1))

    def test_start_limit_interval_matches(self):
        self.assertEqual(self.directive("StartLimitIntervalSec"),
                         engine_mod.START_LIMIT_INTERVAL)

    def test_start_limit_burst_matches(self):
        self.assertEqual(self.directive("StartLimitBurst"),
                         engine_mod.START_LIMIT_BURST)

    def test_restart_on_failure_is_what_makes_a_restart_come_back(self):
        self.assertRegex(self.unit, r"(?m)^Restart=on-failure")

    def test_the_restart_exit_code_is_non_zero(self):
        """Restart=on-failure ignores exit 0, so /restart's code must not be."""
        from bot.engine import Engine
        self.assertNotEqual(Engine.RESTART_EXIT_CODE, 0)

    def test_config_yaml_is_writable_by_the_unit(self):
        """ProtectSystem=strict makes /set fail at runtime without this."""
        m = re.search(r"^ReadWritePaths=(.*)$", self.unit, re.M)
        self.assertIsNotNone(m)
        self.assertIn("config.yaml", m.group(1))
