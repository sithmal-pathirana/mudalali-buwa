"""
Whole-market early-runner study (2026-10-10), to the outside review's brief.
Question: among all eligible contracts at each hour, does open-interest data
add information about which ones make a tradable run?

Everything below was fixed BEFORE looking at any result:
  candidates  every USDT perpetual at every hour close with 24h quote volume
              >= 10M and >= 7 days of history; ANY funding sign, any rank,
              account capacity ignored
  label       entry at the open of the next bar; WIN if +50% is reached before
              -20% within 72h, STOP if -20% first (both in one 5-minute bar =
              STOP, the conservative order), else EXPIRY at the 72h close
  return      standardized trade: +50% / -21% (1% stop slippage) / expiry
              return, minus 0.20% costs; funding reported separately
  base model  returns 15m/1h/4h/24h, 1h volume vs its 7-day hourly median,
              24h realized volatility, last known funding (per 8h), 24h rank
              (as a fraction), age
  OI model    base + OI quantity change 1h and 4h, and 24h quote volume /
              OI value; OI is the last snapshot at least 15 minutes old
  both models are fitted and scored on the SAME rows (OI available)
  model       L2 logistic regression (C=1), standardized on training rows,
              walk-forward: refit monthly on rows whose 72h label ended
              >= 3 days before the month, scored on that month
  report      AUC; per hour the top-1 and top-3 candidates by score: win and
              stop rates and standardized net return, against the same for
              "highest 24h change", "most negative funding" and all candidates;
              lead time to first 24h #1 for winning picks

    python3 tools/early_runner_study.py [--data ~/backtest-data] [--build-only]
"""

from __future__ import annotations

import argparse
import bisect
import json
import pickle
import time
from pathlib import Path

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

B = 300_000
H = 3_600_000
D = 86_400_000
DAY = 288
WIN, STOP, HOLD = 0.50, 0.20, 72 * 12     # barriers and 72h in bars
SLIP, COST = 0.01, 0.002
OI_LAG = 15 * 60_000
MIN_QV = 10_000_000
EXCLUDE = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}
BASE_F = ["r15", "r1h", "r4h", "r24h", "volx", "rv24", "fund", "rank", "age"]
OI_F = BASE_F + ["oi1h", "oi4h", "turn"]


def roll_sum(x, n):
    c = np.concatenate(([0.0], np.cumsum(x)))
    out = np.full(len(x), np.nan)
    out[n - 1:] = c[n:] - c[:-n]
    return out


