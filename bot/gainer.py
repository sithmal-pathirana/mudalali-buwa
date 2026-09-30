"""
Gainer mining: trade whichever USDT perpetual has just become the top gainer.

The rules, as asked for:

  * When a symbol takes FIRST place on the 24h gainer board, open a long on it
    with a take-profit sized to bank `exit.target_usd` and a protective stop.
    The exchange closes it when either triggers.
  * When a different symbol takes first place, open that one too. Every other
    gainer position is re-judged at that moment (`exit.on_new_leader`): still
    in profit (net of the estimated round-trip cost) -> keep it; not in profit
    -> close it.
  * Notify every step: new leader, position opened, kept or closed, each
    profit milestone, and an hourly status of the open positions and the top
    of the board.

The swap guards. On 2026-09-15 AINUSDT and POWERUSDT, both fading after a
50-95% day, traded first place seven times in 77 minutes. Each swap sold one
at a small loss and bought the other, and the account bled about $0.50 with
neither stop nor target ever reached. Four settings stop that:

  * entry.confirm_minutes          a new #1 must hold first place this long
  * entry.buy_only_if_rising       a leader whose 24h % is falling is watched,
                                   not bought, until it rises again
  * entry.rebuy_cooldown_minutes   a coin sold recently is not bought back
  * exit.min_hold_minutes          a young position is not swapped out; its
                                   stop still protects it

A monitor runs inside the engine loop every `board.poll_seconds`. It keeps a
short history of the top of the board and reports how the leader is behaving
(accelerating, holding, fading) and which challenger is closing on it.

Honest label, same as every other strategy in this repo: the "prediction" is an
EXTRAPOLATION of each symbol's recent rate of change in 24h %, not a forecast.
It is right while momentum persists and wrong exactly when it turns. Coins
reach first place AFTER their move, and a coin at the top of the 24h board is
as often at the end of its run as in the middle of it. Nothing here has been
backtested. `dry_run: true` (the default) runs everything -- board, alerts,
simulated fills, simulated take-profit and stop -- with nothing sent.

This module never sizes past the engine's own guards: the risk preflight
(halt, KILL file, equity floor, daily loss limit, trade cap), isolated margin
at risk.max_leverage, and the leverage ceiling across the whole book. Every
live position gets an exchange-side stop, or is closed at market straight away.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .binanceapi import BinanceError
from .notify import Event
from .positions import ActivePosition
from .state import client_order_id

log = logging.getLogger("gainer")

ROOT = Path(__file__).resolve().parent.parent
GAINER_STATE_PATH = ROOT / "data" / "gainer.json"

#: Marks a book position as belonging to this strategy. The supervisor stands
#: aside for these and the tracker re-attaches to them after a restart.
STRATEGY = "gainer"

EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}

#: What happens to the positions already held when a new coin takes first place.
ON_NEW_LEADER = ("close_if_losing", "keep")

#: Closes older than this are forgotten; no cooldown is meant to be longer.
CLOSED_AT_MAX_AGE = 24 * 3600


# ------------------------------------------------------------------- config
@dataclass
class GainerBoardConfig:
    """What is watched: the 24h gainer board."""
    poll_seconds: int = 30
    top_n: int = 10
    #: 24h quote volume floor. A $300k coin can top the board on one order.
    min_quote_volume: float = 10_000_000
    #: Decide on the leader only once per this many minutes, on the first
    #: poll of each period (60 = at the top of every hour). Stops, the ladder,
    #: the time limit and the sweep still run on every poll. 0 = every poll.
    #: Every 2-year replay read the board hourly, so 60 is the tested
    #: behaviour; a 30-second board also buys short spikes it never saw.
    leader_check_minutes: float = 0.0
    #: What makes a coin "the leader". change_24h: the biggest 24h % rise,
    #: which by the time it tops the board has usually already happened
    #: (typically +37%). climb: the biggest rise over the last climb_minutes,
    #: the coin moving fastest now. Needs climb_minutes of price history after
    #: a start before it picks anyone; stops and the ladder run meanwhile.
    rank_by: str = "change_24h"
    climb_minutes: float = 60.0
    #: With rank_by climb, only coins whose volume over the climb window is at
    #: least this many times their 24h average for such a window. It is
    #: estimated from the rolling 24h volume Binance reports. 0 = off.
    climb_volume_surge_x: float = 0.0

    def __post_init__(self):
        if self.rank_by not in ("change_24h", "climb"):
            raise ValueError("gainer.board.rank_by must be change_24h or climb, "
                             f"not {self.rank_by!r}")
        if self.rank_by == "climb" and self.climb_minutes <= 0:
            raise ValueError("gainer.board.climb_minutes must be > 0")


@dataclass
class GainerEntryConfig:
    """When a leader is bought, and how much."""
    #: False: the leader found at boot is a baseline, not a new leader.
    trade_on_start: bool = False
    #: A new symbol must hold first place this long before it counts as the
    #: leader, so two coins trading places minute by minute are not traded.
    confirm_minutes: float = 5.0
    #: Buy a leader only while its 24h % is still climbing. A fading leader is
    #: watched while it stays first, and bought if it starts rising again.
    buy_only_if_rising: bool = True
    #: The climb that counts as rising, in 24h % points per minute, measured
    #: over forecast.slope_minutes.
    min_rise_pct_per_min: float = 0.05
    #: A coin sold (for any reason) is not bought again as a new leader for
    #: this long. 0 turns it off. Re-entry on a new high is exempt: it has its
    #: own trigger.
    rebuy_cooldown_minutes: float = 60.0
    #: When a position on the leader closes (stop or take-profit) and the
    #: symbol is still first, buy it again once its price trades above the
    #: 24h high recorded at the close. One attempt per new high.
    reentry_on_new_high: bool = True
    #: USDT per trade. Must clear Binance's $5 minimum after lot rounding.
    notional_usdt: float = 5.5
    #: Size each trade as this percent of the equity the bot trades with
    #: (money set aside for the Funding wallet excluded) instead of the fixed
    #: notional_usdt. 0 = use notional_usdt. With max_positions 5, 10 keeps
    #: the whole book at 50% of equity: in the 2-year replay that version
    #: stayed above deposits in 20 of 24 months, against 3 of 24 for one
    #: position at 50%.
    notional_pct_of_equity: float = 0.0
    #: With notional_pct_of_equity, never size a trade below this: a small
    #: account's percentage falls under Binance's $5 minimum after lot
    #: rounding (10% of $50), and every trade would be refused. The engine's
    #: leverage ceiling and equity floor still apply. 0 = no minimum.
    min_notional_usdt: float = 0.0
    max_positions: int = 2


@dataclass
class GainerExitConfig:
    """When a position is sold."""
    #: Net profit the take-profit is placed to bank, in USDT.
    target_usd: float = 2.0
    #: Take-profit this percent above the entry instead (100 = when the coin
    #: has doubled); 0 = use target_usd. It works alongside the ladder: the
    #: stop walks up as usual, and a run that reaches it is banked.
    target_pct: float = 0.0
    stop_pct: float = 5.0
    #: Round-trip cost estimate as % of notional. Added to the take-profit
    #: distance so the target is NET, and subtracted before calling a position
    #: "still profitable" when a new leader arrives.
    fee_pct: float = 0.15
    #: close_if_losing: a held position that is not in profit is sold when
    #: another coin takes first place. keep: only its stop and take-profit
    #: ever close it.
    on_new_leader: str = "close_if_losing"
    #: A position younger than this is never sold because of a new leader.
    min_hold_minutes: float = 15.0
    #: The stop ladder. Once the price has been ladder_first_pct above entry,
    #: the stop moves to the entry price. From two steps of ladder_step_pct
    #: on, it sits one step below the highest step reached:
    #:   first 10, step 10:  +10 -> entry, +20 -> +10, +30 -> +20, ...
    #:   first 10, step 20:  +10 -> entry, +40 -> +20, +60 -> +40, ...
    #:   first 20, step 20:  +20 -> entry, +40 -> +20, +60 -> +40, ...
    #: With the ladder on, target_usd 0 means no take-profit at all: only the
    #: stop, walking up, closes the trade.
    ladder_enabled: bool = False
    ladder_first_pct: float = 10.0
    ladder_step_pct: float = 10.0
    #: Close at market a position whose ladder has not armed (never reached
    #: ladder_first_pct) after this many hours. 0 = never. Without it one
    #: coin that drifts between its stop and the first step blocks a
    #: one-position book for weeks: in the 6-month replay a 30% stop left
    #: OGNUSDT open from 2026-07-08 to the end of the data.
    unarmed_max_hours: float = 0.0
    #: lock: at ladder_first_pct the stop jumps to entry, then one step below
    #: each new step. trail: the stop rises ladder_step_pct for every
    #: ladder_step_pct gained, keeping its starting distance.
    ladder_mode: str = "lock"
    #: fixed: stop_pct below entry. atr: atr_stop_mult x the coin's 12h ATR %,
    #: kept between atr_stop_min_pct and atr_stop_max_pct.
    stop_mode: str = "fixed"
    atr_stop_mult: float = 3.0
    atr_stop_min_pct: float = 10.0
    atr_stop_max_pct: float = 40.0
    #: Sell partial_fraction of the position once it is this % above entry,
    #: with an exchange-side take-profit placed at open. 0 = off.
    partial_take_pct: float = 0.0
    partial_fraction: float = 0.5

    def __post_init__(self):
        if self.on_new_leader not in ON_NEW_LEADER:
            raise ValueError(f"gainer.exit.on_new_leader must be one of "
                             f"{' | '.join(ON_NEW_LEADER)}, not {self.on_new_leader!r}")
        if self.ladder_enabled and (self.ladder_first_pct <= 0 or self.ladder_step_pct <= 0):
            raise ValueError("gainer.exit.ladder_first_pct and ladder_step_pct must be > 0")
        if self.ladder_mode not in ("lock", "trail"):
            raise ValueError("gainer.exit.ladder_mode must be lock or trail")
        if self.stop_mode not in ("fixed", "atr"):
            raise ValueError("gainer.exit.stop_mode must be fixed or atr")
        if not 0 < self.partial_fraction < 1:
            raise ValueError("gainer.exit.partial_fraction must be between 0 and 1")

    @property
    def has_target(self) -> bool:
        return self.target_pct > 0 or self.target_usd > 0

    def target_for(self, entry: float, qty: float) -> float:
        """The take-profit level for a fill, or 0.0 for none."""
        if self.target_pct > 0:
            return entry * (1 + self.target_pct / 100.0)
        if self.target_usd > 0:
            return target_price(entry, qty, self.target_usd, self.fee_pct)
        return 0.0


@dataclass
class GainerFiltersConfig:
    """Which new leaders are skipped, as a trader would. Every check is off at
    its default. A skipped leader is not watched or bought later."""
    #: Skip when the 1-hour RSI(14) is above this (overbought). 0 = off.
    max_rsi_1h: float = 0.0
    #: Skip coins listed on futures fewer than this many days ago. 0 = off.
    min_listing_age_days: float = 0.0
    #: Skip while BTC's 24h change is below this percent (e.g. -2). None = off.
    btc_min_change_24h_pct: float | None = None
    #: Skip while BTC has fallen more than this percent in the last hour. 0 = off.
    btc_max_drop_1h_pct: float = 0.0
    #: Skip coins priced below this, in USDT. 0 = off.
    min_price: float = 0.0


@dataclass
class GainerSizingConfig:
    """How big each trade is, on top of entry.notional_pct_of_equity."""
    #: fixed: entry.notional_pct_of_equity. volatility: scaled by
    #: volatility_target_atr_pct / the coin's 12h ATR %, within min/max.
    #: conviction: high_pct when the conviction checks all pass, else low_pct.
    mode: str = "fixed"
    volatility_target_atr_pct: float = 3.0
    min_pct: float = 5.0
    max_pct: float = 15.0
    conviction_high_pct: float = 15.0
    conviction_low_pct: float = 7.5
    conviction_max_rsi_1h: float = 85.0
    conviction_min_listing_age_days: float = 3.0
    conviction_btc_min_change_24h_pct: float = -2.0

    def __post_init__(self):
        if self.mode not in ("fixed", "volatility", "conviction"):
            raise ValueError("gainer.sizing.mode must be fixed, volatility or conviction")


@dataclass
class GainerLimitsConfig:
    """When no new gainer trade is opened. Open trades are never touched."""
    #: New trades per UTC day. 0 = no cap.
    max_new_trades_per_day: int = 0
    #: After this many losing trades in a row, open nothing for pause_hours. 0 = off.
    pause_after_losses: int = 0
    pause_hours: float = 24.0
    #: All open gainer positions together at most this % of equity. 0 = off.
    max_exposure_pct: float = 0.0
    #: Stop opening for the rest of the UTC day once gainer trades closed
    #: today lost more than this % of the day's opening equity. 0 = off.
    daily_loss_limit_pct: float = 0.0
    #: Stop opening while equity is more than this % below its highest point. 0 = off.
    drawdown_pause_pct: float = 0.0


@dataclass
class GainerAlertsConfig:
    """What is announced beyond each trade."""
    milestones_usd: list = field(default_factory=lambda: [1.0, 1.5, 2.0])
    status_minutes: int = 60


@dataclass
class GainerForecastConfig:
    """How the board's recent motion is measured."""
    history_minutes: int = 60
    #: Window the rate of change is measured over, and how far it is projected.
    slope_minutes: float = 10.0
    predict_minutes: float = 15.0
    #: |slope| below this (24h % points per minute) reads as "holding".
    trend_eps: float = 0.05


