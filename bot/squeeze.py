"""
Squeeze mode: buy the coins whose shorts are most crowded.

When a perpetual's funding rate is deeply negative, shorts are paying longs to
keep their positions open: too many traders are betting on a fall. Crowded
shorts tend to be squeezed, and a long collects the funding while it waits.

The rules:

  * Shortly after each 8-hour funding settlement (00:00, 08:00, 16:00 UTC)
    read the rates every symbol just settled, scaled to an 8-hour period
    (some contracts settle every 1 or 4 hours).
  * Buy up to entry.max_new_per_round of the most negative ones at or below
    entry.funding_at_most_pct, with at least board.min_quote_volume of 24h
    volume, that are not already held.
  * Every position gets an exchange-side stop exit.stop_pct below entry and
    no take-profit, and is closed at market after exit.max_hold_hours.

Replayed on 2 years of live data (Sep 2024 - Sep 2026, every USDT perpetual
including delisted ones, 0.2% round-trip cost): funding <= -0.10%, 72h hold,
20% stop gave +1.29% per trade over 2,272 trades, positive in both years
(+1.93% / +0.73%). It is directional: a squeeze that does not come is a
stop-out.

Order handling is GainerMiner's, which is tested on the live exchange path:
market entry, fill read back, exchange-side stop or flatten, % of equity
sizing, the protection watchdog told there is deliberately no take-profit,
and the Funding-wallet sweep. The "ladder" is configured never to arm, so
its unarmed time limit is exactly the 72-hour exit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .binanceapi import BinanceError
from .gainer import GainerConfig, GainerMiner, GainerSweepConfig, ROOT

log = logging.getLogger("squeeze")

SQUEEZE_STATE_PATH = ROOT / "data" / "squeeze.json"
FUNDING_HOURS = (0, 8, 16)                  # UTC settlements every contract shares


@dataclass
class SqueezeBoardConfig:
    """What is watched."""
    #: Minutes after each 00/08/16 UTC settlement to read the rates, so the
    #: settlement has been published.
    check_minutes_after_funding: float = 2.0
    #: A settlement older than this is not acted on, so a restart later in
    #: the 8 hours does not buy the same round again.
    act_within_minutes: float = 30.0
    #: 24h quote volume floor, in USDT.
    min_quote_volume: float = 20_000_000
    poll_seconds: int = 60


@dataclass
class SqueezeEntryConfig:
    """When a coin is bought, and how much."""
    #: Buy at or below this funding rate, in percent per 8 hours (-0.10 =
    #: shorts paying 0.10% every 8h).
    funding_at_most_pct: float = -0.10
    #: New positions opened per settlement, most negative first.
    max_new_per_round: int = 3
    max_positions: int = 5
    #: Size as percent of the equity the bot trades with, never below
    #: min_notional_usdt (Binance's $5 minimum after rounding).
    notional_pct_of_equity: float = 10.0
    min_notional_usdt: float = 6.0


@dataclass
class SqueezeExitConfig:
    """When a position is sold."""
    stop_pct: float = 20.0
    max_hold_hours: float = 72.0
    fee_pct: float = 0.15


@dataclass
class SqueezeAlertsConfig:
    status_minutes: int = 60


GROUPS = {"board": SqueezeBoardConfig, "entry": SqueezeEntryConfig,
          "exit": SqueezeExitConfig, "alerts": SqueezeAlertsConfig,
          "sweep": GainerSweepConfig}


@dataclass
class SqueezeConfig:
    enabled: bool = False
    #: Paper-trade: nothing is sent to the exchange. The top-level dry_run
    #: forces this on as well.
    dry_run: bool = True
    board: SqueezeBoardConfig = field(default_factory=SqueezeBoardConfig)
    entry: SqueezeEntryConfig = field(default_factory=SqueezeEntryConfig)
    exit: SqueezeExitConfig = field(default_factory=SqueezeExitConfig)
    alerts: SqueezeAlertsConfig = field(default_factory=SqueezeAlertsConfig)
    #: Same rules as gainer.sweep: pct of each winning trade to Funding.
    sweep: GainerSweepConfig = field(default_factory=GainerSweepConfig)

    def __post_init__(self):
        for name, cls in GROUPS.items():
            value = getattr(self, name)
            if value is None:
                setattr(self, name, cls())
            elif isinstance(value, dict):
                try:
                    setattr(self, name, cls(**value))
                except TypeError as e:
                    valid = ", ".join(sorted(cls.__dataclass_fields__))
                    raise TypeError(f"bad key under squeeze.{name} ({e}). "
                                    f"Valid keys: {valid}") from None
            elif not isinstance(value, cls):
                raise TypeError(f"squeeze.{name} must be a block of settings")

    def as_gainer_config(self) -> GainerConfig:
        """The order-handling settings, in the shape GainerMiner reads."""
        en, ex = self.entry, self.exit
        return GainerConfig(
            enabled=True, dry_run=self.dry_run,
            board=dict(poll_seconds=self.board.poll_seconds,
                       min_quote_volume=self.board.min_quote_volume),
            entry=dict(trade_on_start=False, confirm_minutes=0,
                       buy_only_if_rising=False, rebuy_cooldown_minutes=0,
                       reentry_on_new_high=False, notional_usdt=en.min_notional_usdt,
                       notional_pct_of_equity=en.notional_pct_of_equity,
                       min_notional_usdt=en.min_notional_usdt,
                       max_positions=en.max_positions),
            # No take-profit; a ladder that never arms, so its unarmed time
            # limit is the max_hold_hours exit.
            exit=dict(target_usd=0.0, stop_pct=ex.stop_pct, fee_pct=ex.fee_pct,
                      on_new_leader="keep", min_hold_minutes=0,
                      ladder_enabled=True, ladder_first_pct=1e9, ladder_step_pct=1e9,
                      unarmed_max_hours=ex.max_hold_hours),
            alerts=dict(milestones_usd=[], status_minutes=self.alerts.status_minutes),
            sweep=self.sweep)


def per_8h(rate: float, interval_hours: float) -> float:
    """A settlement's rate scaled to an 8-hour period."""
    return rate * 8.0 / interval_hours if interval_hours > 0 else rate


