"""
Run the strategy across many symbols and timeframes at once.

The point is dispersion, not the average. A real edge shows up in most cells.
A curve-fit shows up in a few cells and reverses in the rest -- and a single
good cell is what a backtest hands you right before you lose money on it.

    /usr/bin/python3 tools/sweep.py
"""

from __future__ import annotations

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
INTERVALS = ["5m", "15m", "1h", "4h"]
MINUTES = {"5m": 5, "15m": 15, "1h": 60, "4h": 240}


def main(equity: float = 43.0) -> None:
    cfg = Config.load()
    api = Binance(testnet=False)
    info = api.exchange_info()

    print(f"\n  SWEEP  strategy={cfg.strategy}  equity=${equity:,.2f}  "
          f"risk={cfg.risk.risk_per_trade_pct}%/trade  max_lev={cfg.risk.max_leverage}x")
    print("  each cell is net P&L per day, in dollars, after fees\n")
    print(f"  {'symbol':<10}" + "".join(f"{i:>10}" for i in INTERVALS) + f"{'trades':>9}")
    print("  " + "-" * (10 + 10 * len(INTERVALS) + 9))

    cells, wins = [], 0
    for sym in SYMBOLS:
        try:
            rules = SymbolRules.from_exchange_info(info, sym)
        except KeyError:
            continue
        row, total_trades = [], 0
        for iv in INTERVALS:
            try:
                raw = api.klines(sym, iv, limit=1500)
            except BinanceError:
                row.append(None)
                continue
            bars = [Bar.from_kline(k) for k in raw[:-1]]
            strat = build(cfg.strategy, cfg.params)
            min_notional = rules.min_affordable_notional(bars[-1].close)
            res = bt.run(bars, strat, equity=equity,
                         risk_pct=cfg.risk.risk_per_trade_pct,
                         max_leverage=cfg.risk.max_leverage,
                         min_notional=min_notional)
            days = len(bars) / (1440 / MINUTES[iv])
            per_day = res.net / days if days else 0.0
            row.append(per_day)
            total_trades += len(res.trades)
            cells.append(per_day)
            wins += per_day > 0

        cell_s = "".join(f"{v:>10.3f}" if v is not None else f"{'-':>10}" for v in row)
        print(f"  {sym:<10}{cell_s}{total_trades:>9}")

    print()
    live = [c for c in cells if c is not None]
    if live:
        avg = sum(live) / len(live)
        best, worst = max(live), min(live)
        print(f"  cells profitable    {wins}/{len(live)}  ({wins/len(live)*100:.0f}%)")
        print(f"  mean $/day          {avg:+.4f}")
        print(f"  best / worst cell   {best:+.4f} / {worst:+.4f}")
        print()
        if wins / len(live) < 0.6 or avg <= 0:
            print("  VERDICT: no edge. The profitable cells are noise -- picking one")
            print("  and trading it is how backtest-driven accounts die.")
        else:
            print("  VERDICT: consistently positive here, which is necessary but not")
            print("  sufficient. Re-run on a different date range before believing it.")
        print(f"\n  For $2.00/day at the mean rate you would need "
              f"${equity * 2.0 / avg:,.0f} of capital." if avg > 0 else
              "\n  $2.00/day is not reachable with this strategy at any account size.")
    print()


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 43.0)