@dataclass
class GainerSweepConfig:
    """Banking part of each winning trade in the Funding wallet."""
    #: After every profitable gainer trade, move pct of its profit from the
    #: USD-M futures wallet to the Funding wallet, where it is no longer
    #: traded. In a 2-year replay of the hybrid ladder, 25% per winning trade
    #: left $1,009 in Funding and cut the months spent below deposits from 22
    #: of 24 to 20; a monthly sweep of profit above deposits almost never
    #: fired, because this strategy's gains arrive in sudden spikes.
    #: Live only (testnet has no Funding wallet): paper and testnet report
    #: what would have moved. The API key needs "Permits Universal Transfer".
    enabled: bool = False
    pct: float = 25.0
    #: Amounts under this are carried forward and added to the next one.
    min_transfer_usdt: float = 1.0
    #: Keep every profit trading until equity reaches this, then start moving
    #: pct of wins to Funding. 0 = sweep from the first win.
    start_when_equity_usdt: float = 0.0
    #: Principal recovery. When the trading balance reaches principal_trigger_x
    #: times everything deposited so far, move the deposits not yet moved out
    #: to Funding; the rest (1.5x the deposits at the default 2.5) is profit,
    #: and that keeps trading.
    principal_enabled: bool = False
    principal_trigger_x: float = 2.5
    #: Deposits are the transfers INTO the USD-M futures wallet from this UTC
    #: date on (YYYY-MM-DD), read from Binance's income history. Required:
    #: the rule stays off until it is set, so years-old transfers never count.
    deposits_since: str = ""
    #: Use this figure instead of Binance's history (paper/testnet rehearsals,
    #: or deposits made before deposits_since). 0 = read Binance.
    deposits_override_usdt: float = 0.0


GROUPS = {"board": GainerBoardConfig, "entry": GainerEntryConfig,
          "exit": GainerExitConfig, "alerts": GainerAlertsConfig,
          "forecast": GainerForecastConfig, "sweep": GainerSweepConfig,
          "filters": GainerFiltersConfig, "sizing": GainerSizingConfig,
          "limits": GainerLimitsConfig}


@dataclass
class GainerConfig:
    enabled: bool = False
    #: Paper-trade: watch the board, simulate fills and exits, send every
    #: alert, and send NOTHING to the exchange. Independent of the top-level
    #: dry_run, which forces this on when it is true.
    dry_run: bool = True
    board: GainerBoardConfig = field(default_factory=GainerBoardConfig)
    entry: GainerEntryConfig = field(default_factory=GainerEntryConfig)
    exit: GainerExitConfig = field(default_factory=GainerExitConfig)
    alerts: GainerAlertsConfig = field(default_factory=GainerAlertsConfig)
    forecast: GainerForecastConfig = field(default_factory=GainerForecastConfig)
    sweep: GainerSweepConfig = field(default_factory=GainerSweepConfig)
    filters: GainerFiltersConfig = field(default_factory=GainerFiltersConfig)
    sizing: GainerSizingConfig = field(default_factory=GainerSizingConfig)
    limits: GainerLimitsConfig = field(default_factory=GainerLimitsConfig)

    def __post_init__(self):
        # config.yaml hands each group over as a dict.
        for name, cls in GROUPS.items():
            value = getattr(self, name)
            if value is None:
                setattr(self, name, cls())
            elif isinstance(value, dict):
                try:
                    setattr(self, name, cls(**value))
                except TypeError as e:
                    valid = ", ".join(sorted(cls.__dataclass_fields__))
                    raise TypeError(f"bad key under gainer.{name} ({e}). "
                                    f"Valid keys: {valid}") from None
            elif not isinstance(value, cls):
                raise TypeError(f"gainer.{name} must be a block of settings")


# ------------------------------------------------------------------ the board
@dataclass
class Row:
    symbol: str
    change_pct: float
    price: float
    quote_volume: float
    rank: int = 0
    #: % rise over board.climb_minutes, when the board ranks by climb.
    climb_pct: float = 0.0


