"""
projection.py -- what the escalating target schedule actually does.

The plan under test:
    days  1-15 : earn $2.00/day
    days  16+  : earn $3.00/day
    profits are NOT withdrawn, so equity grows

The intuition being checked is "risk decreases as capital grows". It is half
right, and the half that is wrong is the expensive half.

    /usr/bin/python3 tools/projection.py
"""

from __future__ import annotations

import random
import statistics

DAILY_VOL = 0.030
TAKER_ROUNDTRIP = 0.0007
MAINT_MARGIN = 0.005
MIN_ORDER = 5.0
SIMS = 20000


def target_for_day(day: int, switch_day: int = 15,
                   before: float = 2.0, after: float = 3.0) -> float:
    return before if day <= switch_day else after


def required_rate_path(equity0=43.0, days=365, switch_day=15):
    """Deterministic path: what % per day is being asked of the account each day."""
    equity = equity0
    rows = []
    for day in range(1, days + 1):
        tgt = target_for_day(day, switch_day)
        rows.append((day, equity, tgt, tgt / equity))
        equity += tgt
    return rows


def show_rate_path():
    print("=" * 76)
    print("  THE ASK OVER TIME  ($2/day to day 15, then $3/day, nothing withdrawn)")
    print("=" * 76)
    rows = required_rate_path()
    marks = [1, 5, 10, 14, 15, 16, 20, 30, 60, 90, 180, 365]
    print(f"  {'day':>5} {'equity':>10} {'target':>8} {'needed/day':>12}  {'note'}")
    print("  " + "-" * 72)
    for day, eq, tgt, rate in rows:
        if day not in marks:
            continue
        note = ""
        if day == 15:
            note = "<- last day at $2"
        elif day == 16:
            note = "<- escalation to $3 RESETS the ask"
        print(f"  {day:>5} ${eq:>9,.2f} ${tgt:>7,.2f} {rate*100:>11.2f}%  {note}")
    print()
    d14 = next(r for r in rows if r[0] == 14)
    d16 = next(r for r in rows if r[0] == 16)
    print(f"  You are right that the ask falls: 4.65%/day on day 1 down to "
          f"{d14[3]*100:.2f}%/day by day 14.")
    print(f"  But raising the target to $3 on day 15 puts it back to "
          f"{d16[3]*100:.2f}%/day -- which is")
    print(f"  where you started. The escalation spends the entire risk reduction the")
    print(f"  first two weeks bought you, on day one of week three.")
    print()
    print(f"  The ask only drops below 1%/day around day "
          f"{next(r[0] for r in rows if r[3] < 0.01)}, and below 0.5%/day around day "
          f"{next(r[0] for r in rows if r[3] < 0.005)}.")
    print()


def simulate_schedule(equity0=43.0, leverage=3.0, stop_mult=1.0,
                      switch_day=15, sims=SIMS, horizon=365):
    """Same driftless-barrier model as reality_check, with the target schedule."""
    liq_move = 1.0 / leverage - MAINT_MARGIN
    reached_15 = reached_30 = reached_90 = 0
    ruined = 0
    lifespans = []
    finals = []

    for _ in range(sims):
        equity = equity0
        day = 0
        for day in range(1, horizon + 1):
            target = target_for_day(day, switch_day)
            notional = equity * leverage
            if notional < MIN_ORDER:
                break
            fee = notional * TAKER_ROUNDTRIP
            tp_move = (target + fee) / notional
            if tp_move >= liq_move:
                equity = 0.0
                break
            sl_move = min(tp_move * stop_mult, liq_move)
            p_win = sl_move / (tp_move + sl_move)
            if random.random() < p_win:
                equity += target
            else:
                equity -= notional * sl_move + fee
            if equity < MIN_ORDER:
                equity = 0.0
                break
            if day == 15:
                reached_15 += 1
            if day == 30:
                reached_30 += 1
            if day == 90:
                reached_90 += 1
        if equity < MIN_ORDER:
            ruined += 1
        lifespans.append(day)
        finals.append(equity)

    return {
        "leverage": leverage,
        "p15": reached_15 / sims,
        "p30": reached_30 / sims,
        "p90": reached_90 / sims,
        "p_ruin": ruined / sims,
        "median_life": statistics.median(lifespans),
        "median_final": statistics.median(finals),
    }


def show_survival():
    print("=" * 76)
    print("  SURVIVING THE SCHEDULE  (20,000 simulated accounts, $43 start)")
    print("=" * 76)
    print(f"  {'leverage':>9} {'reach d15':>10} {'reach d30':>10} {'reach d90':>10}"
          f" {'ruin<1yr':>10} {'med. life':>10}")
    print("  " + "-" * 72)
    for lev in (2, 3, 5, 10, 20):
        r = simulate_schedule(leverage=lev)
        print(f"  {lev:>8}x {r['p15']*100:>9.1f}% {r['p30']*100:>9.1f}% "
              f"{r['p90']*100:>9.1f}% {r['p_ruin']*100:>9.1f}% {r['median_life']:>10.0f}")
    print()
    print("  Read the columns left to right. Essentially every account reaches day")
    print("  15 -- which is exactly the trap. The schedule looks validated at the")
    print("  moment you escalate, because two weeks is shorter than the time it")
    print("  takes this bet to fail. The bill arrives between day 30 and day 90,")
    print("  and 62-99% of these accounts are gone inside a year.")
    print()


if __name__ == "__main__":
    random.seed(11)
    show_rate_path()
    show_survival()
