"""
Record open interest and long/short ratios for every USDT perpetual, so a later
backtest can test them. Binance serves only the last 30 days of this history,
so it has to be saved as it goes.

    python3 tools/record_oi.py            # run twice a day (cron)

Each run fetches the last 500 five-minute points (~41 hours) per symbol and
metric and merges them into <data>/oi/<symbol>.pkl as
{time_ms: [oi_qty, oi_usdt, accounts_long_short, top_traders_long_short,
taker_buy_sell]}. Public endpoints, no key. It paces itself (about two
requests a second), pauses while the IP's used weight is high and stops on
HTTP 429/418, so the trading bot on the same IP is never crowded out.
"""

from __future__ import annotations

import json
import os
import pickle
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://fapi.binance.com"
OUT = Path(os.environ.get("BACKTEST_DATA", Path.home() / "backtest-data")) / "oi"
EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}
METRICS = [  # (endpoint, fields taken, slot in the stored row)
    ("/futures/data/openInterestHist", ("sumOpenInterest", "sumOpenInterestValue"), 0),
    ("/futures/data/globalLongShortAccountRatio", ("longShortRatio",), 2),
    ("/futures/data/topLongShortPositionRatio", ("longShortRatio",), 3),
    ("/futures/data/takerlongshortRatio", ("buySellRatio",), 4),
]
PAUSE = 0.5


class Stop(Exception):
    pass


def get(path: str, params: dict | None = None):
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                used = int(r.headers.get("X-MBX-USED-WEIGHT-1M") or 0)
                if used > 900:
                    time.sleep(60)               # leave the minute's weight to the bot
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429):
                raise Stop(f"HTTP {e.code} from Binance: stopping this run")
            if e.code == 400:
                return []                        # symbol without this history
            time.sleep(5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(5 * (attempt + 1))
    return []


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    info = get("/fapi/v1/exchangeInfo")
    syms = sorted(s["symbol"] for s in info.get("symbols", [])
                  if s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
                  and s.get("quoteAsset") == "USDT" and s.get("baseAsset") not in EXCLUDE_BASES)
    started, points = time.time(), 0
    try:
        for n, sym in enumerate(syms, 1):
            path = OUT / f"{sym}.pkl"
            data = pickle.load(open(path, "rb")) if path.exists() else {}
            for endpoint, fields, slot in METRICS:
                rows = get(endpoint, {"symbol": sym, "period": "5m", "limit": 500})
                time.sleep(PAUSE)
                for r in rows or []:
                    t = int(r["timestamp"])
                    row = data.setdefault(t, [None] * 5)
                    for k, f in enumerate(fields):
                        try:
                            row[slot + k] = float(r[f])
                        except (KeyError, TypeError, ValueError):
                            pass
            with open(str(path) + ".tmp", "wb") as fh:
                pickle.dump(data, fh)
            os.replace(str(path) + ".tmp", path)
            points += len(data)
            if n % 100 == 0:
                print(f"{n}/{len(syms)} symbols, {time.time() - started:.0f}s", flush=True)
    except Stop as e:
        print(e, flush=True)
        return 1
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} recorded {len(syms)} symbols "
          f"({points:,} five-minute points stored) in {time.time() - started:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
