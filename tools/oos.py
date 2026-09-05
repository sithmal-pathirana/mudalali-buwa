"""
Out-of-sample test for the regime router.

The routing rule in RegimeSwitcher.DEFAULT_ROUTES was derived by measuring the
MOST RECENT ~1500 bars. Scoring it on those same bars proves nothing: any rule
chosen to fit a window will fit that window.

This re-runs the identical comparison on the window immediately BEFORE it --
data the rule never saw. If the switcher's advantage survives here, it is
evidence. If it flips sign, the in-sample result was noise and the router
should be thrown away rather than tuned.

    /usr/bin/python3 tools/oos.py
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
MINUTES = {"15m": 15, "1h": 60, "4h": 240}
CONTENDERS = ["trend_atr", "mean_reversion", "switcher", "momentum_burst"]
BARS = 1500


def window(api, sym, iv, which):
    """which=0 -> the recent (in-sample) window; 1 -> the one before it."""
    recent = api.klines(sym, iv, limit=BARS)
    if which == 0:
        return recent
    first_open = int(recent[0][0])
    return api.klines(sym, iv, limit=BARS, end_ms=first_open - 1)


def measure(api, info, cfg, equity, which):
    out = {c: [] for c in CONTENDERS}
    spans = []
    for sym in SYMBOLS:
        try:
            rules = SymbolRules.from_exchange_info(info, sym)
        except KeyError:
            continue
        for iv in INTERVALS:
            try:
                raw = window(api, sym, iv, which)
            except BinanceError:
                continue
            bars = [Bar.from_kline(k) for k in raw[:-1]]
            if len(bars) < 200:
                continue
            spans.append((bars[0].open_time, bars[-1].open_time))
            days = len(bars) / (1440 / MINUTES[iv])
            mn = rules.min_affordable_notional(bars[-1].close)
            for name in CONTENDERS:
                strat = build(name, cfg.params if name == cfg.strategy else {})
                res = bt.run(bars, strat, equity=equity,
                             risk_pct=cfg.risk.risk_per_trade_pct,
                             max_leverage=cfg.risk.max_leverage, min_notional=mn)
                out[name].append(res.net / days if days else 0.0)
    return out, spans


def main(equity: float = 43.0) -> int:
    import datetime as dt
    cfg = Config.load()
    api = Binance(testnet=False)
    info = api.exchange_info()

    print("\n  IN-SAMPLE vs OUT-OF-SAMPLE\n")
    results = {}
    for which, label in ((0, "in-sample (rule was fitted here)"),
                         (1, "OUT-OF-SAMPLE (never seen)")):
        res, spans = measure(api, info, cfg, equity, which)
        results[label] = res
        if spans:
            a = dt.datetime.fromtimestamp(min(s[0] for s in spans) / 1000, dt.UTC)
            b = dt.datetime.fromtimestamp(max(s[1] for s in spans) / 1000, dt.UTC)
            print(f"  {label}")
            print(f"    span {a:%Y-%m-%d} to {b:%Y-%m-%d}, {len(res[CONTENDERS[0]])} cells")
            for c in CONTENDERS:
                v = res[c]
                pct = sum(1 for x in v if x > 0) / len(v) * 100 if v else 0
                print(f"    {c:<16} mean {statistics.fmean(v):+.4f} $/day   "
                      f"positive {pct:.0f}% of cells")
            print()

    a = results["in-sample (rule was fitted here)"]
    b = results["OUT-OF-SAMPLE (never seen)"]
    sw_in, sw_out = statistics.fmean(a["switcher"]), statistics.fmean(b["switcher"])
    tr_out = statistics.fmean(b["trend_atr"])

    print("  " + "-" * 62)
    print(f"  switcher in-sample      {sw_in:+.4f} $/day")
    print(f"  switcher out-of-sample  {sw_out:+.4f} $/day")
    print(f"  trend_atr out-of-sample {tr_out:+.4f} $/day")
    print()
    if sw_out > 0 and sw_out > tr_out:
        print("  HOLDS. Still positive and still beating its component on data")
        print("  the rule never saw.")
        # A ratio is only meaningful when both windows share a sign; across a
        # sign change "decayed 115%" is arithmetic noise, not a finding.
        if sw_in > 0:
            print(f"  Decayed {(1 - sw_out / sw_in) * 100:.0f}% from in-sample, "
                  f"which is normal and healthy --")
            print("  a rule that does NOT decay usually means the windows overlap.")
        else:
            print(f"  In-sample was NEGATIVE ({sw_in:+.4f}) and out-of-sample is "
                  f"positive ({sw_out:+.4f}).")
            print("  That is a sign flip, not decay, and it is a warning rather than")
            print("  a triumph: it means the result is unstable across windows and")
            print("  neither number should be trusted yet. Re-run on more windows.")
        print("  Worth testnet, not worth conviction.")
    elif sw_out > tr_out:
        print("  PARTIAL. Still beats its component out-of-sample but is not")
        print("  positive. The gating helps; the underlying signal does not clear")
        print("  costs. Not tradeable as it stands.")
    else:
        print("  FAILS. The advantage did not survive out-of-sample, so the")
        print("  in-sample result was noise fitted to one window. Do not trade it,")
        print("  and do not tune it -- tuning from here fits the noise harder.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
