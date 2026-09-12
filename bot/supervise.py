"""
Deciding what to do with a position that is already open.

Until this module existed the bot could not change its mind. The stop and the
take-profit were written to the exchange in the same second as the entry and
were never revisited, so every trade was a coin flip between two numbers chosen
before the market had shown anything. Measured over the first 15 self-managed
closes that produced a 27% win rate at a 1.73 payoff -- an expectancy of
-$0.10 a trade -- while the three positions closed by hand, at prices the bot
had no instrument to reach, all made money.

Four rules, each gated on the one before it where that matters:

  1. BREAK EVEN      once the trade has been +1R in hand, it may not lose.
  2. RUNNER          approaching the target with the trend intact, bank half
                     at the target and trail the rest with no ceiling.
  3. HORIZON         if the target cannot arrive inside the horizon, stop
                     reaching for it: harvest a profit, or leave.
  4. FAILED BREAKOUT a breakout that gives back its own trigger level is over.

Everything is measured in R -- the money between entry and the ORIGINAL stop --
never in percent. Stop distances on this account have ranged from 1.0% to 12.1%
across symbols, so a rule written in percent is hair-trigger on one symbol and
inert on the next. In R it behaves the same on both.

This module is pure: it reads a position and a market reading and returns a
Plan. It never calls the exchange. The engine executes the plan, and is the
only thing that can spend money.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Reading:
    """What the supervisor can see about the market on one tick."""

    price: float
    #: Mean true range in PRICE units, not percent. 0 means "no bar data this
    #: tick", which disables every rule that needs to size a distance.
    atr: float = 0.0
    #: Efficiency ratio of the held symbol right now. Negative means unknown,
    #: which is treated as "cannot confirm the trend", never as "trending".
    efficiency: float = -1.0
    #: Net price change per bar over the recent window, SIGNED in market terms
    #: (positive = price rising). The supervisor applies the position's own
    #: direction to it. This is separate from `efficiency` on purpose: the
    #: efficiency ratio measures how DIRECTIONAL a move is, never which way, so
    #: a clean run against the position scores just as high as one in its
    #: favour. Forecasting from efficiency alone reported a target as
    #: "reachable soon" exactly when price was running away from it.
    net_move_per_bar: float = 0.0
    #: Seconds since the entry order was placed.
    age_seconds: float = 0.0
    #: Length of one bar, for converting the horizon into a bar count.
    bar_seconds: float = 900.0


@dataclass
class Plan:
    """What to do about it. Every field is optional; an empty Plan is falsey."""

    stop: float | None = None
    target: float | None = None
    #: Reduce the take-profit to this quantity, leaving the rest to run.
    target_qty: float | None = None
    exit_now: bool = False
    notes: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.stop is not None or self.target is not None
                    or self.target_qty is not None or self.exit_now)

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def why(self) -> str:
        return "; ".join(self.notes)


@dataclass
class SuperviseConfig:
    #: OFF by default. An exit rule that has not been measured has no business
    #: touching money -- the same standard risk.trailing_atr_mult is held to.
    enabled: bool = False

    #: Round-trip cost as a percentage of notional, used as the margin above
    #: entry that "break even" actually means. Fees alone are ~0.07% (0.02%
    #: maker in, 0.05% taker out); the rest is slippage, which on a thin
    #: microcap is the larger half. Never set this to the fee figure alone.
    cost_buffer_pct: float = 0.15

    # -- rule 1
    breakeven_at_r: float = 1.0

    # -- rule 2
    #: Fraction of the way to the ORIGINAL target at which the runner arms.
    #: Must be < 1.0: at 1.0 the take-profit has already filled and there is
    #: nothing left to decide.
    runner_at_target_frac: float = 0.8
    #: Trail distance for the runner, in ATR.
    trail_atr_mult: float = 1.5

    # -- rule 3
    horizon_hours: float = 5.0
    #: A trade is left alone for this long before the horizon rule may fire.
    #: Without it a position that ticks adverse in its first seconds is killed
    #: before the setup has had a single bar to work.
    grace_minutes: float = 20.0
    #: Trail distance once harvesting, in ATR. Tighter than the runner's: the
    #: point is no longer to let it run, it is to leave on the next swing.
    harvest_trail_atr_mult: float = 0.5
    #: Efficiency ratio below which the trend is no longer considered intact.
    #: Keep at or above params.regime.trend_above, or the supervisor will call
    #: a trend healthy that the router would not have entered on.
    min_trend_efficiency: float = 0.25

    #: Bars the drift estimate is measured over. Short enough to notice a turn
    #: -- 8 bars is 2 hours on the 15m interval -- without reading noise as a
    #: reversal on every tick.
    drift_window_bars: int = 8

    # -- rule 4
    failed_breakout_exit: bool = True


def _sign(pos) -> int:
    return 1 if pos.is_long else -1


def favour(pos, price: float) -> float:
    """How far price has moved in this position's favour, in price units."""
    return (price - pos.entry) * _sign(pos)


