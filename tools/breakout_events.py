"""
Early-continuation challenger (2026-10-09), specified in advance by an outside
review and NOT tuned here. Writes an events file for

    backtest_gainer run --set test.signal=events --set test.events_file=<csv>
                        --set test.event_stops=1 --set test.risk_pct=6 ...

    python3 tools/breakout_events.py [--data ~/backtest-data] [--out events.csv]

Rule, on completed 5-minute bars, using only data available at each close:
  trigger   a close above the highest high of the 72 bars before it (6 hours),
            crossing it fresh (the previous close was not above its own 6h high);
            the level is frozen at that 6h high
  volume    last 3 bars' quote volume >= 3x the median of non-overlapping 3-bar
            blocks over the 7 days before (current block excluded)
  liquidity 24h quote volume >= 10M USDT (the bot's board minimum)
  strength  1h log return minus the eligible universe's median > 0, and in the
            universe's top quarter (at the trigger and again at entry)
  confirm   at least 2 of the next 3 closes above the level
  no chase  entry close <= level + 1 hourly ATR (14 hourly true ranges, from
            rolling 12-bar blocks ending before the trigger bar; simple mean)
  invalid   any close below level - ATR ends the candidate
  expiry    entry must come within 30 minutes (6 bars) of the trigger
  stop      level - ATR, fixed; written as stop_pct below the entry close
  cooldown  a new candidate on the same coin only 180 minutes after the last ended
  rank      candidates entering at the same check: mean percentile rank of
            volume ratio and strength, ties by lower extension then symbol
"""

from __future__ import annotations

import argparse
import csv
import pickle
from pathlib import Path

import numpy as np

B = 300_000
DAY = 288
WEEK = 7 * DAY
LOOK = 72
EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}
MIN_QV = 10_000_000
RV_MIN = 3.0
EXPIRY = 6
COOLDOWN = 36


def symbols(data: Path) -> list:
    return [p for p in sorted((data / "5m").glob("*.pkl"))
            if p.stem.endswith("USDT") and p.stem[:-4] not in EXCLUDE_BASES]


def load(p: Path):
    """One coin's candles, or None when it has too little history."""
    T, O, H, L, C, Q = pickle.load(open(p, "rb"))
    if len(T) < WEEK + LOOK + 50:
        return None
    return (np.asarray(T, dtype=np.int64),) + tuple(np.asarray(x, dtype=np.float64)
                                                    for x in (O, H, L, C, Q))


def basics(T, C, Q, t0):
    """Grid index, 1h log return and eligibility of every bar."""
    qv24 = rolling_sum(Q, DAY)
    r1 = np.full(len(C), np.nan)
    with np.errstate(divide="ignore", invalid="ignore"):
        r1[12:] = np.log(C[12:] / C[:-12])
    elig = (qv24 >= MIN_QV) & (np.arange(len(C)) >= WEEK + LOOK) & np.isfinite(r1)
    return (T - t0) // B, r1, elig


def rolling_sum(x, n):
    c = np.concatenate(([0.0], np.cumsum(x)))
    out = np.full(len(x), np.nan)
    out[n - 1:] = c[n:] - c[:-n]
    return out


