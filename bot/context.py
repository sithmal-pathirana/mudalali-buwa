"""
The bigger picture: which way a slower chart is trending.

The strategy trades 15m breakouts and the supervisor reads 15m bars, so
neither could tell a breakout riding a 4h uptrend from one fighting a 4h
downtrend. Replayed over the 53 strategy trades of 2026-09-07 to 09-19:

  4h trend AGAINST the trade    4 trades   -4.01R held to stop/target
  4h trend WITH the trade      31 trades   +9.71R held, +0.58R supervised
  4h trend flat                18 trades   +0.17R held, +4.14R supervised

Two uses follow, each behind its own switch and OFF by default:
  context.entry.block_against_trend   do not open a trade against the trend
  supervise.patience                  relax the supervisor's early exits
                                      while the trend is behind the trade

Also here, because it is the other entry filter: context.entry.
coin_cooldown_minutes, which keeps the bot off a coin it has just closed.

The trend is read from CLOSED bars only. A slow bar lasts longer than most
trades (median 48 minutes), so this is context fixed at entry, not a signal
that changes during the trade.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContextTrendConfig:
    #: The slower chart the trend is read from.
    interval: str = "4h"
    #: Uptrend: the last close above its sma_bars average AND that average
    #: higher than it was slope_bars ago. Downtrend: both the other way.
    #: Anything else is flat. 20 and 5 are the values the replay used.
    sma_bars: int = 20
    slope_bars: int = 5


@dataclass
class ContextEntryConfig:
    #: Refuse a new trade whose side is against the slower chart's trend. A
    #: flat or unreadable trend never blocks anything.
    block_against_trend: bool = False
    #: Do not open a coin again until this many minutes after its last
    #: position closed; 0 = off. Replayed over 85 signals of 2026-09-05 to
    #: 09-19, re-buying a coin within 2h of closing it won 2 of 10 trades
    #: for -6.8R, mostly straight after a win: chasing a move already taken. 2h after any exit took the replay from +10.8R to
    #: +17.7R (held to stop/target: +6.6R to +11.6R); 1h to 8h all helped.
    coin_cooldown_minutes: float = 0.0


GROUPS = {"trend": ContextTrendConfig, "entry": ContextEntryConfig}


@dataclass
class ContextConfig:
    trend: ContextTrendConfig = field(default_factory=ContextTrendConfig)
    entry: ContextEntryConfig = field(default_factory=ContextEntryConfig)

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
                    raise TypeError(f"bad key under context.{name} ({e}). "
                                    f"Valid keys: {valid}") from None
            elif not isinstance(value, cls):
                raise TypeError(f"context.{name} must be a block of settings")


def trend_direction(closes: list[float], sma_bars: int = 20,
                    slope_bars: int = 5) -> int:
    """+1 up, -1 down, 0 flat or not enough bars. `closes` are CLOSED bars,
    oldest first."""
    if sma_bars < 1 or slope_bars < 1 or len(closes) < sma_bars + slope_bars:
        return 0
    sma = sum(closes[-sma_bars:]) / sma_bars
    before = sum(closes[-sma_bars - slope_bars:-slope_bars]) / sma_bars
    last = closes[-1]
    if last > sma and sma > before:
        return 1
    if last < sma and sma < before:
        return -1
    return 0


def closed_closes(klines: list, now_ms: int) -> list[float]:
    """Close prices of the bars that had closed by now_ms (kline[6] is the
    bar's close time), so a forming bar never counts."""
    return [float(k[4]) for k in klines if int(k[6]) < now_ms]
