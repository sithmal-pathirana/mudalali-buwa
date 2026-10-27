# Unconventional angles for picking runners among Binance USD-M #1 24h gainers

Scope note: the bot buys the new #1 24h gainer on Binance USD-M perps and exits on a 10%-step trailing ladder with a 30% stop. Conventional features (price, volume, funding, taker flow, rush orders, spot-led checks, listing news, sector contagion, top-N, ML on these) gave AUC around 0.5 out of sample. Below, each idea is labelled **[EVIDENCE]** (some published or empirical support, usually indirect) or **[SPECULATIVE]** (theory or practitioner lore only), with the **data needed** and whether it is **free and historical for 2024–2026**.

Overall finding: I found **no published study that predicts the continuation of individual extreme crypto pumps** with out-of-sample skill. Most evidence is indirect: it concerns cross-sectional reversal or momentum, venue leadership for BTC/ETH, or wash-trading detection. Treat everything below as hypotheses to backtest, not known edges.

## 1. Cross-exchange lead-lag: does the source venue of the pump predict durability?

### Takeaway
Binance leads price discovery for BTC, but there is **no systematic study of altcoin lead-lag across Binance, Bybit, OKX, Bitget, Upbit or DEXs**, and nothing ties the source venue to durability. The strongest indirect evidence: Korean (Upbit) demand is retail-only and less arbitraged, so it can push prices further, while venue-specific post-listing behaviour differs a lot (OKX listings mostly peak on day 1; Bybit and BitMEX listings mostly do not). This is worth testing; it is not established.

