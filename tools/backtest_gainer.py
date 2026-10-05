"""
Replay the gainer strategy on stored 5-minute candles and write a report.

Change a setting, run it, read the report:

    python3 -m tools.backtest_gainer run
    python3 -m tools.backtest_gainer run --set gainer.filters.max_rsi_1h=85
    python3 -m tools.backtest_gainer run --grid my-grid.yaml
    python3 -m tools.backtest_gainer search          # tools/backtest_space.yaml
    python3 -m tools.backtest_gainer merge out1/results.json out2/results.json --out merged

Each run writes report.md (tables written to be read by a person or an AI),
results.csv (one row per run) and results.json (every number, month by
month) under <data>/reports/.

`search` covers every setting in tools/backtest_space.yaml in two rounds:
round 1 changes one setting at a time over each listed value; round 2 runs
every combination of the round-1 values that beat live in both years.

Every run starts from config.yaml (or --config), the file the bot itself
reads, and the settings are built into the bot's own GainerConfig and
RiskConfig, so a bad key fails here exactly as it would at boot. The live
settings are always replayed as well, as the baseline every run is compared
with.

A grid file names the settings to vary; every combination is run:

    vary:
      gainer.filters.max_rsi_1h: [0, 80, 85]
      gainer.exit.ladder_step_pct: [5, 10]
    set:                        # optional: applied to every run
      gainer.exit.target_pct: 95
    runs:                       # optional: named sets, each crossed with vary
      - name: listed 3d + BTC guard
        set: {gainer.filters.min_listing_age_days: 3,
              gainer.filters.btc_min_change_24h_pct: -2}
    deposits: {start: 50, monthly: 50}

For `run`, --shard K/N runs every Nth combination, so N machines with the same data
can split one grid; `merge` joins their results.json files into one report.

What is replayed, per 5-minute step over every coin in the data (delisted
coins included, so there is no survivorship bias): the gainers board
(board.*), the leader checks and confirmation (leader_check_minutes,
confirm_minutes), the new-leader filters (filters.*), the entry guards
(rebuy cooldown, buy_only_if_rising) with the bot's retry while the coin
stays #1, sizing (entry.*, sizing.*), limits (limits.*), the engine's
leverage ceiling, equity floor and daily caps (risk.*), exits (stop, ATR
stop, take-profit, partial take-profit, ladder lock/trail, unarmed time
limit, on_new_leader close_if_losing), and the Funding sweep and principal
recovery (sweep.*) with deposits of $start + $monthly every 30 days.
Settings the replay cannot reproduce are listed in the report.

Nothing here talks to Binance or touches the running bot.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import datetime as dt
import hashlib
import itertools
import json
import math
import os
import pickle
import sys
import time
from dataclasses import asdict
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import RiskConfig                                    # noqa: E402
from bot.gainer import (EXCLUDE_BASES, GainerConfig, atr_pct,       # noqa: E402
                        ladder_stop, rsi)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA = Path(os.environ.get("BACKTEST_DATA", Path.home() / "backtest-data"))
VERSION = 2                     # bump when a change alters replay results
FEATS_VERSION = 3               # bump when the measurements at a leader change

#: Ideas the bot does not have yet, tried in the replay only (set as test.*).
#:   when_full: what a new leader does when every slot is taken.
#:     refuse          the bot's behaviour: it is not bought
#:     replace_oldest  sell the position held longest, buy the new leader
#:     replace_worst   sell the position doing worst right now, buy it
#:     replace_losing  as replace_worst, but only if that position is losing
#:   replace_min_hold_hours: a position younger than this is never sold for room.
#:   Pump-and-dump checks on a new leader (skip it when the check fails; 0 = off):
#:   max_volume_surge_x       24h volume over its normal day (avg of the 7 days before)
#:   min_volume_surge_x       ... the opposite: a real move needs real volume
#:   min_prior_volume_usdt    normal daily volume before today (thin coins are pumped)
#:   max_rise_share_1h / 4h   share of the 24h rise made in the last 1h / 4h (vertical)
#:   max_upper_wick           last hour's upper wick, 0-1 of its range (sellers)
#:   min_volume_trend         last hour's volume over the 6 hours before (exhaustion)
#:   max_below_high_pct       already this far under its 24h high (dump started)
#:   max_funding_pct          funding per 8h, % (crowded longs)
#:   max_prior_pump_pct       biggest 24h rise in the 30 days before (repeat pumps)
TEST_DEFAULTS = {"when_full": "refuse", "replace_min_hold_hours": 0.0,
                 "max_volume_surge_x": 0.0, "min_volume_surge_x": 0.0,
                 "min_prior_volume_usdt": 0.0, "max_rise_share_1h": 0.0,
                 "max_rise_share_4h": 0.0, "max_upper_wick": 0.0,
                 "min_volume_trend": 0.0, "max_below_high_pct": 0.0,
                 "max_funding_pct": 0.0, "max_prior_pump_pct": 0.0,
                 # rush orders (data.binance.vision: trades and taker-buy volume)
                 "min_buy_share_1h": 0.0, "max_buy_share_1h": 0.0,
                 "min_trades_surge_x": 0.0, "max_trades_surge_x": 0.0,
                 "min_trade_size_x": 0.0, "max_trade_size_x": 0.0,
                 # a classifier's score per leader (tools/backtest_ml.py)
                 "ml_scores": "", "min_ml_score": 0.0}
#: test setting -> (feature, skip when the feature is ABOVE the limit?)
PUMP_CHECKS = [("max_volume_surge_x", "vol_surge", True),
               ("min_volume_surge_x", "vol_surge", False),
               ("min_prior_volume_usdt", "prior_vol", False),
               ("max_rise_share_1h", "share_1h", True),
               ("max_rise_share_4h", "share_4h", True),
               ("max_upper_wick", "upper_wick", True),
               ("min_volume_trend", "vol_trend", False),
               ("max_below_high_pct", "below_high", True),
               ("max_funding_pct", "funding", True),
               ("max_prior_pump_pct", "prior_pump", True),
               ("min_buy_share_1h", "buy_share_1h", False),
               ("max_buy_share_1h", "buy_share_1h", True),
               ("min_trades_surge_x", "trades_surge", False),
               ("max_trades_surge_x", "trades_surge", True),
               ("min_trade_size_x", "trade_size_x", False),
               ("max_trade_size_x", "trade_size_x", True)]
WHEN_FULL = ("refuse", "replace_oldest", "replace_worst", "replace_losing")

B = 300_000                     # one 5-minute bar, ms
DAY_BARS = 288
H = 3_600_000
D = 86_400_000
MONTH = 30 * D
MIN_ORDER_USDT = 5.0            # Binance's minimum notional

#: Settings the replay does not reproduce. A run that changes one of these
#: is flagged in the report.
NOT_REPLAYED = {
    "gainer.enabled": "the replay always trades the gainer",
    "gainer.dry_run": "no effect on a replay",
    "gainer.board.poll_seconds": "the data is 5-minute candles; every check is on a 5-minute close",
    "gainer.board.top_n": "display only",
    "gainer.entry.trade_on_start": "the first leader in the data is a baseline",
    "gainer.entry.reentry_on_new_high": "re-entry above the 24h high is not replayed",
    "gainer.exit.target_usd": "replayed only through target_pct; a dollar target is not",
    "gainer.sweep.deposits_since": "deposits come from the replay's deposit schedule",
    "gainer.sweep.deposits_override_usdt": "deposits come from the replay's deposit schedule",
}
IGNORED_PREFIXES = ("gainer.alerts.", "gainer.forecast.history_minutes",
                    "gainer.forecast.predict_minutes", "gainer.forecast.trend_eps")


# ================================================================ settings
def parse_value(text: str):
    """--set values are YAML: 85, 0.5, null, true, lock, [1, 2]."""
    return yaml.safe_load(text)


def set_path(raw: dict, key: str, value) -> None:
    node = raw
    parts = key.split(".")
    for p in parts[:-1]:
        if not isinstance(node.get(p), dict):
            node[p] = {}
        node = node[p]
    node[parts[-1]] = value


def flatten(obj, prefix: str = "") -> dict:
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(flatten(v, key + "."))
        else:
            out[key] = v
    return out


class Settings:
    """One run's settings, built by the bot's own config classes."""

    def __init__(self, base_raw: dict, overrides: dict):
        raw = copy.deepcopy(base_raw)
        self.test = dict(TEST_DEFAULTS)
        for k, v in overrides.items():
            if k.startswith("test."):
                name = k[5:]
                if name not in TEST_DEFAULTS:
                    raise SystemExit(f"unknown test setting {k}; known: "
                                     + ", ".join("test." + n for n in TEST_DEFAULTS))
                self.test[name] = v
                continue
            if not (k.startswith("gainer.") or k.startswith("risk.")):
                raise SystemExit(f"--set {k}: only gainer.*, risk.* and test.* settings "
                                 f"are replayed")
            set_path(raw, k, v)
        if self.test["when_full"] not in WHEN_FULL:
            raise SystemExit(f"test.when_full must be one of {', '.join(WHEN_FULL)}")
        try:
            self.g = GainerConfig(**copy.deepcopy(raw.get("gainer") or {}))
            self.risk = RiskConfig(**copy.deepcopy(raw.get("risk") or {}))
        except (TypeError, ValueError) as e:
            raise SystemExit(f"bad setting: {e}") from None
        self.overrides = dict(overrides)
        self.flat = {**flatten(asdict(self.g), "gainer."),
                     **flatten(asdict(self.risk), "risk."),
                     **{f"test.{k}": v for k, v in self.test.items()}}
        for k in overrides:
            if k not in self.flat:
                raise SystemExit(f"unknown setting {k}")

    # keys for the caches: only what each stage depends on
    def board_key(self) -> tuple:
        b = self.g.board
        if b.rank_by == "climb":
            return ("climb", float(b.min_quote_volume), bars_for(b.climb_minutes),
                    float(b.climb_volume_surge_x))
        return ("change_24h", float(b.min_quote_volume))

    def check_ms(self) -> int:
        return max(1, bars_for(self.g.board.leader_check_minutes)) * B

    def stream_key(self) -> tuple:
        return self.board_key() + (self.check_ms(), float(self.g.entry.confirm_minutes))

    def exit_key(self, cost: float) -> tuple:
        ex = self.g.exit
        k = (round(cost, 8), float(ex.stop_pct), float(ex.target_pct), ex.ladder_enabled,
             float(ex.ladder_first_pct), float(ex.ladder_step_pct), ex.ladder_mode,
             float(ex.unarmed_max_hours), ex.stop_mode, float(ex.atr_stop_mult),
             float(ex.atr_stop_min_pct), float(ex.atr_stop_max_pct),
             float(ex.partial_take_pct), float(ex.partial_fraction), ex.on_new_leader)
        if ex.on_new_leader == "close_if_losing":
            k += (float(ex.min_hold_minutes), float(ex.fee_pct)) + self.stream_key()
        return k


def bars_for(minutes: float) -> int:
    return max(0, int(math.ceil(float(minutes) / 5.0 - 1e-9)))


def notes_for(s: Settings) -> list[str]:
    """What this run changes that the replay cannot reproduce."""
    notes = []
    for k in s.overrides:
        if k in NOT_REPLAYED:
            notes.append(f"{k}: {NOT_REPLAYED[k]}")
        elif k.startswith(IGNORED_PREFIXES):
            notes.append(f"{k}: alerts and forecast display are not replayed")
    g = s.g
    if g.exit.target_pct <= 0 and g.exit.target_usd > 0:
        notes.append("exit.target_usd is set with target_pct 0: the replay uses NO take-profit")
    if g.entry.reentry_on_new_high:
        notes.append("entry.reentry_on_new_high is on: re-entries are not replayed")
    m = g.board.leader_check_minutes
    if m % 5:
        notes.append(f"board.leader_check_minutes {m:g} is replayed as "
                     f"{max(1, bars_for(m)) * 5} (5-minute data)")
    if g.entry.confirm_minutes % 5:
        notes.append(f"entry.confirm_minutes {g.entry.confirm_minutes:g} is replayed at "
                     f"5-minute resolution")
    return notes


# ==================================================================== data
class Market:
    """The candle files, and the boards and leader streams built from them."""

    def __init__(self, data_dir: Path, log):
        self.dir = data_dir / "5m"
        self.cache = data_dir / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log
        files = sorted(p for p in self.dir.glob("*.pkl"))
        if not files:
            raise SystemExit(f"no candle files in {self.dir}")
        self.symbols = [p.stem for p in files
                        if p.stem.endswith("USDT") and p.stem[:-4] not in EXCLUDE_BASES]
        fp = hashlib.sha1(json.dumps([(p.name, p.stat().st_size) for p in files]).encode())
        self.fingerprint = fp.hexdigest()[:12]
        self._boards: dict = {}
        self._streams: dict = {}
        self._btc = None
        self.index = self._index()

    def load(self, sym: str):
        with open(self.dir / f"{sym}.pkl", "rb") as f:
            return pickle.load(f)

    def _cache_path(self, kind: str, key) -> Path:
        h = hashlib.sha1(repr((VERSION, self.fingerprint, key)).encode()).hexdigest()[:16]
        return self.cache / f"{kind}-{h}.pkl"

    def _index(self) -> dict:
        """First and last bar of every coin, and the replay's time span."""
        path = self._cache_path("index", "index")
        if path.exists():
            with open(path, "rb") as f:
                return pickle.load(f)
        self.log(f"indexing {len(self.symbols)} coins (once) ...")
        first, last = {}, {}
        for sym in self.symbols:
            T = self.load(sym)[0]
            if len(T):
                first[sym], last[sym] = T[0], T[-1]
        start = min(first.values())
        idx = dict(first=first, last=last, data_start=start,
                   t_lo=(start // B) * B + DAY_BARS * B + B,
                   t_hi=max(last.values()) + B)
        with open(path, "wb") as f:
            pickle.dump(idx, f)
        return idx

    @property
    def t_lo(self) -> int:
        return self.index["t_lo"]

    @property
    def t_hi(self) -> int:
        return self.index["t_hi"]

    def flow(self, sym):
        """(times, trades, taker-buy quote volume) per 5-minute bar, or None."""
        p = self.dir.parent / "flow" / f"{sym}.pkl"
        if not p.exists():
            return None
        with open(p, "rb") as f:
            return pickle.load(f)

    def funding(self, sym):
        """(times, % per 8h) of the coin's funding settlements, or None."""
        if not hasattr(self, "_funding"):
            p = self.dir.parent / "funding.pkl"
            self._funding = pickle.load(open(p, "rb")) if p.exists() else {}
        return self._funding.get(sym)

    def btc(self):
        if self._btc is None:
            self._btc = self.load("BTCUSDT")
        return self._btc

    # ------------------------------------------------------------ the board
    def board(self, key: tuple):
        """
        The #1 coin at every 5-minute close: (symbol index, bar index) arrays
        over steps t_lo, t_lo + 5 min, ... A step with no eligible coin is -1.
        """
        if key in self._boards:
            return self._boards[key]
        path = self._cache_path("board", key)
        if path.exists():
            with open(path, "rb") as f:
                self._boards[key] = pickle.load(f)
            return self._boards[key]
        import array
        t_lo, t_hi = self.t_lo, self.t_hi
        n = (t_hi - t_lo) // B + 1
        best = array.array("d", [-1e18]) * n
        bsym = array.array("i", [-1]) * n
        bidx = array.array("i", [0]) * n
        climb = key[0] == "climb"
        min_qv = key[1]
        w = key[2] if climb else DAY_BARS
        surge = key[3] if climb else 0.0
        self.log(f"building the board {key} over {len(self.symbols)} coins (once; "
                 f"about 5-15 minutes) ...")
        started = time.time()
        span = DAY_BARS * B
        wspan = w * B
        for si, sym in enumerate(self.symbols):
            T, _O, _H, _L, C, Q = self.load(sym)
            acc = 0.0
            accw = 0.0
            for i in range(len(T)):
                q = Q[i]
                acc += q
                accw += q
                if i >= DAY_BARS:
                    acc -= Q[i - DAY_BARS]
                if i >= w:
                    accw -= Q[i - w]
                if i < DAY_BARS or acc < min_qv or T[i] - T[i - DAY_BARS] != span:
                    continue
                if climb:
                    if T[i] - T[i - w] != wspan:
                        continue
                    if surge > 0 and accw < surge * acc * w / DAY_BARS:
                        continue
                    score = C[i] / C[i - w]
                else:
                    score = C[i] / C[i - DAY_BARS]
                k = (T[i] + B - t_lo) // B
                if 0 <= k < n and score > best[k]:
                    best[k] = score
                    bsym[k] = si
                    bidx[k] = i
            if si % 100 == 0:
                self.log(f"  board {si}/{len(self.symbols)} coins, {time.time() - started:.0f}s")
        out = (bsym, bidx)
        with open(path, "wb") as f:
            pickle.dump(out, f)
        self._boards[key] = out
        return out

    def stream(self, s: Settings):
        """
        Confirmed new leaders at the bot's check times, as the bot's
        check_leader decides them: [(t, symbol, end)], where end is the
        first later check at which another coin tops the board (the bot
        retries a waiting entry only until then).
        """
        key = s.stream_key()
        if key in self._streams:
            return self._streams[key]
        bsym, _ = self.board(s.board_key())
        period = s.check_ms()
        confirm = s.g.entry.confirm_minutes * 60_000
        t_lo = self.t_lo
        first = ((t_lo + period - 1) // period) * period
        leader = cand = None
        since = 0
        events = []
        for t in range(first, self.t_hi + 1, period):
            k = (t - t_lo) // B
            si = bsym[k]
            if si < 0:
                continue
            sym = self.symbols[si]
            if events and events[-1][2] is None and events[-1][1] != sym:
                events[-1][2] = t                           # streak over
            if leader is None:
                leader = sym                                # the baseline leader
                continue
            if sym == leader:
                cand = None
                continue
            if sym != cand:
                cand, since = sym, t
            if t - since < confirm:
                continue
            leader, cand = sym, None
            events.append([t, sym, None])
        for e in events:
            if e[2] is None:
                e[2] = self.t_hi
        out = [tuple(e) for e in events]
        self._streams[key] = out
        return out


# ================================================================== oracle
class Oracle:
    """
    Per-coin facts the account replay asks for: features at a time, the
    outcome of a trade opened at a time, the first check a waiting leader
    is rising. Unknown answers are recorded as misses, computed in bulk
    (each coin loaded once) and cached on disk.
    """

    def __init__(self, market: Market, log):
        self.m = market
        self.log = log
        self.feats: dict = {}
        self.outs: dict = {}              # exit key -> {(sym, t): outcome}
        self.rising: dict = {}
        self.marks: dict = {}             # (sym, t_entry, t) -> price at t / entry
        self.misses: dict = {}            # sym -> set of requests
        self._dirty = set()
        self._load("feats", self.feats, f"all-{FEATS_VERSION}")
        self._load("rising", self.rising)
        self._load("marks", self.marks)

    # -------------------------------------------------------------- cache
    def _load(self, kind, into, key="all"):
        p = self.m._cache_path(kind, key)
        if p.exists():
            with open(p, "rb") as f:
                into.update(pickle.load(f))

    def _outs_for(self, ek):
        if ek not in self.outs:
            self.outs[ek] = {}
            self._load("outs", self.outs[ek], ek)
        return self.outs[ek]

    def save(self):
        for kind in self._dirty:
            if kind == "feats":
                obj, key = self.feats, f"all-{FEATS_VERSION}"
            elif kind == "marks":
                obj, key = self.marks, "all"
            elif kind == "rising":
                obj, key = self.rising, "all"
            else:
                obj, key = self.outs[kind[1]], kind[1]
                kind = "outs"
            p = self.m._cache_path(kind, key)
            with open(str(p) + ".tmp", "wb") as f:
                pickle.dump(obj, f)
            os.replace(str(p) + ".tmp", p)
        self._dirty.clear()

    # ----------------------------------------------------------- requests
    def _miss(self, sym, req):
        self.misses.setdefault(sym, set()).add(req)

    def feature(self, sym, t):
        v = self.feats.get((sym, t))
        if v is None:
            self._miss(sym, ("f", t))
        return v

    def outcome(self, ek, sym, t):
        """(return, exit time, reason); False when the coin has no data then; None = miss."""
        memo = self._outs_for(ek)
        if (sym, t) not in memo:
            self._miss(sym, ("o", t, ek))
            return None
        return memo[(sym, t)] or False

    def mark(self, sym, t_entry, t):
        """Price at the close at t over the entry (the open of the bar at t_entry)."""
        key = (sym, t_entry, t)
        if key not in self.marks:
            self._miss(sym, ("m", t_entry, t))
            return None
        return self.marks[key]

    def first_rising(self, sym, a, b, period, minutes, min_rise):
        key = (sym, a, b, period, minutes, min_rise)
        if key in self.rising:
            return self.rising[key]
        self._miss(sym, ("r", a, b, period, minutes, min_rise))
        return "miss"

    # ------------------------------------------------------------ compute
    def fill(self, context) -> int:
        """Compute every recorded miss. Returns how many there were."""
        misses, self.misses = self.misses, {}
        total = sum(len(v) for v in misses.values())
        if not total:
            return 0
        self.log(f"  computing {total:,} trade facts on {len(misses)} coins ...")
        started = time.time()
        for n, (sym, reqs) in enumerate(sorted(misses.items())):
            d = self.m.load(sym)
            for req in reqs:
                if req[0] == "f":
                    self.feats[(sym, req[1])] = self._features(sym, d, req[1])
                    self._dirty.add("feats")
                elif req[0] == "o":
                    ek = req[2]
                    f = self.feats.get((sym, req[1])) or self._features(sym, d, req[1])
                    self._outs_for(ek)[(sym, req[1])] = self._outcome(
                        sym, d, req[1], ek, f, context)
                    self._dirty.add(("outs", ek))
                elif req[0] == "m":
                    T, O, _H, _L, C, _Q = d
                    j = bisect.bisect_left(T, req[1])
                    k = bisect.bisect_right(T, req[2] - B) - 1     # last bar closed by t
                    ok = j < len(T) and 0 <= k and O[j] > 0
                    self.marks[(sym, req[1], req[2])] = C[max(k, j)] / O[j] if ok else 1.0
                    self._dirty.add("marks")
                else:
                    self.rising[(sym,) + req[1:]] = self._rising(d, *req[1:])
                    self._dirty.add("rising")
            if n % 100 == 99:
                self.log(f"    {n + 1}/{len(misses)} coins, {time.time() - started:.0f}s")
        self.save()
        return total

    def _features(self, sym, d, t) -> dict:
        T, O, Hh, L, C, Q = d
        j = bisect.bisect_left(T, t)                # the bar that opens at t
        i = j - 1                                   # the bar that closed at t
        if j >= len(T) or i < 0 or T[i] + B != t:
            return {"ok": False}
        out = {"ok": True, "price": C[i], "rsi": None, "atr": None, "age": None,
               "btc24": None, "btc1": None}
        if i >= 168:
            out["rsi"] = rsi([C[i - 12 * k] for k in range(14, -1, -1)])
        if i >= 12 * 13:
            bars = []
            for h in range(12, -1, -1):
                a, b = i - 12 * h - 11, i - 12 * h
                bars.append((O[a], max(Hh[a:b + 1]), min(L[a:b + 1]), C[b]))
            out["atr"] = atr_pct(bars)
        first = self.m.index["first"].get(sym, T[0])
        if first - self.m.index["data_start"] > 7 * D:   # listed after the data starts
            out["age"] = (t - first) / D
        out.update(self._pump_features(sym, d, i, t))
        bT, _bO, _bH, _bL, bC, _bQ = self.m.btc()
        k = bisect.bisect_left(bT, t) - 1
        if k >= DAY_BARS and bT[k] + B == t:
            out["btc24"] = (bC[k] / bC[k - DAY_BARS] - 1) * 100
            out["btc1"] = (bC[k] / bC[k - 12] - 1) * 100
        return out

    def _pump_features(self, sym, d, i, t) -> dict:
        """The pump-and-dump measurements behind the test.max_* / min_* checks."""
        T, O, Hh, L, C, Q = d
        f = {}
        if i >= 8 * DAY_BARS:
            q24 = sum(Q[i - DAY_BARS + 1:i + 1])
            normal = sum(Q[i - 8 * DAY_BARS + 1:i - DAY_BARS + 1]) / 7
            f["prior_vol"] = normal
            f["vol_surge"] = q24 / normal if normal > 0 else None
        if i >= DAY_BARS and C[i - DAY_BARS] > 0:
            g24 = C[i] / C[i - DAY_BARS]
            if g24 > 1.0:
                f["share_1h"] = math.log(C[i] / C[i - 12]) / math.log(g24)
                f["share_4h"] = math.log(C[i] / C[i - 48]) / math.log(g24)
            f["below_high"] = (1 - C[i] / max(Hh[i - DAY_BARS + 1:i + 1])) * 100
        if i >= 84:
            hi, lo = max(Hh[i - 11:i + 1]), min(L[i - 11:i + 1])
            f["upper_wick"] = (hi - C[i]) / (hi - lo) if hi > lo else 0.0
            before = sum(Q[i - 83:i - 11]) / 6
            f["vol_trend"] = sum(Q[i - 11:i + 1]) / before if before > 0 else None
        if i >= 32 * DAY_BARS:
            f["prior_pump"] = max((C[j] / C[j - DAY_BARS] - 1) * 100
                                  for j in range(i - 31 * DAY_BARS, i - 2 * DAY_BARS, 12))
        fl = self.m.flow(sym)
        if fl and i >= 8 * DAY_BARS:
            fT, fN, fB = fl
            k = bisect.bisect_left(fT, T[i])
            if k < len(fT) and fT[k] == T[i] and k >= 8 * DAY_BARS:
                q1 = sum(Q[i - 11:i + 1])
                if q1 > 0:
                    f["buy_share_1h"] = sum(fB[k - 11:k + 1]) / q1
                n1 = sum(fN[k - 11:k + 1])
                n7 = sum(fN[k - 8 * DAY_BARS + 1:k - DAY_BARS + 1])
                q7 = sum(Q[i - 8 * DAY_BARS + 1:i - DAY_BARS + 1])
                if n7 > 0:
                    f["trades_surge"] = n1 / (n7 / (7 * 24))
                if n1 > 0 and n7 > 0 and q7 > 0:
                    f["trade_size_x"] = (q1 / n1) / (q7 / n7)
        fu = self.m.funding(sym)
        if fu:
            k = bisect.bisect_right(fu[0], t) - 1
            if k >= 0 and t - fu[0][k] < 9 * H:
                f["funding"] = fu[1][k]
        return f

    @staticmethod
    def _rising(d, a, b, period, minutes, min_rise):
        """First check in [a, b) where the 24h % climbs >= min_rise per minute."""
        T, _O, _H, _L, C, _Q = d
        back = max(1, bars_for(minutes))
        t = ((a + period - 1) // period) * period
        while t < b:
            i = bisect.bisect_left(T, t - B)
            if i < len(T) and T[i] + B == t and i - back - DAY_BARS >= 0:
                now = C[i] / C[i - DAY_BARS]
                then = C[i - back] / C[i - back - DAY_BARS]
                if (now - then) * 100 / (back * 5) >= min_rise:
                    return t
            t += period
        return None

    def _outcome(self, sym, d, t, ek, feat, context):
        """(net return, exit time, reason) of a long opened at the open of the bar at t."""
        (cost, stop_pct, target_pct, ladder_on, first, step, mode, max_h, stop_mode,
         atr_mult, atr_min, atr_max, part_pct, part_frac, on_new) = ek[:15]
        T, O, Hh, L, C, _Q = d
        j = bisect.bisect_left(T, t)
        if j >= len(T) or not feat.get("ok"):
            return False
        if stop_mode == "atr" and feat.get("atr"):
            stop_pct = min(atr_max, max(atr_min, atr_mult * feat["atr"]))
        e = O[j]
        init = e * (1 - stop_pct / 100)
        sl = init
        tp = e * (1 + target_pct / 100) if target_pct > 0 else 0.0
        part = e * (1 + part_pct / 100) if part_pct > 0 else 0.0
        banked, left = 0.0, 1.0
        peak = e
        t0 = T[j]
        changes = ()
        if on_new == "close_if_losing":
            min_hold, fee = ek[15] * 60_000, ek[16]
            changes = context["changes"][ek[17:]]
            c = bisect.bisect_right(changes, (t, "￿"))
        for k in range(j, len(T)):
            if L[k] <= sl:
                px = min(sl, O[k])
                why = "stop" if sl < e else ("ladder stop at entry" if sl == e else "ladder stop in profit")
                return banked + left * (px / e - 1) - cost, T[k] + B, why
            if part and left == 1.0 and Hh[k] >= part:
                banked = part_frac * (max(part, O[k]) / e - 1)
                left = 1.0 - part_frac
            if tp and Hh[k] >= tp:
                return banked + left * (max(tp, O[k]) / e - 1) - cost, T[k] + B, "take-profit"
            close_t = T[k] + B
            if ladder_on and max_h > 0 and sl < e and close_t - t0 >= max_h * H:
                return banked + left * (C[k] / e - 1) - cost, close_t, "time limit"
            if ladder_on and Hh[k] > peak:
                peak = Hh[k]
                lv = ladder_stop(e, peak, first, step, mode=mode, init_stop=init)
                if lv is not None and lv > sl:
                    sl = lv
            if changes:
                while c < len(changes) and changes[c][0] <= close_t:
                    ct, new = changes[c]
                    c += 1
                    if new != sym and ct - t0 >= min_hold and ct == close_t \
                            and C[k] <= e * (1 + fee / 100):
                        return (banked + left * (C[k] / e - 1) - cost, close_t,
                                "new leader, not profitable")
        return banked + left * (C[-1] / e - 1) - cost, T[-1] + B, "end of data"


# ================================================================= account
_SCORES: dict = {}


def load_scores(path: str) -> dict:
    """{(symbol, time): score} from a classifier's CSV (symbol,t,score)."""
    if path not in _SCORES:
        import csv
        with open(path) as f:
            _SCORES[path] = {(r["symbol"], int(r["t"])): float(r["score"])
                             for r in csv.DictReader(f)}
    return _SCORES[path]


def export_events(s: Settings, market: Market, oracle: Oracle, cost: float, out: Path,
                  log) -> int:
    """Every new leader with its measurements and the result of buying it alone
    with these exits: the training data for tools/backtest_ml.py."""
    import csv
    events = market.stream(s)
    ek = s.exit_key(cost)
    context = {"changes": {}}
    if s.g.exit.on_new_leader == "close_if_losing":
        context["changes"][s.stream_key()] = [(e[0], e[1]) for e in events]
    while True:
        missing = 0
        for t, sym, _ in events:
            if oracle.feature(sym, t) is None:
                missing += 1
            if oracle.outcome(ek, sym, t) is None:
                missing += 1
        if not missing:
            break
        oracle.fill(context)
    keys = ["price", "rsi", "atr", "age", "btc24", "btc1", "prior_vol", "vol_surge",
            "share_1h", "share_4h", "below_high", "upper_wick", "vol_trend", "prior_pump",
            "funding", "buy_share_1h", "trades_surge", "trade_size_x"]
    n = 0
    with open(out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["symbol", "t", "change_24h"] + keys + ["ret", "exit_t", "why"])
        for t, sym, _ in events:
            f = oracle.feature(sym, t)
            o = oracle.outcome(ek, sym, t)
            if not f or not f.get("ok") or not o:
                continue
            w.writerow([sym, t, ""] + [("" if f.get(k) is None else f.get(k)) for k in keys]
                       + [o[0], o[1], o[2]])
            n += 1
    log(f"exported {n} leader events to {out}")
    return n


def utc_day(ms: int) -> int:
    return ms // D


class Account:
    """One replayed account, following the bot's open/close/sweep rules."""

    def __init__(self, s: Settings, oracle: Oracle, events, lo, hi, cost, deposits, context,
                 keep_trades: bool = False):
        self.s, self.o = s, oracle
        self.keep_trades = keep_trades
        self.events = [e for e in events if lo <= e[0] < hi]
        self.lo, self.hi = lo, hi
        self.cost = cost
        self.ek = s.exit_key(cost)
        self.start_dep, self.monthly_dep = deposits
        self.context = context
        self.complete = True
        self.ml = load_scores(s.test["ml_scores"]) if s.test["ml_scores"] else None

    def _feat(self, sym, t):
        f = self.o.feature(sym, t)
        if f is None:
            self.complete = False
            self.o.outcome(self.ek, sym, t)     # likely needed next; saves a pass
        return f

    def run(self) -> dict:
        s, g, risk = self.s, self.s.g, self.s.risk
        en, ex, fl, sz, lm, sw = g.entry, g.exit, g.filters, g.sizing, g.limits, g.sweep
        period = s.check_ms()
        bal = dep = self.start_dep
        pending = withdrawn = income_month = income_total = 0.0
        swept = principal_out = 0.0
        held = {}
        closed_at = {}
        day_opens, day_real, day_start = {}, {}, {}
        loss_streak, pause_until, peak = 0, 0, bal
        months, trades = [], []
        skips, exits = {}, {}
        next_month = self.lo + MONTH

        def skip(why):
            skips[why] = skips.get(why, 0) + 1

        def equity():
            return bal - pending

        def close(sym):
            nonlocal bal, pending, withdrawn, loss_streak, pause_until, swept, principal_out
            nonlocal income_month
            p = held.pop(sym)
            pnl = p["r"] * p["n"]
            bal += pnl
            closed_at[sym] = p["x"]
            day = utc_day(p["x"])
            day_real[day] = day_real.get(day, 0.0) + pnl
            exits[p["why"]] = exits.get(p["why"], 0) + 1
            trades.append(dict(sym=sym, t=p["t"], x=p["x"], n=round(p["n"], 2),
                               ret=round(p["r"] * 100, 2), pnl=round(pnl, 2), why=p["why"]))
            if pnl < 0:
                loss_streak += 1
                if lm.pause_after_losses > 0 and loss_streak >= lm.pause_after_losses:
                    pause_until = p["x"] + lm.pause_hours * H
                    loss_streak = 0
            else:
                loss_streak = 0
            if not sw.enabled:
                return
            if pnl > 0 and not (sw.start_when_equity_usdt > 0 and equity() < sw.start_when_equity_usdt):
                pending += pnl * sw.pct / 100
                amount = int(pending * 100) / 100
                if amount >= sw.min_transfer_usdt:
                    bal -= amount
                    pending = round(pending - amount, 8)
                    income_month += amount
                    swept += amount
            if sw.principal_enabled:
                owed = int((dep - withdrawn) * 100) / 100
                if owed > 0 and bal >= sw.principal_trigger_x * dep:
                    bal -= owed
                    withdrawn += owed
                    income_month += owed
                    principal_out += owed

        def month_end(t):
            nonlocal next_month, bal, dep, income_month, income_total
            income_total += income_month
            months.append(dict(month=len(months) + 1, end=fmt(t), deposited=dep,
                               to_funding=round(income_month, 2),
                               funding_so_far=round(income_total, 2),
                               futures_balance=round(bal, 2),
                               profit_so_far=round(income_total + bal - dep, 2)))
            income_month = 0.0
            bal += self.monthly_dep
            dep += self.monthly_dep
            next_month += MONTH

        def advance(u):
            while True:
                nxt = min((p["x"] for p in held.values()), default=None)
                if next_month <= u and (nxt is None or next_month < nxt):
                    month_end(next_month)
                elif nxt is not None and nxt <= u:
                    close(min(held, key=lambda k: held[k]["x"]))
                else:
                    return

        def notional_for(f):
            if en.notional_pct_of_equity <= 0:
                return en.notional_usdt
            share = en.notional_pct_of_equity
            if sz.mode == "volatility" and f.get("atr"):
                share = min(sz.max_pct, max(sz.min_pct, share * sz.volatility_target_atr_pct / f["atr"]))
            elif sz.mode == "conviction":
                age = f["age"] if f.get("age") is not None else 1e9
                strong = ((f.get("rsi") is None or f["rsi"] <= sz.conviction_max_rsi_1h)
                          and age >= sz.conviction_min_listing_age_days
                          and (f.get("btc24") is None
                               or f["btc24"] >= sz.conviction_btc_min_change_24h_pct))
                share = sz.conviction_high_pct if strong else sz.conviction_low_pct
            pct = max(0.0, equity()) * share / 100
            return max(pct, en.min_notional_usdt) if pct > 0 else 0.0

        def filter_reason(f):
            if fl.min_price > 0 and f["price"] < fl.min_price:
                return "filter: min_price"
            if fl.min_listing_age_days > 0 and f.get("age") is not None \
                    and f["age"] < fl.min_listing_age_days:
                return "filter: min_listing_age_days"
            if fl.btc_min_change_24h_pct is not None and f.get("btc24") is not None \
                    and f["btc24"] < fl.btc_min_change_24h_pct:
                return "filter: btc_min_change_24h_pct"
            if fl.btc_max_drop_1h_pct > 0 and f.get("btc1") is not None \
                    and f["btc1"] < -fl.btc_max_drop_1h_pct:
                return "filter: btc_max_drop_1h_pct"
            if fl.max_rsi_1h > 0 and f.get("rsi") is not None and f["rsi"] > fl.max_rsi_1h:
                return "filter: max_rsi_1h"
            if self.ml is not None and float(s.test["min_ml_score"] or 0) > 0:
                sc = self.ml.get((self._sym, self._t))
                if sc is not None and sc < float(s.test["min_ml_score"]):
                    return "test: min_ml_score"
            for name, feat, above in PUMP_CHECKS:
                limit = float(s.test[name] or 0)
                v = f.get(feat)
                if limit > 0 and v is not None and (v > limit if above else v < limit):
                    return f"test: {name}"
            return ""

        policy = s.test["when_full"]
        min_hold = float(s.test["replace_min_hold_hours"]) * H

        def full(n):
            used = sum(p["n"] for p in held.values())
            eq = equity()
            return (len(held) >= en.max_positions
                    or (lm.max_exposure_pct > 0 and used + n > eq * lm.max_exposure_pct / 100 + 1e-9)
                    or used + n > eq * risk.max_leverage + 1e-9)

        def victim(t):
            """test.when_full: the position to sell for room, None, or 'miss'."""
            pool = [k for k, p in held.items() if t - p["t"] >= min_hold]
            if not pool:
                return None
            if policy == "replace_oldest":
                return min(pool, key=lambda k: held[k]["t"])
            marks = {}
            for k in pool:
                m = self.o.mark(k, held[k]["t"], t)
                if m is None:
                    self.complete = False
                    return "miss"
                marks[k] = m
            worst = min(pool, key=lambda k: marks[k])
            if policy == "replace_losing" and marks[worst] - 1 - self.cost >= 0:
                return None
            held[worst]["mark"] = marks[worst]
            return worst

        def make_room(t, f):
            """Sell positions until the new leader fits, as test.when_full says."""
            while held and full(notional_for(f)):
                v = victim(t)
                if v is None or v == "miss":
                    return v
                p = held[v]
                m = p.get("mark")
                if m is None:
                    m = self.o.mark(v, p["t"], t)
                    if m is None:
                        self.complete = False
                        return "miss"
                p.update(r=m - 1 - self.cost, x=t, why="replaced by a new leader")
                close(v)
            return "ok"

        def open_(sym, t):
            nonlocal peak
            eq = equity()
            peak = max(peak, eq)
            day = utc_day(t)
            day_start.setdefault(day, eq)
            if len(held) >= en.max_positions and policy == "refuse":
                return skip("max_positions full")
            f = self._feat(sym, t)
            if f is None:
                return
            if not f.get("ok"):
                return skip("no candle data at entry")
            if policy != "refuse" and make_room(t, f) == "miss":
                return
            if len(held) >= en.max_positions:
                return skip("max_positions full")
            eq = equity()
            n = notional_for(f)
            if lm.max_new_trades_per_day > 0 and day_opens.get(day, 0) >= lm.max_new_trades_per_day:
                return skip("limit: max_new_trades_per_day")
            if t < pause_until:
                return skip("limit: pause_after_losses")
            if lm.max_exposure_pct > 0 and sum(p["n"] for p in held.values()) + n \
                    > eq * lm.max_exposure_pct / 100 + 1e-9:
                return skip("limit: max_exposure_pct")
            if lm.daily_loss_limit_pct > 0 and day_real.get(day, 0.0) \
                    < -day_start[day] * lm.daily_loss_limit_pct / 100:
                return skip("limit: daily_loss_limit_pct")
            if lm.drawdown_pause_pct > 0 and eq < peak * (1 - lm.drawdown_pause_pct / 100):
                return skip("limit: drawdown_pause_pct")
            if n < MIN_ORDER_USDT:
                return skip("under the $5 exchange minimum")
            if eq < risk.min_equity_usdt:
                return skip("risk: below min_equity_usdt")
            if risk.daily_loss_limit_pct > 0 and day_start[day] > 0 and \
                    -day_real.get(day, 0.0) / day_start[day] * 100 >= risk.daily_loss_limit_pct:
                return skip("risk: daily_loss_limit_pct")
            if risk.max_trades_per_day > 0 and day_opens.get(day, 0) >= risk.max_trades_per_day:
                return skip("risk: max_trades_per_day")
            if sum(p["n"] for p in held.values()) + n > eq * risk.max_leverage + 1e-9:
                return skip("risk: max_leverage ceiling")
            out = self.o.outcome(self.ek, sym, t)
            if out is None:
                self.complete = False
                return
            if out is False:
                return skip("no candle data at entry")
            r, x, why = out
            held[sym] = dict(t=t, x=x, r=r, n=n, why=why)
            day_opens[day] = day_opens.get(day, 0) + 1

        def blocker(sym, t):
            closed = closed_at.get(sym)
            if en.rebuy_cooldown_minutes > 0 and closed is not None \
                    and t - closed < en.rebuy_cooldown_minutes * 60_000:
                return "cooldown"
            if en.buy_only_if_rising:
                r = self.o.first_rising(sym, t, t + 1, period, g.forecast.slope_minutes,
                                        en.min_rise_pct_per_min)
                if r == "miss":
                    self.complete = False
                    return "unknown"
                if r is None:
                    return "not rising"
            return ""

        def retry(sym, t, end, until):
            """The check at which a waiting leader is bought, or None."""
            a = t + period
            closed = closed_at.get(sym)
            if en.rebuy_cooldown_minutes > 0 and closed is not None:
                a = max(a, closed + int(en.rebuy_cooldown_minutes * 60_000))
            a = ((a + period - 1) // period) * period
            b = min(end, until)
            if a >= b:
                return None
            if not en.buy_only_if_rising:
                return a
            r = self.o.first_rising(sym, a, b, period, g.forecast.slope_minutes,
                                    en.min_rise_pct_per_min)
            if r == "miss":
                self.complete = False
                return None
            return r

        evs = self.events
        for n_ev, (t, sym, end) in enumerate(evs):
            advance(t)
            until = evs[n_ev + 1][0] if n_ev + 1 < len(evs) else self.hi
            if sym in held:
                skip("already holding it")
                continue
            f = self._feat(sym, t)
            if f is None:
                continue
            if not f.get("ok"):
                skip("no candle data at entry")
                continue
            self._sym, self._t = sym, t
            why = filter_reason(f)
            if why:
                skip(why)
                continue
            b = blocker(sym, t)
            if b == "unknown":
                continue
            if b:
                r = retry(sym, t, end, until)
                if r is None:
                    skip(f"entry guard: {b} while #1")
                    continue
                advance(r)
                if sym in held:
                    continue
                open_(sym, r)
            else:
                open_(sym, t)
        advance(self.hi)
        while next_month <= self.hi:
            month_end(next_month)
        for sym in sorted(held, key=lambda k: held[k]["x"]):
            close(sym)                          # still open: booked at their own exit
        income_total += income_month
        wins = [x for x in trades if x["pnl"] > 0]
        profits = [m["profit_so_far"] for m in months]
        return dict(
            real_profit=round(income_total + bal - dep, 2),
            deposited=dep, to_funding_total=round(income_total, 2),
            of_which_profit_sweeps=round(swept, 2), of_which_principal=round(principal_out, 2),
            futures_balance_end=round(bal, 2),
            income_per_month=round(income_total / max(1, len(months)), 2),
            trades=len(trades), win_rate_pct=round(100 * len(wins) / max(1, len(trades)), 1),
            avg_return_per_trade_pct=round(sum(x["ret"] for x in trades) / max(1, len(trades)), 2),
            trading_pnl=round(sum(x["pnl"] for x in trades), 2),
            worst_profit_so_far=round(min(profits), 2) if profits else 0.0,
            months_behind_deposits=sum(1 for p in profits if p < 0),
            months=len(months), exit_reasons=exits, skipped=skips,
            best_trade=max(trades, key=lambda x: x["pnl"], default=None),
            worst_trade=min(trades, key=lambda x: x["pnl"], default=None),
            monthly=months, **({"trade_list": trades} if self.keep_trades else {}))


def fmt(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


# ==================================================================== runs
def periods(m: Market) -> dict:
    lo, hi = m.t_lo, m.t_hi
    mid = lo + 365 * D
    return {"2y": (lo, hi), "year1": (lo, mid), "year2": (mid, hi),
            "last6m": (hi - 180 * D, hi)}


def replay(runs: list, market: Market, oracle: Oracle, cost: float, deposits, log,
           keep_trades: bool = False) -> list:
    """Replay every run over every period, filling the oracle until complete."""
    context = {"changes": {}}
    for r in runs:
        s = r["settings"]
        if s.g.exit.on_new_leader == "close_if_losing":
            k = s.stream_key()
            if k not in context["changes"]:
                context["changes"][k] = [(e[0], e[1]) for e in market.stream(s)]
    per = periods(market)
    todo = list(runs)
    started = time.time()
    for p in itertools.count(1):
        pending = []
        for i, r in enumerate(todo, 1):
            s = r["settings"]
            events = market.stream(s)
            r["results"] = {}
            done = True
            for name, (lo, hi) in per.items():
                acct = Account(s, oracle, events, lo, hi, cost, deposits, context,
                               keep_trades=keep_trades)
                r["results"][name] = acct.run()
                done = done and acct.complete
            if not done:
                pending.append(r)
            if i % 250 == 0:
                log(f"  pass {p}: {i}/{len(todo)} runs, {time.time() - started:.0f}s")
        n = oracle.fill(context)
        log(f"pass {p}: {len(todo)} runs replayed, {len(pending)} need more facts, "
            f"{n:,} facts computed")
        if not pending:
            return runs
        if not n:
            raise RuntimeError("replay could not complete: facts missing but none computed")
        todo = pending


def grid_specs(grid: dict | None, sets: dict) -> list:
    """A grid file (+ --set) as run specs: every combination of `vary`."""
    grid = grid or {}
    fixed = {**(grid.get("set") or {}), **sets}
    named = grid.get("runs") or [dict(name="", set={})]
    vary = grid.get("vary") or {}
    keys = list(vary)
    specs = []
    for spec in named:
        for combo in itertools.product(*(vary[k] if isinstance(vary[k], list) else [vary[k]]
                                         for k in keys)):
            ov = {**fixed, **(spec.get("set") or {}), **dict(zip(keys, combo))}
            if ov:
                specs.append(dict(name=spec.get("name") or "", overrides=ov))
    return specs


def build_runs(base_raw: dict, specs: list, shard=None, live: dict | None = None) -> list:
    """The live run first, then one run per distinct spec (same-as-live dropped)."""
    runs = [live or dict(name="LIVE (config.yaml as is)", overrides={})] + [dict(x) for x in specs]
    seen, out = set(), []
    for i, r in enumerate(runs):
        if i == 0 and "settings" in r:
            seen.add(r["id"])
            out.append(r)
            continue
        r["settings"] = Settings(base_raw, r["overrides"])
        changed = {k: v for k, v in r["settings"].flat.items()
                   if v != runs[0]["settings"].flat.get(k)} if i else {}
        r["changes"] = changed
        r.setdefault("round", 0 if i == 0 else 1)
        if i and not changed:
            continue                            # this combination is the live settings
        r["id"] = "live" if i == 0 else "r" + hashlib.sha1(
            json.dumps(changed, sort_keys=True, default=str).encode()).hexdigest()[:8]
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    if shard:
        k, n = shard
        out = [out[0]] + [r for i, r in enumerate(out[1:]) if i % n == k - 1]
    return out


def round2_specs(runs1: list, cfg: dict, log, select: str = "both") -> tuple[list, dict]:
    """Every combination, across settings, of the round-1 values that beat live
    in both years (each setting may also stay at its live value)."""
    keep = int(cfg.get("keep_per_setting", 2))
    max_runs = int(cfg.get("max_runs", 3000))
    L = runs1[0]["results"]
    wins: dict = {}
    for r in runs1[1:]:
        x = r["results"]
        if select == "year1":
            # choose on year 1 alone, so year 2 stays an unseen test
            if x["year1"]["real_profit"] > L["year1"]["real_profit"]:
                wins.setdefault(r["group"], []).append((x["year1"]["real_profit"],
                                                        r["overrides"]))
        elif (x["year1"]["real_profit"] > L["year1"]["real_profit"]
                and x["year2"]["real_profit"] > L["year2"]["real_profit"]):
            wins.setdefault(r["group"], []).append((x["2y"]["real_profit"], r["overrides"]))
    for g in wins:
        wins[g].sort(key=lambda w: -w[0])
    groups = sorted(wins, key=lambda g: -wins[g][0][0])
    dropped = []

    def count(k):
        return math.prod(min(k, len(wins[g])) + 1 for g in groups)

    while groups and count(keep) > max_runs:
        if keep > 1:
            keep -= 1
        else:
            dropped.append(groups.pop())         # the weakest winner
    specs = []
    for combo in itertools.product(*[[None] + [ov for _, ov in wins[g][:keep]] for g in groups]):
        chosen = [ov for ov in combo if ov is not None]
        if len(chosen) < 2:
            continue                             # singles were round 1
        merged, clash = {}, False
        for ov in chosen:
            for k, v in ov.items():
                clash = clash or (k in merged and merged[k] != v)
                merged[k] = v
        if not clash:
            specs.append(dict(name="", overrides=merged, round=2, group="combo"))
    log(f"round 2: {len(groups)} settings beat live in both years alone "
        f"({', '.join(groups) or 'none'}); {len(specs)} combinations")
    return specs, dict(keep_per_setting=keep, groups=groups, dropped=dropped,
                       round2_runs=len(specs), select=select)


# ================================================================== report
def describe(r) -> str:
    if r["id"] == "live":
        return "(live settings)"
    text = ", ".join(f"{k.removeprefix('gainer.')}={json.dumps(v)}" for k, v in r["changes"].items())
    return (f"{r['name']}: " if r.get("name") else "") + (text or "(same as live)")


def write_report(doc: dict, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    runs = doc["runs"]
    live = next(r for r in runs if r["id"] == "live")
    L = live["results"]

    def beats(r):
        return (r["results"]["year1"]["real_profit"] > L["year1"]["real_profit"]
                and r["results"]["year2"]["real_profit"] > L["year2"]["real_profit"])

    order = sorted(runs, key=lambda r: -r["results"]["2y"]["real_profit"])
    limit = int(doc.get("report_rows") or 150)
    rounds = sorted({r.get("round", 1) for r in runs if r["id"] != "live"}) or [1]
    lines = [
        "# Gainer strategy backtest report", "",
        f"- Generated: {doc['generated']} (tool version {doc['version']})",
        f"- Base config: `{doc['config']}`",
        f"- Data: {doc['data']['coins']} USDT perpetuals with 5-minute candles, delisted coins "
        f"included, {doc['data']['from']} to {doc['data']['to']}",
        f"- Deposits: ${doc['deposits']['start']:g} at the start, then "
        f"${doc['deposits']['monthly']:g} every 30 days",
        f"- Cost per round trip: {doc['cost_pct']:.2f}% of the trade (exit.fee_pct + "
        f"{doc['slippage_pct']:.2f}% slippage)",
        f"- Runs: {len(runs)} (the live settings + {len(runs) - 1} variants). Every run is in "
        f"results.csv (one row each) and results.json (all numbers, month by month).", "",
        "## How to read this", "",
        "- Each run is the live config.yaml with the settings in `changes` replaced. "
        "Settings not listed are the live values (see the last section).",
        "- Each period is a separate account that starts empty and gets the deposits above.",
        "- `real profit` = money moved to Funding + futures balance at the end - "
        "everything deposited. Positive means the account made money.",
        "- `year1`, `year2` are the first and second 365 days; `last6m` is the last 180 days.",
        "- `beats live both years` = more real profit than live in year 1 AND year 2, "
        "the check for a change that is not just luck in one period.",
        "- `worst` is the lowest profit-so-far at any month end over 2 years (how deep "
        "the account went under what was deposited); `behind` is how many month ends "
        "were below deposits.",
        "- With many runs some look good by chance. Prefer changes that beat live in both "
        "years, by a margin, and that make sense as trading rules.",
    ]
    if doc.get("search"):
        sr = doc["search"]
        lines += ["", "## Search", "",
                  "- Round 1 changes one setting at a time, over every value in the search space.",
                  ("- Round 2 combines, across settings, the round-1 values that beat live in "
                   "YEAR 1 (chosen on year 1 alone: year 2 is an unseen test, judge on it): "
                   if sr.get("select") == "year1" else
                   "- Round 2 combines, across settings, the round-1 values that beat live in "
                   "both years: ")
                  + f"up to {sr['keep_per_setting']} best value(s) per setting "
                  f"(or the live value), {sr['round2_runs']} combinations.",
                  "- Settings carried into round 2 (best 2y first): "
                  + (", ".join(sr["groups"]) or "none -- no single change beat live in both years")]
        if sr.get("dropped"):
            lines.append("- Winners left out of round 2 to stay under max_runs: "
                         + ", ".join(sr["dropped"]))
    head = ("| # | id | changes vs live | 2y profit | year1 | year2 | last6m | beats live both "
            "years | trades 2y | win % | avg/trade % | Funding $/month | worst | behind |")
    for rnd in rounds:
        title = {1: "Round 1: one setting changed at a time" if doc.get("search") else "Results",
                 2: "Round 2: combinations of the round-1 winners"}.get(rnd, f"Round {rnd}")
        rows = [live] + [r for r in order if r.get("round", 1) == rnd and r["id"] != "live"]
        rows.sort(key=lambda r: -r["results"]["2y"]["real_profit"])
        shown = rows[:limit + 1]
        if live not in shown:
            shown.append(live)
        lines += ["", f"## {title}, best 2-year real profit first", ""]
        if len(rows) > len(shown):
            lines += [f"Top {len(shown) - 1} of {len(rows) - 1} runs; the rest are in "
                      f"results.csv.", ""]
        lines += [head, "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for n, r in enumerate(shown, 1):
            x = r["results"]
            a = x["2y"]
            lines.append(
                f"| {n} | {r['id']} | {describe(r)} | {a['real_profit']:+.2f} | "
                f"{x['year1']['real_profit']:+.2f} | {x['year2']['real_profit']:+.2f} | "
                f"{x['last6m']['real_profit']:+.2f} | "
                f"{'-' if r['id'] == 'live' else ('YES' if beats(r) else 'no')} | {a['trades']} | "
                f"{a['win_rate_pct']:.0f} | {a['avg_return_per_trade_pct']:+.2f} | "
                f"{a['income_per_month']:.2f} | {a['worst_profit_so_far']:+.0f} | "
                f"{a['months_behind_deposits']}/{a['months']} |")
    import csv
    with open(out / "results.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["round", "id", "changes_vs_live", "profit_2y", "profit_year1", "profit_year2",
                    "profit_last6m", "beats_live_both_years", "trades_2y", "win_pct",
                    "avg_trade_pct", "funding_per_month", "worst_profit_so_far",
                    "months_behind_deposits"])
        for r in order:
            x = r["results"]
            a = x["2y"]
            w.writerow([r.get("round", 1), r["id"], describe(r), a["real_profit"],
                        x["year1"]["real_profit"], x["year2"]["real_profit"],
                        x["last6m"]["real_profit"],
                        "" if r["id"] == "live" else ("yes" if beats(r) else "no"),
                        a["trades"], a["win_rate_pct"], a["avg_return_per_trade_pct"],
                        a["income_per_month"], a["worst_profit_so_far"],
                        a["months_behind_deposits"]])
    flagged = [r for r in runs if r.get("notes")]
    if flagged:
        lines += ["", "## Replay warnings", ""]
        for r in flagged:
            for note in r["notes"]:
                lines.append(f"- {r['id']}: {note}")
    best = order[0] if order[0]["id"] != "live" else (order[1] if len(order) > 1 else None)
    for title, r in (("Live settings", live), ("Best run", best)):
        if r is None:
            continue
        a = r["results"]["2y"]
        lines += ["", f"## {title}: {r['id']} over 2 years", "", f"Changes: {describe(r)}", "",
                  f"Real profit {a['real_profit']:+.2f} on ${a['deposited']:,.0f} deposited: "
                  f"${a['to_funding_total']:,.2f} moved to Funding (${a['of_which_profit_sweeps']:,.2f} "
                  f"profit sweeps, ${a['of_which_principal']:,.2f} principal) and "
                  f"${a['futures_balance_end']:,.2f} left in futures.", "",
                  "Exits: " + ", ".join(f"{k} {v}" for k, v in sorted(a["exit_reasons"].items(),
                                                                       key=lambda kv: -kv[1])),
                  "",
                  "Leaders not bought: " + (", ".join(
                      f"{k} {v}" for k, v in sorted(a["skipped"].items(),
                                                    key=lambda kv: -kv[1])) or "none"), ""]
        for label in ("best_trade", "worst_trade"):
            tr = a.get(label)
            if tr:
                lines.append(f"- {label.replace('_', ' ')}: {tr['sym']} opened {fmt(tr['t'])}, "
                             f"{tr['ret']:+.2f}% ({tr['pnl']:+.2f} USDT), {tr['why']}")
        lines += ["", "| month | ends | deposited | to Funding | Funding so far | futures | "
                  "profit so far |", "|---|---|---|---|---|---|---|"]
        for m in a["monthly"]:
            lines.append(f"| {m['month']} | {m['end']} | {m['deposited']:.0f} | "
                         f"{m['to_funding']:.2f} | {m['funding_so_far']:.2f} | "
                         f"{m['futures_balance']:.2f} | {m['profit_so_far']:+.2f} |")
    lines += ["", "## Live settings every run starts from", "", "```"]
    lines += [f"{k} = {json.dumps(v)}" for k, v in sorted(doc["live_settings"].items())]
    lines += ["```", "", "## Method and limits", ""] + [f"- {x}" for x in METHOD]
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "results.json").write_text(json.dumps(doc, default=str))


METHOD = [
    "The board is rebuilt at every 5-minute close from the stored candles: 24h quote volume "
    "and 24h change (or the climb window) per coin, the same eligibility as the bot "
    "(board.min_quote_volume, excluded stablecoins).",
    "New leaders follow the bot's check_leader: checks every board.leader_check_minutes, "
    "entry.confirm_minutes of holding #1, and a waiting leader (rebuy cooldown, "
    "buy_only_if_rising) retried at later checks while it stays #1.",
    "An entry fills at the open of the next 5-minute bar. Within a bar the stop is checked "
    "before the take-profit (the cautious order); a stop that gaps fills at the bar's open.",
    "The ladder moves on each bar's high and protects from the next bar; the time limit and "
    "on_new_leader are judged at bar closes.",
    "Filters use 1h RSI(14) and 12h ATR built from 5-minute candles, BTC's 24h and 1h change, "
    "and the coin's first candle as its listing date (unknown for coins older than the data).",
    "A partial take-profit is booked with the rest of the trade when it closes.",
    "Sizing uses the balance without unrealised P&L. Below risk.min_equity_usdt the real bot "
    "halts until restarted; the replay just skips trades until deposits lift it.",
    "Positions still open at the end are booked at their own later exit (or the last price).",
    "Funding transfers follow sweep.*: pct of each win, carried in whole cents until "
    "min_transfer_usdt, and deposits moved out at principal_trigger_x.",
]


# ===================================================================== cli
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=str(ROOT / "config.yaml"))
    common.add_argument("--data", default=str(DEFAULT_DATA), help="folder holding 5m/ (and cache/)")
    common.add_argument("--out", help="report folder (default: <data>/reports/<time>)")
    common.add_argument("--deposit-start", type=float, default=None)
    common.add_argument("--deposit-monthly", type=float, default=None)
    common.add_argument("--slippage-pct", type=float, default=0.05,
                        help="added to exit.fee_pct as the round-trip cost (default 0.05)")
    common.add_argument("--report-rows", type=int, default=150,
                        help="rows per table in report.md (every run is in results.csv)")
    common.add_argument("--keep-trades", action="store_true",
                        help="keep every run's trade list in results.json (big)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", parents=[common], help="replay settings and write a report")
    run.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                     help="change one setting, e.g. gainer.filters.max_rsi_1h=85 (repeatable)")
    run.add_argument("--grid", help="YAML file of settings to vary (every combination is run)")
    run.add_argument("--shard", help="K/N: run only every Nth combination, starting at K")
    sr = sub.add_parser("search", parents=[common],
                        help="round 1: every value of every setting alone; "
                             "round 2: every combination of the round-1 winners")
    sr.add_argument("--space", default=str(ROOT / "tools" / "backtest_space.yaml"))
    sr.add_argument("--select-on", choices=("both", "year1"), default="both",
                    help="year1: pick round-2 settings on year 1 alone, so year 2 is "
                         "an unseen test")
    ex = sub.add_parser("export", parents=[common],
                        help="every new leader, its measurements and its result (CSV)")
    ex.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    ex.add_argument("--csv", required=True)
    mg = sub.add_parser("merge", help="join results.json files from several machines")
    mg.add_argument("files", nargs="+")
    mg.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    def log(msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    if args.cmd == "merge":
        docs = [json.loads(Path(f).read_text()) for f in args.files]
        doc = docs[0]
        seen = {r["id"] for r in doc["runs"]}
        for d in docs[1:]:
            if d["data"] != doc["data"] or d["version"] != doc["version"]:
                raise SystemExit("cannot merge: different data or tool version")
            for r in d["runs"]:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    doc["runs"].append(r)
        write_report(doc, Path(args.out))
        log(f"merged {len(doc['runs'])} runs into {args.out}/report.md")
        return 0

    base_raw = yaml.safe_load(Path(args.config).read_text()) or {}
    grid = yaml.safe_load(Path(args.grid).read_text()) if getattr(args, "grid", None) else {}
    space = yaml.safe_load(Path(args.space).read_text()) if args.cmd == "search" else {}
    dep = (grid or space or {}).get("deposits") or {}
    deposits = (args.deposit_start if args.deposit_start is not None else float(dep.get("start", 50)),
                args.deposit_monthly if args.deposit_monthly is not None else float(dep.get("monthly", 50)))
    data = Path(args.data).expanduser()
    market = Market(data, log)
    oracle = Oracle(market, log)
    started = time.time()
    search_info = None

    if args.cmd == "export":
        sets = {k.strip(): parse_value(v) for k, v in (x.split("=", 1) for x in args.set)}
        st = Settings(base_raw, sets)
        export_events(st, market, oracle, (st.g.exit.fee_pct + args.slippage_pct) / 100,
                      Path(args.csv), log)
        return 0

    if args.cmd == "run":
        sets = {}
        for item in args.set:
            if "=" not in item:
                raise SystemExit(f"--set {item}: expected KEY=VALUE")
            k, v = item.split("=", 1)
            sets[k.strip()] = parse_value(v)
        shard = None
        if args.shard:
            k, n = (int(x) for x in args.shard.split("/"))
            if not 1 <= k <= n:
                raise SystemExit("--shard K/N needs 1 <= K <= N")
            shard = (k, n)
        runs = build_runs(base_raw, grid_specs(grid, sets), shard)
        cost = check_cost(runs, args.slippage_pct)
        log(f"{len(runs)} runs ({len(runs) - 1} variants + live)")
        replay(runs, market, oracle, cost, deposits, log, args.keep_trades)
    else:
        r1 = space.get("round1") or {}
        specs = [dict(name="", overrides={k: v}, group=k, round=1)
                 for k, values in (r1.get("single") or {}).items() for v in values]
        for group, sets_ in (r1.get("bundles") or {}).items():
            specs += [dict(name=group, overrides=ov, group=group, round=1) for ov in sets_]
        runs = build_runs(base_raw, specs)
        cost = check_cost(runs, args.slippage_pct)
        log(f"round 1: {len(runs) - 1} single changes + live")
        replay(runs, market, oracle, cost, deposits, log, args.keep_trades)
        specs2, search_info = round2_specs(runs, space.get("round2") or {}, log,
                                           args.select_on)
        runs2 = build_runs(base_raw, specs2, live=runs[0])[1:]
        if runs2:
            check_cost(runs2, args.slippage_pct)
            replay(runs2, market, oracle, cost, deposits, log, args.keep_trades)
        runs += runs2

    live = runs[0]["settings"]
    doc = dict(
        version=VERSION, generated=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        config=args.config, deposits=dict(start=deposits[0], monthly=deposits[1]),
        cost_pct=cost * 100, slippage_pct=args.slippage_pct, keep_trades=args.keep_trades,
        report_rows=args.report_rows, search=search_info,
        data=dict(coins=len(market.symbols), fingerprint=market.fingerprint,
                  **{"from": fmt(market.t_lo), "to": fmt(market.t_hi)}),
        periods={k: [fmt(a), fmt(b)] for k, (a, b) in periods(market).items()},
        live_settings={k: v for k, v in live.flat.items()
                       if not k.startswith(IGNORED_PREFIXES)},
        runs=[dict(id=r["id"], name=r["name"], round=r.get("round", 1), changes=r["changes"],
                   notes=notes_for(r["settings"]), results=r["results"]) for r in runs])
    out = Path(args.out) if args.out else data / "reports" / time.strftime("%Y%m%d-%H%M%S")
    write_report(doc, out)
    log(f"done in {time.time() - started:.0f}s: {len(runs)} runs -> {out}/report.md")
    return 0


def check_cost(runs: list, slippage_pct: float) -> float:
    if len({r["settings"].g.exit.fee_pct for r in runs}) > 1:
        raise SystemExit("exit.fee_pct must be the same in every run (it is the cost model)")
    return (runs[0]["settings"].g.exit.fee_pct + slippage_pct) / 100


if __name__ == "__main__":
    sys.exit(main())
