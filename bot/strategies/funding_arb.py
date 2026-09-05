"""
Funding-rate capture: long spot, short the perpetual, collect funding.

This is the strategy with a real structural edge -- you are paid for taking
the other side of leveraged retail longs, not for predicting anything. It is
also the strategy that is honest about needing capital, so it refuses to run
when the account cannot support both legs. That refusal is a feature.
"""

from __future__ import annotations

from .base import Bar, Signal, Strategy

PERIODS_PER_DAY = 3          # funding settles every 8 hours


class FundingArb(Strategy):
    name = "funding_arb"
    warmup = 1

    def __init__(self, min_funding_rate: float = 0.00005, target_usd_per_day: float = 2.0):
        self.min_funding_rate = min_funding_rate
        self.target_usd_per_day = target_usd_per_day

    def feasible(self, equity: float, price: float, rules) -> tuple[bool, str]:
        """
        Both legs must clear MIN_NOTIONAL, and the spot leg must be funded in
        full. Report the capital actually required rather than failing quietly.
        """
        cheapest = rules.min_affordable_notional(price)
        needed_for_one_leg = cheapest * 2          # perp margin + matching spot
        daily_rate = self.min_funding_rate * PERIODS_PER_DAY
        notional_for_target = self.target_usd_per_day / daily_rate
        capital_for_target = notional_for_target * 2

        if equity < needed_for_one_leg:
            return False, (
                f"needs at least ${needed_for_one_leg:,.2f} to open both legs on "
                f"{rules.symbol} (cheapest legal order is ${cheapest:,.2f} per leg); "
                f"account holds ${equity:,.2f}")

        earn = equity / 2 * daily_rate
        return True, (
            f"at ${equity:,.2f} this earns about ${earn:.4f}/day at {self.min_funding_rate*100:.4f}% "
            f"funding. Hitting ${self.target_usd_per_day:.2f}/day needs roughly "
            f"${capital_for_target:,.0f} of capital.")

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        # The funding leg is managed by the engine's dedicated path, not by bar
        # signals; this strategy never emits directional trades.
        return None
