"""
How many positions can this account actually hold at once?

Concurrency is not free and it is not a preference -- it is bounded by three
independent limits, and the smallest one wins:

  1. PORTFOLIO RISK. If every open position risks `risk_per_trade_pct` and all
     of them stop out together (which is exactly what a correlated crypto
     selloff does), total loss is N x that. Capping the sum is the only thing
     standing between "diversified" and "one bad hour".
  2. LEVERAGE. Total notional across all positions must respect max_leverage.
  3. MINIMUM ORDER SIZE. Each leg has to clear the exchange's floor, or it is
     not a position at all.

Splitting a small account across more positions makes each one smaller, and
below the minimum notional it stops being tradeable. That is why concurrency
is a capital question before it is an engineering one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Capacity:
    equity: float
    max_concurrent: int
    per_trade_risk_pct: float
    portfolio_risk_pct: float
    notional_per_slot: float
    limited_by: str
    detail: str = ""

    def __str__(self) -> str:
        return (f"{self.max_concurrent} concurrent position(s) of about "
                f"${self.notional_per_slot:,.2f} notional each "
                f"(limited by {self.limited_by})")


def capacity(equity: float, per_trade_risk_pct: float, portfolio_risk_pct: float,
             max_leverage: float, requested: int,
             stop_distance: float = 0.02,
             min_notional: float = 5.0) -> Capacity:
    """
    The number of slots this account can genuinely support, and what binds it.

    stop_distance is the representative fraction between entry and stop; it
    converts a risk budget in dollars into a notional size.
    """
    if equity <= 0 or requested < 1:
        return Capacity(equity, 0, per_trade_risk_pct, portfolio_risk_pct,
                        0.0, "no equity")

    by_risk = int(portfolio_risk_pct // per_trade_risk_pct) if per_trade_risk_pct else requested
    by_risk = max(by_risk, 0)

    notional_per_trade = equity * per_trade_risk_pct / 100 / stop_distance
    total_capacity = equity * max_leverage
    by_leverage = int(total_capacity // notional_per_trade) if notional_per_trade else 0

    by_minimum = int(total_capacity // min_notional) if min_notional else requested

    slots = max(0, min(requested, by_risk, by_leverage, by_minimum))
    limits = {"portfolio risk": by_risk, "leverage": by_leverage,
              "minimum order size": by_minimum, "your setting": requested}
    limited_by = min(limits, key=lambda k: limits[k])

    per_slot = min(notional_per_trade, total_capacity / slots) if slots else 0.0
    detail = ", ".join(f"{k} allows {v}" for k, v in limits.items())

    return Capacity(equity=equity, max_concurrent=slots,
                    per_trade_risk_pct=per_trade_risk_pct,
                    portfolio_risk_pct=portfolio_risk_pct,
                    notional_per_slot=per_slot,
                    limited_by=limited_by, detail=detail)


def explain(equity: float, per_trade: float, portfolio: float,
            leverage: float, requested: int) -> str:
    c = capacity(equity, per_trade, portfolio, leverage, requested)
    lines = [
        f"  equity                    ${equity:,.2f}",
        f"  risk per trade            {per_trade:.1f}%  (${equity * per_trade / 100:,.2f})",
        f"  portfolio risk cap        {portfolio:.1f}%  (${equity * portfolio / 100:,.2f})",
        f"  max leverage              {leverage:g}x  (${equity * leverage:,.2f} notional)",
        "",
        f"  {c.detail}",
        "",
        f"  -> {c}",
    ]
    if c.max_concurrent <= 1:
        lines += ["", "  At this size concurrency buys you nothing: the account can",
                  "  fund one position. Scanning wide still helps -- it picks WHICH",
                  "  one -- but holding several is a capital decision, not a setting."]
    return "\n".join(lines)
