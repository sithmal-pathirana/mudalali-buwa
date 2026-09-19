"""
The protection watchdog: no open position is ever left without a stop-loss
and a take-profit on the exchange.

On 2026-09-19 an STGUSDT limit buy was priced off a bar close of 0.1566 while
the market had already fallen to 0.1523. The limit sat above the market, so it
filled at once, and the stop at 0.1513 -- 0.66% under the fill -- was refused
with -2021 because the mark price had already dipped through it. The halt path
then ran cancel_all(), which cancels orders and not positions, reported
"Nothing is open", and left 71 STG long on the exchange with no stop for four
hours while the bot logged "NOT tracking" once a minute.

This module decides what the missing orders should be. It does no I/O: the
engine reads the exchange, calls plan_protection(), and places the result.

The order of preference is deliberate:

  1. The levels the trade was opened with, if the market still allows them.
     They are the strategy's own plan, sized against its own risk.
  2. If the market has already gone through the planned STOP, the trade is
     over by its own rules -- close it, do not widen the stop to keep it.
  3. Otherwise (no plan survives, or the position was never tracked) read the
     symbol again: a stop stop.atr_mult ATRs from the current mark, and a
     target target.atr_mult ATRs away -- nearer when the market is choppy,
     because a trend-sized target is not reachable in a range.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProtectWatchConfig:
    #: Check every open position for a missing stop or take-profit on each
    #: reconcile (about once a minute), halted or not, and put back what is
    #: missing. Off, the bot only warns, as it did before.
    enabled: bool = True
    #: A position the bot is not tracking at all (a fill it lost, a manual
    #: trade) is taken over -- tracked, protected and supervised -- instead of
    #: only being reported.
    adopt_untracked: bool = True


@dataclass
class ProtectStopConfig:
    #: A stop rebuilt from the market sits this many ATRs from the current
    #: mark. 2.0 matches params.trend.atr_stop_mult, the distance the
    #: strategy's own stops are placed at.
    atr_mult: float = 2.0
    #: Used instead when the symbol's bars cannot be read, as a percent of the
    #: mark price.
    fallback_pct: float = 3.0
    #: Never place a stop closer than this to the mark, in percent. Closer
    #: than this and one tick of noise triggers it, or Binance refuses it with
    #: -2021 before it is even placed -- the STGUSDT failure.
    min_gap_pct: float = 0.5


@dataclass
class ProtectTargetConfig:
    #: A take-profit rebuilt from the market sits this many ATRs from the
    #: current mark. 3.0 matches params.trend.atr_target_mult.
    atr_mult: float = 3.0
    #: In a choppy market (efficiency ratio below choppy_below_efficiency) the
    #: target is pulled in to this many ATRs: a trend-sized move is not coming
    #: in a range, and an unreachable target is no target.
    choppy_atr_mult: float = 1.5
    #: Matches params.regime.range_below, the router's own "ranging" line.
    choppy_below_efficiency: float = 0.20
    #: Never place a take-profit closer than this to the mark, in percent.
    min_gap_pct: float = 0.3


@dataclass
class ProtectFailureConfig:
    #: After this many consecutive failed attempts to place a STOP, close the
    #: position at market. A position the bot cannot protect is not one it
    #: should keep holding. 0 = never close, only alert.
    close_after_attempts: int = 3


@dataclass
class ProtectEntryConfig:
    #: Before an entry is sent, the price may have moved since the signal's
    #: bar closed. Skip the trade when less than this fraction of the planned
    #: entry-to-stop distance is left between the price it will fill at and
    #: the stop. STGUSDT had 0.19 of it left (0.1523 - 0.1513 of a planned
    #: 0.1566 - 0.1513); 0.5 would have skipped it. 0 = off.
    min_stop_room_frac: float = 0.5


GROUPS = {"watch": ProtectWatchConfig, "stop": ProtectStopConfig,
          "target": ProtectTargetConfig, "failure": ProtectFailureConfig,
          "entry": ProtectEntryConfig}


@dataclass
class ProtectConfig:
    watch: ProtectWatchConfig = field(default_factory=ProtectWatchConfig)
    stop: ProtectStopConfig = field(default_factory=ProtectStopConfig)
    target: ProtectTargetConfig = field(default_factory=ProtectTargetConfig)
    failure: ProtectFailureConfig = field(default_factory=ProtectFailureConfig)
    entry: ProtectEntryConfig = field(default_factory=ProtectEntryConfig)

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
                    raise TypeError(f"bad key under supervise.protect.{name} "
                                    f"({e}). Valid keys: {valid}") from None
            elif not isinstance(value, cls):
                raise TypeError(f"supervise.protect.{name} must be a block of settings")


@dataclass
class ProtectionPlan:
    #: Level for a missing stop, or None when the stop is already in place.
    stop: float | None = None
    #: Level for a missing take-profit, or None when it is already in place.
    target: float | None = None
    #: Close at market instead: the planned stop has already been passed.
    close_now: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def why(self) -> str:
        return "; ".join(self.notes)


def _clear_of(level: float, mark: float, long: bool, below: bool,
              min_gap_pct: float) -> bool:
    """Whether `level` sits on the right side of `mark` by at least the gap.

    `below` is in LONG terms: a long's stop sits below the mark, its target
    above. A short flips both.
    """
    if level <= 0 or mark <= 0:
        return False
    gap = mark * min_gap_pct / 100.0
    want_below = below if long else not below
    return level <= mark - gap if want_below else level >= mark + gap


def plan_protection(*, long: bool, mark: float, atr: float, efficiency: float,
                    has_stop: bool, has_target: bool,
                    planned_stop: float = 0.0, planned_target: float = 0.0,
                    cfg: ProtectConfig | None = None) -> ProtectionPlan:
    """
    The stop and take-profit to put back on one open position.

    `atr` is in price units (0 when unknown). `efficiency` is the efficiency
    ratio of recent bars, -1 when unknown. `planned_*` are the levels the trade
    was opened with, 0 when there are none (an adopted position).
    """
    cfg = cfg or ProtectConfig()
    plan = ProtectionPlan()
    if mark <= 0:
        plan.notes.append("no mark price; nothing placed")
        return plan
    sign = 1.0 if long else -1.0

    if not has_stop:
        if planned_stop > 0 and not _clear_of(planned_stop, mark, long, True, 0.0):
            # Already through the planned stop: the trade's own rule says it
            # is over. Widening the stop to stay in would be inventing a new,
            # larger risk the trade was never sized for.
            plan.close_now = True
            plan.notes.append(f"mark {mark:.8g} is already through the planned "
                              f"stop {planned_stop:.8g}; closing")
            return plan
        if _clear_of(planned_stop, mark, long, True, cfg.stop.min_gap_pct):
            plan.stop = planned_stop
            plan.notes.append(f"stop restored at the planned {planned_stop:.8g}")
        else:
            if atr > 0:
                dist = cfg.stop.atr_mult * atr
                basis = f"{cfg.stop.atr_mult:g} ATR ({atr:.8g})"
            else:
                dist = mark * cfg.stop.fallback_pct / 100.0
                basis = f"{cfg.stop.fallback_pct:g}% (no ATR)"
            dist = max(dist, mark * cfg.stop.min_gap_pct / 100.0)
            plan.stop = mark - sign * dist
            why = ("the planned stop is too close to the market"
                   if planned_stop > 0 else "no planned stop")
            plan.notes.append(f"stop rebuilt at {plan.stop:.8g}, {basis} from "
                              f"mark {mark:.8g} ({why})")

    if not has_target:
        if _clear_of(planned_target, mark, long, False, cfg.target.min_gap_pct):
            plan.target = planned_target
            plan.notes.append(f"take-profit restored at the planned {planned_target:.8g}")
        else:
            choppy = 0.0 <= efficiency < cfg.target.choppy_below_efficiency
            mult = cfg.target.choppy_atr_mult if choppy else cfg.target.atr_mult
            if atr > 0:
                dist = mult * atr
                basis = f"{mult:g} ATR"
            else:
                # No bars: mirror the fallback stop, scaled by the same ratio
                # the ATR multiples keep between stop and target.
                ratio = mult / cfg.stop.atr_mult if cfg.stop.atr_mult else 1.0
                dist = mark * cfg.stop.fallback_pct / 100.0 * ratio
                basis = f"{cfg.stop.fallback_pct * ratio:g}% (no ATR)"
            dist = max(dist, mark * cfg.target.min_gap_pct / 100.0)
            plan.target = mark + sign * dist
            regime = (f"choppy, ER {efficiency:.2f}" if choppy else
                      f"ER {efficiency:.2f}" if efficiency >= 0 else "ER unknown")
            plan.notes.append(f"take-profit rebuilt at {plan.target:.8g}, {basis} "
                              f"from mark ({regime})")
    return plan


def stop_room(*, long: bool, entry: float, stop: float, mark: float) -> float:
    """
    The fraction of the planned entry-to-stop distance still left once the
    entry fills. A long limit at `entry` fills at the lower of entry and mark,
    a short at the higher, so that is where the stop is measured from.
    1.0 when the market has not moved against the plan; 0 or less when the
    stop has already been passed.
    """
    planned = (entry - stop) if long else (stop - entry)
    if planned <= 0 or mark <= 0:
        return 1.0
    fill = min(entry, mark) if long else max(entry, mark)
    left = (fill - stop) if long else (stop - fill)
    return left / planned
