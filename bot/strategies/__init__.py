from .base import Bar, Signal, Strategy
from .funding_arb import FundingArb
from .mean_reversion import MeanReversion
from .momentum_burst import MomentumBurst
from .switcher import RegimeSwitcher
from .trend_atr import TrendATR

REGISTRY = {
    "trend_atr": TrendATR,
    "mean_reversion": MeanReversion,
    "switcher": RegimeSwitcher,
    "momentum_burst": MomentumBurst,     # aggressive mode only
    "funding_arb": FundingArb,
}


def build(name: str, params: dict) -> Strategy:
    if name not in REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; have {sorted(REGISTRY)}")
    return REGISTRY[name](**params)
