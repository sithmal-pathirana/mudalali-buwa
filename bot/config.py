"""Config loading. YAML for behaviour, environment for secrets -- never mixed."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RiskConfig:
    max_leverage: int = 3
    risk_per_trade_pct: float = 2.0        # % of equity risked between entry and stop
    max_position_pct: float = 100.0        # cap on notional as % of equity * leverage
    daily_loss_limit_pct: float = 5.0      # halt for the day past this drawdown
    max_trades_per_day: int = 10
    min_equity_usdt: float = 10.0          # below this the bot stops permanently
    allow_averaging_down: bool = False     # keep False; see README
    entry_expiry_minutes: int = 60         # cancel a limit entry that never fills
    # What `touch KILL` means. "flatten" closes the position at market first,
    # then cancels everything and exits. "protect" stops trading but leaves the
    # position and its stop on the book. Doing neither -- cancelling the stop
    # and walking away -- is the one outcome to avoid. (QA R2)
    kill_action: str = "flatten"
    #: Trade as if the account held this much, regardless of the real balance.
    #: 0 disables it. Testnet hands out a large demo balance, which makes every
    #: sizing decision unrepresentative of the account you actually intend to
    #: fund -- and hides the minimum-order constraint entirely.
    equity_cap_usdt: float = 0.0


@dataclass
class AlertConfig:
    approach_pct: float = 80.0      # warn once price is this % of the way to TP or SL
    heartbeat_minutes: int = 360    # periodic "still alive" summary; 0 disables
    email_on_every_tick: bool = False


@dataclass
class DashboardConfig:
    enabled: bool = True
    host: str = "127.0.0.1"     # see README before changing; this endpoint closes trades
    port: int = 8080


@dataclass
class AggressiveConfig:
    """
    Off by default and deliberately so. When on it REPLACES the risk profile
    rather than adjusting it, and the engine says so on every surface.
    """
    enabled: bool = False
    profile: str = "moderate"        # moderate | high | maximum
    keep_daily_loss_limit: bool = True
    confirm_live: bool = True


@dataclass
class PortfolioConfig:
    """
    Concurrency is derived, not chosen. Fix the portfolio risk you can accept
    and let per-trade risk shrink as slots are added; the count then scales
    with equity instead of inheriting a number tuned for a different account.
    """
    enabled: bool = False            # opt in: the engine still trades one symbol
    max_concurrent: str | int = "auto"
    portfolio_risk_pct: float = 6.0
    hard_cap: int = 40
    stop_distance: float = 0.02      # representative, for converting risk to size
    single_position_cap_pct: float = 2.0

    #: filled in at runtime once equity and the eligible count are known
    resolved_slots: int = 0
    resolved_risk_pct: float = 0.0


@dataclass
class TelegramConfig:
    # Off by default: a control surface should be opted into, not inherited by
    # anyone who configured a token for alerts. (QA R7)
    control: bool = False       # accept /commands, not just send alerts
    poll_seconds: int = 0       # 0 = long-poll (recommended); >0 forces short polls


@dataclass
class Config:
    mode: str = "testnet"                  # testnet | live
    symbol: str = "BTCUSDT"
    interval: str = "15m"
    strategy: str = "trend_atr"
    poll_seconds: int = 20
    dry_run: bool = True                   # log intended orders, send nothing
    realtime: bool = True                  # websockets; False falls back to polling
    risk: RiskConfig = field(default_factory=RiskConfig)
    alerts: AlertConfig = field(default_factory=AlertConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)
    aggressive: AggressiveConfig = field(default_factory=AggressiveConfig)
    targets: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)

    unknown_keys: list = field(default_factory=list)
    config_path: str = ""

    api_key: str = ""
    api_secret: str = ""
    telegram_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email: str = ""
    dashboard_token: str = ""
    signal_chat_id: str = ""

    @property
    def testnet(self) -> bool:
        return self.mode != "live"

    @classmethod
    def load(cls, path: str | Path = ROOT / "config.yaml") -> "Config":
        raw = yaml.safe_load(Path(path).read_text()) or {}

        # config.local.yaml overlays the tracked defaults and is gitignored.
        # Editing the tracked file directly means every `git pull` conflicts
        # with your own deployment settings, which is a fight you lose weekly.
        local = Path(path).with_name("config.local.yaml")
        if local.exists():
            overlay = yaml.safe_load(local.read_text()) or {}
            raw = _deep_merge(raw, overlay)

        # Table-driven so adding a section cannot be half-wired: forgetting an
        # entry here would let the raw dict flow through into the field, which
        # is precisely how the `telegram:` section broke once.
        sections = {"risk": RiskConfig, "alerts": AlertConfig,
                    "dashboard": DashboardConfig, "telegram": TelegramConfig,
                    "portfolio": PortfolioConfig, "aggressive": AggressiveConfig}
        built = {}
        for name, factory in sections.items():
            section = raw.pop(name, {}) or {}
            if not isinstance(section, dict):
                raise TypeError(f"config.yaml: `{name}:` must be a block of settings, "
                                f"got {type(section).__name__}")
            try:
                built[name] = factory(**section)
            except TypeError as e:
                valid = ", ".join(sorted(factory.__dataclass_fields__))
                raise TypeError(f"config.yaml: bad key under `{name}:` ({e}). "
                                f"Valid keys: {valid}") from None

        known = set(cls.__annotations__)
        unknown = sorted(k for k in raw if k not in known)
        cfg = cls(**built, **{k: v for k, v in raw.items() if k in known})
        cfg.unknown_keys = unknown
        cfg.config_path = str(Path(path))
        if local.exists():
            cfg.config_path += f" + {local.name}"

        # Every declared section must be its dataclass, never a passed-through dict.
        for name, factory in sections.items():
            assert isinstance(getattr(cfg, name), factory), \
                f"config section `{name}` was not built into {factory.__name__}"

        _load_env_file(ROOT / ".env")
        prefix = "BINANCE_TESTNET" if cfg.testnet else "BINANCE_LIVE"
        cfg.api_key = os.environ.get(f"{prefix}_KEY", "")
        cfg.api_secret = os.environ.get(f"{prefix}_SECRET", "")
        cfg.telegram_token = os.environ.get("TELEGRAM_TOKEN", "")
        cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        cfg.smtp_host = os.environ.get("SMTP_HOST", "")
        cfg.smtp_port = int(os.environ.get("SMTP_PORT", "587"))
        cfg.smtp_user = os.environ.get("SMTP_USER", "")
        cfg.smtp_password = os.environ.get("SMTP_PASSWORD", "")
        cfg.alert_email = os.environ.get("ALERT_EMAIL", "")
        cfg.dashboard_token = os.environ.get("DASHBOARD_TOKEN", "")
        cfg.signal_chat_id = os.environ.get("TELEGRAM_SIGNAL_CHAT_ID", "")
        return cfg

    @property
    def aggressive_on(self) -> bool:
        return bool(self.aggressive.enabled)

    def validate(self) -> list[str]:
        problems = []
        if self.aggressive.enabled:
            from .aggressive import PROFILES
            if self.aggressive.profile not in PROFILES:
                problems.append(f"aggressive.profile must be one of "
                                f"{', '.join(PROFILES)}, got "
                                f"{self.aggressive.profile!r}")
            problems.append(
                "AGGRESSIVE MODE IS ENABLED. This replaces the risk profile "
                "wholesale. Run `run.py risk` to see the modelled probability "
                "of ruin for your configured profile and equity.")
        if self.risk.kill_action not in ("flatten", "protect"):
            problems.append(f"risk.kill_action must be 'flatten' or 'protect', "
                            f"got {self.risk.kill_action!r}")
        if self.unknown_keys:
            # A typo like `dry_runn: false` used to be dropped in silence, so you
            # could believe you were trading ETH 1h while the bot traded BTC 15m.
            problems.append(
                f"config.yaml has unrecognised top-level keys: "
                f"{', '.join(self.unknown_keys)}. These are IGNORED -- check for "
                f"a typo (did you mean one of: "
                f"{', '.join(sorted(k for k in self.__annotations__ if not k.startswith('_')))[:120]}...)")
        if self.risk.max_leverage > 5:
            problems.append(
                f"max_leverage={self.risk.max_leverage}. Above 5x, the reality_check "
                f"simulation puts one-year ruin above 90%. Lower it or accept that.")
        if self.risk.allow_averaging_down:
            problems.append("allow_averaging_down is True -- this is the single most "
                            "common cause of total account loss.")
        if not self.testnet and self.dry_run is False and not self.api_key:
            problems.append("live mode with no API key configured")
        if not (self.telegram_token or self.smtp_host):
            problems.append("no alert channel configured -- you will not be told "
                            "about fills, stops, or halts. Fill in .env.")
        if self.telegram.control and self.telegram_token and not self.telegram_chat_id:
            problems.append(
                "TELEGRAM_TOKEN is set but TELEGRAM_CHAT_ID is not. The chat id is "
                "the ONLY thing stopping a stranger who finds your bot from issuing "
                "commands -- control stays disabled until you set it.")
        if self.dashboard.enabled and self.dashboard.host not in ("127.0.0.1", "localhost"):
            problems.append(
                f"dashboard is bound to {self.dashboard.host}, which exposes an "
                f"endpoint that can close trades. Use an SSH tunnel instead "
                f"unless you have put it behind TLS and a firewall.")
        steps = self.targets.get("schedule", [])
        if len(steps) > 1:
            problems.append(
                "target schedule escalates. Run tools/projection.py: raising the "
                "target resets the required daily return to roughly where it "
                "started, undoing the risk reduction from equity growth.")
        return problems


def _deep_merge(base: dict, overlay: dict) -> dict:
    """
    Overlay wins, one section at a time.

    Nested so `portfolio: {enabled: true}` in the override does not wipe the
    other portfolio settings from the tracked defaults.
    """
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_env_file(path: Path) -> None:
    """Tiny .env reader so secrets stay out of the YAML and out of git."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip("'\""))
