"""
The settings a control surface is allowed to change, and how to write them.

Two rules shape this module.

**An allowlist, never arbitrary YAML.** A chat message is a hostile input
surface for a live-money account: a typo in a key name that silently creates a
new setting, or a value that quietly disables a limit, is worse than refusing
the edit. Every editable key is declared here with a type and a range, and
anything not declared is rejected by name.

**Comments survive the edit.** config.yaml is the only place the reasoning
behind these numbers is written down -- why max_leverage is 3, why
single_position_cap_pct was raised, which choices were made "by request". A
yaml.safe_dump() round-trip would silently delete all of it and hand back a
file that says what but never why. So writes are line-surgical: find the one
line that declares the key, replace the value between the colon and any
trailing comment, and leave every other byte of the file alone.

Nothing here applies a setting to a running bot. Most of these are read once
during Engine.startup() -- aggressive.apply() in particular overwrites the risk
block wholesale and cannot be undone in place -- so a write lands in the file
and takes effect on the next restart. The control surfaces say so explicitly.
"""

from __future__ import annotations

import logging
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("settings")

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Setting:
    """One editable key: how to parse it, and what it is allowed to be."""
    key: str
    kind: str                       # float | int | bool | choice
    note: str
    lo: float | None = None
    hi: float | None = None
    choices: tuple[str, ...] = ()

    def describe_range(self) -> str:
        if self.kind == "bool":
            return "true or false"
        if self.kind == "choice":
            return " | ".join(self.choices)
        return f"{self.lo:g} to {self.hi:g}"


#: What a control surface may change. Deliberately absent:
#:
#:   mode, dry_run  -- arming the bot for real money is a decision that should
#:                     need a shell and the CONFIRM_LIVE gate in the unit file,
#:                     not two taps on a phone.
#:   api keys       -- they live in .env and never in config.yaml.
#:   kill_action    -- what `touch KILL` does is a safety contract; changing it
#:                     remotely is how you find out it changed at the worst
#:                     possible moment.
EDITABLE: dict[str, Setting] = {
    s.key: s for s in (
        Setting("strategy", "choice",
                "which strategy runs, or the switcher that routes between them",
                choices=("switcher", "trend_atr", "mean_reversion", "funding_arb")),

        Setting("risk.max_leverage", "int",
                "exchange leverage. Liquidation sits at roughly (100/leverage - "
                "maintenance margin) percent from entry, and it MUST stay outside "
                "the strategy's stop or the stop can never fire",
                lo=1, hi=20),
        Setting("risk.risk_per_trade_pct", "float",
                "percent of equity risked between entry and stop",
                lo=0.1, hi=50.0),
        Setting("risk.daily_loss_limit_pct", "float",
                "drawdown that halts the day", lo=1.0, hi=100.0),
        Setting("risk.max_trades_per_day", "int",
                "hard cap on entries per day", lo=1, hi=200),
        Setting("risk.min_equity_usdt", "float",
                "permanent floor; below this the bot stops for good",
                lo=0.0, hi=100000.0),
        Setting("risk.entry_expiry_minutes", "int",
                "cancel a limit entry that never fills", lo=1, hi=1440),

        Setting("aggressive.enabled", "bool",
                "aggressive mode on or off"),
        Setting("aggressive.profile", "choice",
                "which aggressive profile applies",
                choices=("moderate", "high", "maximum")),
        Setting("aggressive.keep_daily_loss_limit", "bool",
                "keep the daily loss limit while aggressive"),

        Setting("portfolio.enabled", "bool",
                "scan a universe instead of trading the single configured symbol"),
        Setting("portfolio.single_position_cap_pct", "float",
                "most one position may risk", lo=0.1, hi=100.0),
        Setting("portfolio.portfolio_risk_pct", "float",
                "total risk across all open positions", lo=0.1, hi=100.0),
        Setting("portfolio.stop_distance", "float",
                "stop distance the allocator sizes from, as a fraction",
                lo=0.001, hi=0.5),

        Setting("universe.min_efficiency", "float",
                "minimum efficiency ratio for a symbol to be considered",
                lo=0.0, hi=1.0),
        Setting("universe.min_atr_pct", "float",
                "minimum ATR percent for a symbol to be considered",
                lo=0.0, hi=50.0),
        Setting("universe.rescan_seconds", "int",
                "how often the universe is rescanned", lo=30, hi=3600),
        Setting("universe.min_quote_volume", "float",
                "minimum 24h quote volume for a symbol to be considered",
                lo=0.0, hi=1e12),
    )
}

TRUE = {"true", "yes", "on", "1"}
FALSE = {"false", "no", "off", "0"}


