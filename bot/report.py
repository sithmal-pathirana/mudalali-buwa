"""
Run report: what actually happened, from the exchange's own records.

Local logs say what the bot BELIEVED. `/fapi/v1/income`, `/userTrades` and
`/allOrders` say what the exchange DID. Where those disagree is exactly where
the interesting bugs live -- and the reconciliation path has never been checked
against a real account, so this comparison is the point of the report, not a
side effect of it.

The output carries no keys, no tokens and no account identifiers, so it is safe
to paste anywhere.
"""

from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def _ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%m-%d %H:%M")


def gather(api, symbol: str, days: int) -> dict:
    start = int((time.time() - days * 86400) * 1000)
    out: dict = {"symbol": symbol, "days": days,
                 "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")}

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as e:
            out.setdefault("errors", []).append(f"{label}: {type(e).__name__}: {e}")
            return default

    out["equity"] = safe("equity", api.usdt_equity, 0.0)
    out["income"] = safe("income", lambda: api.income(start_ms=start, limit=1000), [])
    out["trades"] = safe("userTrades",
                         lambda: api.user_trades(symbol, start_ms=start), [])
    out["orders"] = safe("allOrders",
                         lambda: api.all_orders(symbol, start_ms=start), [])
    return out


def _local_state() -> dict:
    p = ROOT / "data" / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _log_signals(days: int) -> dict:
    """Cheap signal counts from the bot's own log, if one is present."""
    p = ROOT / "logs" / "bot.log"
    if not p.exists():
        return {}
    counts = Counter()
    regimes = Counter()
    try:
        text = p.read_text(errors="replace")
    except OSError:
        return {}
    for line in text.splitlines():
        for key, needle in (
            ("restarts", "mode=testnet"), ("restarts_live", "mode=live"),
            ("halts", "HALTED"), ("stream_drops", "websocket dropped"),
            ("stream_stale", "stream silent"), ("reconnects", "websocket connected"),
            ("rejected_by_risk", "rejected by risk"),
            ("exchange_errors", "exchange error"),
            ("no_signal", "no signal"),
        ):
            if needle in line:
                counts[key] += 1
        for r in ("trending", "ranging", "unclear"):
            if f"[{r} " in line:
                regimes[r] += 1
    return {"log_counts": dict(counts), "log_regimes": dict(regimes)}


