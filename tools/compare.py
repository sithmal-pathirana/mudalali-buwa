"""
Head-to-head: does regime routing beat the strategies it routes between?

Runs every strategy over identical data and reports each one's mean daily P&L
and the share of cells it was profitable in. The switcher only earns its
complexity if it beats BOTH components -- if it lands between them, it is an
expensive way to average two things.

    /usr/bin/python3 tools/compare.py
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
from bot.strategies import build                     # noqa: E402
from bot.strategies.base import Bar                  # noqa: E402

SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "BNBUSDT"]
INTERVALS = ["15m", "1h", "4h"]
MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}
CONTENDERS = ["trend_atr", "mean_reversion", "switcher"]


def main(equity: float = 43.0) -> int:
    cfg = Config.load()
    api = Binance(testnet=False)
    info = api.exchange_info()

    print(f"\n  HEAD-TO-HEAD   equity ${equity:,.2f}   "
          f"risk {cfg.risk.risk_per_trade_pct}%/trade   max_lev {cfg.risk.max_leverage}x")
    print(f"  {len(SYMBOLS)} symbols x {len(INTERVALS)} intervals = "
          f"{len(SYMBOLS)*len(INTERVALS)} cells per strategy\n")

    results: dict[str, list[float]] = {c: [] for c in CONTENDERS}
    trades: dict[str, int] = {c: 0 for c in CONTENDERS}
    regime_split: list[str] = []

    header = f"  {'cell':<16}" + "".join(f"{c:>16}" for c in CONTENDERS)
    print(header)
    print("  " + "-" * (16 + 16 * len(CONTENDERS)))

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
            if len(bars) < 100:
                continue
            days = len(bars) / (1440 / MINUTES[iv])
            min_notional = rules.min_affordable_notional(bars[-1].close)

            row = []
            for name in CONTENDERS:
                strat = build(name, cfg.params if name == "trend_atr" else {})
                res = bt.run(bars, strat, equity=equity,
                             risk_pct=cfg.risk.risk_per_trade_pct,
                             max_leverage=cfg.risk.max_leverage,
                             min_notional=min_notional)
                per_day = res.net / days if days else 0.0
                results[name].append(per_day)
                trades[name] += len(res.trades)
                row.append(per_day)
                if name == "switcher" and hasattr(strat, "describe_activity"):
                    regime_split.append(strat.describe_activity())

            print(f"  {sym[:-4]+' '+iv:<16}" + "".join(f"{v:>16.3f}" for v in row))

    print()
    print(f"  {'':<16}" + "".join(f"{c:>16}" for c in CONTENDERS))
    print("  " + "-" * (16 + 16 * len(CONTENDERS)))
    summary = {}
    for label, fn in (("mean $/day", statistics.fmean),
                      ("median $/day", statistics.median)):
        line = f"  {label:<16}"
        for c in CONTENDERS:
            line += f"{fn(results[c]):>16.4f}" if results[c] else f"{'-':>16}"
        print(line)
    line = f"  {'cells positive':<16}"
    for c in CONTENDERS:
        v = results[c]
        pct = sum(1 for x in v if x > 0) / len(v) * 100 if v else 0
        summary[c] = (statistics.fmean(v) if v else 0.0, pct)
        line += f"{f'{pct:.0f}%':>16}"
    print(line)
    line = f"  {'total trades':<16}"
    for c in CONTENDERS:
        line += f"{trades[c]:>16}"
    print(line)

    if regime_split:
        print(f"\n  switcher activity (last cell): {regime_split[-1]}")

    # ---- the verdict the switcher has to earn -------------------------------
    print()
    sw_mean, sw_pct = summary.get("switcher", (0, 0))
    comp = [summary[c][0] for c in ("trend_atr", "mean_reversion") if c in summary]
    best_component = max(comp) if comp else 0.0

    if sw_mean > best_component and sw_mean > 0:
        print("  VERDICT: routing beats both components AND is positive.")
        print("  Necessary but not sufficient -- re-run on a different date range")
        print("  before trusting it, and check the fill rate is realistic.")
    elif sw_mean > best_component:
        print(f"  VERDICT: routing beats its best component ({sw_mean:+.4f} vs "
              f"{best_component:+.4f}/day)")
        print("  but is still negative. It is allocating better between two")
        print("  losing strategies, which is not the same as making money.")
    else:
        print(f"  VERDICT: routing does NOT beat its best component "
              f"({sw_mean:+.4f} vs {best_component:+.4f}/day).")
        print("  The switching layer is not paying for itself. Routing cannot")
        print("  create an edge -- it can only allocate one that already exists,")
        print("  and neither component has one here.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(float(sys.argv[1]) if len(sys.argv) > 1 else 43.0))