def risk_per_unit(pos) -> float:
    """1R in price units, measured against the ORIGINAL stop.

    Against the original, never the current one: once rule 1 has moved the
    stop to break even the remaining risk is ~0, and every R reading after
    that would divide by it and explode.
    """
    base = pos.initial_stop or pos.stop
    return abs(pos.entry - base)


def r_multiple(pos, price: float) -> float:
    r = risk_per_unit(pos)
    return favour(pos, price) / r if r > 0 else 0.0


def _tighter(pos, current: float, proposed: float) -> float | None:
    """The proposed stop, but only if it is an improvement.

    A stop may move toward price and never away from it. Returns None when the
    proposal is not an improvement, so the caller can tell "no change" from
    "change to the same number".
    """
    if current <= 0:
        return proposed
    better = proposed > current if pos.is_long else proposed < current
    return proposed if better else None


def _pull_in(pos, current: float, proposed: float) -> float | None:
    """A take-profit moved CLOSER to price, or None if that is not an
    improvement.

    Opposite polarity to _tighter. A stop improves by moving toward price in
    the direction that reduces risk; a target improves by moving toward price
    in the direction that makes it reachable -- for a long that is DOWN, which
    is the direction _tighter rejects. Sharing one helper between the two was
    a bug: it silently refused every pull-in the horizon rule asked for.
    """
    if current <= 0:
        return proposed
    closer = proposed < current if pos.is_long else proposed > current
    return proposed if closer else None


