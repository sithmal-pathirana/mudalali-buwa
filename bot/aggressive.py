"""
Aggressive mode: a separate risk profile, and the warnings that go with it.

This replaces the safe profile wholesale rather than nudging it. Nothing about
the safe path changes while it is off, and switching it on is deliberate.

What it does NOT change, in any profile:

  * every position keeps a stop on the exchange
  * the KILL file and the equity floor stay absolute

Those are not caution. A position with no stop is an unbounded liability the
moment the process dies, the VPS reboots, or the network drops -- "unprotected"
is a bug, not a risk setting. Everything else is yours to turn up.

The warning text is COMPUTED, not written. It runs the same ruin model as
tools/reality_check.py against the profile actually configured, so it stays
truthful when the settings or the strategy change.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass

MAINT_MARGIN = 0.005
TAKER_ROUNDTRIP = 0.0007
MIN_ORDER = 5.0


@dataclass
class Profile:
    name: str
    leverage: int
    risk_per_trade_pct: float
    portfolio_risk_pct: float
    interval: str
    rescan_seconds: int
    trades_per_day: int
    trailing_atr_mult: float
    stall_bars: int

    def describe(self) -> str:
        return (f"{self.name}: {self.leverage}x leverage, "
                f"{self.risk_per_trade_pct:.1f}% per trade, "
                f"{self.portfolio_risk_pct:.0f}% portfolio, {self.interval} bars")


# The interval is 15m in every profile, and that is a measured choice rather
# than a preference. momentum_burst on 5m -- the "faster is more aggressive"
# intuition -- is badly negative; on 15m it is the only strategy in this
# repository positive in BOTH windows:
#
#   interval   in-sample   out-of-sample   OOS cells positive
#   5m          -0.5204        -1.4202            14%
#   15m         +0.1291        +0.2709            86%
#   1h          -0.0743        +0.0602            71%
#
# Aggressive means bigger and more concurrent, not shorter bars. Shortening
# the timeframe only bought more fees against more noise.
PROFILES = {
    "moderate": Profile("moderate", 10, 2.0, 20.0, "15m", 300, 6, 2.0, 12),
    "high":     Profile("high",     20, 4.0, 30.0, "15m", 300, 12, 1.5, 8),
    "maximum":  Profile("maximum",  50, 8.0, 50.0, "15m", 180, 20, 1.0, 5),
}


def ruin_probability(equity: float, profile: Profile, sims: int = 4000,
                     horizon: int = 180, seed: int | None = 7) -> tuple[float, float]:
    """
    P(account falls below the minimum order) and median days survived.

    Driftless double-barrier, i.e. it assumes NO EDGE -- which is what this
    repository has actually measured out-of-sample. If a strategy with genuine
    edge is found, aggressive sizing amplifies that instead and these numbers
    become wrong in the operator's favour. That caveat is stated wherever the
    number is shown.
    """
    if seed is not None:
        random.seed(seed)
    liq = 1.0 / profile.leverage - MAINT_MARGIN
    ruined, lifespans = 0, []
    for _ in range(sims):
        eq, day = equity, 0
        for day in range(1, horizon + 1):
            for _ in range(profile.trades_per_day):
                if eq < MIN_ORDER:
                    break
                notional = min(eq * profile.leverage,
                               eq * profile.risk_per_trade_pct / 100 / 0.02
                               * profile.leverage)
                stop_move = min(profile.risk_per_trade_pct / 100, liq)
                tp_move = stop_move * 1.5
                fee = notional * TAKER_ROUNDTRIP
                if random.random() < stop_move / (tp_move + stop_move):
                    eq += notional * tp_move - fee
                else:
                    eq -= notional * stop_move + fee
            if eq < MIN_ORDER:
                eq = 0.0
                break
        if eq < MIN_ORDER:
            ruined += 1
        lifespans.append(day)
    return ruined / sims, statistics.median(lifespans)


def banner(equity: float, profile: Profile, width: int = 66) -> str:
    """The warning, with this profile's own numbers in it."""
    p_ruin, life = ruin_probability(equity, profile)
    bar = "!" * width
    lines = [
        bar,
        "  AGGRESSIVE MODE IS ON".ljust(width),
        "",
        f"  {profile.describe()}",
        f"  scanning every {profile.rescan_seconds // 60} min, "
        f"market entries, trailing stops",
        "",
        f"  Modelled on ${equity:,.2f} over 180 days, assuming no edge:",
        f"    probability of ruin      {p_ruin * 100:.1f}%",
        f"    median days survived     {life:.0f}",
        "",
        "  'No edge' is what this repository has measured out-of-sample.",
        "  Find a strategy with real edge and these numbers change in your",
        "  favour -- until then they are the honest expectation.",
        "",
        "  Still enforced: exchange-side stop on every position, the KILL",
        "  file, and the equity floor. Everything else is off the leash.",
        bar,
    ]
    return "\n".join(lines)


def short_warning(equity: float, profile: Profile) -> str:
    p_ruin, life = ruin_probability(equity, profile, sims=1500)
    return (f"AGGRESSIVE / {profile.name}: {profile.leverage}x, "
            f"P(ruin) {p_ruin * 100:.0f}%, median life {life:.0f}d")


def apply(cfg, profile: Profile):
    """Overlay the profile onto a config. Safe settings are left untouched
    when aggressive is off, because this is never called then."""
    cfg.risk.max_leverage = profile.leverage
    cfg.risk.risk_per_trade_pct = profile.risk_per_trade_pct
    cfg.interval = profile.interval
    cfg.portfolio.portfolio_risk_pct = profile.portfolio_risk_pct
    cfg.portfolio.single_position_cap_pct = profile.risk_per_trade_pct
    cfg.universe = dict(cfg.universe or {})
    cfg.universe["rescan_seconds"] = profile.rescan_seconds
    cfg.universe["interval"] = profile.interval
    # Aggressive wants movement, not safety: loosen trendiness, demand range.
    cfg.universe.setdefault("min_efficiency", 0.20)
    cfg.universe["min_atr_pct"] = max(0.6, cfg.universe.get("min_atr_pct", 0.25))
    return cfg