def render(data: dict) -> str:
    L: list[str] = []
    add = L.append
    sym, days = data["symbol"], data["days"]

    add("=" * 68)
    add(f"  RUN REPORT   {sym}   last {days} days")
    add(f"  generated {data['generated']}")
    add("=" * 68)

    if data.get("errors"):
        add("\n  COULD NOT FETCH:")
        for e in data["errors"]:
            add(f"    {e}")

    # ---------------------------------------------------------- money
    income = data.get("income") or []
    by_type = defaultdict(float)
    by_day = defaultdict(float)
    for row in income:
        try:
            amt = float(row.get("income", 0))
        except (TypeError, ValueError):
            continue
        by_type[row.get("incomeType", "?")] += amt
        by_day[_day(int(row.get("time", 0)))] += amt

    net = sum(by_type.values())
    add(f"\n  EQUITY NOW            {data.get('equity', 0):,.2f} USDT")
    add(f"  NET OVER PERIOD       {net:+.4f} USDT")
    if days:
        add(f"  PER DAY               {net / days:+.4f} USDT")

    if by_type:
        add("\n  where it came from")
        for k, v in sorted(by_type.items(), key=lambda kv: -abs(kv[1])):
            add(f"    {k:<22} {v:+12.4f}")

    if by_day:
        add("\n  by day")
        add(f"    {'date':<12}{'net':>12}   {'cumulative':>12}")
        cum = 0.0
        for d in sorted(by_day):
            cum += by_day[d]
            add(f"    {d:<12}{by_day[d]:>+12.4f}   {cum:>+12.4f}")
        vals = list(by_day.values())
        green = sum(1 for v in vals if v > 0)
        add(f"\n    {green}/{len(vals)} days positive"
            f"   best {max(vals):+.4f}   worst {min(vals):+.4f}")

    # ---------------------------------------------------------- execution
    trades = data.get("trades") or []
    orders = data.get("orders") or []
    add(f"\n  FILLS                 {len(trades)}")
    add(f"  ORDERS PLACED         {len(orders)}")

    if orders:
        st = Counter(o.get("status", "?") for o in orders)
        add("\n  order outcomes")
        for k, v in st.most_common():
            add(f"    {k:<22} {v:>4}")
        types = Counter(o.get("type", "?") for o in orders)
        add("\n  order types")
        for k, v in types.most_common():
            add(f"    {k:<22} {v:>4}")

        filled = [o for o in orders if o.get("status") == "FILLED"]
        limits = [o for o in orders if o.get("type") == "LIMIT"]
        if limits:
            lf = sum(1 for o in limits if o.get("status") == "FILLED")
            add(f"\n  limit entry fill rate  {lf}/{len(limits)} "
                f"({lf / len(limits) * 100:.0f}%)")
            add("    (the backtest assumes ~ this; a big gap means the model is wrong)")

        # Slippage: what we asked for vs what we got.
        slips = []
        for o in filled:
            try:
                want, got = float(o.get("price", 0)), float(o.get("avgPrice", 0))
            except (TypeError, ValueError):
                continue
            if want > 0 and got > 0:
                slips.append((got - want) / want * 100)
        if slips:
            add(f"\n  slippage on filled orders (% vs requested price)")
            add(f"    median {statistics.median(slips):+.4f}   "
                f"mean {statistics.fmean(slips):+.4f}   "
                f"worst {max(slips, key=abs):+.4f}")

    if trades:
        fees = sum(float(t.get("commission", 0)) for t in trades)
        makers = sum(1 for t in trades if t.get("maker"))
        add(f"\n  commission paid       {fees:.4f}")
        add(f"  maker fills           {makers}/{len(trades)} "
            f"({makers / len(trades) * 100:.0f}%)")
        add("    (low maker % means entries are crossing the spread and paying more)")
        add("\n  last fills")
        for t in trades[-8:]:
            add(f"    {_ts(int(t.get('time', 0)))}  {t.get('side','?'):<4} "
                f"{float(t.get('qty', 0)):>10.4f} @ {float(t.get('price', 0)):>12,.4f}"
                f"   pnl {float(t.get('realizedPnl', 0)):>+9.4f}"
                f"   {'maker' if t.get('maker') else 'taker'}")

    # ---------------------------------------------------------- bot's own view
    st = data.get("state") or {}
    if st:
        add("\n  BOT STATE (local)")
        for k in ("day", "day_start_equity", "trades_today", "realized_today",
                  "total_trades", "halted", "halt_reason", "schedule_start_date",
                  "strategy_override", "target_reached_today"):
            if k in st and st[k] not in ("", None):
                add(f"    {k:<24} {st[k]}")

    lg = data.get("log") or {}
    if lg.get("log_counts"):
        add("\n  FROM THE LOG")
        for k, v in sorted(lg["log_counts"].items()):
            add(f"    {k:<24} {v}")
    if lg.get("log_regimes"):
        tot = sum(lg["log_regimes"].values()) or 1
        add("\n  regime split (signals tagged)")
        for k, v in sorted(lg["log_regimes"].items()):
            add(f"    {k:<24} {v}  ({v / tot * 100:.0f}%)")

    # ---------------------------------------------------------- the check
    add("\n  " + "-" * 64)
    local_trades = st.get("total_trades")
    if local_trades is not None and trades:
        add(f"  RECONCILIATION  bot counted {local_trades} fills, "
            f"exchange shows {len(trades)}")
        if local_trades != len(trades):
            add("    MISMATCH -- worth investigating; this path has never been")
            add("    verified against a real account.")
        else:
            add("    agree")
    add("=" * 68)
    return "\n".join(L)