def coin_rows(sym, d, fund, oi):
    T, O, Hh, L, C, Q = (np.asarray(x, dtype=np.int64 if k == 0 else np.float64)
                         for k, x in enumerate(d))
    n = len(T)
    if n < 7 * DAY + HOLD:
        return None
    close_t = T + B
    hours = np.nonzero(close_t % H == 0)[0]
    hours = hours[(hours >= 7 * DAY) & (hours + 1 + HOLD < n)]
    qv24 = roll_sum(Q, DAY)
    hours = hours[qv24[hours] >= MIN_QV]
    if not len(hours):
        return None
    lc = np.log(np.maximum(C, 1e-12))
    lr = np.diff(lc, prepend=lc[0])
    s1, s2 = roll_sum(lr, DAY), roll_sum(lr * lr, DAY)
    rv24 = np.sqrt(np.maximum(0.0, s2 / DAY - (s1 / DAY) ** 2))
    q12 = roll_sum(Q, 12)
    # 1h volume vs the median of the 168 hourly volumes before it (same phase)
    volx = np.full(len(hours), np.nan)
    for j, i in enumerate(hours):
        past = q12[i - 12 * np.arange(1, 169)]
        past = past[np.isfinite(past)]
        if len(past) >= 100:
            m = np.median(past)
            if m > 0:
                volx[j] = q12[i] / m
    i = hours
    rows = dict(t=close_t[i], r15=lc[i] - lc[i - 3], r1h=lc[i] - lc[i - 12],
                r4h=lc[i] - lc[i - 48], r24h=lc[i] - lc[i - DAY], volx=volx, rv24=rv24[i],
                age=np.minimum(365.0, (T[i] - T[0]) / D), qv24=qv24[i])
    # funding: last settlement at or before t (% per 8h)
    fu = np.full(len(i), np.nan)
    if fund:
        FT, FR = np.asarray(fund[0], dtype=np.int64), np.asarray(fund[1], dtype=np.float64)
        k = np.searchsorted(FT, rows["t"], side="right") - 1
        ok = k >= 0
        fu[ok] = FR[k[ok]]
    rows["fund"] = fu
    # open interest: last snapshot at least OI_LAG old
    o1 = np.full(len(i), np.nan)
    o4 = np.full(len(i), np.nan)
    turn = np.full(len(i), np.nan)
    if oi is not None and len(oi[0]):
        OT, OQ, OV = oi

        def at(ts):
            k = np.searchsorted(OT, ts, side="right") - 1
            ok = (k >= 0) & (ts - OT[np.maximum(k, 0)] <= 30 * 60_000)   # not stale
            return np.where(ok, OQ[np.maximum(k, 0)], np.nan), \
                np.where(ok, OV[np.maximum(k, 0)], np.nan)
        q0, v0 = at(rows["t"] - OI_LAG)
        q1, _ = at(rows["t"] - OI_LAG - H)
        q4, _ = at(rows["t"] - OI_LAG - 4 * H)
        with np.errstate(divide="ignore", invalid="ignore"):
            o1 = np.log(q0 / q1)
            o4 = np.log(q0 / q4)
            turn = rows["qv24"] / v0
        for a in (o1, o4, turn):
            a[~np.isfinite(a)] = np.nan
    rows.update(oi1h=o1, oi4h=o4, turn=turn)
    # labels: entry at the open of bar i+1
    e = O[i + 1]
    hi_max = sliding_window_view(Hh[1:], HOLD).max(axis=1)      # max of H[k+1 .. k+HOLD]
    lo_min = sliding_window_view(L[1:], HOLD).min(axis=1)
    hm, lm = hi_max[i], lo_min[i]
    out = np.zeros(len(i), dtype=np.int8)
    ret = np.zeros(len(i))
    xt = np.zeros(len(i), dtype=np.int64)
    for j in range(len(i)):
        ii, ej = i[j], e[j]
        up, dn = ej * (1 + WIN), ej * (1 - STOP)
        last = ii + HOLD
        if hm[j] < up and lm[j] > dn:
            out[j], ret[j], xt[j] = 0, C[last] / ej - 1, close_t[last]
            continue
        hs, ls = Hh[ii + 1:last + 1], L[ii + 1:last + 1]
        ku = int(np.argmax(hs >= up)) if hm[j] >= up else HOLD
        kd = int(np.argmax(ls <= dn)) if lm[j] <= dn else HOLD
        if kd <= ku:
            out[j], ret[j], xt[j] = -1, -STOP - SLIP, close_t[ii + 1 + kd]
        else:
            out[j], ret[j], xt[j] = 1, WIN, close_t[ii + 1 + ku]
    rows.update(out=out, ret=ret - COST, xt=xt, sym=np.array([sym] * len(i)))
    # funding received by a long over the standardized hold (fraction of entry notional)
    fr = np.zeros(len(i))
    if fund:
        FT, FR = np.asarray(fund[0], dtype=np.int64), np.asarray(fund[1], dtype=np.float64)
        a = np.searchsorted(FT, rows["t"], side="right")
        b = np.searchsorted(FT, xt, side="right")
        # each settlement pays its own interval's share of the per-8h rate
        gap = np.diff(FT, prepend=FT[0] - 8 * H) / H
        gap = np.clip(np.round(gap), 1, 8)
        cs = np.concatenate(([0.0], np.cumsum(FR * gap / 8)))
        fr = -(cs[b] - cs[a]) / 100            # on entry notional (not scaled by price)
    rows["fund_paid"] = fr
    return rows


