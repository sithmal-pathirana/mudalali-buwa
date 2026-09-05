"""
Regime-routed strategy selection.

Most retail bots run one strategy in every market condition, which guarantees
they spend part of every year in the conditions that strategy is worst at.
This routes instead:

    trending  ->  trend_atr   (buy the break)
    ranging   ->  nothing     (stand down)
    unclear   ->  nothing     (stand down)

The standing-down branches are the ones that earn their keep. A router that
must always pick a strategy is just a slower way of always trading.

The default routing above is not a guess -- it is what tools/regime_edge.py
measured. Tagging every trade by the regime in force when it opened gave:

    trend_atr       trending    +0.1661 $/trade   52% win
    trend_atr       ranging     -0.0990 $/trade   38% win
    mean_reversion  ranging     -0.0738 $/trade   43% win
    mean_reversion  trending    -0.3168 $/trade   27% win

Both strategies respond to regime in the expected direction, but only
trend_atr is ever POSITIVE, so the useful router gates it rather than
alternating. Routing to mean_reversion in ranging markets -- the obvious
symmetric design, and the one this file shipped first -- allocated into a
strategy that loses in every regime, and scored worse than trend_atr alone.

Set routes explicitly if your own measurements say otherwise. Routing cannot
create an edge; it can only concentrate one that already exists.
"""

from __future__ import annotations

import logging

from ..regime import Regime, RegimeDetector
from .base import Bar, Signal, Strategy
from .mean_reversion import MeanReversion
from .trend_atr import TrendATR

log = logging.getLogger("switcher")


class RegimeSwitcher(Strategy):
    name = "switcher"

    #: regime name -> strategy name, or None to stand down
    DEFAULT_ROUTES = {"trending": "trend_atr", "ranging": None, "unclear": None}

    def __init__(self, regime: dict | None = None,
                 trend: dict | None = None,
                 revert: dict | None = None,
                 routes: dict | None = None):
        self.detector = RegimeDetector(**(regime or {}))
        self.trend = TrendATR(**(trend or {}))
        self.revert = MeanReversion(**(revert or {}))
        self.routes = {**self.DEFAULT_ROUTES, **(routes or {})}
        unknown = set(self.routes) - {r.value for r in Regime}
        if unknown:
            raise ValueError(f"unknown regime(s) in routes: {sorted(unknown)}")
        self._by_name = {"trend_atr": self.trend, "mean_reversion": self.revert}
        for target in self.routes.values():
            if target is not None and target not in self._by_name:
                raise ValueError(f"routes may only target "
                                 f"{sorted(self._by_name)}, got {target!r}")
        self.warmup = max(self.detector.warmup, self.trend.warmup,
                          self.revert.warmup) + 2

        # Bookkeeping the backtest and the dashboard both read.
        self.override: str | None = None      # None = automatic
        self.last_reading = None
        self.bars_in: dict[str, int] = {r.value: 0 for r in Regime}
        self.signals_from: dict[str, int] = {"trend_atr": 0, "mean_reversion": 0}
        self.stood_down = 0

    # ------------------------------------------------------------- routing
    @property
    def choices(self) -> list[str]:
        """What a manual override may be set to."""
        return ["auto", *sorted(self._by_name), "none"]

    def set_override(self, name: str | None) -> str:
        """
        Force one strategy regardless of regime, or return to automatic.

        "auto" restores regime routing, "none" stands down entirely, and a
        strategy name pins that strategy. Returns a human-readable result --
        the caller reports it back to whoever asked.
        """
        if name in (None, "", "auto"):
            self.override = None
            return "automatic: routing by market regime"
        if name == "none":
            self.override = "none"
            return "manual: standing down, no strategy will trade"
        if name not in self._by_name:
            raise ValueError(f"unknown strategy {name!r}; choose from "
                             f"{', '.join(self.choices)}")
        self.override = name
        return f"manual: {name} pinned regardless of regime"

    @property
    def mode(self) -> str:
        return "auto" if self.override is None else f"manual/{self.override}"

    def route(self, regime: Regime) -> Strategy | None:
        # A manual override wins over the regime reading, always. The regime
        # is still measured and reported so you can see what you are overriding.
        if self.override == "none":
            return None
        if self.override is not None:
            return self._by_name[self.override]
        target = self.routes.get(regime.value)
        return self._by_name[target] if target else None

    def on_bars(self, bars: list[Bar], position_amt: float) -> Signal | None:
        if len(bars) < self.warmup:
            return None

        reading = self.detector.update(bars)
        self.last_reading = reading
        self.bars_in[reading.regime.value] += 1

        if position_amt != 0:
            return None

        chosen = self.route(reading.regime)
        if chosen is None:
            self.stood_down += 1
            return None

        signal = chosen.on_bars(bars, position_amt)
        if signal is None:
            return None

        self.signals_from[chosen.name] += 1
        tag = reading.regime.value if self.override is None else f"forced:{self.override}"
        signal.reason = f"[{tag} ER={reading.efficiency:.2f}] {signal.reason}"
        return signal

    # --------------------------------------------------------------- report
    def describe_activity(self) -> str:
        total = sum(self.bars_in.values()) or 1
        parts = [f"{k} {v / total * 100:.0f}%" for k, v in self.bars_in.items() if v]
        return (f"mode: {self.mode} | regime split: {', '.join(parts)} | "
                f"switches: {self.detector.switches} | "
                f"signals: trend {self.signals_from['trend_atr']}, "
                f"revert {self.signals_from['mean_reversion']}, "
                f"stood down {self.stood_down}")

    def feasible(self, equity: float, price: float, rules) -> tuple[bool, str]:
        return True, ("routes between trend_atr and mean_reversion by "
                      "efficiency ratio; stands down when neither applies")
