# Derivatives and market-microstructure signals for separating continuing pumps from fading ones (Binance USD-M top gainers)

Research date: 2026-10-08. Overall finding: **no published out-of-sample study tests OI, liquidations, order-book depth, basis or long/short ratios for the specific question "will the #1 24h gainer go another +50% to +1000% in 3 days?"** Most claims below are practitioner heuristics, BTC/ETH-only studies, or descriptions of single events. The most useful concrete result is about data: **Binance's free bulk archive has 5-minute open interest and long/short ratios (the `metrics` files) going back years, and about 30-second order-book depth snapshots (`bookDepth`) from 2023 onward, for small altcoins too.** The 30-day limit applies only to the REST endpoint. This means the untested features can be backtested on 2023–2026 top gainers now (verified by listing the S3 bucket directly; see the last section).

## Q1. Rising price with rising vs falling open interest; short-squeeze pumps vs leveraged-long pumps

### Takeaway
The usual reading is that price up with OI up means new longs, and price up with OI down means short covering. This framing is standard, but I found **no out-of-sample evidence** on which pump type lasts longer on altcoins. Commentary on BTC treats short-covering or spot-led rallies with flat or falling OI as "healthier", and broad OI expansion as the sign of overheating. That view rests on narrative, not on tests, and examples show the labels are often mixed.

