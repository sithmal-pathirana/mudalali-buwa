"""
Universe scanner: watch many symbols, trade the one worth trading.

Capital decides the shape of this. A $43 account has a risk budget of about
$0.86 per trade, so it can afford exactly one position -- scanning 100 coins
cannot change that. The design is therefore SCAN WIDE, HOLD ONE: rank the
universe every bar, and when flat, trade the best candidate.

Ranking is not a prediction. It scores the conditions the strategy was measured
to need, and nothing else:

  * trendiness   -- efficiency ratio. trend_atr earned +$0.166/trade in
                    trending regimes and lost money everywhere else, so this
                    is the dominant term.
  * room         -- ATR as a percentage of price. A move has to be bigger than
                    the round trip cost before direction matters at all.
  * liquidity    -- 24h quote volume. Thin books turn a modelled fill into a
                    real one at a worse price.
  * affordability - the cheapest legal order must fit the risk budget, or the
                    risk layer will refuse the trade anyway.

Anything failing a hard filter is excluded rather than down-weighted, so a
wildly illiquid coin cannot score its way in on trendiness alone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .regime import efficiency_ratio, realised_vol_pct
from .strategies.base import Bar

log = logging.getLogger("scanner")

# Perpetuals that are not really directional instruments, or are pegged.
EXCLUDE_BASES = {"USDC", "BUSD", "TUSD", "FDUSD", "DAI", "EUR", "USDP", "AEUR"}


@dataclass
class Candidate:
    symbol: str
    price: float
    quote_volume: float
    efficiency: float
    atr_pct: float
    min_notional: float
    score: float = 0.0
    rejected: str = ""
    #: the bars fetched during scoring, reused for the entry decision rather
    #: than re-requested -- 100 symbols is already 100 klines calls
    bars: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.rejected

    def line(self) -> str:
        if self.rejected:
            return f"  {self.symbol:<14} {'--':>8}   rejected: {self.rejected}"
        return (f"  {self.symbol:<14} {self.score:>8.3f}   "
                f"ER {self.efficiency:>5.3f}  ATR {self.atr_pct:>5.2f}%  "
                f"vol ${self.quote_volume/1e6:>8.1f}M  min ${self.min_notional:>6.2f}")


@dataclass
class ScanConfig:
    max_symbols: int = 100          # how many to rank after the volume prefilter
    min_quote_volume: float = 20e6  # 24h USDT volume floor
    min_atr_pct: float = 0.25       # must have room to cover the round trip
    max_atr_pct: float = 12.0       # excludes a coin mid-catastrophe
    min_efficiency: float = 0.30    # must actually be trending
    interval: str = "15m"
    lookback: int = 60
    rescan_seconds: int = 300
    weight_efficiency: float = 1.0
    weight_atr: float = 0.35
    weight_liquidity: float = 0.15


@dataclass
class ScanResult:
    ranked: list[Candidate] = field(default_factory=list)
    rejected: list[Candidate] = field(default_factory=list)
    considered: int = 0
    scanned_at: float = field(default_factory=time.time)
    elapsed: float = 0.0

    @property
    def best(self) -> Candidate | None:
        return self.ranked[0] if self.ranked else None

    def summary(self) -> str:
        if not self.ranked:
            return (f"no candidate passed the filters "
                    f"({self.considered} considered, {self.elapsed:.1f}s)")
        b = self.best
        return (f"best {b.symbol} score {b.score:.3f} "
                f"(ER {b.efficiency:.2f}, ATR {b.atr_pct:.2f}%) -- "
                f"{len(self.ranked)}/{self.considered} passed, {self.elapsed:.1f}s")


class Scanner:
    def __init__(self, api, cfg: ScanConfig | None = None):
        self.api = api
        self.cfg = cfg or ScanConfig()
        self._rules_cache: dict = {}
        self._info = None
        self.last: ScanResult | None = None
        self._last_scan = 0.0

    # ----------------------------------------------------------- universe
    def universe(self) -> list[dict]:
        """
        Liquid, actively traded USDT perpetuals, cheapest-first by one request.

        /ticker/24hr returns every symbol in a single call, which keeps the
        prefilter cheap; only the survivors cost a klines request each.
        """
        if self._info is None:
            self._info = self.api.exchange_info()
        tradable = {
            s["symbol"]: s for s in self._info["symbols"]
            if s.get("status") == "TRADING"
            and s.get("contractType") == "PERPETUAL"
            and s.get("quoteAsset") == "USDT"
            and s.get("baseAsset") not in EXCLUDE_BASES
        }
        tickers = self.api.ticker_24hr()
        rows = []
        for t in tickers:
            sym = t.get("symbol")
            if sym not in tradable:
                continue
            try:
                qv = float(t.get("quoteVolume", 0))
            except (TypeError, ValueError):
                continue
            if qv < self.cfg.min_quote_volume:
                continue
            rows.append({"symbol": sym, "quote_volume": qv,
                         "price": float(t.get("lastPrice", 0) or 0)})
        rows.sort(key=lambda r: -r["quote_volume"])
        return rows[: self.cfg.max_symbols]

    # -------------------------------------------------------------- score
    def score(self, c: Candidate, risk_budget_notional: float) -> Candidate:
        k = self.cfg
        if c.quote_volume < k.min_quote_volume:
            c.rejected = f"illiquid (${c.quote_volume/1e6:.1f}M)"
        elif c.atr_pct < k.min_atr_pct:
            c.rejected = f"too quiet (ATR {c.atr_pct:.2f}%)"
        elif c.atr_pct > k.max_atr_pct:
            c.rejected = f"too violent (ATR {c.atr_pct:.2f}%)"
        elif c.efficiency < k.min_efficiency:
            c.rejected = f"not trending (ER {c.efficiency:.2f})"
        elif risk_budget_notional and c.min_notional > risk_budget_notional:
            c.rejected = (f"min order ${c.min_notional:.2f} exceeds risk budget "
                          f"${risk_budget_notional:.2f}")
        if c.rejected:
            return c

        # Diminishing returns on both volatility and liquidity: past a point,
        # more of either is not better, and untreated it swamps the ER term.
        import math
        atr_term = math.log1p(c.atr_pct) / math.log1p(k.max_atr_pct)
        liq_term = math.log1p(c.quote_volume / k.min_quote_volume) / math.log1p(50)
        c.score = (k.weight_efficiency * c.efficiency
                   + k.weight_atr * atr_term
                   + k.weight_liquidity * min(liq_term, 1.0))
        return c

    # --------------------------------------------------------------- scan
    def scan(self, risk_budget_notional: float = 0.0,
             rules_for=None) -> ScanResult:
        started = time.time()
        res = ScanResult()
        rows = self.universe()
        res.considered = len(rows)

        for row in rows:
            sym = row["symbol"]
            try:
                kl = self.api.klines(sym, self.cfg.interval,
                                     limit=self.cfg.lookback + 2)
            except Exception as e:
                res.rejected.append(Candidate(sym, row["price"], row["quote_volume"],
                                              0, 0, 0, rejected=f"klines: {e}"))
                continue
            bars = [Bar.from_kline(k) for k in kl[:-1]]
            if len(bars) < self.cfg.lookback:
                continue

            min_notional = 0.0
            if rules_for is not None:
                try:
                    min_notional = rules_for(sym).min_affordable_notional(bars[-1].close)
                except Exception:
                    min_notional = 0.0

            c = Candidate(symbol=sym, price=bars[-1].close,
                          quote_volume=row["quote_volume"],
                          efficiency=efficiency_ratio(bars, self.cfg.lookback),
                          atr_pct=realised_vol_pct(bars, 14),
                          min_notional=min_notional, bars=bars)
            c = self.score(c, risk_budget_notional)
            (res.ranked if c.ok else res.rejected).append(c)

        res.ranked.sort(key=lambda c: -c.score)
        res.elapsed = time.time() - started
        self.last = res
        self._last_scan = time.time()
        log.info("scan: %s", res.summary())
        return res

    def due(self) -> bool:
        return time.time() - self._last_scan >= self.cfg.rescan_seconds