def last_settlement_ms(now: float) -> int:
    """The most recent 00/08/16 UTC settlement at or before `now` (seconds)."""
    hour = int(now // 3600)
    while (hour % 24) not in FUNDING_HOURS:
        hour -= 1
    return hour * 3600 * 1000


def pick(rates: dict, volumes: dict, held: set, tradable: set,
         at_most_pct: float, min_volume: float, n: int) -> list[tuple[str, float]]:
    """The n most negative per-8h rates (percent) that qualify, most negative first."""
    rows = [(r, s) for s, r in rates.items()
            if r <= at_most_pct and s in tradable and s not in held
            and volumes.get(s, 0.0) >= min_volume]
    rows.sort()
    return [(s, r) for r, s in rows[:max(0, n)]]


class SqueezeTrader(GainerMiner):
    strategy = "squeeze"
    entry_label = "funding"
    close_label = "squeeze"
    INFO_MAX_AGE = 3600.0

    def __init__(self, engine, cfg: SqueezeConfig, path: Path = SQUEEZE_STATE_PATH):
        self.squeeze_cfg = cfg
        self.done_round = 0                 # settlement already acted on (ms)
        self._intervals: dict[str, float] = {}
        self._intervals_at = 0.0
        super().__init__(engine, cfg.as_gainer_config(), path=path)

    def exit_plan_text(self) -> str:
        ex = self.squeeze_cfg.exit
        return f"\nNo take-profit; closed after {ex.max_hold_hours:g}h if the stop is not hit"

    # ------------------------------------------------------------------ loop
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._last_poll < self.cfg.board.poll_seconds:
            return
        self._last_poll = now
        try:
            tickers = self.engine.api.ticker_24hr()
        except BinanceError as e:
            log.warning("squeeze: prices unavailable: %s", e)
            return
        prices, volumes = {}, {}
        for t in tickers or []:
            try:
                prices[t["symbol"]] = float(t.get("lastPrice") or 0)
                volumes[t["symbol"]] = float(t.get("quoteVolume") or 0)
            except (KeyError, TypeError, ValueError):
                continue
        self.sync_tracks(prices, {}, now)
        self.manage_ladder(prices, now)            # the max_hold_hours exit
        self.check_round(now, prices, volumes)
        status = self.cfg.alerts.status_minutes
        if status and now - self._last_status >= status * 60:
            self._last_status = now
            self.send_status(now, prices)

    def check_round(self, now: float, prices: dict, volumes: dict) -> None:
        settled = last_settlement_ms(now)
        wait = self.squeeze_cfg.board.check_minutes_after_funding * 60
        age_ms = now * 1000 - settled
        if (settled <= self.done_round or age_ms < wait * 1000
                or age_ms > self.squeeze_cfg.board.act_within_minutes * 60_000):
            return
        self.done_round = settled
        try:
            rows = self.engine.api.funding_rates_since(settled - 60_000)
        except BinanceError as e:
            log.warning("squeeze: funding rates unavailable: %s", e)
            self.done_round = 0                   # try again next poll
            return
        intervals = self.intervals()
        rates = {}
        for r in rows or []:
            try:
                sym = r["symbol"]
                rates[sym] = per_8h(float(r["fundingRate"]),
                                    intervals.get(sym, 8.0)) * 100.0
            except (KeyError, TypeError, ValueError):
                continue
        en = self.squeeze_cfg.entry
        chosen = pick(rates, volumes, set(self.tracks) | set(self.engine.book),
                      self.tradable(now), en.funding_at_most_pct,
                      self.squeeze_cfg.board.min_quote_volume, en.max_new_per_round)
        if not chosen:
            log.info("squeeze: settlement %s -- no symbol at or below %.3f%%",
                     time.strftime("%H:%M", time.gmtime(settled / 1000)),
                     en.funding_at_most_pct)
            return
        self.notify("SQUEEZE ROUND: crowded shorts -- " + ", ".join(
            f"{s} {r:+.3f}%/8h" for s, r in chosen))
        for sym, rate in chosen:
            px = prices.get(sym, 0.0)
            if px > 0:
                self.open(sym, px, rate)

    def intervals(self) -> dict:
        """Hours between settlements, for symbols that are not on 8 hours."""
        if self._intervals and time.time() - self._intervals_at < self.INFO_MAX_AGE:
            return self._intervals
        try:
            rows = self.engine.api.funding_info() or []
            self._intervals = {r["symbol"]: float(r.get("fundingIntervalHours") or 8)
                               for r in rows if r.get("symbol")}
            self._intervals_at = time.time()
        except (BinanceError, KeyError, TypeError, ValueError) as e:
            log.warning("squeeze: funding intervals unavailable (%s); assuming 8h", e)
        return self._intervals
