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

    #: Rank and filter by volume from the LIVE exchange even while trading on
    #: testnet. Testnet's own 24h volume is synthetic -- test bots churn it --
    #: which makes min_quote_volume above meaningless there. Measured
    #: 2026-09-20: of the 100 symbols selected on testnet volume, 54 were under
    #: the $20M floor on real volume, and only 46 would have been in the live
    #: universe at all. The bot was trading $5M microcaps believing them to be
    #: $1B markets, and paying for it in slippage. No effect in live mode,
    #: where the trading venue already IS the live venue.
    rank_by_live_volume: bool = False
    #: Drop symbols the live exchange no longer lists. Testnet keeps delisted
    #: contracts in TRADING status long after the real venue has settled them
    #: -- FXSUSDT, RDNTUSDT and SLERFUSDT on 2026-09-20.
    require_listed_live: bool = False


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
            # Name the filter that actually did the damage. "no candidate
            # passed" alone reads like a quiet market, so a budget or config
            # mistake that rejects all 100 symbols looks identical to a genuine
            # lull -- and stays invisible for as long as you are willing to
            # wait for the next scan.
            line = (f"no candidate passed the filters "
                    f"({self.considered} considered, {self.elapsed:.1f}s)")
            if self.rejected:
                import re
                from collections import Counter

                def kind(reason: str) -> str:
                    return re.sub(r"\$[\d.,]+", "$_", reason.split("(")[0].strip())

                top, n = Counter(kind(c.rejected) for c in self.rejected).most_common(1)[0]
                example = next(c.rejected for c in self.rejected if kind(c.rejected) == top)
                line += f" -- {n}/{self.considered} rejected: {example}"
            return line
        b = self.best
        return (f"best {b.symbol} score {b.score:.3f} "
                f"(ER {b.efficiency:.2f}, ATR {b.atr_pct:.2f}%) -- "
                f"{len(self.ranked)}/{self.considered} passed, {self.elapsed:.1f}s")


class Scanner:
    #: How long live-venue liquidity data is reused. The scan runs every
    #: rescan_seconds; refetching a 700-symbol ticker board each time would be
    #: pure waste when 24h volume barely moves in five minutes.
    REF_TTL = 900.0

    def __init__(self, api, cfg: ScanConfig | None = None):
        self.api = api
        self.cfg = cfg or ScanConfig()
        self._rules_cache: dict = {}
        self._info = None
        self.last: ScanResult | None = None
        self._last_scan = 0.0
        self._ref = None
        self._ref_cache = None
        self._ref_at = 0.0
        self._ref_warned = False
        self._last_delisted: list[str] = []

    # ----------------------------------------------------------- universe
    def _live_reference(self):
        """
        Read-only public client on the LIVE venue, for liquidity data only.

        The trading client is untouched: this one never places an order and
        needs no credentials, because /exchangeInfo and /ticker/24hr are
        unsigned. Returns None when there is nothing to borrow -- either the
        feature is off, or the bot is already trading live.
        """
        if not (self.cfg.rank_by_live_volume or self.cfg.require_listed_live):
            return None
        if not getattr(self.api, "testnet", False):
            return None
        if self._ref is None:
            from .binanceapi import Binance
            self._ref = Binance(testnet=False,
                                timeout=getattr(self.api, "timeout", 10))
        return self._ref

    def _live_liquidity(self):
        """
        (quote volumes, listed symbols) from the live venue, cached.

        Returns (None, None) when unavailable, and the caller then falls back
        to the trading venue's own figures -- a scan on imperfect volume data
        beats no scan at all.
        """
        ref = self._live_reference()
        if ref is None:
            return None, None
        if self._ref_cache and time.time() - self._ref_at < self.REF_TTL:
            return self._ref_cache
        try:
            info = ref.exchange_info()
            listed = {s["symbol"] for s in info["symbols"]
                      if s.get("status") == "TRADING"
                      and s.get("contractType") == "PERPETUAL"}
            vols = {t["symbol"]: float(t.get("quoteVolume", 0) or 0)
                    for t in ref.ticker_24hr()}
        except Exception as exc:
            if not self._ref_warned:
                log.warning("live liquidity reference unreachable (%s); using "
                            "the trading venue's own volume instead", exc)
                self._ref_warned = True
            return None, None
        if self._ref_warned:
            log.info("live liquidity reference is back")
            self._ref_warned = False
        self._ref_cache = (vols, listed)
        self._ref_at = time.time()
        return self._ref_cache

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
        live_vols, live_listed = self._live_liquidity()

        if live_listed is not None and self.cfg.require_listed_live:
            gone = sorted(s for s in tradable if s not in live_listed)
            for sym in gone:
                del tradable[sym]
            if gone != self._last_delisted:
                if gone:
                    log.info("universe: dropped %d symbol(s) the live exchange "
                             "no longer lists: %s", len(gone), ", ".join(gone[:12]))
                self._last_delisted = gone

        # Volume decides both the floor and the ranking, so it has to be the
        # real thing. Price still comes from the venue we actually trade on.
        ranking_vols = live_vols if self.cfg.rank_by_live_volume else None

        tickers = self.api.ticker_24hr()
        rows = []
        for t in tickers:
            sym = t.get("symbol")
            if sym not in tradable:
                continue
            if ranking_vols is not None:
                qv = ranking_vols.get(sym)
                if qv is None:
                    continue      # not quoted live: no real liquidity to rank
            else:
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

    def stale(self, now: float | None = None) -> bool:
        """
        True when a bar has closed since the last scan's newest bar.

        The cached candidates carry the bars the scan fetched, and the entry
        decision reuses them. rescan_seconds alone does not keep them current:
        a /scan at 05:13 on 15m bars caches data ending at the 04:45 close, and
        the 05:15 bar-close cycle -- under 300s later -- traded AKEUSDT off it.
        The signal was priced at 0.013395 with the market at 0.0120, so its
        take-profit was already crossed, Binance answered -2021, and the bot
        halted. A signal must never be computed from a bar that is not the
        latest one closed.
        """
        res = self.last
        if res is None or not res.ranked or not res.ranked[0].bars:
            return False
        step = interval_ms(self.cfg.interval)
        if not step:
            return False
        now_ms = int((time.time() if now is None else now) * 1000)
        newest_closed = (now_ms // step) * step - step
        return res.ranked[0].bars[-1].open_time < newest_closed


def interval_ms(interval: str) -> int:
    """Binance kline interval ("15m", "1h", "1d", ...) in ms; 0 if unknown."""
    units = {"m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
    try:
        return int(interval[:-1]) * units[interval[-1]]
    except (KeyError, ValueError, IndexError, TypeError):
        return 0