def build(data: Path, cache: Path, only=None):
    fund_all = pickle.load(open(data / "funding.pkl", "rb"))
    parts = []
    files = sorted(p for p in (data / "5m").glob("*.pkl")
                   if p.stem.endswith("USDT") and p.stem[:-4] not in EXCLUDE)
    if only:
        files = [p for p in files if p.stem in only]
    t0 = time.time()
    for n, p in enumerate(files):
        sym = p.stem
        oi = None
        mp = data / "metrics_all" / f"{sym}.pkl"
        if mp.exists():
            st = pickle.load(open(mp, "rb"))
            ks = sorted(k for k, v in st["rows"].items() if v and v[0] is not None and v[0] > 0)
            if ks:
                oi = (np.array(ks, dtype=np.int64),
                      np.array([st["rows"][k][0] for k in ks]),
                      np.array([st["rows"][k][1] or np.nan for k in ks]))
        r = coin_rows(sym, pickle.load(open(p, "rb")), fund_all.get(sym), oi)
        if r:
            parts.append(r)
        if n % 50 == 49:
            print(f"  {n + 1}/{len(files)} coins, {time.time() - t0:.0f}s", flush=True)
    rows = {k: np.concatenate([r[k] for r in parts]) for k in parts[0]}
    # cross-sectional 24h rank at each hour (1 = top gainer), as a fraction
    order = np.lexsort((-rows["r24h"], rows["t"]))
    t_sorted = rows["t"][order]
    starts = np.r_[0, np.nonzero(np.diff(t_sorted))[0] + 1]
    rank = np.empty(len(order))
    pos = np.arange(len(order))
    grp_start = np.repeat(starts, np.diff(np.r_[starts, len(order)]))
    rank[order] = pos - grp_start + 1
    size = np.empty(len(order))
    size[order] = np.repeat(np.diff(np.r_[starts, len(order)]), np.diff(np.r_[starts, len(order)]))
    rows["rank1"] = rank
    rows["rank"] = (rank - 1) / np.maximum(1, size - 1)
    np.savez_compressed(cache, **rows)
    return rows


def auc(y, s):
    o = np.argsort(s)
    r = np.empty(len(s))
    r[o] = np.arange(1, len(s) + 1)
    pos = y == 1
    n1, n0 = pos.sum(), (~pos).sum()
    return (r[pos].sum() - n1 * (n1 + 1) / 2) / max(1, n1 * n0)


def topk(rows, mask, score, k):
    """Per hour, the k best-scored rows among mask: their indices."""
    idx = np.nonzero(mask)[0]
    o = idx[np.lexsort((-score[idx], rows["t"][idx]))]
    t = rows["t"][o]
    starts = np.r_[0, np.nonzero(np.diff(t))[0] + 1]
    pos = np.arange(len(o)) - np.repeat(starts, np.diff(np.r_[starts, len(o)]))
    return o[pos < k]