### Cited Findings
- Carol Alexander's minute-level analysis (2021 data, 11 products) found that about 70% of bitcoin price moves start on Binance and other venues follow. In Bybit's system, Coinbase was the leader, and the Binance USDT perp usually led when paired with Coinbase. This covers BTC only and is dated. — [Social Science International summary](https://www.socialscience.international/almost-all-bitcoin-price-transmission-comes-from-binance)
- A study of ETH across CEXs and DEXs found that Binance corrects deviations from other venues faster, consistent with Binance being the information centre for liquid assets. — [UFSC repository](https://repositorio.ufsc.br/handle/123456789/247716)
- BitMEX Q1-2025 derivatives report on perp listings at BitMEX, Binance, Bybit and OKX through 18 Mar 2025: over 70% of OKX listings hit their all-time high on day 1. Most Bybit and BitMEX listings kept rising after day 1; 58.33% of BitMEX listings appreciated after day 1, with an average of 7.14 days to peak. So post-listing path depends on the exchange. — [BitMEX blog](https://blog.bitmex.com/2025q1-derivatives-report)
- Upbit listing pumps: HYPER +475%, SYRUP +29%, MOODENG +57% in July 2025. Upbit led all CEXs in SYRUP spot volume ($320M vs Binance's $156M). Smaller circulating market cap went with larger rallies. — [The Defiant](https://thedefiant.io/news/cefi/korean-degens-continue-to-send-altcoins-soaring)
- 4Pillars argues that Korean real-name KRW rails keep foreign market makers and arbitrageurs out, so domestic retail sets prices and momentum compounds without a professional fade. Altcoins are about 85% of Korean volume, and Korea has no domestic futures or margin. — [4Pillars: tale of the tail](https://research.4pillars.io/en/research/the-tale-of-the-tail-in-the-korea-cex)
- 4Pillars also argues the Upbit listing pump is "dying". Upbit listed 58 names in 7 months, which weakens the signal and spreads out demand. — [4Pillars: dying Upbit listing pump](https://research.4pillars.io/en/data/content/the-dying-upbit-listing-pump)
- During Upbit's 2025 hack, when arbitrage bots were paused, local buy orders had a larger price effect. This is anecdotal evidence that Korea-origin moves are less arbitraged. — [MEXC news aggregation](https://www.mexc.com/news/262129) (aggregator; weak source)
- An analyst claim reported by CryptoSlate: price discovery for new tokens happens on DEXs first, and CEXs act as exit liquidity. CAT and ME fell about 70% after listing. This is opinion, not a study. — [CryptoSlate](https://cryptoslate.com/decentralized-exchanges-post-record-1-43t-q3-volume-what-it-means-for-price-discovery/)
- Plazuelo Pascual et al. (arXiv 2506.08718) compare ETH on Binance vs Uniswap v2. I could not extract the results. — [arXiv](https://arxiv.org/pdf/2506.08718)

### Inferences
- **Idea 1a [SPECULATIVE, testable]: Korean premium / Upbit-origin flag.** At signal time, compute the coin's KRW price on Upbit or Bithumb converted at the USDT/KRW rate (Upbit's KRW-USDT market) minus the Binance price (the "kimchi premium"), and check whether Upbit 1-minute volume spiked before Binance's. Hypothesis: pumps driven by Korean retail, with a positive and rising premium, persist longer because they are poorly arbitraged. The counter-hypothesis is that they mean-revert when arbitrage reconnects. Either way it could separate classes.
  - Data: the Upbit public REST API serves historical minute candles (`/v1/candles/minutes/1` with a `to=` parameter). It is free, but there is no bulk archive, so you page through history per coin, within rate limits. Only coins listed on Upbit KRW are covered. This availability is my understanding of the API and should be verified.
- **Idea 1b [SPECULATIVE]: Where did the move start?** For each #1-gainer event, compute the cross-correlation of 1-second or 1-minute returns between Binance USD-M and Bybit linear perp (and Binance spot) over the hour before the signal. Label the event "Binance-led", "Bybit-led" or "spot-led". The user already tested spot-led and it failed, so the incremental hope lies in the **non-Binance perp-led** and **Korea-led** categories.
  - Data: Bybit's public trade archive (public.bybit.com, gzipped daily CSVs per symbol for linear perps and spot) is free. Download tooling exists. — [bybit-bulk-downloader](https://github.com/aoki-h-jp/bybit-bulk-downloader), [Clojure client docs describing public.bybit.com](https://cljdoc.org/d/io.github.bortexz/bybit/0.0.2/api/bortexz.bybit.public-data)
  - I could not confirm an official free OKX bulk archive. Tardis offers free first-of-month samples only, and the OKX REST API has limited lookback. — [Tardis OKX](https://docs.tardis.dev/historical-data-details/okex-futures)
- **Idea 1c [SPECULATIVE]: Venue of perp listing.** If the coin's Bybit, OKX or Bitget perp was listed recently, or the Binance perp is newer than the Bybit one, the "first-day peak" pattern differs by venue (per BitMEX). The listing date is computable from the first file date in each venue's archive. Free.
- **Idea 1d [SPECULATIVE, costly to backtest]: DEX-origin.** If the token's on-chain pool (Uniswap, Raydium, PancakeSwap) moved first, the pump may be on-chain-native and narrative-driven. Free historical DEX swap data requires a node, The Graph or Dune, which is free-tier but laborious. It is impractical on a free server for hundreds of events.

### Gaps
- I found no study of altcoin lead-lag across Binance, Bybit and OKX perps, and no study linking pump source venue to continuation.
- I found no systematic Upbit-vs-Binance timing study. The evidence is anecdotal.
- Free historical availability of OKX tick data is not confirmed.

## 2. Shape and path-signature features (staircase vs spike, higher lows, first pullback, down-candle volume, compression, Hurst)

### Takeaway
There is **no direct evidence** that intraday shape features predict pump continuation. Hurst-type persistence evidence for crypto is mixed, mostly daily, and drifts over time. Pump-and-dump literature does describe a "spike and full retrace" fingerprint (High/Low diverging for a few minutes, then reconverging), which suggests that spike-shaped pumps are the manipulated, fading class.

### Cited Findings
- Karbalaii (arXiv 2504.15790, Poloniex, 485 candidate P&D events, Aug 2024–Feb 2025, minute OHLCV): confirmed pumps show High and Low diverging sharply for a few minutes, then converging back to pre-event levels. Prices returned to pre-event levels right after the pump. There were two archetypes, "pre-accumulated" (volume spikes before) and "on-the-spot". Accumulation spans averaged about 36 hours with a heavy right tail. This is a single-author preprint with internally inconsistent timing claims. — [arXiv 2504.15790](https://arxiv.org/html/2504.15790v1)
- Real-time P&D detection work treats order size as a behaviour signal (early positioning, cautious vs aggressive buying). Pump-day volume was about 62.5x the prior day on Poloniex vs 23.2x on KuCoin. — [arXiv 2412.18848](https://arxiv.org/pdf/2412.18848)
- Hurst persistence (H > 0.5) appears in 4 of the top 5 coins in a 2025 Physica A study, while a 2023 study of a wide sample found returns follow a random walk. Persistence drifts over time, and results depend on window and method. I found no out-of-sample intraday altcoin test. — [Quantpedia summary](https://vvv.quantpedia.com/es/wp-json/wp/v2/posts/790), [IDEAS: JRFM 2023](https://ideas.repec.org/a/gam/jijfss/v11y2023i1p40-d1082630.html), [arXiv 1804.05916](https://arxiv.org/pdf/1804.05916)
- Bitcoin intraday time-series momentum (Shen, Urquhart & Wang, 2022): the first half-hour return predicts the last half-hour return, strongest in high-volume or high-volatility sessions, and attributed to liquidity provision. — [Birmingham research portal](https://research.birmingham.ac.uk/en/publications/bitcoin-intraday-time-series-momentum/)
- Caporale & Plastun: on days with abnormal returns, hourly returns are significantly higher in the direction of the move (intraday momentum after one-day abnormal returns), for BTC, ETH and LTC from 2015 to 2019. — [HU Berlin/SSRN listing](https://ideas.repec.org/a/eee/finlet/v34y2020ics1544612319301904.html) (listing found via search; exact paper link should be checked)

### Inferences (each computable from data.binance.vision 1m klines or aggTrades, free, 2024–2026)
- **Idea 2a [SPECULATIVE, grounded in the P&D fingerprint]: Retrace ratio of the first pullback.** After the first local high following the breakout, measure the drawdown as a fraction of the advance. Hypothesis: runners hold at least 50% of their gain on the first pullback (higher-low structure), while manipulated spikes give back more than 70% (the "reconverge to pre-event level" pattern). This needs the event to be old enough to have had a pullback, which fits the bot's "#1 gainer" timing, since by then the move is often hours old.
- **Idea 2b [SPECULATIVE]: Staircase score.** Count higher lows on 15-minute bars over the pump. Compute R² of log price on time (a linear staircase) versus the share of gain made in the single largest 5-minute bar (spike concentration). Hypothesis: the gain is spread across many bars in runners and concentrated in one bar for fades. Rationale: concentration indicates a single burst of liquidity or manipulation, while distributed gains indicate persistent demand (consistent with the liquidity-provision story in Shen et al.).
- **Idea 2c [SPECULATIVE]: Down-candle volume asymmetry.** Ratio of volume on red 5-minute bars to volume on green 5-minute bars during the pump, or better, aggTrades seller-initiated volume during pullbacks. A low ratio means pullbacks happen on light selling (absorption). This is Wyckoff-style practitioner lore; I found no crypto evidence.
- **Idea 2d [SPECULATIVE]: Pre-breakout compression.** Ratio of realised volatility (or high-low range) over the 3–7 days before the pump to the coin's 30-day average. The pre-accumulation archetype in Karbalaii suggests manipulated pumps follow dormancy, but compression is also a classic breakout precursor, so the sign is ambiguous. Free from 1-hour klines.
- **Idea 2e [SPECULATIVE, weak priors]: Rolling Hurst or variance ratio** on 1-minute returns during the pump window, with variance ratio VR(q) > 1 indicating trending. Evidence for Hurst as a predictor is mixed and noisy on short windows; it has low priority.
- **Idea 2f [SPECULATIVE]: Path signatures.** Signature transforms of (time, log price, cumulative volume) fed to a simple linear model. Given that the ML on scalar features gave AUC 0.5, signatures are unlikely to rescue it with only a few hundred events, because the sample is too small for high-dimensional features. Mention this only as a last resort.

### Gaps
- I found no study testing shape, retrace or staircase features on crypto pump continuation.
- I found no study of down-volume asymmetry in crypto.

## 3. Time-of-day, day-of-week and funding-settlement timing

### Takeaway
Time-of-day effects in crypto are real for **liquidity and volatility** (they peak during US/London overlap) and somewhat for BTC returns (US hours stronger). Nothing ties them to the durability of altcoin pumps. The hypothesis "a pump that starts in thin Asian or weekend hours fades when the US and EU liquidity arrives" is plausible and cheap to test.

### Cited Findings
- Crypto liquidity peaks when US and London equity markets are both open. Volume is lower and spreads wider when CME BTC futures are closed (weekends). — [arXiv 2109.12142](https://arxiv.org/pdf/2109.12142)
- Amberdata, BTC/USDT on Binance 2018–2023: US hours had only 2 of 8 negative-return hours vs 3 for EU and 4 for Asian hours. Returns correlate with rising volume from late EU hours and peak in the first US hours. This is a practitioner report. — [Amberdata PDF](https://go.amberdata.io/hubfs/TradeBetweenHrsVolatDispersAccrExch.pdf)
- NYDIG: BTC returns up to the 2017 peak came mostly from Asia hours; North America dominated through 2020. So the session effect is regime-dependent. — [NYDIG](https://nydig.com/research/a-look-at-geographic-drivers-of-returns)
- Concretum: intraday BTC trend-following shows seasonality in when trends persist, with a gross Sharpe of about 1.6 over 2018–2025 using volatility scaling. — [Concretum](https://concretumgroup.com/seasonality-in-bitcoin-intraday-trend-trading/)

### Inferences
- **Idea 3a [SPECULATIVE, cheap]:** Bucket the signal time by UTC hour (0–8 Asia, 8–13 EU, 13–21 US, 21–24 dead zone) and by weekday vs weekend. Measure the forward-3-day max-gain distribution per bucket. Free from klines. The risk is multiple-comparison overfitting with few hundred events per year, so pre-register just 2–3 buckets.
- **Idea 3b [SPECULATIVE]:** Minutes until the next funding settlement (00/08/16 UTC, or the coin's own interval if Binance set it to 4h or 1h). Shorts often cover, or longs exit, around settlement when funding is extreme. Binance moves high-volatility coins to 1–4h funding intervals; **a change of funding interval is itself a "Binance sees this as extreme" flag**. The historical funding-rate files on data.binance.vision (`fundingRate` monthly) show the interval implicitly through timestamps. Free.
- **Idea 3c [SPECULATIVE]: "Survives the US open"** as a confirmation filter. Don't buy Asian-session #1s until the coin holds its gains through 13:30–15:00 UTC. This is an option-like delay, discussed further in section 8.

### Gaps
- I found no altcoin-specific study of intraday or session effects on continuation.

## 4. Relative-strength persistence (time spent at #1) and market breadth / altcoin regime

### Takeaway
The academic literature splits. Short-horizon **reversal** dominates among small and illiquid coins, while "MAX momentum" and **time-series** momentum appear among large winners and in upturns. **Market regime and dispersion** are the most plausible conditioners. A breadth or regime filter is a better-supported idea than any per-coin feature.

### Cited Findings
- Kozlowski, Puleo & Zhou (2021, 200 coins, 2015–2019): significant reversal at daily, weekly and monthly frequency, strongest in small, illiquid coins. — [IDEAS](https://ideas.repec.org/a/taf/apeclt/v28y2021i11p887-893.html), [Fairfield](https://fairfield.elsevierpure.com/en/publications/cryptocurrency-return-reversals/)
- The "MAX momentum" paper (Reading): coins with the highest extreme daily return in the past month outperform those with the lowest. The effect is strongest in market upturns and in low-sentiment periods. This is the opposite of the equity MAX effect. I could not open the full paper to confirm authors or venue. — [Reading eprint](https://reading-clone.eprints-hosting.org/99101/1/MAX%20Momentum.R1.pdf)
- Han, Kang & Ryu: under realistic assumptions, time-series momentum evidence is strong and cross-sectional momentum weak. Momentum is concentrated among large winners; losers often rebound. Transaction costs and daily fluctuations liquidate many momentum portfolios. — [AUT ACFR PDF](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- Crypto momentum suffers severe crashes, and one coin can make portfolio momentum insignificant. Volatility management mitigates the crashes. — [Univ. of Vaasa](https://osuva.uwasa.fi/items/2b92e591-acb3-445f-b33d-1a943a8972f5/full)
- One GitHub backtest found cross-sectional dispersion fell from 3.20% (before 2023) to 1.98% after, and the gross momentum Sharpe went from 1.12 to -0.68. This is a single unreviewed source. — [GitHub README](https://raw.githubusercontent.com/prams2104/crypto-momentum-backtest/main/README.md)
- Bitfinex warned that altcoin open interest made higher highs while the OTHERS index (alts outside the top 10) did not. Rising leverage without breadth signals crowding. — [Informer Post summary](https://theinformerpost.beehiiv.com/p/bitfinex-issues-altcoin-warning-says-concerning-indicator-warranting-caution-despite-rallies)
- Borgards (2021): crypto momentum lasts longer and is larger than equity momentum at all frequencies, which the author attributes to noise traders and the lack of an anchoring intrinsic value. — [search result listing](https://econpapers.repec.org/RePEc:eee:ecofin:v:57:y:2021:i:c:s1062940821000590)

### Inferences
- **Idea 4a [EVIDENCE-adjacent]: Regime filter.** Trade #1 gainers only when the broad alt index is trending up, for example equal-weighted USD-M alts above their 20-day EMA, or BTC above its 50-day EMA. MAX momentum is "best in upturns", and momentum crashes cluster in reversals. It is computable from free daily klines for every USD-M symbol.
- **Idea 4b [SPECULATIVE]: Breadth or dispersion at signal time.** Count the coins up more than 20% in 24h, and measure cross-sectional return dispersion. There are two competing hypotheses. (i) A "lone pump" in a flat market is idiosyncratic and likely manipulated, so it fades. (ii) A "hot tape", with many coins pumping, means speculative capital is abundant, so runners are more likely, though attention is split. Test both directions. This is the regime idea most consistent with the dispersion-collapse evidence.
- **Idea 4c [SPECULATIVE]: Tenure at #1.** Track hours at #1 (or in the top 3) since it first took the spot, and how quickly the previous #1 was dethroned. A coin still #1 after N hours has survived displacement. Combine this with a delayed entry (section 8). Computable by reconstructing rolling 24h returns for all symbols from 1-minute klines. Free, but heavy compute: about 300–500 symbols x 1440 minutes/day, feasible in chunks.
- **Idea 4d [SPECULATIVE]: Large vs small winner.** Han et al. say momentum is concentrated among large winners, while reversal is strongest among small, illiquid coins. Condition on 24h quote volume or OI rank, not price change. The user may already have volume features. The novel twist is to use **rank within the USD-M universe on that day**, not raw level.

### Gaps
- I found no study measuring "tenure at #1" or the gainers-list breadth effect on subsequent returns.

## 5. Market-maker and manipulation fingerprints in trades

### Takeaway
There is solid academic methodology (Cong et al.) for detecting wash or fake trading from **trade-size distributions**: Benford first-digit deviation, excess clustering at round sizes, and broken power-law tails. Aloosh & Li validated Benford and clustering (but not power-law tails) with leaked exchange data. Applying these **per coin, per event** to Binance aggTrades is novel. The hypothesis is that pumps with anomalous, bot-like size distributions are orchestrated and fade.

### Cited Findings
- Cong, Li, Tang & Yang, "Crypto Wash Trading" (Management Science 2023): across 29 exchanges, authentic trading follows Benford's law in first significant digits, clusters at round sizes, and has power-law tails. Unregulated exchanges violate these, and their estimated wash share averaged above 70% of volume. — [arXiv 2108.10984](https://arxiv.org/pdf/2108.10984), [NBER w30783](https://www.nber.org/system/files/working_papers/w30783/w30783.pdf)
- Aloosh & Li used leaked internal records from a major BTC exchange. Benford, size-clustering, lognormal and structural-break measures were useful, while power-law-tail measures gave opposite conclusions. — [DCU repository](https://doras.dcu.ie/32898/1/Direct%20evidence%20of%20bitcoin%20wash%20trading.pdf)
- Sila, Kočenda, Kristoufek & Kukacka applied roundness and Benford tests to 4 major coins, Nov 2020–Jan 2022. — [Quantpedia summary](https://quantpedia.com/?p=37233)
- P&D detection: order size distinguishes early positioning, cautious buying and aggressive buying during pumps. — [arXiv 2412.18848](https://arxiv.org/pdf/2412.18848)

### Inferences
- **How to compute from Binance aggTrades** (columns: agg_id, price, qty, first_id, last_id, time, is_buyer_maker). The files are free at data.binance.vision under `futures/um/daily/aggTrades/<SYM>/`. Note that aggTrades merge fills of a single taker order at the same price, so `qty` approximates the **taker order size** (better than raw fills for this purpose), and `last_id - first_id + 1` gives the number of fills.
  1. **Benford deviation [EVIDENCE for the method; SPECULATIVE as a predictor]:** take the first significant digit of qty and of notional (qty x price) for trades in the pump window. Compute chi-square or MAD vs Benford, and compare with the same coin's prior 7 days.
  2. **Round-size clustering:** share of trades whose qty or notional is a round number (multiples of 10, 100 or 1,000 units, or of $100 or $1,000 notional), relative to neighbouring bins, as in Cong et al.
  3. **Repeated identical sizes:** share of taker trades whose exact qty repeats at least K times within 60 seconds, and an inter-arrival-time regularity score (coefficient of variation of gaps for same-size trades). Metronomic, identical clips point to a single algorithm, either a TWAP accumulator (good?) or a painting bot (bad?).
  4. **Size-mix shift:** fraction of buy notional from trades above $10k or $50k vs below $1k, before and during the pump. Hypothesis: runners attract widening participation (growing small-trade count, many distinct sizes). Fades are one-whale or one-algo events, where a handful of big clips are followed by retail chasing.
  5. **Fills per aggTrade** as a sweep-depth proxy: large buy aggTrades consuming many fills = market orders sweeping thin books.
- Rationale: the user's failed "taker flow" feature measures direction, not **who** is trading. Size-distribution "texture" is orthogonal and rarely used by retail bots.

### Gaps
- I found no study using Binance futures aggTrades size distributions to predict pump outcomes.
- Wash-trading tests are calibrated on exchange-level distributions. Small per-event samples may be noisy. Several thousand trades per window is likely needed; most #1 gainers clear that easily.

## 6. Behavioural angles: attention saturation, the #1 slot on the Binance app, trending pages

### Takeaway
Academic evidence links investor attention (Google searches, Twitter) to positive short-horizon crypto returns with only partial reversal, and past returns drive future attention. Nothing measures the "#1 in the Binance app gainers list" effect. The historical data for attention proxies (Binance Square, CoinMarketCap/CoinGecko trending) is largely **not freely available historically**, which makes most of these angles hard to backtest.

### Cited Findings
- Liu & Tsyvinski: investor-attention proxies, including Google searches, strongly forecast crypto returns. A 1-standard-deviation rise in BTC Google searches predicts about +1.8% to +2.3% returns over the next one to two weeks; summaries differ on horizon. — [NBER w24877](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf), [VoxEU](https://cepr.org/voxeu/columns/risks-and-returns-cryptocurrencies)
- Coins with abnormal Google search volume show higher subsequent returns, volatility and volume, with only partial reversal. — [IDEAS (Behav. Exp. Finance 2024)](https://ideas.repec.org/a/eee/beexfi/v44y2024ics2214635024001060.html)
- Lin (2021): past returns strongly drive future attention, and the reverse effect is weak. — [IDEAS FRL 2021](https://ideas.repec.org/a/eee/finlet/v40y2021ics1544612320306590.html)
- Maître, Pugachyov & Weigert (CFR WP 25-02, Twitter 2018–2022): abnormal attention is positively associated with same-day and next-day performance. — [CFR PDF](https://www.cfr-cologne.de/download/workingpaper/cfr-25-02.pdf)

### Inferences
- **Idea 6a [SPECULATIVE]: Attention saturation.** If the coin was already a top-3 gainer within the last 7–14 days (repeat appearances), the retail audience has already seen it, so novelty is gone. A first-ever appearance has more untapped attention. Computable from free klines by reconstructing past gainer rankings.
- **Idea 6b [SPECULATIVE]: Google Trends.** Hourly data is only available for short recent windows, and daily data over longer ranges is sampled and rescaled, so historical intraday backtests are unreliable. pytrends is unofficial and rate-limited. Low priority.
- **Idea 6c [SPECULATIVE]: "Displayed #1" mechanics.** Retail scanning the Binance app sees the top 24h % change list. A coin that becomes #1 because the previous #1's 24h window rolled off (passive) is different from one that overtook it by rising (active). This is free to compute from klines and is a novel, cheap feature.
- CoinGecko/CMC trending and Binance Square have no free history; they can only be collected forward live.

### Gaps
- I found no evidence on the effect of the Binance app gainers list itself.
- I found no free historical archive of CMC/CoinGecko trending or Binance Square post counts.

## 7. What public pump-riding bot builders and prop traders report

### Takeaway
I found **no credible public record** (GitHub repos with audited logs, or verified prop write-ups) of a profitable "buy the top gainer" strategy on Binance futures. Public "top gainer" bots are dashboards or unproven. The credible practitioner consensus is that edge comes from **trend-following mechanics plus volatility sizing across many assets**, not from picking which pump runs.

### Cited Findings
- wferreirauy/binance-bot has a "top gainers" TUI dashboard and a backtester but publishes no results. — [pkg.go.dev](https://pkg.go.dev/github.com/wferreirauy/binance-bot)
- Gainium forum: self-reported 1-minute futures strategy results, +13.22% in 2022 and +39.17% in 2023. Commenters note that backtests ignored funding and generated too many trades. This is unverified. — [Gainium community](https://community.gainium.io/t/very-simple-and-profitable-futures-trading-bot-need-help-optimising/2255)
- Man Group: crypto suits trend-following because of high volatility and no valuation anchor. The best risk-adjusted results came from about 10–15 coins; beyond that, costs outweigh diversification. Breakout and Donchian families are standard. — [Man Group](https://www.man.com/insights/in-crypto-we-trend)
- Barbon, Pagani & Zarattini ("Catching Crypto Trends", SFI RP 25-80): an ensemble of Donchian breakouts across lookbacks with volatility-based sizing, on a survivorship-free universe of all coins since 2015. — [abarbon.com](https://abarbon.com/papers/catching-crypto-trends)
- AdaptiveTrend (arXiv 2602.11708): 6h trend signals, volatility-calibrated trailing stop, over 150 pairs, claimed Sharpe 2.41 over 2022–2024. Single author and likely overfit, so treat with skepticism. — [arXiv 2602.11708](https://arxiv.org/pdf/2602.11708.pdf)

### Inferences
- The practitioner edge appears to be **exit design and sizing across many trends**, which agrees with the user's finding that entry prediction is about AUC 0.5. The bot's trailing ladder is already of this type. Improvements are likelier to come from volatility-scaled stops (stop at k x ATR instead of a fixed 30%) and an ensemble of exits than from new entry features.

### Gaps
- I found no X/Twitter threads or verifiable prop desk reports with audited numbers on this specific strategy.

## 8. Meta-angle: approaches that avoid needing to predict

### Takeaway
Given AUC around 0.5, the most defensible route is to **change the payoff structure, not the selection**. That means regime gating, many small bets, adding size only after confirmation (pyramiding), volatility-scaled stops, and time stops. Evidence: momentum concentrates in large winners and is crash-prone, and volatility management mitigates the crashes. These ideas shape the right tail without requiring a classifier.

### Cited Findings
- Volatility management mitigates crypto momentum crashes. — [Univ. of Vaasa](https://osuva.uwasa.fi/items/2b92e591-acb3-445f-b33d-1a943a8972f5/full)
- Momentum is concentrated in large winners, and losers rebound. — [AUT ACFR PDF](https://acfr.aut.ac.nz/__data/assets/pdf_file/0009/918729/Time_Series_and_Cross_Sectional_Momentum_in_the_Cryptocurrency_Market_with_IA.pdf)
- Trend-following with ensemble breakouts and volatility sizing works across all coins. — [abarbon.com](https://abarbon.com/papers/catching-crypto-trends), [Man Group](https://www.man.com/insights/in-crypto-we-trend)
- Most BitMEX and Bybit listings keep rising after day 1, while most OKX listings peak on day 1. A delayed-entry or confirmation rule can reasonably distinguish them. — [BitMEX blog](https://blog.bitmex.com/2025q1-derivatives-report)

### Inferences (all backtestable on free klines)
- **8a Pyramid into confirmation [SPECULATIVE but structurally sound]:** enter 1/3 size at signal, add 1/3 at +20%, and another 1/3 at +40%, all with the same trailing ladder. In a 59%-fade population, most fades are stopped out on the small starter, while runners carry full size. This turns "prediction" into "let the market tell you". It pairs with Idea 2a (add only after a held first pullback).
- **8b Delayed entry / tenure filter:** buy only if the coin is still #1, or still within 10% of its high, N hours after first becoming #1. This trades some upside for selection by survival.
- **8c Volatility-scaled initial stop** (for example 2–3x the 1h ATR at entry) instead of a fixed 30%, with position size set so that the dollar risk is constant. Small bets in wild coins, larger in orderly ones.
- **8d Time stop:** exit if the coin has not made a new high within X hours. This frees capital, which is consistent with the user's view that held positions are an opportunity cost.
- **8e Regime gate (Idea 4a):** do not trade in down-trending alt regimes, or trade at half size.
- **8f Many small bets:** trade the top 3 gainers each at a third of the size, or every new #1 at a smaller size. The user tried top-N portfolios, but combining them with 8a or 8c changes the payoff shape, so it may be worth re-testing.
- **8g Option-like exposure:** Binance options cover few altcoins, so this is generally not available for the target coins. The pyramid plus trailing stop is the synthetic equivalent.

### Gaps
- I found no published backtest of pyramiding or delayed-entry rules specifically on crypto top-gainer events.

---

### Data availability summary (free, 2024–2026)
| Data | Source | Free history? | Ideas |
|---|---|---|---|
| USD-M 1m klines, aggTrades, trades, fundingRate | data.binance.vision `futures/um/` | Yes | 2, 3, 4, 5, 6c, 8 |
| Binance spot klines/aggTrades | data.binance.vision `spot/` | Yes | 1b |
| Binance futures OI and long/short metrics (5m) | data.binance.vision `futures/um/daily/metrics` (verify) | Likely yes | squeeze/OI ideas |
| Bybit linear perp and spot trades | public.bybit.com | Yes | 1b, 1c |
| OKX trades | REST only (limited lookback); Tardis paid, samples free | Not confirmed | 1b |
| Upbit KRW minute candles | Upbit REST `candles/minutes` paging (verify) | Yes, slow | 1a |
| DEX swaps | Dune / The Graph / own node | Free tier, laborious | 1d |
| Google Trends | pytrends (unofficial) | Coarse only | 6b |
| CMC/CoinGecko trending, Binance Square | none | No | 6 |