def rank_board(tickers: list, tradable: set, min_quote_volume: float) -> list[Row]:
    """Every eligible symbol, best 24h change first, ranks from 1."""
    rows = []
    for t in tickers or []:
        sym = t.get("symbol")
        if tradable and sym not in tradable:
            continue
        try:
            qv = float(t.get("quoteVolume") or 0)
            row = Row(sym, float(t["priceChangePercent"]),
                      float(t.get("lastPrice") or 0), qv)
        except (KeyError, TypeError, ValueError):
            continue
        if qv < min_quote_volume or row.price <= 0:
            continue
        rows.append(row)
    rows.sort(key=lambda r: -r.change_pct)
    for i, r in enumerate(rows, 1):
        r.rank = i
    return rows


def rank_by_climb(rows: list[Row], history: dict, now: float, minutes: float,
                  surge_x: float = 0.0) -> list[Row]:
    """
    The same rows, fastest climber first, using `history` (symbol -> list of
    (time, price, 24h quote volume), oldest first). A symbol whose history
    does not yet reach back `minutes` is left out.
    """
    window = minutes * 60.0
    ranked = []
    for r in rows:
        h = history.get(r.symbol)
        if not h or now - h[0][0] < window:
            continue
        base = None
        for t, px, qv in h:                  # the last sample at or before the window start
            if now - t >= window:
                base = (px, qv)
            else:
                break
        if base is None or base[0] <= 0:
            continue
        if surge_x > 0:
            share = window / 86400.0
            # rolling 24h volume: what arrived = change + what rolled out (~average)
            recent = r.quote_volume - base[1] + base[1] * share
            if recent < surge_x * r.quote_volume * share:
                continue
        r.climb_pct = (r.price / base[0] - 1) * 100.0
        ranked.append(r)
    ranked.sort(key=lambda x: -x.climb_pct)
    for i, r in enumerate(ranked, 1):
        r.rank = i
    return ranked


@dataclass
class Forecast:
    leader: str
    leader_trend: str           # accelerating | holding | fading | unknown
    leader_slope: float         # 24h % points per minute
    challenger: str = ""
    eta_minutes: float = 0.0    # 0 = not closing
    lines: list = field(default_factory=list)

    def text(self) -> str:
        head = f"leader {self.leader} is {self.leader_trend} ({self.leader_slope:+.2f} %/min)"
        if self.challenger:
            eta = (f", could overtake in ~{self.eta_minutes:.0f} min"
                   if self.eta_minutes else "")
            head += f"\nlikely next: {self.challenger}{eta}"
        else:
            head += "\nno challenger closing on it"
        return head + "\n(extrapolated from the last few minutes, not a prediction)"


class GainerBoard:
    """A short history of the top of the board, and what it is doing."""

    def __init__(self, cfg: GainerConfig):
        self.cfg = cfg
        #: (timestamp, {symbol: change_pct}) for the top 3*top_n each poll, so
        #: a climber from outside the top 10 already has history when it enters.
        self.history: deque = deque()
        self.rows: list[Row] = []

    def update(self, rows: list[Row], now: float) -> None:
        self.rows = rows
        keep = rows[: self.cfg.board.top_n * 3]
        self.history.append((now, {r.symbol: r.change_pct for r in keep}))
        horizon = now - self.cfg.forecast.history_minutes * 60
        while self.history and self.history[0][0] < horizon:
            self.history.popleft()

    @property
    def top(self) -> list[Row]:
        return self.rows[: self.cfg.board.top_n]

    @property
    def leader(self) -> Row | None:
        return self.rows[0] if self.rows else None

    def slope(self, symbol: str, now: float) -> float | None:
        """24h % points per minute over about slope_minutes; None if unknown."""
        if not self.history or symbol not in self.history[-1][1]:
            return None
        t_now, latest = self.history[-1]
        target = t_now - self.cfg.forecast.slope_minutes * 60
        base = None
        for t, snap in self.history:
            if symbol in snap:
                base = (t, snap[symbol])
                if t >= target:
                    break
        if base is None or t_now - base[0] < 60:
            return None
        return (latest[symbol] - base[1]) / ((t_now - base[0]) / 60.0)

    def trend_word(self, slope: float | None) -> str:
        if slope is None:
            return "unknown"
        if slope > self.cfg.forecast.trend_eps:
            return "accelerating"
        if slope < -self.cfg.forecast.trend_eps:
            return "fading"
        return "holding"

    def forecast(self, now: float) -> Forecast | None:
        lead = self.leader
        if lead is None:
            return None
        ls = self.slope(lead.symbol, now)
        fc = Forecast(lead.symbol, self.trend_word(ls), ls or 0.0)
        horizon = self.cfg.forecast.predict_minutes
        lead_proj = lead.change_pct + (ls or 0.0) * horizon
        best, best_proj = None, lead_proj
        for r in self.top[1:]:
            s = self.slope(r.symbol, now)
            if s is None:
                continue
            proj = r.change_pct + s * horizon
            if proj > best_proj:
                best, best_proj = (r, s), proj
        if best is not None:
            r, s = best
            closing = s - (ls or 0.0)
            fc.challenger = r.symbol
            if closing > 0:
                fc.eta_minutes = max(1.0, (lead.change_pct - r.change_pct) / closing)
        return fc

    def board_lines(self, now: float, held: set) -> list[str]:
        out = []
        for r in self.top:
            s = self.slope(r.symbol, now)
            arrow = {"accelerating": "^", "fading": "v", "holding": "=",
                     "unknown": "?"}[self.trend_word(s)]
            mark = " *held" if r.symbol in held else ""
            out.append(f"{r.rank:>2}. {r.symbol:<14} {r.change_pct:+7.2f}% {arrow}{mark}")
        return out


# ---------------------------------------------------------------- arithmetic
def target_price(entry: float, qty: float, target_usd: float, fee_pct: float) -> float:
    """Long take-profit that nets target_usd after the estimated round trip."""
    cost = entry * qty * fee_pct / 100.0
    return entry + (target_usd + cost) / qty


def stop_price(entry: float, stop_pct: float) -> float:
    return entry * (1 - stop_pct / 100.0)


def net_pnl(entry: float, qty: float, price: float, fee_pct: float) -> float:
    return (price - entry) * qty - entry * qty * fee_pct / 100.0


