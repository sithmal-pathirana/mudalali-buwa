"""
Does the regime signal carry information at all?

The switcher failing could mean two very different things: the thresholds are
wrong, or Efficiency Ratio simply does not separate the conditions these two
strategies need. Those have opposite responses -- tune, or abandon.

The test: run each strategy standalone, tag every trade by the regime that was
in force when it opened, and compare. If ER is informative, trend_atr should
earn more per trade in TRENDING bars than in RANGING ones, and mean_reversion
the reverse. If both are flat across regimes, no threshold rescues the router.

    /usr/bin/python3 tools/regime_edge.py
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import backtest as bt                       # noqa: E402
from bot.binanceapi import Binance, BinanceError     # noqa: E402
from bot.config import Config                        # noqa: E402
from bot.filters import SymbolRules                  # noqa: E402
from bot.regime import Regime, RegimeDetector        # noqa: E402
from bot.strategies import build                     # noqa: E402
from bot.strategies.base import Bar                  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT"]
INTERVALS = ["15m", "1h"]


def main(equity: float = 43.0) -> int:
    cfg = Config.load()
    api = Binance(testnet=False)
    info = api.exchange_info()

    # trades[strategy][regime] = list of per-trade P&L
    buckets: dict[str, dict[str, list[float]]] = {
        n: {r.value: [] for r in Regime} for n in ("trend_atr", "mean_reversion")}

    print("\n  REGIME CONDITIONAL EDGE\n"
          "  every trade tagged by the regime in force when it opened\n")

    for sym in SYMBOLS:
        try:
            rules = SymbolRules.from_exchange_info(info, sym)
        except KeyError:
            continue
        for iv in INTERVALS:
            try:
                raw = api.klines(sym, iv, limit=1500)
            except BinanceError:
                continue
            bars = [Bar.from_kline(k) for k in raw[:-1]]
            if len(bars) < 120:
                continue
            mn = rules.min_affordable_notional(bars[-1].close)

            # Regime label for every bar, using the same detector the bot uses.
            det = RegimeDetector()
            labels = []
            for i in range(len(bars)):
                if i < det.warmup:
                    labels.append(Regime.UNCLEAR.value)
                else:
                    labels.append(det.update(bars[:i + 1]).regime.value)

            for name in ("trend_atr", "mean_reversion"):
                strat = build(name, cfg.params if name == cfg.strategy else {})
                res = bt.run(bars, strat, equity=equity,
                             risk_pct=cfg.risk.risk_per_trade_pct,
                             max_leverage=cfg.risk.max_leverage, min_notional=mn)
                for t in res.trades:
                    if 0 <= t.entry_index < len(labels):
                        buckets[name][labels[t.entry_index]].append(t.pnl)

    print(f"  {'strategy':<16}{'regime':<12}{'trades':>8}{'mean $/trade':>15}{'win rate':>11}")
    print("  " + "-" * 62)
    readings = {}
    for name, by_regime in buckets.items():
        for regime, pnls in by_regime.items():
            if len(pnls) < 15:
                continue
            mean = statistics.fmean(pnls)
            win = sum(1 for p in pnls if p > 0) / len(pnls) * 100
            readings[(name, regime)] = mean
            print(f"  {name:<16}{regime:<12}{len(pnls):>8}{mean:>15.4f}{win:>10.0f}%")
        print()

    # ---- the question that decides tune-vs-abandon -------------------------
    t_trend = readings.get(("trend_atr", "trending"))
    t_range = readings.get(("trend_atr", "ranging"))
    m_trend = readings.get(("mean_reversion", "trending"))
    m_range = readings.get(("mean_reversion", "ranging"))

    print("  " + "-" * 62)
    if None in (t_trend, t_range, m_trend, m_range):
        print("  Not enough trades in every bucket to judge.")
        return 0

    trend_sep = t_trend - t_range        # want > 0
    revert_sep = m_range - m_trend       # want > 0
    print(f"  trend_atr:      trending minus ranging  = {trend_sep:+.4f} $/trade")
    print(f"  mean_reversion: ranging minus trending  = {revert_sep:+.4f} $/trade")
    print()
    if trend_sep > 0 and revert_sep > 0:
        print("  The regime signal SEPARATES them in the expected direction.")
        print("  Routing is worth tuning: the thresholds are wrong, not the idea.")
    elif trend_sep > 0 or revert_sep > 0:
        print("  Only one strategy responds to regime as expected. A router can")
        print("  at best gate that one -- it cannot usefully alternate between two.")
    else:
        print("  Neither strategy performs better in the regime meant to suit it.")
        print("  Efficiency Ratio carries no usable information for these two, so")
        print("  no threshold rescues the switcher. Abandon the router rather")
        print("  than tuning it -- tuning here would be fitting noise.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