### Cited Findings
- Practitioner reading: rising OI with rising price means new longs entering and conviction; falling OI with rising price means existing shorts covering. Both are described as momentum. No test is given — [Mudrex Learn](https://mudrex.com/learn/how-open-interest-impacts-crypto-futures/); [Sharpe AI](https://www.sharpe.ai/learn/futures-open-interest)
- CoinDesk (Aug 25, 2026): BTC rose about $62k to $80k in a week (its second-largest weekly gain in five years). BTC-denominated futures OI fell about 9%, from 645,760 to 587,584 BTC, and annualized funding stayed below 10%. The article calls the short-covering rally "structurally healthy" and says low derivatives participation produces steadier moves. This is the author's opinion with no forward test — [CoinDesk](https://www.coindesk.com/markets/2026/08/25/a-bitcoin-short-squeeze-for-the-ages-as-futures-open-interest-collapses)
- Glassnode (Sep–Oct 2026) says the altcoin rally is mostly spot-driven, with coin-denominated alt-perp OI barely up over 30 days and fewer than half of markets adding positions. It contrasts this with Feb 2021 and Dec 2024, when broad OI jumps marked overheating. Here a broad OI jump is treated as a warning, not as fuel. This is aggregate and descriptive — [Glassnode via CryptoBriefing](https://cryptobriefing.com/glassnode-altcoin-leverage-subdued-price-rise/); [Glassnode X article](https://x.com/glassnode/article/2102766576730812638)
- Counterexample to clean labels: ADA rose about 11% in two days while OI rose 25% to $304M. Funding was at its most negative of the month on Oct 2 and then turned positive. Santiment's read was that fresh positions joined the short covering, so "short squeeze" was not a clean label — [CryptoTimes / Santiment](https://www.cryptotimes.io/2026/10/06/santiment-challenges-ada-short-squeeze-narrative-as-open-interest-hits-304m/)
- Practitioner caution: short spells of negative funding (a few days) are common and carry no predictive value — [Phemex blog](https://phemex.com/blogs/bitcoin-funding-rates-negative-46-days-ftx-bottom) (low-authority source)
- Data-quality warning: OI in BTC perpetual swaps is "systematically misquoted by some of the largest derivatives exchanges" when reconciled against traded volume (2023 data, published in Ledger 2024) — [Giagkiozis & Said, arXiv 2310.14973](https://arxiv.org/html/2310.14973v2); [Ledger](https://ledger.pitt.edu/ojs/ledger/article/view/325)
- BIS "Crypto carry" (BTC/ETH, 2019–2024): high carry (futures basis) predicts liquidations of *short* futures positions. A 10% rise in standardized carry predicts short liquidations equal to 22% of OI over the next month. Max leverage sometimes exceeds 100x on crypto-native venues. This is monthly and BTC/ETH only — [BIS WP 1087](https://www.bis.org/publ/work1087.pdf)
- Christin et al. find derivatives volume is driven by the long side, and short-perp/long-spot earned in-sample Sharpe of about 7–10. This means perps carry a persistent long-crowding premium — [The Crypto Carry Trade](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf)

### Inferences
- Two mechanisms pull in opposite directions. (a) A short-squeeze pump (OI falling, funding negative) uses up its own fuel: once shorts are closed, the forced buyers are gone. This argues for fading unless spot buyers take over. (b) A leveraged-long pump (OI rising, funding positive) builds liquidation fuel *below* price, which argues for a sharp reversal once momentum stalls. Neither has been tested for altcoin top gainers. A plausible testable hypothesis: runners are pumps where **OI keeps rising while funding stays flat or negative**. That would mean new shorts keep entering against the move and adding squeeze fuel, as in the "crowded short" pattern. Blow-off-and-fade cases would look like OI rising together with sharply positive funding.
- Normalize OI changes by market cap or 24h volume, and measure them in coin units as well as USD. A price rise mechanically inflates USD OI.
- Because funding *level* alone gave AUC around 0.5 in your tests, try *interactions* and *changes*: ΔOI(coin units) over 1h/4h/24h × sign(funding), OI/market-cap, and the change in top-trader position ratio.

### Gaps
- No paper or vendor study measures the forward returns of altcoin pumps split by the sign of ΔOI. This has to be tested in-house.

## Q2. Liquidation cascades and estimating liquidation clusters from public data

### Takeaway
Liquidation heatmaps such as CoinGlass's are **model estimates** built from OI changes, assumed leverage tiers and entry prices. The methods are proprietary and unvalidated. Binance's public liquidation feed is throttled to one order per symbol per second. The historical bulk liquidation dump was discontinued, and for USD-M it is not on data.binance.vision at all. I found no evidence that "large short-liquidation clusters above price" predict longer pumps.

### Cited Findings
- CoinGlass describes its heatmap as combining market data with several leverage levels to project concentration zones. It says the map is a prediction of trigger zones, not a count, and that the scale is relative — [CoinGlass intro](https://www.coinglass.com/vi/learn/coinglass-intro-en)
- Third-party guide: heatmap providers rebuild positions from OI, funding flows, recent volume and a leverage-usage model, because exchanges do not publish entry prices or leverage. This is secondary and not confirmed by CoinGlass — [DEXTools guide](https://www.dextools.io/tutorials/how-to-read-liquidation-maps-in-crypto-guide-2026)
- Open-source TradingView clones approximate clusters from price and volume alone. One author claims they match CoinGlass's major zones, but this is not independently tested — [TradingView script](https://kr.tradingview.com/script/LFDkYZ0P-LiquidityMap/)
- Binance `!forceOrder@arr` and `<symbol>@forceOrder` streams: "only the latest one liquidation order within 1000ms will be pushed as the snapshot". These streams undercount liquidations during cascades — [Binance docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams)
- Binance's per-event `liquidationSnapshot` dumps have been discontinued — [arXiv 2607.27070](https://arxiv.org/html/2607.27070v1). Verified: on data.binance.vision, `liquidationSnapshot` exists only under COIN-M (`futures/cm/daily`), and for BTCUSD_PERP it runs 2023-06-25 to 2024-10-14. There is no USD-M `liquidationSnapshot` folder (direct S3 listing, 2026-10-08).
- A study of seven BTC liquidation cascades on Binance (2022–2025) finds no early-warning variable that works across events; only endogenous-buildup cascades show a price signature. This is a single-author preprint — [arXiv 2607.27070](https://arxiv.org/pdf/2607.27070)
- On Binance USDT-margined perps (10 coins, 2023–2024), liquidation cascades spread across 6 of 10 assets within 2 hours — [Emerald SEF 2026](https://www.emerald.com/sef/article/doi/10.1108/SEF-04-2026-0367/1388748/Systemic-risk-from-financial-leverage-in-digital)
- In the Oct 2025 crash, futures led, and the BTC basis swung from −$534 to +$833 in eight minutes — [Lim, SSRN 6579278](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=6579278)
- An SEC comment letter summarizing Binance/BitMEX studies says perp markets "can support long-biased speculation and amplify price movements through liquidation cascades" — [SEC comment](https://www.sec.gov/comments/S7-2026-21/s7202621-1011079-3235289.pdf)
- Paid sources: CoinGlass heatmap/API (paid tiers), Kaiko, Amberdata and Laevitas liquidation data are commercial. Amberdata has a free blog explainer only — [Amberdata blog](https://blog.amberdata.io/liquidations-in-crypto-how-to-anticipate-volatile-market-moves)

### Inferences
- A free approximation of a short-liquidation map: take each 5-minute bar where coin-unit OI *increased* (from `metrics`) and treat the increase as new positions opened at that bar's VWAP. Split long vs short using the taker ratio or the change in global long/short ratio. Project liquidation prices at assumed leverage tiers (e.g. 5/10/20/25/50x, with tier weights as a free parameter), and remove "positions" as price crosses their liquidation level or OI falls. The result is a rough estimate, but a backtest can check whether "estimated short notional within +X% above price ÷ ask depth to +X%" separates runners. Treat it as a hypothesis.
- Realized short liquidations can be read indirectly: a sharp fall in OI during a fast up-move, with a spike in the taker-buy ratio, is the signature. That is computable from the free 5-minute `metrics` plus 1-minute klines.
- Live forward collection: subscribe to `!forceOrder@arr` now to build your own liquidation history. It is free but undercounts (1 per symbol per second).

### Gaps
- No evidence was found, for or against, that residual short-liquidation clusters above price predict pump continuation.
- CoinGlass's heatmap methodology and leverage weights are undisclosed.

## Q3. Order-book depth and imbalance (thin asks, spoofing, market-maker withdrawal)

### Takeaway
Order-book imbalance predicts returns over **seconds to minutes**, and the effect is mostly contemporaneous. Effects at multi-hour horizons are tiny (R² around 0.05–0.18%). Nothing shows that it predicts multi-day continuation of altcoin pumps. Thin books do amplify moves, as Kaiko's case studies show. The free Binance `bookDepth` archive makes "ask depth within +1–5% relative to bid depth and to traded volume" testable for 2023–2026.

### Cited Findings
- Quarter-hour opening order imbalance on six Binance perps predicts returns out of sample over about 4–12 hours, but with R² of 0.046%–0.176%. Effects are small — [Kim & Hansen, arXiv 2607.09426](https://arxiv.org/pdf/2607.09426)
- A thesis on Binance futures (BTC/ETH/LTC) finds order-book asymmetry significant mainly over short intervals. The evidence is in-sample, and the author flags costs and latency — [Charles University thesis](https://dspace.cuni.cz/handle/20.500.11956/200516)
- A Hawkes/LOB-imbalance sign forecast was validated on only 50 two-minute samples — [arXiv 2312.16190](https://arxiv.org/html/2312.16190v1)
- An imbalance model shorted the Oct 10, 2025 crash profitably. This is one anecdotal event, and the authors warn about feedback loops if the approach is widely used — [arXiv 2602.00776](https://arxiv.org/pdf/2602.00776)
- In Cont-style order-flow-imbalance work, the strong relationship is contemporaneous, not predictive — summarized in [tradingstrategy.ai order flow](https://tradingstrategy.ai/docs/learn/order-flow.html)
- Kaiko: AEUR rose more than 300% in minutes on its Binance launch as bid and ask depth fell 96% and 92%. For Bittensor, liquidity did not keep pace with a rally from below $50 to above $350. Altcoin depth is concentrated in the top 10 coins, so smaller tokens move more on modest flows. These are case studies, not tests — [Kaiko resources](https://www.kaiko.com/resources/the-crypto-liquidity-concentration-report); [Kaiko Q1 2025](https://marketing.kaiko.com/hubfs/Kaiko%20Research%20-%20Quarterly%20Report,%20Q1%202025.pdf)
- Free data (verified 2026-10-08, see Q5): `futures/um/daily/bookDepth` holds snapshots about every 30 seconds of **cumulative depth (qty and notional) at ±0.2%, ±1%, ±2%, ±3%, ±4%, ±5% from mid**. Sample file 1000PEPEUSDT 2026-10-01 has 34,560 rows with columns `timestamp,percentage,depth,notional`.

### Inferences
- Candidate features from `bookDepth`: ask notional within +1%/+2%/+5% ÷ 1h quote volume ("how much size is needed to push +5%"), the bid/ask depth ratio at 1–5%, the change in total depth over the pump (market makers pulling out vs adding), and the asymmetry of depth withdrawal. A pump where ask depth is thin *and* bid depth is building may differ from one where both sides thin out. This is untested.
- Spoofing cannot be detected from 30-second cumulative snapshots. It needs full L2 diffs, which are not in the free archive.

### Gaps
- No study links book depth or imbalance to multi-day pump continuation.
- Free full-depth L2 order-book history for Binance USD-M was not found. Tardis.dev, Kaiko and CoinAPI sell it (paid).

## Q4. Perp-spot basis / premium index, mark vs index divergence, top-trader long/short ratios

### Takeaway
Theory and BTC/ETH evidence link the basis to future short liquidations and to a long-side crowding premium. I found **no evidence** on altcoin-pump continuation for the premium index, mark-index divergence, or top-trader long/short ratios. All of these are freely available historically and can be tested.

### Cited Findings
- Funding is approximately the average futures-spot spread over the previous 8 hours, and longs pay shorts. Perp prices have no-arbitrage bounds only up to trading costs — [He, Manela, Ross, von Wachter, arXiv 2212.06888](https://arxiv.org/html/2212.06888v5)
- High carry predicts short-futures liquidations (BTC/ETH, monthly) — [BIS WP 1087](https://www.bis.org/publ/work1087.pdf)
- Short-perp/long-spot carry was highly profitable in-sample, and long-side demand drives perp volume — [Christin et al.](https://www.andrew.cmu.edu/user/azj/files/CarryTrade.v1.0.pdf); a Binance study (cited in an SEC letter) found short-perp/long-spot earned high realized returns across many contracts — [SEC comment](https://www.sec.gov/comments/S7-2026-21/s7202621-1011079-3235289.pdf)
- Binance REST endpoints `openInterestHist`, `globalLongShortAccountRatio` and `basis` return only the last 30 days / 1 month (max 500 rows per call) — [Binance docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics)

### Inferences
- The premium index (`premiumIndexKlines`, available at 1-minute resolution in the free archive) is a higher-resolution, forward-looking version of funding. Funding is a lagged, clamped average of it. A test of premium-index *path* features (e.g. a premium that turns deeply negative while price rises, meaning perps are being sold against spot buying) is more promising than funding level, which you already found to give AUC around 0.5.
- For coins without a Binance spot market, the index uses other venues. Mark-index divergence may then reflect thin index sources rather than positioning.
- Top-trader ratios: Binance publishes top-trader *account* and *position* ratios. A divergence where top-trader positions are getting shorter while retail accounts are getting longer is a common practitioner signal, but I found no published test of it.

### Gaps
- No academic or vendor evidence was found on long/short ratios or mark-index divergence as predictors for altcoins.
- Laevitas, Velo, Kaiko and Amberdata publish mainly product pages. I found no free altcoin-pump research from them (their data is paid).

## Q5. Exactly which Binance USD-M historical datasets are free (verified on data.binance.vision, 2026-10-08)

### Takeaway
I verified this by listing the S3 bucket behind data.binance.vision directly (`s3-ap-northeast-1.amazonaws.com/data.binance.vision?prefix=data/futures/um/...`). **USD-M daily folders:** aggTrades, bookDepth, bookTicker, indexPriceKlines, klines, markPriceKlines, metrics, premiumIndexKlines, trades. **USD-M monthly folders:** aggTrades, bookTicker, fundingRate, indexPriceKlines, klines, markPriceKlines, premiumIndexKlines, trades. **There is no USD-M liquidationSnapshot.** Coverage runs through 2026-10-07 (daily) and 2026-09 (monthly) for everything except bookTicker.

### Cited Findings (all from the direct S3 listing; base URL https://data.binance.vision/?prefix=data/futures/um/)
| Dataset | BTCUSDT coverage | Small-cap coverage | Notes |
|---|---|---|---|
| daily/metrics | 2020-09-01 → 2026-10-07 (2,228 files) | 1000PEPEUSDT 2023-05-05 →; MYXUSDT 2025-06-18 →; COAIUSDT 2025-09-25 → 2026-10-07 | 5-minute rows (288/day). Columns: `create_time, symbol, sum_open_interest, sum_open_interest_value, count_toptrader_long_short_ratio, sum_toptrader_long_short_ratio, count_long_short_ratio, sum_taker_long_short_vol_ratio`. **This removes the 30-day REST limit for OI, L/S ratios and taker ratio.** |
| daily/bookDepth | 2023-01-01 → 2026-10-07 (1,373 files) | from listing date (PEPE 2023-05-05; MYX 2025-06-18; COAI 2025-09-25) | About 30-second snapshots of cumulative depth and notional at ±0.2, 1, 2, 3, 4, 5% |
| daily/bookTicker, monthly/bookTicker | 2023-05-16 → 2024-03-30 (daily); 2023-05 → 2024-04 (monthly) | same window for PEPE | **Discontinued**, so it does not cover 2024-04 to 2026 |
| daily/premiumIndexKlines, monthly (1m) | 2019-12 → 2026-10-07 / 2026-09 | PEPE 2023-05 → | Multiple intervals (1m … 12h) |
| markPriceKlines, indexPriceKlines (daily+monthly) | 2020-01 → 2026-09 (monthly 1m) | PEPE 2023-05 → | |
| monthly/fundingRate | 2020-01 → 2026-09 | PEPE 2023-05 → | Monthly files only |
| aggTrades, trades, klines (daily+monthly) | aggTrades 2019-12-31 → 2026-10-07 | PEPE 2023-05-05 → | Tick-level trades allow building your own order-flow imbalance, CVD and large-trade features |
| liquidationSnapshot | **not present for USD-M** | — | Only COIN-M: BTCUSD_PERP 2023-06-25 → 2024-10-14, then stopped (consistent with [arXiv 2607.27070](https://arxiv.org/html/2607.27070v1)) |

- REST-only limits (for live use): `openInterestHist`, `globalLongShortAccountRatio`, `topLongShortPositionRatio` and `basis` cover only the latest 30 days / 1 month, max 500 rows — [Binance docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics). The bulk `metrics` files cover older history.
- Live liquidations: the `!forceOrder@arr` websocket is free but sends at most 1 order per symbol per second — [Binance docs](https://developers.binance.com/docs/derivatives/usds-margined-futures/websocket-market-streams/All-Market-Liquidation-Order-Streams)
- Paid only: CoinGlass liquidation heatmaps and aggregated multi-exchange OI/liquidations via its API (paid tiers), Kaiko, Amberdata, Laevitas, Velo pro data, and Tardis.dev full L2/L3 books.

### Inferences
- Column mapping (my inference from the names; check by comparing against the live REST endpoints on overlapping days): `count_toptrader_long_short_ratio` = top-trader *account* ratio, `sum_toptrader_long_short_ratio` = top-trader *position* ratio, `count_long_short_ratio` = global account ratio, `sum_taker_long_short_vol_ratio` = taker buy/sell volume ratio.
- In the sample PEPE file, timestamps were not strictly in order (e.g. 00:15 followed by 02:20 near the top), although there were 288 rows in total. Sort the rows and check for gaps and duplicates before computing 5-minute changes.
- Recommended test: for each historical day's #1 USD-M gainer in 2023-05 → 2026-09, compute coin-unit ΔOI (1h/4h/since pump start), OI/volume, the three L/S ratios and their changes, premium-index path, ask/bid depth at 1–5% (from bookDepth), and trade-level CVD. Label continuation (+50% within 3 days) vs fade, then measure walk-forward AUC. The free data covers this whole window.

### Gaps
- I did not check every top-gainer symbol for missing days in `metrics`/`bookDepth`. Check delisted symbols in particular, since their folders may remain or be removed.
- I could not confirm when `metrics` files began for all symbols (BTC starts 2020-09-01; newer coins start from listing).
