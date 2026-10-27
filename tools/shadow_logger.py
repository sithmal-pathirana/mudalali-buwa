"""
Shadow logger: records, from today on, the live-only data that the new runner
and full-slot ideas need (research_notes/new_methods_2026-10-09.md). It never
trades: every request goes through ReadOnly, which refuses anything but GET.

    sudo -u trader python3 tools/shadow_logger.py          # systemd: trading-shadow

Writes JSON lines under <bot>/data/shadow/:
  events-YYYYMMDD.jsonl     one row per new 24h #1: board, churn, Binance risk
                            tiers and funding settings, liquidation fuel, open
                            interest, the bot's positions and the would-be
                            full-slot decisions (ideas 6-10)
  refill-YYYYMMDD.jsonl     the 10 minutes after each new #1: taker buying, and
                            how much of it hit hidden size, i.e. more bought at
                            an ask price than was ever shown there (idea 3)
  liq-YYYYMMDD.jsonl        forced orders per symbol per minute (idea 2)
  brackets.jsonl            leverage tier changes, any symbol (idea 1)
  funding_info.jsonl        funding interval / cap changes, any symbol (idea 1)
  positions-YYYYMMDD.jsonl  the bot's open positions every 15 minutes (idea 9)

Directions fixed BEFORE any data (scored after ~150-300 leaders):
  1  max leverage or tier cap CUT on the coin during its pump -> fade;
     funding switched to 1h while funding is negative -> run
  2  last hour's short-liquidation $ falling while price still rises -> fade
  3  hidden-size share of taker buying (iceberg sellers) high -> fade; low -> run
  4  many distinct #1s in 24h (rotation) -> leaders fade faster
  9  a held trade's remaining return falls with age when it has not armed
REST weight is about 45 a minute, far under the IP's 2,400 shared with the bot.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict, deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.binanceapi import Binance, BinanceError       # noqa: E402
from bot.config import Config                         # noqa: E402
from bot.gainer import EXCLUDE_BASES, rank_board      # noqa: E402

OUT = ROOT / "data" / "shadow"
# Binance split the USD-M streams in 2026: the old root still accepts a
# connection but sends no trades or liquidations (checked 2026-10-09).
WS_MARKET = "wss://fstream.binance.com/market"     # aggTrade, forceOrder
WS_PUBLIC = "wss://fstream.binance.com/public"     # depth
BOARD_SECONDS = 60
TIERS_SECONDS = 600
POSITIONS_SECONDS = 900
REFILL_MINUTES = 10
MAX_WATCHERS = 3
LIQ_KEEP_MINUTES = 240


class ReadOnly:
    """The Binance client with every non-GET request refused."""

    def __init__(self, api: Binance):
        self._api = api

    def get(self, path: str, params: dict | None = None, signed: bool = False):
        return self._api._request("GET", path, params, signed=signed)


def day_file(kind: str, t: float | None = None) -> Path:
    return OUT / f"{kind}-{time.strftime('%Y%m%d', time.gmtime(t or time.time()))}.jsonl"


def write(path: Path, row: dict) -> None:
    with open(path, "a") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def tiers_of(entry: dict) -> dict:
    """What one leverageBracket entry says, reduced to the comparable parts."""
    b = sorted(entry.get("brackets") or [], key=lambda x: x.get("bracket", 0))
    if not b:
        return {}
    return dict(max_lev=b[0].get("initialLeverage"), cap1=b[0].get("notionalCap"),
                cap_last=b[-1].get("notionalCap"), n=len(b))


class IcebergMeter:
    """
    Hidden selling into a pump (idea 3). For every ask price, the most size
    the book ever SHOWED there, against what takers actually BOUGHT there.
    Buying beyond the shown size was filled by size nobody could see, an
    iceberg seller, or one who re-offers as fast as he is hit. Market makers
    flickering their quotes does not count, because only traded size does.
    """

    def __init__(self):
        self.shown: dict[float, float] = defaultdict(float)
        self.bought: dict[float, float] = defaultdict(float)
        self.buy_usd = self.sell_usd = 0.0

    def book(self, asks: list) -> None:
        for p, q in asks:
            p, q = float(p), float(q)
            if q > self.shown[p]:
                self.shown[p] = q

    def trade(self, price: float, qty: float, buyer_is_maker: bool) -> None:
        if buyer_is_maker:
            self.sell_usd += price * qty
        else:
            self.buy_usd += price * qty
            self.bought[price] += qty

    def hidden_usd(self) -> float:
        """Bought beyond the shown size, at prices the book was seen at."""
        return sum(max(0.0, q - self.shown[p]) * p for p, q in self.bought.items() if p in self.shown)

    def seen_usd(self) -> float:
        """Taker buying at prices the book was seen at (the ratio's base)."""
        return sum(q * p for p, q in self.bought.items() if p in self.shown)


def book_usd(levels: list) -> float:
    return sum(float(p) * float(q) for p, q in levels)


class Shadow:
    def __init__(self):
        self.cfg = Config.load(ROOT / "config.yaml")
        if self.cfg.testnet:
            raise SystemExit("shadow logger: live mode only (it records live market data)")
        self.api = ReadOnly(Binance(self.cfg.api_key, self.cfg.api_secret, testnet=False))
        OUT.mkdir(parents=True, exist_ok=True)
        self.state_path = OUT / "state.json"
        st = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.leaders: deque = deque(st.get("leaders", []))      # [t, symbol]
        self.tiers: dict = st.get("tiers", {})
        self.finfo: dict = st.get("finfo", {})
        self.leader = st.get("leader")
        self.liq: dict = defaultdict(lambda: [0.0, 0.0, 0])  # (sym, minute) -> [short$, long$, n]
        self.tradable: set = set()
        self.tradable_at = 0.0
        self.watchers: set = set()

    def save(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(leaders=list(self.leaders), tiers=self.tiers,
                                       finfo=self.finfo, leader=self.leader)))
        tmp.replace(self.state_path)

    async def rest(self, path, params=None, signed=False):
        return await asyncio.to_thread(self.api.get, path, params, signed)

    # ------------------------------------------------------------ the board
    async def board_loop(self):
        while True:
            try:
                await self.board_once()
            except (BinanceError, OSError, ValueError) as e:
                print(f"board: {e}", flush=True)
            await asyncio.sleep(BOARD_SECONDS)

    async def board_once(self):
        now = time.time()
        if now - self.tradable_at > 3600:
            info = await self.rest("/fapi/v1/exchangeInfo")
            self.tradable = {s["symbol"] for s in info.get("symbols", [])
                             if s.get("status") == "TRADING"
                             and s.get("contractType") == "PERPETUAL"
                             and s.get("quoteAsset") == "USDT"
                             and s.get("baseAsset") not in EXCLUDE_BASES}
            self.tradable_at = now
        rows = rank_board(await self.rest("/fapi/v1/ticker/24hr"), self.tradable,
                          self.cfg.gainer.board.min_quote_volume)
        if not rows or rows[0].symbol == self.leader:
            return
        prev, self.leader = self.leader, rows[0].symbol
        self.leaders.append([now, self.leader])
        while self.leaders and self.leaders[0][0] < now - 86400:
            self.leaders.popleft()
        self.save()
        if prev is None:
            return                          # first look after a start is not an event
        await self.event(now, rows[:10], prev)

    async def event(self, now, top, prev):
        sym = top[0].symbol
        row = dict(t=round(now), sym=sym, prev=prev,
                   top=[[r.symbol, round(r.change_pct, 2), round(r.quote_volume)] for r in top],
                   price=top[0].price,
                   churn_24h=len({s for _, s in self.leaders}),
                   leaders_24h=len(self.leaders),
                   tiers=self.tiers.get(sym), funding_info=self.finfo.get(sym))
        try:
            pi = await self.rest("/fapi/v1/premiumIndex", {"symbol": sym})
            row.update(funding=float(pi["lastFundingRate"]), mark=float(pi["markPrice"]),
                       index=float(pi["indexPrice"]))
            oi = await self.rest("/fapi/v1/openInterest", {"symbol": sym})
            row["oi_qty"] = float(oi["openInterest"])
        except (BinanceError, OSError, KeyError, ValueError) as e:
            row["err_market"] = str(e)
        m = int(now // 60)
        for w in (15, 60, 240):
            s = l = n = 0
            for k in range(m - w, m + 1):
                v = self.liq.get((sym, k))
                if v:
                    s, l, n = s + v[0], l + v[1], n + v[2]
            row[f"liq_{w}m"] = [round(s), round(l), n]       # short$, long$, count
        try:
            row["bot"] = await self.bot_context(now)
        except (BinanceError, OSError, KeyError, ValueError) as e:
            row["err_bot"] = str(e)
        write(day_file("events", now), row)
        print(f"#1 {sym} (was {prev}) churn {row['churn_24h']}", flush=True)
        if len(self.watchers) < MAX_WATCHERS:
            task = asyncio.create_task(self.watch_refill(sym, now))
            self.watchers.add(task)
            task.add_done_callback(self.watchers.discard)

    # ------------------------------------------------- the bot's positions
    def tracks(self) -> dict:
        out = {}
        for name in ("gainer", "squeeze"):
            p = ROOT / "data" / f"{name}.json"
            try:
                for s, t in (json.loads(p.read_text()).get("tracks") or {}).items():
                    out[s] = (name, t)
            except (OSError, ValueError):
                pass
        return out

    async def positions(self, now):
        acct = await self.rest("/fapi/v2/account", signed=True)
        eq = float(acct["totalMarginBalance"])
        tr, pos = self.tracks(), []
        for p in acct.get("positions", []):
            amt = float(p.get("positionAmt") or 0)
            if not amt:
                continue
            kind, t = tr.get(p["symbol"], ("other", {}))
            mark = abs(float(p["notional"]) / amt)
            entry = float(t.get("entry") or p.get("entryPrice") or 0)
            peak = float(t.get("peak") or 0)
            pos.append(dict(sym=p["symbol"], kind=kind, notional=round(abs(float(p["notional"])), 2),
                            ret=round(100 * (mark / entry - 1), 2) if entry else None,
                            best=round(100 * (peak / entry - 1), 2) if entry and peak else None,
                            age_h=round((now - float(t["opened_at"])) / 3600, 2)
                            if t.get("opened_at") else None))
        return eq, pos

    async def bot_context(self, now):
        eq, pos = await self.positions(now)
        g, sq = self.cfg.gainer, self.cfg.squeeze
        gain = [p for p in pos if p["kind"] == "gainer"]
        squ = [p for p in pos if p["kind"] == "squeeze"]
        size = eq * g.entry.notional_pct_of_equity / 100
        used = sum(p["notional"] for p in gain)
        full = (len(gain) >= g.entry.max_positions
                or (g.limits.max_exposure_pct > 0 and used + size > eq * g.limits.max_exposure_pct / 100))
        arm = g.exit.ladder_first_pct
        unarmed = [p for p in gain if (p["best"] or 0) < arm]
        idle_sq = max(0, sq.entry.max_positions - len(squ)) * sq.entry.notional_pct_of_equity
        return dict(
            equity=round(eq, 2), positions=pos, gainer_full=full,
            # idea 6: the squeeze budget sitting unused could fund this leader
            would_borrow_squeeze=bool(full and idle_sq >= g.entry.notional_pct_of_equity),
            squeeze_idle_pct=idle_sq,
            # idea 7: a 3% probe is opened (scored offline from candles)
            would_probe=full,
            # ideas 7 and 9: the main position a promotion or swap would sell
            weakest_unarmed=min(unarmed, key=lambda p: p["ret"] or 0)["sym"] if unarmed else None)

    async def positions_loop(self):
        while True:
            now = time.time()
            try:
                eq, pos = await self.positions(now)
                if pos:
                    write(day_file("positions", now), dict(t=round(now), equity=round(eq, 2),
                                                           positions=pos))
            except (BinanceError, OSError, KeyError, ValueError) as e:
                print(f"positions: {e}", flush=True)
            await asyncio.sleep(POSITIONS_SECONDS)

    # ----------------------------------------- Binance's own risk settings
    async def tiers_loop(self):
        while True:
            now = time.time()
            try:
                top = {s for _, s in self.leaders}
                for e in await self.rest("/fapi/v1/leverageBracket", signed=True):
                    s, new = e.get("symbol"), tiers_of(e)
                    old = self.tiers.get(s)
                    if old is not None and new != old:
                        write(OUT / "brackets.jsonl", dict(t=round(now), sym=s, old=old, new=new,
                                                           was_leader_24h=s in top))
                    self.tiers[s] = new
                info = {e["symbol"]: dict(h=e.get("fundingIntervalHours"),
                                          cap=e.get("adjustedFundingRateCap"),
                                          floor=e.get("adjustedFundingRateFloor"))
                        for e in await self.rest("/fapi/v1/fundingInfo")}
                if self.finfo:
                    for s in set(info) | set(self.finfo):
                        if info.get(s) != self.finfo.get(s):
                            write(OUT / "funding_info.jsonl",
                                  dict(t=round(now), sym=s, old=self.finfo.get(s), new=info.get(s),
                                       was_leader_24h=s in top))
                self.finfo = info
                self.save()
            except (BinanceError, OSError, KeyError, ValueError, TypeError) as e:
                print(f"tiers: {e}", flush=True)
            await asyncio.sleep(TIERS_SECONDS)

    # ----------------------------------------------------- websockets
    async def liq_loop(self):
        import websockets
        while True:
            try:
                async with websockets.connect(f"{WS_MARKET}/ws/!forceOrder@arr", ping_interval=60) as ws:
                    async for msg in ws:
                        o = json.loads(msg).get("o") or {}
                        usd = float(o.get("ap") or o.get("p") or 0) * float(o.get("z") or o.get("q") or 0)
                        k = (o.get("s"), int(o.get("T", time.time() * 1000) // 60000))
                        v = self.liq[k]
                        v[0 if o.get("S") == "BUY" else 1] += usd   # BUY = a short was liquidated
                        v[2] += 1
            except Exception as e:                       # noqa: BLE001 -- reconnect on anything
                print(f"liquidations: {e}; reconnecting", flush=True)
                await asyncio.sleep(10)

    async def liq_flush_loop(self):
        while True:
            await asyncio.sleep(60)
            m = int(time.time() // 60)
            for (s, k) in sorted(k for k in self.liq if k[1] == m - 1):
                v = self.liq[(s, k)]
                write(day_file("liq", k * 60), dict(m=k, sym=s, short=round(v[0], 2),
                                                    long=round(v[1], 2), n=v[2]))
            for k in [k for k in self.liq if k[1] < m - LIQ_KEEP_MINUTES]:
                del self.liq[k]

    async def watch_refill(self, sym, t0):
        import websockets
        s, meter, end = sym.lower(), IcebergMeter(), t0 + REFILL_MINUTES * 60
        px = dict(first=None, last=None, hi=0.0, lo=float("inf"))
        books = {}
        mins, done = [], [0.0, 0.0, 0.0]          # per-minute buy$, sell$, hidden$

        async def trades():
            async with websockets.connect(f"{WS_MARKET}/ws/{s}@aggTrade", ping_interval=60) as ws:
                while time.time() < end:
                    try:
                        d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    except asyncio.TimeoutError:
                        continue
                    p, q = float(d["p"]), float(d["q"])
                    px["first"] = px["first"] or p
                    px["last"], px["hi"], px["lo"] = p, max(px["hi"], p), min(px["lo"], p)
                    meter.trade(p, q, bool(d.get("m")))

        async def book():
            async with websockets.connect(f"{WS_PUBLIC}/ws/{s}@depth20@100ms", ping_interval=60) as ws:
                while time.time() < end:
                    try:
                        d = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                    except asyncio.TimeoutError:
                        continue
                    if d.get("a") and d.get("b"):
                        meter.book(d["a"])
                        snap = dict(ask=book_usd(d["a"]), bid=book_usd(d["b"]))
                        books.setdefault("start", snap)
                        books["end"] = snap

        async def minutes():
            while time.time() < end:
                await asyncio.sleep(60)
                now = [meter.buy_usd, meter.sell_usd, meter.hidden_usd()]
                mins.append(dict(buy=round(now[0] - done[0]), sell=round(now[1] - done[1]),
                                 hidden=round(now[2] - done[2])))
                done[:] = now

        try:
            await asyncio.gather(trades(), book(), minutes())
        except Exception as e:                           # noqa: BLE001
            print(f"refill {sym}: {e}", flush=True)
        seen = meter.seen_usd()
        write(day_file("refill", t0), dict(
            t=round(t0), sym=sym, minutes=len(mins), taker_buy=round(meter.buy_usd),
            taker_sell=round(meter.sell_usd), buy_at_seen_prices=round(seen),
            hidden=round(meter.hidden_usd()),
            hidden_share=round(meter.hidden_usd() / seen, 3) if seen else None,
            price_start=px["first"], price_end=px["last"], high=px["hi"] or None,
            low=px["lo"] if px["lo"] < float("inf") else None,
            book20_start=books.get("start"), book20_end=books.get("end"), per_minute=mins))

    async def main(self):
        print("shadow logger started (read-only)", flush=True)
        await asyncio.gather(self.board_loop(), self.tiers_loop(), self.positions_loop(),
                             self.liq_loop(), self.liq_flush_loop())


if __name__ == "__main__":
    asyncio.run(Shadow().main())