def parse_value(setting: Setting, text: str):
    """Text from a chat message to a typed, range-checked value."""
    raw = (text or "").strip()
    if not raw:
        raise ValueError(f"{setting.key} needs a value ({setting.describe_range()})")

    if setting.kind == "bool":
        low = raw.lower()
        if low in TRUE:
            return True
        if low in FALSE:
            return False
        raise ValueError(f"{setting.key} must be true or false, not {raw!r}")

    if setting.kind == "choice":
        low = raw.lower()
        if low not in setting.choices:
            raise ValueError(f"{setting.key} must be one of "
                             f"{setting.describe_range()}, not {raw!r}")
        return low

    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{setting.key} must be a number, not {raw!r}") from None
    if setting.kind == "int":
        if value != int(value):
            raise ValueError(f"{setting.key} must be a whole number, not {raw!r}")
        value = int(value)
    if setting.lo is not None and value < setting.lo:
        raise ValueError(f"{setting.key} must be at least {setting.lo:g} "
                         f"({setting.describe_range()})")
    if setting.hi is not None and value > setting.hi:
        raise ValueError(f"{setting.key} must be at most {setting.hi:g} "
                         f"({setting.describe_range()})")
    return value


def format_value(value) -> str:
    """A typed value back to the scalar text YAML expects."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # Keep it readable: 20.0 stays 20.0, 0.07 stays 0.07, no 1e-02.
        return f"{value:.10g}"
    return str(value)


def current_value(cfg, key: str):
    """Read what a loaded Config currently holds for a dotted key."""
    if "." not in key:
        return getattr(cfg, key, None)
    section, leaf = key.split(".", 1)
    block = getattr(cfg, section, None)
    if isinstance(block, dict):
        return block.get(leaf)
    return getattr(block, leaf, None)


#: `key:` at some indent, its value, and any trailing comment, kept apart so a
#: write can replace the middle group and nothing else.
def _line_re(leaf: str) -> re.Pattern:
    return re.compile(
        r"^(?P<indent>\s*)(?P<key>" + re.escape(leaf) + r")"
        r"(?P<colon>:[ \t]*)(?P<value>[^#\n]*?)(?P<pad>[ \t]*)"
        r"(?P<comment>#.*)?(?P<eol>\r?\n?)$")


def _find_line(lines: list[str], key: str) -> int:
    """
    Index of the line declaring `key`, or -1.

    Dotted keys are resolved against the section actually open at that point in
    the file, so `portfolio.enabled` cannot match the `enabled:` belonging to
    `aggressive:` a few lines earlier -- which is the entire hazard of editing
    YAML by line.
    """
    top_re = re.compile(r"^(?P<key>[A-Za-z_][\w-]*):")
    if "." in key:
        section, leaf = key.split(".", 1)
    else:
        section, leaf = None, key
    pattern = _line_re(leaf)
    open_section: str | None = None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        top = top_re.match(line)
        if top:                                  # indent 0: a new section starts
            open_section = top.group("key")
            if section is None and open_section == leaf:
                return i
            continue
        if section is not None and open_section == section and pattern.match(line):
            return i
    return -1


def write_setting(key: str, value, path: Path | str | None = None) -> tuple[str, str]:
    """
    Replace one value in config.yaml in place, preserving everything else.

    Returns (old_text, new_text) for the value, so a caller can report the edit
    it actually made rather than the one it intended. Raises KeyError when the
    key is not declared in the file -- writes never invent a line, because a
    key appended in the wrong block reads as valid YAML and silently does
    nothing.
    """
    if key not in EDITABLE:
        raise KeyError(f"{key} is not an editable setting")
    p = Path(path) if path is not None else ROOT / "config.yaml"
    lines = p.read_text().splitlines(keepends=True)

    i = _find_line(lines, key)
    if i < 0:
        raise KeyError(f"{key} is not declared in {p.name}; add it by hand first")

    leaf = key.split(".")[-1]
    m = _line_re(leaf).match(lines[i])
    assert m is not None, "line matched during search but not during rewrite"
    old = m.group("value").strip()
    new = format_value(value)
    lines[i] = (m.group("indent") + m.group("key") + m.group("colon") + new
                + m.group("pad") + (m.group("comment") or "") + (m.group("eol") or "\n"))

    text = "".join(lines)

    # Prove the edit before it lands. A regex that mangled the line would
    # otherwise produce a config.yaml that does not parse, and the bot does not
    # start without one -- from a phone, with no shell, that is unrecoverable.
    parsed = yaml.safe_load(text) or {}
    if _lookup(parsed, key) != value:
        raise ValueError(f"refusing to write: {key} did not read back as "
                         f"{new!r} after the edit")

    # The usual temp-file-then-rename is not available here: the systemd unit
    # runs under ProtectSystem=strict, so the install directory is read-only
    # even when config.yaml itself is listed in ReadWritePaths -- and rename
    # needs a writable *directory*, not just a writable file. So back up into
    # data/ (which is writable) and then write the file in place.
    backup = p.parent / "data" / f"{p.name}.bak"
    try:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, backup)
    except OSError:
        # A missing backup is worth continuing over -- the change is one line
        # and reversible by sending the opposite /set -- but not worth hiding.
        log.warning("could not back up %s to %s before editing", p, backup)

    p.write_text(text)
    return old, new


def _lookup(raw: dict, key: str):
    """Read a dotted key out of freshly parsed YAML."""
    node = raw
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node
