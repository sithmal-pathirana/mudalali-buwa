"""
Does the daily profit cap help or hurt?

`stop_when_reached: true` stops trading once the day's target is banked. It is
the honest version of "guarantee $2/day": it cannot make the target arrive, but
it stops you handing a won day back.

The concern is asymmetry. The cap truncates the RIGHT tail -- your best days --
while leaving the left tail untouched, because a losing day still runs to the
stop. For a trend-following system, whose entire expectancy lives in a small
number of large wins, cutting winners is the classic error.

Which effect dominates is an empirical question, so this measures both.

    /usr/bin/python3 tools/cap_test.py
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
INTERVALS = ["15m", "1h"]
MINUTES = {"15m": 15, "1h": 60}


def main(equity: float = 43.0, target: float = 2.0) -> int:
    cfg = Config.load()
    api = Binance(testnet=False)
    info = api.exchange_info()

    print(f"\n  DAILY CAP TEST   strategy={cfg.strategy}   cap=${target:.2f}/day\n")
    print(f"  {'cell':<14}{'uncapped':>12}{'capped':>12}{'difference':>13}{'days hit':>10}")
    print("  " + "-" * 61)

    uncapped, capped, hits = [], [], []
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
            if len(bars) < 200:
                continue
            days = len(bars) / (1440 / MINUTES[iv])
            mn = rules.min_affordable_notional(bars[-1].close)

            row = []
            for cap in (0.0, target):
                strat = build(cfg.strategy, cfg.params if cfg.strategy == cfg.strategy else {})
                res = bt.run(bars, strat, equity=equity,
                             risk_pct=cfg.risk.risk_per_trade_pct,
                             max_leverage=cfg.risk.max_leverage,
                             min_notional=mn, daily_target=cap)
                row.append((res.net / days if days else 0.0, len(res.capped_days)))
            uncapped.append(row[0][0])
            capped.append(row[1][0])
            hits.append(row[1][1])
            print(f"  {sym[:-4]+' '+iv:<14}{row[0][0]:>12.4f}{row[1][0]:>12.4f}"
                  f"{row[1][0]-row[0][0]:>13.4f}{row[1][1]:>10}")

    if not uncapped:
        print("\n  no data\n")
        return 1

    mu, mc = statistics.fmean(uncapped), statistics.fmean(capped)
    print("\n  " + "-" * 61)
    print(f"  {'mean $/day':<14}{mu:>12.4f}{mc:>12.4f}{mc-mu:>13.4f}{sum(hits):>10}")
    better = sum(1 for a, b in zip(uncapped, capped) if b > a)
    print(f"  cap improved {better}/{len(capped)} cells")
    print()
    worse = sum(1 for a, b in zip(uncapped, capped) if b < a)
    unchanged = len(capped) - better - worse
    print(f"  improved {better}, worsened {worse}, unchanged {unchanged}")
    print()

    if sum(hits) == 0:
        print("  The cap NEVER FIRED. At this account size the strategy does not")
        print(f"  reach ${target:.2f} in a day, so `stop_when_reached` is inert --")
        print("  it is neither helping nor hurting, just unused configuration.")
    elif mc > mu and better <= len(capped) / 2:
        # A mean can be moved by one or two cells. Saying "it helped" on that
        # basis is the same error this repo keeps warning about elsewhere.
        print("  INCONCLUSIVE. The mean improved, but fewer than half the cells")
        print(f"  did ({better} of {len(capped)}), so the average is being carried by")
        print("  a couple of outliers rather than a consistent effect. Treat this")
        print("  as noise until it reproduces on another window.")
    elif mc > mu:
        print("  The cap HELPED, and in a majority of cells. Protecting won days")
        print("  outweighed the truncated upside on this sample.")
    else:
        print("  The cap HURT. It truncates your best days while losing days still")
        print("  run to the stop -- the left tail is untouched and the right tail")
        print("  is cut. For a trend system, whose expectancy lives in a few large")
        print("  wins, that asymmetry is the classic way to turn a positive")
        print("  strategy negative.")
    print()
    return 0


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 43.0,
         float(sys.argv[2]) if len(sys.argv) > 2 else 2.0)