def rolling_max_prev(h, n):
    """max of h[i-n .. i-1] for each i (nan before n bars)."""
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(h), np.nan)
    if len(h) > n:
        out[n:] = sliding_window_view(h, n).max(axis=1)[:-1]
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(Path.home() / "backtest-data"))
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    data = Path(a.data)
    out_path = Path(a.out) if a.out else data / "events" / "breakout_challenger.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    files = symbols(data)
    span = {}
    for p in files:
        T = pickle.load(open(p, "rb"))[0]
        if len(T) >= WEEK + LOOK + 50:
            span[p] = (T[0], T[-1])
    files = list(span)
    print(f"{len(files)} coins")
    t0 = min(v[0] for v in span.values())
    t1 = max(v[1] for v in span.values())
    n_grid = (t1 - t0) // B + 1

    R = np.full((len(files), n_grid), np.nan, dtype=np.float32)   # eligible 1h log returns
    for row, p in enumerate(files):
        T, O, H, L, C, Q = load(p)
        g, r1, elig = basics(T, C, Q, t0)
        R[row, g[elig]] = r1[elig]
    print("cross-section quantiles ...")
    q50 = np.full(n_grid, np.nan)
    q75 = np.full(n_grid, np.nan)
    step = 4000
    import warnings
    for c in range(0, n_grid, step):
        blk = R[:, c:c + step]
        ok = np.isfinite(blk).sum(axis=0) >= 20
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            q = np.nanpercentile(blk, [50, 75], axis=0)
        q50[c:c + step] = np.where(ok, q[0], np.nan)
        q75[c:c + step] = np.where(ok, q[1], np.nan)
    del R

    def strong(g, r1, elig, i):
        if not elig[i]:
            return None
        m, h = q50[g[i]], q75[g[i]]
        if not (np.isfinite(m) and np.isfinite(h)):
            return None
        rs = r1[i] - m
        return rs if (rs > 0 and r1[i] >= h) else None

    events = []
    stats = dict(triggers=0, rv=0, rs=0, confirmed=0, entered=0)
    for p in files:
        sym = p.stem
        T, O, H, L, C, Q = load(p)
        g, r1, elig = basics(T, C, Q, t0)
        ref, s3 = rolling_max_prev(H, LOOK), rolling_sum(Q, 3)
        n = len(C)
        above = C > ref
        fresh = np.zeros(n, dtype=bool)
        fresh[1:] = above[1:] & ~above[:-1] & np.isfinite(ref[1:]) & np.isfinite(ref[:-1])
        cand = np.nonzero(fresh & elig)[0]
        free_from = 0
        for i in cand:
            if i < free_from or i + EXPIRY >= n:
                continue
            stats["triggers"] += 1
            idx = np.arange(i - 3, i - 3 * 672 - 1, -3)
            idx = idx[idx >= 2]
            if len(idx) < 300:
                continue
            base = np.median(s3[idx])
            if not base > 0:
                continue
            rv = s3[i] / base
            if rv < RV_MIN:
                continue
            stats["rv"] += 1
            if strong(g, r1, elig, i) is None:
                continue
            stats["rs"] += 1
            level = ref[i]
            # 14 hourly true ranges from 12-bar blocks ending at bar i-1
            trs = []
            for h in range(14, 0, -1):
                a, b = i - 12 * h, i - 12 * h + 12          # bars a..b-1
                if a - 1 < 0:
                    break
                pc = C[a - 1]
                trs.append(max(H[a:b].max(), pc) - min(L[a:b].min(), pc))
            if len(trs) < 14:
                continue
            atr = sum(trs) / 14
            end = i + EXPIRY
            above_n, conf_at, entry = 0, None, None
            for k in range(i + 1, end + 1):
                if C[k] < level - atr:
                    break                                 # invalidated
                if k <= i + 3 and C[k] > level:
                    above_n += 1
                    if above_n == 2:
                        conf_at = k
                if conf_at is None:
                    if k >= i + 3:
                        break                             # not confirmed in 3 closes
                    continue
                if C[k] > level and C[k] <= level + atr and k + 1 < n:
                    rs = strong(g, r1, elig, k)
                    if rs is not None:
                        entry = (k, rs)
                        break
            if conf_at is not None:
                stats["confirmed"] += 1
            free_from = (entry[0] if entry else min(end, n - 1)) + COOLDOWN
            if not entry:
                continue
            k, rs = entry
            stop = level - atr
            if stop <= 0:
                continue
            stats["entered"] += 1
            events.append(dict(t=int(T[k]) + B, symbol=sym, trigger_t=int(T[i]) + B,
                               level=float(level), atr=float(atr), rv=round(float(rv), 3),
                               rs=round(float(rs), 5),
                               ext=round(float(C[k] / level - 1), 5),
                               stop_pct=round(float((C[k] - stop) / C[k] * 100), 3)))
    # rank within each check time
    by_t = {}
    for e in events:
        by_t.setdefault(e["t"], []).append(e)
    for t, es in by_t.items():
        def pct(key):
            vals = sorted(x[key] for x in es)
            return {id(x): (sum(v < x[key] for v in vals) + 0.5 * (sum(v == x[key] for v in vals) - 1))
                    / max(1, len(vals) - 1) if len(vals) > 1 else 1.0 for x in es}
        prv, prs = pct("rv"), pct("rs")
        es.sort(key=lambda x: (-(prv[id(x)] + prs[id(x)]) / 2, x["ext"], x["symbol"]))
        for r, x in enumerate(es, 1):
            x["rank"] = r
    events.sort(key=lambda x: (x["t"], x["rank"]))
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["t", "symbol", "rank", "stop_pct", "trigger_t",
                                          "level", "atr", "rv", "rs", "ext"])
        w.writeheader()
        w.writerows(events)
    print(stats)
    print(f"{len(events)} events -> {out_path}")
    if events:
        sp = sorted(e["stop_pct"] for e in events)
        print("stop % quartiles", sp[len(sp) // 4], sp[len(sp) // 2], sp[3 * len(sp) // 4])


if __name__ == "__main__":
    main()
