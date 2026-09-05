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


def auto_slots(equity: float, portfolio_risk_pct: float, max_leverage: float,
               stop_distance: float = 0.02, min_notional: float = 5.0,
               hard_cap: int = 40) -> tuple[int, float]:
    """
    How many slots this equity supports, and the per-trade risk that fits them.

    Rather than fixing per-trade risk and discovering how few positions fit,
    fix the PORTFOLIO risk and let per-trade risk shrink as slots are added:

        per_trade_risk = portfolio_risk / slots

    Slots are then bounded by whichever binds first -- every slot must still
    clear the exchange minimum, and total notional must respect leverage. This
    scales in both directions: a $43 account and a $4,300 account both fill
    their capacity instead of inheriting a number tuned for the other.

    Returns (slots, per_trade_risk_pct).
    """
    if equity <= 0 or portfolio_risk_pct <= 0:
        return 0, 0.0

    total_notional = equity * max_leverage
    best = 0
    for n in range(1, hard_cap + 1):
        per_trade = portfolio_risk_pct / n
        notional = equity * per_trade / 100 / stop_distance
        if notional < min_notional:
            break                       # slots would be too small to place
        if notional * n > total_notional + 1e-9:
            break                       # would exceed the leverage ceiling
        best = n
    return best, (portfolio_risk_pct / best if best else 0.0)


def explain_auto(equity: float, portfolio_risk_pct: float = 6.0,
                 max_leverage: float = 3.0, min_notional: float = 5.0) -> str:
    slots, per_trade = auto_slots(equity, portfolio_risk_pct, max_leverage,
                                  min_notional=min_notional)
    if not slots:
        return f"  ${equity:,.2f}: cannot fund a single position"
    notional = equity * per_trade / 100 / 0.02
    return (f"  ${equity:>10,.2f}  {slots:>3} slots  "
            f"{per_trade:>5.2f}% each  ${notional:>9,.2f}/slot  "
            f"${notional * slots:>10,.2f} deployed")


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