def ladder_stop(entry: float, peak: float, first_pct: float, step_pct: float,
                mode: str = "lock", init_stop: float = 0.0) -> float | None:
    """The stop the ladder calls for at this peak, or None before it arms."""
    # Rounded so an exact step (1.40 / 1.0 reads 39.999...%) counts as reached.
    gain = round((peak / entry - 1) * 100.0, 9) if entry > 0 else 0.0
    if mode == "trail":
        steps = int(gain // step_pct) if step_pct > 0 else 0
        return init_stop + entry * steps * step_pct / 100.0 if steps >= 1 else None
    if gain < first_pct:
        return None
    level = entry
    steps = int(gain // step_pct)
    if steps >= 2:
        level = max(level, entry * (1 + (steps - 1) * step_pct / 100.0))
    return level


def rsi(closes: list) -> float | None:
    """Wilder-free simple RSI over the given closes (15 closes = RSI 14)."""
    if len(closes) < 2:
        return None
    up = down = 0.0
    for a, b in zip(closes, closes[1:]):
        up += max(b - a, 0.0)
        down += max(a - b, 0.0)
    return 100.0 if down == 0 else 100.0 - 100.0 / (1.0 + up / down)


def atr_pct(bars: list) -> float | None:
    """Mean true range of klines as % of the last close. bars: [o,h,l,c] rows."""
    if len(bars) < 2 or bars[-1][3] <= 0:
        return None
    trs = [max(h - l, abs(h - pc), abs(l - pc))
           for (_, h, l, _c), (_, _, _, pc) in zip(bars[1:], bars[:-1])]
    return sum(trs) / len(trs) / bars[-1][3] * 100.0


# ----------------------------------------------------------------- positions
@dataclass
class Track:
    symbol: str
    entry: float
    qty: float
    stop: float
    take_profit: float
    opened_at: float
    paper: bool = True
    milestones_hit: list = field(default_factory=list)
    #: Highest price seen while held; the ladder steps are counted on it.
    peak: float = 0.0
    #: The stop as first placed (a trail ladder keeps this distance).
    init_stop: float = 0.0
    #: exit.partial_take_pct: the level, and whether that part has been sold.
    partial_price: float = 0.0
    partial_done: bool = False


class GainerMiner:
    EXCHANGE_INFO_MAX_AGE = 3600.0     # new listings are often the top gainers
    #: Tag on the engine's positions, so the supervisor stands aside and a
    #: restart re-attaches them to the right strategy. Subclasses override.
    strategy = STRATEGY
    #: How an entry is described in alerts, and the prefix of a close reason.
    entry_label = "top gainer"
    close_label = "gainer mining"
    FILL_WAIT_ATTEMPTS = 4
    FILL_WAIT_SECONDS = 0.5

    def __init__(self, engine, cfg: GainerConfig, path: Path = GAINER_STATE_PATH):
        self.engine = engine
        self.cfg = cfg
        self.path = path
        self.board = GainerBoard(cfg)
        self.leader = ""                # confirmed leader
        self._candidate = ""
        self._candidate_since = 0.0
        self._baselined = False
        self.tracks: dict[str, Track] = {}
        #: symbol -> price it must trade above to be bought again. 0.0 means
        #: "not known yet": the 24h high on the next poll is taken.
        self.rearm: dict[str, float] = {}
        #: symbol -> when a gainer position on it last closed, for the cooldown.
        self.closed_at: dict[str, float] = {}
        #: The confirmed leader an entry guard held back; re-checked each poll
        #: while it stays first.
        self.waiting = ""
        self._leader_period = -1
        self._last_poll = 0.0
        self._last_status = time.time()
        self._tradable: set = set()
        self._tradable_at = 0.0
        #: symbol -> [(time, price, 24h quote volume)], for board.rank_by climb.
        self.price_history: dict = {}
        self._warming_noted = False
        #: filters / sizing / limits bookkeeping
        self._onboard: dict = {}            # symbol -> futures listing time (ms)
        self._btc_24h: float | None = None
        self._features: dict = {}           # symbol -> (time, {rsi, atr})
        self.day_opens: dict = {}           # "YYYY-MM-DD" -> new trades opened
        self.day_realized: dict = {}        # "YYYY-MM-DD" -> P&L of gainer closes
        self.loss_streak = 0
        self.pause_until = 0.0
        self.peak_equity = 0.0
        #: Sweep bookkeeping: waiting to reach min_transfer_usdt, and the
        #: running total moved (or, on paper/testnet, that would have moved).
        self.sweep_pending = 0.0
        self.swept_total = 0.0
        self._sweep_error = ""
        #: Principal already moved to Funding, and principal whose transfer
        #: failed and is waiting. Pending amounts are ring-fenced: the engine
        #: takes them out of the equity it sizes and risks trades with.
        self.principal_withdrawn = 0.0
        self.principal_pending = 0.0
        self._principal_note = ""
        self.load()

    @property
    def paper(self) -> bool:
        return bool(self.cfg.dry_run or self.engine.cfg.dry_run)

    # ------------------------------------------------------------- persistence
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.leader = raw.get("leader", "")
        self.rearm = {s: float(h) for s, h in (raw.get("rearm") or {}).items()}
        self.closed_at = {s: float(t) for s, t in (raw.get("closed_at") or {}).items()}
        self.sweep_pending = float(raw.get("sweep_pending", 0.0))
        self.swept_total = float(raw.get("swept_total", 0.0))
        self.principal_withdrawn = float(raw.get("principal_withdrawn", 0.0))
        self.principal_pending = float(raw.get("principal_pending", 0.0))
        self.day_opens = dict(raw.get("day_opens") or {})
        self.day_realized = dict(raw.get("day_realized") or {})
        self.loss_streak = int(raw.get("loss_streak", 0))
        self.pause_until = float(raw.get("pause_until", 0.0))
        self.peak_equity = float(raw.get("peak_equity", 0.0))
        for sym, t in (raw.get("tracks") or {}).items():
            try:
                self.tracks[sym] = Track(**t)
            except TypeError:
                log.warning("ignoring unreadable gainer track for %s", sym)

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(
                {"leader": self.leader,
                 "rearm": self.rearm,
                 "closed_at": self.closed_at,
                 "sweep_pending": self.sweep_pending,
                 "swept_total": self.swept_total,
                 "principal_withdrawn": self.principal_withdrawn,
                 "principal_pending": self.principal_pending,
                 "day_opens": dict(list(self.day_opens.items())[-7:]),
                 "day_realized": dict(list(self.day_realized.items())[-7:]),
                 "loss_streak": self.loss_streak,
                 "pause_until": self.pause_until,
                 "peak_equity": self.peak_equity,
                 "tracks": {s: asdict(t) for s, t in self.tracks.items()}},
                indent=2))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("could not save gainer state: %s", e)

    def restore(self) -> None:
        """
        After a restart the engine re-adopts open positions from the exchange
        but cannot know which strategy opened them. Re-mark ours so the
        supervisor keeps standing aside, and drop live tracks that are gone.
        """
        for sym, t in list(self.tracks.items()):
            if t.paper:
                if not self.paper:
                    del self.tracks[sym]        # paper trades do not survive going live
                continue
            pos = self.engine.book.get(sym)
            if pos is None:
                log.info("gainer: %s closed while the bot was down", sym)
                del self.tracks[sym]
                self.closed_at[sym] = time.time()
                self._arm(sym, {})
                continue
            pos.strategy = self.strategy
            pos.no_target = t.take_profit <= 0
        self.save()

    # -------------------------------------------------------------------- loop
    def tick(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        if now - self._last_poll < self.cfg.board.poll_seconds:
            return
        self._last_poll = now
        try:
            tickers = self.engine.api.ticker_24hr()
        except BinanceError as e:
            log.warning("gainer board unavailable: %s", e)
            return
        rows = rank_board(tickers, self.tradable(now), self.cfg.board.min_quote_volume)
        if not rows:
            return
        choose = True
        bd = self.cfg.board
        if bd.rank_by == "climb":
            self.record_history(rows, now)
            climbers = rank_by_climb(rows, self.price_history, now, bd.climb_minutes,
                                     bd.climb_volume_surge_x)
            if climbers:
                rows = climbers + [r for r in rows if r not in climbers]
                self._warming_noted = False
            else:
                choose = False              # no history yet: protect, do not pick
                if not self._warming_noted:
                    self._warming_noted = True
                    log.info("gainer: collecting %g min of prices before picking "
                             "the fastest climber", bd.climb_minutes)
        self.board.update(rows, now)
        self.closed_at = {s: t for s, t in self.closed_at.items()
                          if now - t < CLOSED_AT_MAX_AGE}
        prices = {r.symbol: r.price for r in rows}
        highs = {}
        eq = float(getattr(self.engine, "equity", 0.0) or 0.0)
        if eq > self.peak_equity:
            self.peak_equity = eq
        for t in tickers or []:
            sym = t.get("symbol")
            if sym == "BTCUSDT":
                try:
                    self._btc_24h = float(t.get("priceChangePercent"))
                except (TypeError, ValueError):
                    pass
            try:
                if sym in self.tracks or sym in self.rearm:
                    highs[sym] = float(t.get("highPrice") or 0)
                # Held symbols can fall under the volume floor; price them anyway.
                if sym in self.tracks and sym not in prices:
                    prices[sym] = float(t.get("lastPrice") or 0)
            except (TypeError, ValueError):
                pass

        self.sync_tracks(prices, highs, now)
        self.manage_ladder(prices, now)
        if choose and self.leader_due(now):
            self.check_leader(now, prices, highs)
        self.check_milestones(prices)
        status = self.cfg.alerts.status_minutes
        if status and now - self._last_status >= status * 60:
            self._last_status = now
            self.send_status(now, prices)

    def record_history(self, rows: list, now: float) -> None:
        """Keep climb_minutes (+10 min) of prices per symbol for rank_by climb."""
        keep = self.cfg.board.climb_minutes * 60 + 600
        for r in rows:
            h = self.price_history.setdefault(r.symbol, [])
            h.append((now, r.price, r.quote_volume))
            while h and now - h[0][0] > keep:
                h.pop(0)
        for sym in [s for s, h in self.price_history.items() if not h or now - h[-1][0] > keep]:
            del self.price_history[sym]

    def leader_due(self, now: float) -> bool:
        """board.leader_check_minutes: True on the first poll of each period."""
        minutes = self.cfg.board.leader_check_minutes
        if minutes <= 0:
            return True
        period = int(now // (minutes * 60))
        if period == self._leader_period:
            return False
        self._leader_period = period
        return True

    def _manual(self, symbol: str) -> bool:
        is_manual = getattr(self.engine, "is_manual", None)
        return bool(is_manual and is_manual(symbol) is True)

    def tradable(self, now: float) -> set:
        if self._tradable and now - self._tradable_at < self.EXCHANGE_INFO_MAX_AGE:
            return self._tradable
        try:
            info = self.engine.api.exchange_info()
        except BinanceError as e:
            log.warning("exchange info unavailable (%s); keeping the old symbol list", e)
            return self._tradable
        self._onboard = {s["symbol"]: float(s.get("onboardDate") or 0)
                         for s in info.get("symbols", []) if s.get("symbol")}
        self._tradable = {
            s["symbol"] for s in info.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT" and s.get("baseAsset") not in EXCLUDE_BASES
            and not self._manual(s["symbol"])}
        self._tradable_at = now
        return self._tradable

    def check_leader(self, now: float, prices: dict, highs: dict | None = None) -> None:
        lead = self.board.leader
        if lead is None:
            return
        if not self._baselined:
            self._baselined = True
            if not self.cfg.entry.trade_on_start:
                self.leader = lead.symbol
                log.info("gainer: baseline leader %s (%+.2f%%); waiting for a new one",
                         lead.symbol, lead.change_pct)
                self.save()
                return
        if lead.symbol == self.leader:
            self._candidate = ""
            self.check_reentry(lead, highs or {})
            if self.waiting == lead.symbol and lead.symbol not in self.tracks:
                self.try_enter(lead, now)
            return
        if lead.symbol != self._candidate:
            self._candidate, self._candidate_since = lead.symbol, now
        if now - self._candidate_since < self.cfg.entry.confirm_minutes * 60:
            return
        previous, self.leader = self.leader, lead.symbol
        self._candidate = ""
        self.waiting = ""
        self.rearm.clear()          # a new leader is traded by on_new_leader
        self.save()
        self.on_new_leader(lead, previous, now, prices)

    def _arm(self, symbol: str, highs: dict) -> None:
        """A position on the leader closed: allow one buy above its 24h high."""
        if not self.cfg.entry.reentry_on_new_high or symbol != self.leader:
            return
        self.rearm[symbol] = highs.get(symbol, 0.0)
        self.save()
        if self.rearm[symbol] > 0:
            self.notify(f"{symbol} still leads; will buy again above its 24h high "
                        f"{self.rearm[symbol]:,.6g}", symbol=symbol)

    def check_reentry(self, lead: Row, highs: dict) -> None:
        sym = lead.symbol
        if not self.cfg.entry.reentry_on_new_high or sym not in self.rearm or sym in self.tracks:
            return
        high = self.rearm[sym]
        if high <= 0:
            if highs.get(sym, 0.0) > 0:
                self.rearm[sym] = highs[sym]
                self.save()
            return
        if lead.price <= high:
            return
        del self.rearm[sym]         # one attempt; a refusal must not retry every poll
        self.save()
        self.notify(f"RE-ENTRY: {sym} made a new high {lead.price:,.6g} "
                    f"(above {high:,.6g}) and still leads at {lead.change_pct:+.2f}%",
                    symbol=sym)
        self.open(sym, lead.price, lead.change_pct)

    def on_new_leader(self, lead: Row, previous: str, now: float, prices: dict) -> None:
        fc = self.board.forecast(now)
        climb = (f", climbing {lead.climb_pct:+.2f}% in {self.cfg.board.climb_minutes:g} min"
                 if self.cfg.board.rank_by == "climb" else "")
        self.notify(f"NEW TOP GAINER: {lead.symbol} {lead.change_pct:+.2f}% 24h{climb} "
                    f"(was {previous or 'none'})"
                    + (f"\n{fc.text()}" if fc else ""), symbol=lead.symbol)

        self.judge_held(lead.symbol, now, prices)

        if lead.symbol in self.tracks:
            self.notify(f"{lead.symbol} is back on top; already holding it", symbol=lead.symbol)
            return
        why = self.filter_reason(lead, now)
        if why:
            log.info("gainer: skipping %s -- %s", lead.symbol, why)
            self.notify(f"{lead.symbol} SKIPPED by a filter: {why}", symbol=lead.symbol)
            return
        self.try_enter(lead, now)

    # ------------------------------------------------------------- filters
    def features(self, symbol: str) -> dict:
        """1h RSI(14) and 12h ATR % from one klines request, cached a minute."""
        hit = self._features.get(symbol)
        if hit and time.time() - hit[0] < 60:
            return hit[1]
        out = {"rsi": None, "atr": None}
        try:
            k = self.engine.api.klines(symbol, "1h", limit=16)[:-1]      # closed bars
            bars = [(float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in k]
            out["rsi"] = rsi([b[3] for b in bars[-15:]])
            out["atr"] = atr_pct(bars[-13:])
        except (BinanceError, IndexError, TypeError, ValueError) as e:
            log.warning("gainer: no 1h bars for %s (%s)", symbol, e)
        self._features[symbol] = (time.time(), out)
        return out

    def btc_change_1h(self) -> float | None:
        try:
            k = self.engine.api.klines("BTCUSDT", "5m", limit=13)[:-1]
            return (float(k[-1][4]) / float(k[0][1]) - 1) * 100.0
        except (BinanceError, IndexError, TypeError, ValueError, ZeroDivisionError):
            return None

    def filter_reason(self, lead, now: float) -> str:
        """Why this new leader is skipped by gainer.filters, or ''."""
        f = self.cfg.filters
        if f.min_price > 0 and lead.price < f.min_price:
            return f"price {lead.price:.6g} under {f.min_price:g}"
        if f.min_listing_age_days > 0:
            listed = self._onboard.get(lead.symbol, 0.0)
            age = (now * 1000 - listed) / 86_400_000 if listed else None
            if age is not None and age < f.min_listing_age_days:
                return f"listed {age:.1f} days ago (minimum {f.min_listing_age_days:g})"
        if f.btc_min_change_24h_pct is not None and self._btc_24h is not None \
                and self._btc_24h < f.btc_min_change_24h_pct:
            return f"BTC 24h {self._btc_24h:+.2f}% (minimum {f.btc_min_change_24h_pct:+g}%)"
        if f.btc_max_drop_1h_pct > 0:
            b1 = self.btc_change_1h()
            if b1 is not None and b1 < -f.btc_max_drop_1h_pct:
                return f"BTC fell {b1:.2f}% in the last hour"
        if f.max_rsi_1h > 0:
            r = self.features(lead.symbol)["rsi"]
            if r is not None and r > f.max_rsi_1h:
                return f"1h RSI {r:.0f} above {f.max_rsi_1h:g}"
        return ""

    def limit_reason(self, now: float, notional: float) -> str:
        """Why gainer.limits blocks a new trade right now, or ''."""
        lm = self.cfg.limits
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if lm.max_new_trades_per_day > 0 and self.day_opens.get(day, 0) >= lm.max_new_trades_per_day:
            return f"{lm.max_new_trades_per_day} new trades already today"
        if now < self.pause_until:
            return (f"paused after {lm.pause_after_losses} losses in a row, "
                    f"{(self.pause_until - now) / 3600:.1f}h left")
        eq = float(getattr(self.engine, "equity", 0.0) or 0.0)
        if lm.max_exposure_pct > 0:
            held = sum(t.entry * t.qty for t in self.tracks.values())
            if held + notional > eq * lm.max_exposure_pct / 100.0 + 1e-9:
                return (f"exposure ${held + notional:,.2f} would pass "
                        f"{lm.max_exposure_pct:g}% of equity")
        if lm.daily_loss_limit_pct > 0:
            start = float(getattr(getattr(self.engine, "state", None), "day_start_equity", 0) or eq)
            lost = self.day_realized.get(day, 0.0)
            if lost < -start * lm.daily_loss_limit_pct / 100.0:
                return f"gainer lost ${-lost:.2f} today, over {lm.daily_loss_limit_pct:g}%"
        if lm.drawdown_pause_pct > 0 and self.peak_equity > 0 \
                and eq < self.peak_equity * (1 - lm.drawdown_pause_pct / 100.0):
            return (f"equity ${eq:,.2f} is more than {lm.drawdown_pause_pct:g}% "
                    f"below its high ${self.peak_equity:,.2f}")
        return ""

    def note_close(self, pnl: float, now: float | None = None) -> None:
        """Every booked gainer close: loss streak, pause, and today's P&L."""
        now = time.time() if now is None else now
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        self.day_realized[day] = self.day_realized.get(day, 0.0) + pnl
        lm = self.cfg.limits
        if pnl < 0:
            self.loss_streak += 1
            if lm.pause_after_losses > 0 and self.loss_streak >= lm.pause_after_losses:
                self.pause_until = now + lm.pause_hours * 3600
                self.loss_streak = 0
                self.notify(f"PAUSED: {lm.pause_after_losses} losing gainer trades in a row; "
                            f"no new trades for {lm.pause_hours:g}h")
        else:
            self.loss_streak = 0
        self.save()

    def judge_held(self, new_leader: str, now: float, prices: dict) -> None:
        """exit.on_new_leader and exit.min_hold_minutes, for every other position."""
        ex = self.cfg.exit
        for sym, t in list(self.tracks.items()):
            if sym == new_leader:
                continue
            px = prices.get(sym, 0.0)
            if px <= 0:
                self.notify(f"{sym}: no price to judge it by; left open", symbol=sym)
                continue
            pnl = net_pnl(t.entry, t.qty, px, ex.fee_pct)
            if ex.on_new_leader == "keep":
                self.notify(f"{sym} KEPT: exit.on_new_leader is keep, so only its stop "
                            f"and take-profit close it ({pnl:+.2f} USDT)", symbol=sym)
                continue
            age_min = (now - t.opened_at) / 60.0
            if ex.min_hold_minutes > 0 and age_min < ex.min_hold_minutes:
                self.notify(f"{sym} HELD: opened {age_min:.0f} min ago, under "
                            f"exit.min_hold_minutes ({ex.min_hold_minutes:g}); its stop "
                            f"still protects it ({pnl:+.2f} USDT)", symbol=sym)
                continue
            if pnl > 0:
                self.notify(f"{sym} KEPT: still profitable {pnl:+.2f} USDT "
                            f"(now {px:,.6g}, entry {t.entry:,.6g})", symbol=sym)
            else:
                self.close(sym, px, f"no longer leader and not profitable ({pnl:+.2f} USDT)",
                           now=now)

    # ------------------------------------------------------------------- open
    def entry_blocker(self, lead: Row, now: float) -> str:
        """Why the confirmed leader should not be bought yet, or '' to buy it."""
        en = self.cfg.entry
        closed = self.closed_at.get(lead.symbol)
        if en.rebuy_cooldown_minutes > 0 and closed is not None:
            left = en.rebuy_cooldown_minutes * 60 - (now - closed)
            if left > 0:
                return (f"sold {(now - closed) / 60:.0f} min ago, inside "
                        f"entry.rebuy_cooldown_minutes ({en.rebuy_cooldown_minutes:g}; "
                        f"{left / 60:.0f} min left)")
        if en.buy_only_if_rising:
            s = self.board.slope(lead.symbol, now)
            if s is None:
                return "not enough board history yet to tell whether it is rising"
            if s < en.min_rise_pct_per_min:
                return (f"it is not rising ({s:+.2f} %/min, entry.min_rise_pct_per_min "
                        f"is {en.min_rise_pct_per_min:g})")
        return ""

    def try_enter(self, lead: Row, now: float) -> bool:
        """Buy the leader, or watch it while an entry guard holds it back."""
        why = self.entry_blocker(lead, now)
        if why:
            if self.waiting != lead.symbol:
                self.waiting = lead.symbol
                log.info("gainer: waiting on %s -- %s", lead.symbol, why)
                self.notify(f"{lead.symbol} not bought yet: {why}.\n"
                            f"Watching it while it stays #1.", symbol=lead.symbol)
            return False
        self.waiting = ""
        return self.open(lead.symbol, lead.price, lead.change_pct)

    def refuse(self, symbol: str, why: str) -> None:
        log.info("gainer: not opening %s -- %s", symbol, why)
        self.notify(f"{symbol} NOT opened: {why}", symbol=symbol)

    def open(self, symbol: str, price: float, change_pct: float = 0.0) -> bool:
        en, ex, eng = self.cfg.entry, self.cfg.exit, self.engine
        if len(self.tracks) >= en.max_positions:
            self.refuse(symbol, f"already holding {len(self.tracks)}/{en.max_positions} "
                                f"gainer positions")
            return False
        if symbol in eng.book:
            self.refuse(symbol, "another strategy already holds it")
            return False
        if self._manual(symbol):
            self.refuse(symbol, "it is a manual trade (manual_trades.symbols)")
            return False
        notional = self.trade_notional(symbol)
        why = self.limit_reason(time.time(), notional)
        if why:
            self.refuse(symbol, why)
            return False
        stop_pct = self.stop_pct_for(symbol)
        if (not ex.has_target and not ex.ladder_enabled) or ex.stop_pct <= 0 \
                or notional <= 0:
            self.refuse(symbol, "gainer exit.stop_pct and the trade size must be > 0 "
                                "(entry.notional_usdt, or notional_pct_of_equity of a "
                                "positive equity), and exit.target_usd too unless "
                                "exit.ladder_enabled")
            return False

        try:
            rules = eng.rules_for(symbol)
        except (KeyError, BinanceError) as e:
            self.refuse(symbol, f"no symbol filters ({e})")
            return False
        sized = rules.size_for_notional(notional, price)
        if sized is None:
            self.refuse(symbol, f"${notional:.2f} is under the cheapest legal "
                                f"order (${rules.min_affordable_notional(price):.2f})")
            return False
        qty = float(sized[0])

        day = time.strftime("%Y-%m-%d", time.gmtime())
        if self.paper:
            ok = self._opened(symbol, price, qty, paper=True, change_pct=change_pct,
                              stop=stop_price(price, stop_pct), stop_pct=stop_pct)
            if ok:
                self.day_opens[day] = self.day_opens.get(day, 0) + 1
                self.save()
            return ok

        gate = eng.risk.preflight(eng.equity)
        if not gate:
            self.refuse(symbol, gate.reason)
            return False
        held = sum(p.notional for p in eng.book.values())
        ceiling = eng.equity * eng.cfg.risk.max_leverage
        if held + qty * price > ceiling + 1e-9:
            self.refuse(symbol, f"would deploy ${held + qty * price:,.2f} against a "
                                f"${ceiling:,.2f} leverage ceiling")
            return False
        if not eng.prepare_symbol(symbol):
            return False

        eng._seq += 1
        entry_id = client_order_id("g", eng._seq)
        try:
            eng.api.order(symbol=symbol, side="BUY", type="MARKET", quantity=sized[0],
                          newClientOrderId=entry_id)
        except BinanceError as e:
            self.refuse(symbol, f"entry order refused: {e}")
            return False
        eng.risk.record_attempt()

        amt, entry = self._read_fill(symbol)
        if amt <= 0:
            eng.state.halt(f"gainer entry {entry_id} on {symbol} sent but no position "
                           f"could be read back")
            self.notify(f"{symbol}: market entry sent but the position could not be "
                        f"read back. Bot HALTED. Check Binance for an unprotected "
                        f"position.", symbol=symbol, event=Event.HALT)
            return False
        ok = self._protect(symbol, entry, amt, entry_id, rules, change_pct, stop_pct=stop_pct)
        if ok:
            self.day_opens[day] = self.day_opens.get(day, 0) + 1
            self.save()
        return ok

    def trade_notional(self, symbol: str | None = None) -> float:
        """USDT for the next trade: a share of equity, or the fixed amount."""
        en, sz = self.cfg.entry, self.cfg.sizing
        if en.notional_pct_of_equity <= 0:
            return en.notional_usdt
        share = en.notional_pct_of_equity
        if symbol and sz.mode == "volatility":
            a = self.features(symbol)["atr"]
            if a:
                share = min(sz.max_pct, max(sz.min_pct, share * sz.volatility_target_atr_pct / a))
        elif symbol and sz.mode == "conviction":
            feat = self.features(symbol)
            listed = self._onboard.get(symbol, 0.0)
            age = (time.time() * 1000 - listed) / 86_400_000 if listed else 1e9
            strong = ((feat["rsi"] is None or feat["rsi"] <= sz.conviction_max_rsi_1h)
                      and age >= sz.conviction_min_listing_age_days
                      and (self._btc_24h is None
                           or self._btc_24h >= sz.conviction_btc_min_change_24h_pct))
            share = sz.conviction_high_pct if strong else sz.conviction_low_pct
        pct = max(0.0, self.engine.equity) * share / 100.0
        return max(pct, en.min_notional_usdt) if pct > 0 else 0.0

    def stop_pct_for(self, symbol: str) -> float:
        """exit.stop_mode: the fixed stop_pct, or a multiple of the coin's ATR."""
        ex = self.cfg.exit
        if ex.stop_mode == "atr":
            a = self.features(symbol)["atr"]
            if a:
                return min(ex.atr_stop_max_pct, max(ex.atr_stop_min_pct, ex.atr_stop_mult * a))
        return ex.stop_pct

    def _read_fill(self, symbol: str) -> tuple[float, float]:
        for attempt in range(self.FILL_WAIT_ATTEMPTS):
            try:
                rows = self.engine.api.positions(symbol)
            except BinanceError as e:
                log.warning("gainer: reading %s fill failed: %s", symbol, e)
                rows = []
            for r in rows or []:
                amt = float(r.get("positionAmt") or 0.0)
                if amt > 0:
                    return amt, float(r.get("entryPrice") or 0.0)
            if attempt + 1 < self.FILL_WAIT_ATTEMPTS:
                time.sleep(self.FILL_WAIT_SECONDS)
        return 0.0, 0.0

    def _protect(self, symbol, entry, qty, entry_id, rules, change_pct,
                 stop_pct: float | None = None) -> bool:
        """Stop and take-profit for a filled market entry, or flatten it."""
        eng, ex = self.engine, self.cfg.exit
        stop_pct = ex.stop_pct if stop_pct is None else stop_pct
        sl = float(rules.round_price(stop_price(entry, stop_pct)))
        tp = float(rules.round_price(ex.target_for(entry, qty))) if ex.has_target else 0.0
        qty_s = rules.round_qty(qty)
        eng._seq += 1
        stop_id = client_order_id("s", eng._seq)
        tp_id = client_order_id("t", eng._seq)
        leg = "stop"
        try:
            eng.api.algo_order(symbol=symbol, side="SELL", type="STOP_MARKET",
                               triggerPrice=rules.round_price(sl), quantity=qty_s,
                               reduceOnly="true", workingType="MARK_PRICE",
                               clientAlgoId=stop_id)
            leg = "take-profit"
            if tp > 0:
                eng.api.algo_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                                   triggerPrice=rules.round_price(tp), quantity=qty_s,
                                   reduceOnly="true", workingType="MARK_PRICE",
                                   clientAlgoId=tp_id)
            else:
                tp_id = ""
        except BinanceError as e:
            log.critical("gainer: %s %s failed (%s) -- flattening", symbol, leg, e)
            try:
                eng._seq += 1
                eng.api.order(symbol=symbol, side="SELL", type="MARKET", quantity=qty_s,
                              reduceOnly="true",
                              newClientOrderId=client_order_id("x", eng._seq))
                eng.api.cancel_all(symbol)
                self.notify(f"{symbol}: could not place the {leg} ({e}). The position "
                            f"was closed at market. Nothing is open.",
                            symbol=symbol, event=Event.ERROR)
            except BinanceError as e2:
                eng.state.halt(f"gainer {symbol}: {leg} failed and the flatten failed: {e2}")
                self.notify(f"{symbol}: {leg} failed AND the market close failed ({e2}). "
                            f"Bot HALTED. {symbol} may be OPEN WITH NO STOP -- close it "
                            f"on Binance now.", symbol=symbol, event=Event.HALT)
            return False

        part_px = 0.0
        if ex.partial_take_pct > 0:
            part_qty = rules.round_qty(qty * ex.partial_fraction)
            part_px = float(rules.round_price(entry * (1 + ex.partial_take_pct / 100.0)))
            try:
                if float(part_qty) > 0:
                    eng._seq += 1
                    eng.api.algo_order(symbol=symbol, side="SELL", type="TAKE_PROFIT_MARKET",
                                       triggerPrice=rules.round_price(part_px), quantity=part_qty,
                                       reduceOnly="true", workingType="MARK_PRICE",
                                       clientAlgoId=client_order_id("p", eng._seq))
            except BinanceError as e:
                # The stop is on; a missing partial exit only means no partial sale.
                log.warning("gainer: %s partial take-profit not placed: %s", symbol, e)
                part_px = 0.0
        eng.book[symbol] = ActivePosition(
            symbol=symbol, side="BUY", entry=entry, stop=sl, take_profit=tp, qty=qty,
            entry_order_id=entry_id, stop_order_id=stop_id, tp_order_id=tp_id,
            tag=entry_id, opened_ms=int(time.time() * 1000),
            initial_stop=sl, initial_target=tp, initial_risk=entry - sl,
            filled=True, strategy=self.strategy, no_target=tp <= 0)
        eng.risk.record_fill()
        if eng.stream is not None:
            eng.stream.add_symbol(symbol)
        return self._opened(symbol, entry, qty, paper=False, change_pct=change_pct,
                            stop=sl, tp=tp, stop_pct=stop_pct, partial_price=part_px)

    def _opened(self, symbol, entry, qty, paper, change_pct, stop=0.0, tp=0.0,
                stop_pct: float | None = None, partial_price: float | None = None) -> bool:
        ex = self.cfg.exit
        stop_pct = ex.stop_pct if stop_pct is None else stop_pct
        stop = stop or stop_price(entry, stop_pct)
        if not tp and ex.has_target:
            tp = ex.target_for(entry, qty)
        if partial_price is None:
            partial_price = entry * (1 + ex.partial_take_pct / 100.0) if ex.partial_take_pct > 0 else 0.0
        self.tracks[symbol] = Track(symbol, entry, qty, stop, tp, time.time(), paper=paper,
                                    peak=entry, init_stop=stop, partial_price=partial_price)
        self.save()
        loss = net_pnl(entry, qty, stop, ex.fee_pct)
        tp_line = ("TP none" if tp <= 0 else
                   f"TP {tp:,.6g} (+{(tp / entry - 1) * 100:.1f}%)" if ex.target_pct > 0 else
                   f"TP {tp:,.6g} (+{(tp / entry - 1) * 100:.1f}%, nets ${ex.target_usd:.2f})")
        ladder = self.exit_plan_text()
        self.notify(f"{'PAPER ' if paper else ''}OPENED {symbol} ({self.entry_label} "
                    f"{change_pct:+.2f}%)\nBUY {qty:g} @ {entry:,.6g}  "
                    f"(${entry * qty:,.2f})\n{tp_line}\n"
                    f"SL {stop:,.6g} (-{stop_pct:.1f}%, about {loss:+.2f} USDT)" + ladder
                    + (f"\nPartial: sell {ex.partial_fraction:.0%} at {partial_price:,.6g} "
                       f"(+{ex.partial_take_pct:g}%)" if partial_price else ""),
                    symbol=symbol, event=Event.TRADE_OPEN)
        return True

    def exit_plan_text(self) -> str:
        ex = self.cfg.exit
        return (f"\nLadder: stop to entry at +{ex.ladder_first_pct:g}%, then one "
                f"{ex.ladder_step_pct:g}% step below each new step" if ex.ladder_enabled else "")

    # ------------------------------------------------------------------ close
    def close(self, symbol: str, price: float, why: str, now: float | None = None) -> None:
        t = self.tracks.get(symbol)
        if t is None:
            return
        pnl = net_pnl(t.entry, t.qty, price, self.cfg.exit.fee_pct)
        if t.paper:
            del self.tracks[symbol]
            self.closed_at[symbol] = time.time() if now is None else now
            self.save()
            self.notify(f"PAPER CLOSED {symbol} at {price:,.6g} for {pnl:+.2f} USDT\n{why}",
                        symbol=symbol)
            self.note_close(pnl, now)
            self.sweep_profit(symbol, pnl)
            self.check_principal()
            return
        if self.engine.close_position(f"{self.close_label} -- {why}", symbol=symbol):
            del self.tracks[symbol]
            self.closed_at[symbol] = time.time() if now is None else now
            self.save()
            self.notify(f"CLOSED {symbol} near {price:,.6g} (about {pnl:+.2f} USDT)\n{why}",
                        symbol=symbol)
        else:
            self.notify(f"{symbol}: close FAILED; its stop and take-profit are still "
                        f"on the exchange\n{why}", symbol=symbol, event=Event.ERROR)

    def sync_tracks(self, prices: dict, highs: dict | None = None,
                    now: float | None = None) -> None:
        """Resolve paper exits, and forget live ones the exchange already closed."""
        highs = highs or {}
        now = time.time() if now is None else now
        ex = self.cfg.exit
        for sym, t in list(self.tracks.items()):
            if not t.paper:
                if sym not in self.engine.book:
                    # The engine's reconcile has booked it and sent the TP/SL alert.
                    log.info("gainer: %s closed exchange-side", sym)
                    del self.tracks[sym]
                    self.closed_at[sym] = now
                    self.save()
                    self._arm(sym, highs)
                continue
            px = prices.get(sym, 0.0)
            if px <= 0:
                continue
            if t.partial_price and not t.partial_done and px >= t.partial_price:
                part = t.qty * ex.partial_fraction
                pnl = net_pnl(t.entry, part, t.partial_price, ex.fee_pct)
                t.qty -= part
                t.partial_done = True
                self.save()
                self.notify(f"PAPER PARTIAL {sym}: sold {part:g} at {t.partial_price:,.6g} "
                            f"for {pnl:+.2f} USDT", symbol=sym)
                self.note_close(pnl, now)
                self.sweep_profit(sym, pnl)
            if t.take_profit > 0 and px >= t.take_profit:
                self.close(sym, t.take_profit, f"take-profit hit (target ${ex.target_usd:.2f})",
                           now=now)
            elif px <= t.stop:
                what = ("ladder stop hit" if t.stop >= t.entry
                        else f"stop-loss hit (-{ex.stop_pct:g}%)")
                self.close(sym, t.stop, what, now=now)
            else:
                continue
            if sym not in self.tracks:
                self._arm(sym, highs)

    def manage_ladder(self, prices: dict, now: float) -> None:
        """Walk each stop up the ladder, and apply exit.unarmed_max_hours."""
        ex = self.cfg.exit
        if not ex.ladder_enabled:
            return
        for sym, t in list(self.tracks.items()):
            px = prices.get(sym, 0.0)
            if px <= 0:
                continue
            if px > t.peak:
                t.peak = px
                self.save()
            if not t.paper and t.partial_price and not t.partial_done and px >= t.partial_price:
                t.partial_done = True
                self.save()
                self.notify(f"{sym} PARTIAL: price reached {t.partial_price:,.6g}; the "
                            f"exchange sells {ex.partial_fraction:.0%} there", symbol=sym)
            level = ladder_stop(t.entry, t.peak, ex.ladder_first_pct, ex.ladder_step_pct,
                                mode=ex.ladder_mode,
                                init_stop=t.init_stop or stop_price(t.entry, ex.stop_pct))
            if level is None or level <= t.stop:
                hours = (now - t.opened_at) / 3600.0
                if t.stop < t.entry and ex.unarmed_max_hours > 0 and hours >= ex.unarmed_max_hours:
                    self.close(sym, px, f"time limit: {hours:.0f}h without the stop "
                                        f"reaching entry", now=now)
                continue
            gain = (t.peak / t.entry - 1) * 100
            if px <= level:
                # Already back through the level it should now be protected
                # at: the ladder's own rule says take it.
                self.close(sym, px, f"ladder: peak +{gain:.1f}%, price back under "
                                    f"the {level:,.6g} step", now=now)
                continue
            if not t.paper:
                pos = self.engine.book.get(sym)
                if pos is None or not self.engine.replace_protective(pos, "stop", level):
                    continue            # the old stop stays; retried next poll
                pos.stop = level
            t.stop = level
            self.save()
            locked = (level / t.entry - 1) * 100
            self.notify(f"{sym} LADDER: peak +{gain:.1f}%, stop moved to {level:,.6g} "
                        f"({'entry' if locked < 0.01 else f'+{locked:.0f}%'})", symbol=sym)

    # ------------------------------------------------------------------ sweep
    def sweep_profit(self, symbol: str, pnl: float) -> None:
        """
        Bank sweep.pct of a winning trade in the Funding wallet. Called for
        every booked gainer close; losses move nothing. Never raises: the
        close has already happened and must not be disturbed.
        """
        sw = self.cfg.sweep
        if not sw.enabled or pnl <= 0:
            return
        if sw.start_when_equity_usdt > 0 and float(getattr(self.engine, "equity", 0) or 0) \
                < sw.start_when_equity_usdt:
            return                              # keep compounding until the threshold
        self.sweep_pending += pnl * sw.pct / 100.0
        amount = int(self.sweep_pending * 100) / 100.0      # whole cents, rounded down
        if amount < sw.min_transfer_usdt:
            self.save()
            log.info("gainer sweep: %s +%.2f, %.2f USDT pending (moves at %.2f)",
                     symbol, pnl, self.sweep_pending, sw.min_transfer_usdt)
            return
        if not self._transfer(amount, "profit"):
            self.save()
            return
        self.sweep_pending = round(self.sweep_pending - amount, 8)
        self.swept_total += amount
        self.save()
        self.notify(f"SWEEP: {amount:.2f} USDT {self._where()} -- {sw.pct:g}% of "
                    f"{symbol}'s +{pnl:.2f}. Total banked: {self.swept_total:.2f} USDT",
                    symbol=symbol)

    @property
    def simulated(self) -> bool:
        return self.paper or bool(getattr(self.engine.api, "testnet", False))

    def _where(self) -> str:
        return ("would have moved (paper/testnet: nothing sent)" if self.simulated
                else "moved to the Funding wallet")

    def _transfer(self, amount: float, what: str) -> bool:
        """Futures -> Funding. True when it moved (or, simulated, would have)."""
        if self.simulated:
            return True
        try:
            self.engine.api.universal_transfer("USDT", f"{amount:.2f}")
        except Exception as e:               # noqa: BLE001 -- never disturb a close
            msg = str(e)
            log.error("gainer %s transfer of %.2f USDT failed: %s", what, amount, msg)
            if msg != self._sweep_error:
                self._sweep_error = msg
                self.notify(f"SWEEP FAILED: could not move {amount:.2f} USDT ({what}) to "
                            f"the Funding wallet ({msg}). It is set aside -- the bot will "
                            f"not trade with it -- and retried after the next close. "
                            f"Check that the API key has \"Permits Universal Transfer\".",
                            event=Event.ERROR)
            return False
        self._sweep_error = ""
        return True

    @property
    def reserved_usdt(self) -> float:
        """Money waiting to move to Funding. The engine does not trade it."""
        if not self.cfg.sweep.enabled:
            return 0.0
        return max(0.0, self.sweep_pending) + max(0.0, self.principal_pending)

    def deposits(self) -> float | None:
        """USDT transferred INTO the futures wallet since sweep.deposits_since."""
        sw = self.cfg.sweep
        if sw.deposits_override_usdt > 0:
            return sw.deposits_override_usdt
        if not sw.deposits_since:
            return None
        import datetime as _dt
        try:
            since = _dt.datetime.strptime(sw.deposits_since, "%Y-%m-%d").replace(
                tzinfo=_dt.timezone.utc)
        except ValueError:
            return None
        start = int(since.timestamp() * 1000)
        total, seen = 0.0, set()
        for _ in range(20):                  # 1,000 rows a page is plenty per month
            rows = self.engine.api.income("TRANSFER", start_ms=start, limit=1000) or []
            for r in rows:
                key = r.get("tranId") or (r.get("time"), r.get("income"))
                if key in seen or r.get("asset", "USDT") != "USDT":
                    continue
                seen.add(key)
                amt = float(r.get("income") or 0.0)
                if amt > 0:                  # our own sweeps out are negative
                    total += amt
            if len(rows) < 1000:
                break
            start = int(rows[-1].get("time", start)) + 1
        return total

    def check_principal(self) -> None:
        """
        sweep.principal_*: once the trading balance is principal_trigger_x
        times all deposits so far, move the deposits not yet moved out to
        Funding. A failed move is ring-fenced and retried. Never raises.
        """
        sw = self.cfg.sweep
        if not (sw.enabled and sw.principal_enabled):
            return
        try:
            dep = self.deposits()
        except Exception as e:               # noqa: BLE001
            log.warning("gainer principal check: deposits unreadable (%s)", e)
            return
        if dep is None:
            if self._principal_note != "no-date":
                self._principal_note = "no-date"
                self.notify("Principal recovery is on but gainer.sweep.deposits_since is "
                            "not set, so deposits cannot be counted. It stays idle.")
            return
        owed = int((dep - self.principal_withdrawn) * 100) / 100.0
        if self.principal_pending > 0:
            amount = self.principal_pending               # retry a failed move
        elif owed > 0 and self.engine.equity + self.reserved_usdt >= sw.principal_trigger_x * dep:
            amount = owed
        else:
            return
        if not self._transfer(amount, "principal"):
            self.principal_pending = amount
            self.save()
            return
        self.principal_pending = 0.0
        self.principal_withdrawn += amount
        self.save()
        self.notify(f"PRINCIPAL SAFE: {amount:.2f} USDT {self._where()}. The trading "
                    f"balance reached {sw.principal_trigger_x:g}x the {dep:.2f} USDT "
                    f"deposited; {self.principal_withdrawn:.2f} USDT of deposits is now "
                    f"in Funding and only profit is trading.")

    def check_milestones(self, prices: dict) -> None:
        for sym, t in self.tracks.items():
            px = prices.get(sym, 0.0)
            if px <= 0:
                continue
            pnl = net_pnl(t.entry, t.qty, px, self.cfg.exit.fee_pct)
            for m in sorted(float(x) for x in self.cfg.alerts.milestones_usd):
                if pnl >= m and m not in t.milestones_hit:
                    t.milestones_hit.append(m)
                    self.save()
                    where = (f"TP {t.take_profit:,.6g}" if t.take_profit > 0
                             else f"stop {t.stop:,.6g}")
                    self.notify(f"{sym} hit the ${m:g} milestone: {pnl:+.2f} USDT\n"
                                f"now {px:,.6g}, {where}", symbol=sym)

    # ----------------------------------------------------------------- alerts
    def send_status(self, now: float, prices: dict) -> None:
        lines = ["GAINER MINING hourly status" + (" (paper)" if self.paper else "")]
        if self.tracks:
            for sym, t in self.tracks.items():
                px = prices.get(sym, 0.0)
                rank = next((r.rank for r in self.board.rows if r.symbol == sym), 0)
                if px > 0:
                    pnl = net_pnl(t.entry, t.qty, px, self.cfg.exit.fee_pct)
                    span = t.take_profit - t.entry
                    prog = (px - t.entry) / span * 100 if span > 0 else 0.0
                    lines.append(f"{sym}: {pnl:+.2f} USDT, {prog:.0f}% to TP, "
                                 f"rank {rank or '>'+str(len(self.board.rows))}, "
                                 f"held {(now - t.opened_at) / 3600:.1f}h")
                else:
                    lines.append(f"{sym}: no price")
        else:
            lines.append("no open gainer positions")
        if self.waiting:
            lines.append(f"watching {self.waiting} (#1, held back by an entry guard)")
        if self.cfg.sweep.enabled:
            lines.append(f"banked in Funding: {self.swept_total:.2f} USDT profit, "
                         f"{self.principal_withdrawn:.2f} USDT principal"
                         + (f"; set aside, not traded: {self.reserved_usdt:.2f} USDT"
                            if self.reserved_usdt else ""))
        lines.append("")
        lines += self.board.board_lines(now, set(self.tracks))
        fc = self.board.forecast(now)
        if fc:
            lines += ["", fc.text()]
        self.notify("\n".join(lines))

    def notify(self, body: str, symbol: str | None = None, event: Event = Event.GAINER) -> None:
        self.engine.notify.send(event, body, symbol=symbol or "gainer")