def summarize(rows, sel):
    out, ret, fp = rows["out"][sel], rows["ret"][sel], rows["fund_paid"][sel]
    return dict(n=int(len(sel)), win=round(float((out == 1).mean() * 100), 2),
                stop=round(float((out == -1).mean() * 100), 2),
                net_ret=round(float(ret.mean() * 100), 3),
                net_ret_with_funding=round(float((ret + fp).mean() * 100), 3))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(Path.home() / "backtest-data"))
    ap.add_argument("--build-only", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--only", default="", help="comma-separated symbols (testing)")
    ap.add_argument("--out", default="early_runner")
    a = ap.parse_args()
    data = Path(a.data)
    out_dir = data / "reports" / a.out
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = out_dir / "rows.npz"
    if cache.exists() and not a.rebuild:
        z = np.load(cache, allow_pickle=True)
        rows = {k: z[k] for k in z.files}
    else:
        rows = build(data, cache, set(a.only.split(",")) if a.only else None)
    n = len(rows["t"])
    has_oi = np.isfinite(rows["oi1h"]) & np.isfinite(rows["oi4h"]) & np.isfinite(rows["turn"])
    base_ok = np.all([np.isfinite(rows[f]) for f in BASE_F], axis=0)
    mask = has_oi & base_ok
    print(f"{n:,} candidate-hours; {mask.sum():,} with every feature incl. OI "
          f"({100 * mask.mean():.1f}%); wins {int((rows['out'][mask] == 1).sum()):,}")
    if a.build_only:
        return
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler

    y = (rows["out"] == 1).astype(int)
    t = rows["t"]
    months = sorted({(int(x) // (30 * D)) for x in t[mask]})
    first_t = t[mask].min()
    scores = {"base": np.full(n, np.nan), "oi": np.full(n, np.nan)}
    for m in months:
        lo, hi = m * 30 * D, (m + 1) * 30 * D
        if lo < first_t + 150 * D:
            continue                                  # at least ~5 months of training
        train = mask & (rows["xt"] <= lo - 3 * D)
        test = mask & (t >= lo) & (t < hi)
        if test.sum() == 0 or y[train].sum() < 50:
            continue
        for name, feats in (("base", BASE_F), ("oi", OI_F)):
            X = np.column_stack([rows[f] for f in feats])
            sc = StandardScaler().fit(X[train])
            clf = LogisticRegression(C=1.0, max_iter=500).fit(sc.transform(X[train]), y[train])
            scores[name][test] = clf.predict_proba(sc.transform(X[test]))[:, 1]
        print(f"  month {time.strftime('%Y-%m', time.gmtime(lo / 1000))}: train {train.sum():,} "
              f"test {test.sum():,}", flush=True)
    scored = mask & np.isfinite(scores["base"]) & np.isfinite(scores["oi"])
    split = int(np.median(t[scored]))
    report = dict(candidate_hours=int(n), with_oi=int(mask.sum()), scored=int(scored.sum()),
                  split_utc=time.strftime("%Y-%m-%d", time.gmtime(split / 1000)), periods={})
    for pname, pm in (("all", scored), ("first_half", scored & (t < split)),
                      ("second_half", scored & (t >= split))):
        rep = dict(auc_base=round(float(auc(y[pm], scores["base"][pm])), 4),
                   auc_oi=round(float(auc(y[pm], scores["oi"][pm])), 4),
                   all_candidates=summarize(rows, np.nonzero(pm)[0]))
        for k in (1, 3):
            rep[f"top{k}_base"] = summarize(rows, topk(rows, pm, scores["base"], k))
            rep[f"top{k}_oi"] = summarize(rows, topk(rows, pm, scores["oi"], k))
            rep[f"top{k}_highest_24h"] = summarize(rows, topk(rows, pm, rows["r24h"], k))
            rep[f"top{k}_most_negative_funding"] = summarize(rows, topk(rows, pm, -rows["fund"], k))
        report["periods"][pname] = rep
    # lead time of the OI model's winning top-1 picks to the coin's first 24h #1 after it
    sel = topk(rows, scored, scores["oi"], 1)
    wins = sel[rows["out"][sel] == 1]
    first1 = {}
    is1 = np.nonzero(rows["rank1"] == 1)[0]
    for j in is1:
        first1.setdefault(rows["sym"][j], []).append(int(rows["t"][j]))
    for v in first1.values():
        v.sort()
    leads, already = [], []
    for j in wins:
        ts = first1.get(rows["sym"][j], [])
        k = bisect.bisect_left(ts, int(rows["t"][j]))
        if k < len(ts) and ts[k] - rows["t"][j] <= 7 * D:
            leads.append((ts[k] - rows["t"][j]) / H)
        already.append(float(np.expm1(rows["r24h"][j]) * 100))
    report["oi_top1_winners"] = dict(
        n=int(len(wins)), became_no1_within_7d=len(leads),
        median_lead_hours=round(float(np.median(leads)), 1) if leads else None,
        median_24h_rise_at_signal_pct=round(float(np.median(already)), 1) if already else None)
    json.dump(report, open(out_dir / "report.json", "w"), indent=1)
    print(json.dumps(report, indent=1))


if __name__ == "__main__":
    main()
