"""
Symbol filters. Binance rejects orders that violate tick/step/notional rules,
and on a $43 account MIN_NOTIONAL is the constraint that decides what you can
trade at all -- so this is not boilerplate, it is the sizing layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN


@dataclass
class SymbolRules:
    symbol: str
    tick_size: Decimal      # PRICE_FILTER   -- price must be a multiple of this
    step_size: Decimal      # LOT_SIZE       -- quantity must be a multiple of this
    min_qty: Decimal
    max_qty: Decimal
    min_notional: Decimal   # MIN_NOTIONAL   -- price * qty floor
    price_precision: int
    qty_precision: int

    @classmethod
    def from_exchange_info(cls, info: dict, symbol: str) -> "SymbolRules":
        for s in info["symbols"]:
            if s["symbol"] != symbol:
                continue
            f = {x["filterType"]: x for x in s["filters"]}
            return cls(
                symbol=symbol,
                tick_size=Decimal(f["PRICE_FILTER"]["tickSize"]),
                step_size=Decimal(f["LOT_SIZE"]["stepSize"]),
                min_qty=Decimal(f["LOT_SIZE"]["minQty"]),
                max_qty=Decimal(f["LOT_SIZE"]["maxQty"]),
                min_notional=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "5")),
                price_precision=int(s["pricePrecision"]),
                qty_precision=int(s["quantityPrecision"]),
            )
        raise KeyError(f"{symbol} not listed on this venue")

    # ------------------------------------------------------------- rounding
    def round_price(self, price: float) -> str:
        d = (Decimal(str(price)) / self.tick_size).to_integral_value(ROUND_DOWN) * self.tick_size
        return f"{d:.{self.price_precision}f}"

    def round_qty(self, qty: float) -> str:
        d = (Decimal(str(qty)) / self.step_size).to_integral_value(ROUND_DOWN) * self.step_size
        return f"{d:.{self.qty_precision}f}"

    # -------------------------------------------------------------- sizing
    def size_for_notional(self, notional: float, price: float) -> tuple[str, str] | None:
        """
        Largest valid quantity worth at most `notional` at `price`.
        Returns None when the account cannot afford a legal order -- the
        caller must treat that as "do not trade", never as "trade smaller".
        """
        if price <= 0:
            return None
        raw_qty = notional / price
        qty_s = self.round_qty(raw_qty)
        qty = Decimal(qty_s)
        if qty < self.min_qty or qty <= 0:
            return None
        if qty * Decimal(str(price)) < self.min_notional:
            return None
        return qty_s, self.round_price(price)

    def min_affordable_notional(self, price: float) -> float:
        """Cheapest legal order for this symbol right now, in USDT."""
        by_qty = float(self.min_qty) * price
        return max(by_qty, float(self.min_notional))

    def describe(self, price: float) -> str:
        return (f"{self.symbol}: tick={self.tick_size} step={self.step_size} "
                f"minQty={self.min_qty} minNotional={self.min_notional} "
                f"-> cheapest legal order ~${self.min_affordable_notional(price):,.2f}")