def supervise(pos, reading: Reading, cfg: SuperviseConfig,
              scale_out_qty: float = 0.0) -> Plan:
    """
    Decide what should happen to `pos` given `reading`.

    `scale_out_qty` is the quantity the take-profit would be reduced to if the
    account can afford to split the position -- 0 when it cannot, which is what
    keeps rule 2 dormant on an account too small to place two legal orders.
    """
    plan = Plan()
    if not cfg.enabled:
        return plan

    price = reading.price
    r_unit = risk_per_unit(pos)
    if price <= 0 or r_unit <= 0:
        return plan                     # nothing to reason from

    sign = _sign(pos)
    cost = pos.entry * cfg.cost_buffer_pct / 100.0
    breakeven = pos.entry + sign * cost
    peak = pos.high_water or pos.entry
    r_now = favour(pos, price) / r_unit
    r_peak = favour(pos, peak) / r_unit
    in_profit = favour(pos, price) > cost

    # ---------------------------------------------------------------- rule 1
    if r_peak >= cfg.breakeven_at_r:
        moved = _tighter(pos, pos.stop, breakeven)
        if moved is not None:
            plan.stop = moved
            plan.note(f"break even: peak reached {r_peak:.2f}R")

    # ---------------------------------------------------------------- rule 2
    # Arms once, before the take-profit can fill, and only when the account can
    # actually place both legs. Below that size this whole rule is dormant --
    # which is deliberate: without a scale-out it would cancel a take-profit
    # that is about to fill and gamble a booked win on a trail.
    target_r = abs(pos.initial_target - pos.entry) / r_unit if pos.initial_target else 0.0
    if (not pos.runner and scale_out_qty > 0 and target_r > 0
            and r_peak >= cfg.runner_at_target_frac * target_r
            and reading.atr > 0
            and reading.efficiency >= cfg.min_trend_efficiency):
        plan.target_qty = scale_out_qty
        plan.note(f"runner armed at {r_peak:.2f}R of a {target_r:.2f}R target "
                  f"(ER {reading.efficiency:.2f}): banking {scale_out_qty:g}, "
                  f"trailing the rest")

    # A runner is managed by its trail, not by a ceiling. Extending a
    # take-profit number would just move the ceiling; removing it for the half
    # that is still open is what "let the winner run" actually means.
    if (pos.runner or plan.target_qty) and reading.atr > 0:
        trail = peak - sign * cfg.trail_atr_mult * reading.atr
        floor = breakeven if r_peak >= cfg.breakeven_at_r else None
        if floor is not None:
            trail = max(trail, floor) if pos.is_long else min(trail, floor)
        moved = _tighter(pos, plan.stop if plan.stop is not None else pos.stop, trail)
        if moved is not None:
            plan.stop = moved
            plan.note(f"runner trail {cfg.trail_atr_mult:g}xATR from {peak:.6g}")

    # ---------------------------------------------------------------- rule 3
    horizon_bars = (cfg.horizon_hours * 3600.0 / reading.bar_seconds
                    if reading.bar_seconds > 0 else 0.0)
    past_grace = reading.age_seconds >= cfg.grace_minutes * 60.0
    # A negative efficiency reading means "not enough bars to know", which is
    # NOT the same as "no directional progress". Treating the two alike would
    # make a cold start -- the one moment the bot knows least -- close the
    # position for being unreachable.
    if (past_grace and horizon_bars > 0 and reading.atr > 0
            and pos.take_profit and reading.efficiency >= 0):
        remaining = (pos.take_profit - price) * sign
        # Progress per bar in THIS position's favour. Negative means the market
        # is moving away, however cleanly it is trending while it does so.
        drift = reading.net_move_per_bar * sign
        if remaining <= 0:
            bars_needed = 0.0           # already there; the order will fill
        elif drift > 0:
            bars_needed = remaining / drift
        else:
            bars_needed = float("inf")  # moving away: the target never arrives

        if bars_needed > horizon_bars:
            if not in_profit:
                plan.exit_now = True
                plan.note(f"target needs {bars_needed:.0f} bars against a "
                          f"{horizon_bars:.0f}-bar horizon and the trade is not "
                          f"in profit; releasing the slot")
            elif drift <= 0:
                # In profit and the market has turned. This is the case worth
                # the most: take what the trade can actually give rather than
                # holding out for a number it is now walking away from.
                plan.exit_now = True
                plan.note(f"in profit at {r_now:.2f}R and drifting away from "
                          f"the target; banking it")
            else:
                reachable = price + sign * horizon_bars * drift
                pull = _pull_in(pos, pos.take_profit, reachable)
                if pull is not None and favour(pos, pull) > cost:
                    plan.target = pull
                    plan.note(f"target needs {bars_needed:.0f} bars; pulled in "
                              f"to {pull:.6g}")
                tight = peak - sign * cfg.harvest_trail_atr_mult * reading.atr
                moved = _tighter(pos, plan.stop if plan.stop is not None else pos.stop, tight)
                if moved is not None:
                    plan.stop = moved
                    plan.note(f"harvest trail {cfg.harvest_trail_atr_mult:g}xATR")

    # ---------------------------------------------------------------- rule 4
    # Only ever banks a profit. A breakout that has failed AND is underwater is
    # left to the stop: exiting at market there converts a maybe into a certain
    # loss at a worse price than the stop that is already resting.
    if (cfg.failed_breakout_exit and pos.ref_level and in_profit
            and not plan.exit_now):
        given_back = (price - pos.ref_level) * sign <= 0
        if given_back:
            plan.exit_now = True
            plan.note(f"breakout failed: price back through {pos.ref_level:.6g} "
                      f"with {r_now:.2f}R in hand")

    # ------------------------------------------------------------ safety net
    # Binance rejects a trigger the mark price has already passed (-2021), and
    # a stop proposed on the wrong side of price is not a tighter stop, it is
    # an impossible one. If a rule computed that, the honest reading is that
    # the position should already be closed.
    if plan.stop is not None:
        wrong_side = plan.stop >= price if pos.is_long else plan.stop <= price
        if wrong_side:
            plan.stop = None
            if in_profit and not plan.exit_now:
                plan.exit_now = True
                plan.note("stop would sit through the market; closing instead")

    if plan.exit_now:
        # Nothing else matters once the position is leaving.
        plan.stop = None
        plan.target = None
        plan.target_qty = None

    return plan
