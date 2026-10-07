"""
Score every new leader with a classifier trained only on earlier leaders.

    python3 -m tools.backtest_gainer export --config CONFIG --csv events.csv
    python3 tools/backtest_ml.py events.csv --out DIR

The classifier sees the measurements taken when a coin became #1 (RSI, ATR,
pump checks, rush-order checks, funding, BTC ...) and learns whether buying
it with these exits made money. It never sees a leader's own future:

  fixed         trained on year 1 only; year 2 is scored by that one model
  walkforward   retrained every 30 days on everything before (minus a 3-day
                gap, so one pump's measurements cannot leak into its test)

Year 1 itself gets out-of-fold scores (five consecutive blocks, each scored by
a model trained on the other four with the same gap), which are NOT a fair
test -- only year 2 is. Writes scores-<model>-<mode>.csv for the backtest's
test.ml_scores, and a grid file of score thresholds taken from year 1 alone.

Needs numpy and scikit-learn; the bot itself does not.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.pipeline import make_pipeline

D = 86_400_000
GAP = 3 * D
FEATURES = ["price", "rsi", "atr", "age", "btc24", "btc1", "prior_vol", "vol_surge",
            "share_1h", "share_4h", "below_high", "upper_wick", "vol_trend",
            "prior_pump", "funding", "buy_share_1h", "trades_surge", "trade_size_x"]


def models():
    return {
        "gbm": lambda: HistGradientBoostingClassifier(
            max_depth=3, learning_rate=0.05, max_iter=300, min_samples_leaf=50,
            l2_regularization=1.0, random_state=1),
        "forest": lambda: make_pipeline(
            SimpleImputer(strategy="median"),
            RandomForestClassifier(n_estimators=300, min_samples_leaf=40, max_features=0.5,
                                   n_jobs=1, random_state=1)),
    }


def load(path):
    rows = list(csv.DictReader(open(path)))
    t = np.array([int(r["t"]) for r in rows])
    order = np.argsort(t, kind="stable")
    rows = [rows[i] for i in order]
    t = t[order]
    X = np.array([[float(r[k]) if r[k] not in ("", None) else np.nan for k in FEATURES]
                  for r in rows])
    X[:, 0] = np.log10(np.maximum(X[:, 0], 1e-9))             # price spans 1e-6 .. 1e5
    X[:, 6] = np.log10(np.maximum(np.nan_to_num(X[:, 6], nan=1.0), 1.0))
    ret = np.array([float(r["ret"]) for r in rows])
    return rows, t, X, ret


def fit_predict(make, Xtr, ytr, Xte):
    m = make()
    m.fit(Xtr, ytr)
    return m.predict_proba(Xte)[:, 1]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("events")
    ap.add_argument("--out", required=True)
    ap.add_argument("--label", choices=("win", "big"), default="win",
                    help="win: the trade made money; big: it made more than 10%%")
    a = ap.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    rows, t, X, ret = load(a.events)
    y = (ret > (0.10 if a.label == "big" else 0.0)).astype(int)
    mid = t[0] + 365 * D
    y1 = t < mid
    print(f"{len(t)} leaders: year 1 {y1.sum()} ({y[y1].mean():.0%} winners), "
          f"year 2 {(~y1).sum()} ({y[~y1].mean():.0%} winners)")

    for name, make in models().items():
        # out-of-fold scores inside year 1 (consecutive blocks, with a gap)
        s1 = np.full(y1.sum(), np.nan)
        idx = np.where(y1)[0]
        for block in np.array_split(idx, 5):
            lo, hi = t[block[0]], t[block[-1]]
            train = idx[(t[idx] < lo - GAP) | (t[idx] > hi + GAP)]
            s1[block - idx[0]] = fit_predict(make, X[train], y[train], X[block])
        fixed = fit_predict(make, X[y1], y[y1], X[~y1])
        walk = np.full((~y1).sum(), np.nan)
        start = mid
        i2 = np.where(~y1)[0]
        while start <= t[-1]:
            test = i2[(t[i2] >= start) & (t[i2] < start + 30 * D)]
            train = np.where(t < start - GAP)[0]
            if len(test):
                walk[test - i2[0]] = fit_predict(make, X[train], y[train], X[test])
            start += 30 * D
        auc1 = roc_auc_score(y[y1], s1)
        print(f"\n{name}: year-1 out-of-fold AUC {auc1:.3f} (not a fair test)")
        for mode, s2 in (("fixed", fixed), ("walkforward", walk)):
            print(f"  {mode:11s} year-2 AUC {roc_auc_score(y[~y1], s2):.3f}  "
                  f"(0.5 = no better than chance)")
            q = np.quantile(s2, [0.2, 0.4, 0.6, 0.8])
            for lo_q, hi_q in zip([-1] + list(q), list(q) + [2]):
                m = (s2 > lo_q) & (s2 <= hi_q)
                print(f"     year-2 score {max(lo_q, 0):.3f}-{min(hi_q, 1):.3f}: "
                      f"{m.sum():5d} leaders, win {y[~y1][m].mean():.0%}, "
                      f"avg return {ret[~y1][m].mean() * 100:+.2f}%")
            path = out / f"scores-{a.label}-{name}-{mode}.csv"
            with open(path, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["symbol", "t", "score"])
                for r, sc in zip(rows, np.concatenate([s1, s2])):
                    w.writerow([r["symbol"], r["t"], f"{sc:.6f}"])
            # thresholds from YEAR 1 only: skip the lowest 20/40/60% of year-1 scores
            th = [round(float(v), 6) for v in np.quantile(s1, [0.2, 0.4, 0.6])]
            (out / f"grid-{a.label}-{name}-{mode}.yaml").write_text(
                f"set:\n  test.ml_scores: {path}\nvary:\n  test.min_ml_score: {th}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
