# Structural, supply-side and event-driven causes of extreme altcoin pumps on Binance USD-M futures (2024–2026), and whether they predict continuation vs reversal

Scope note: about 17 searches/fetches. Most sources on individual pumps are crypto media, analytics-vendor threads (Bubblemaps, Rena Labs) or opinion newsletters (Wu Blockchain Substack, Rekt). I found no peer-reviewed study that conditions on "already the #1 gainer" and tests continuation. Treat everything below as case evidence plus mechanism, not as validated predictive signals. Two fetches failed: 4pillars Korea research (HTTP 429) and BitMEX "State of Perps 2025" (HTTP 404). Some of the TUT coverage (interactivecrypto.com, dated "Aug 2026") looks auto-generated and has low reliability.

## 1. Low float / circulating-supply share / holder concentration

### Takeaway
Industry studies are consistent that low float plus high FDV produces early scarcity-driven spikes and then long-horizon underperformance, measured from launch over months. Every 2025 mega-runner I could document (MYX, COAI, ALPACA, JELLYJELLY, STO) combined a small or thin float with concentrated or cluster-controlled supply and perp open interest large relative to that float. No source tests whether concentration separates runners from faders after a coin is already the top gainer. Concentration looks like a necessary condition for a squeeze, not a sufficient predictor.

### Cited Findings
- GSR studied 2,300+ listings. Tokens with FDV above $1B had a median 1-year return of -81%. Median circulating share falls as FDV rises: 97% of supply circulates for FDV under $10M versus 14% for FDV over $1B. — [KuCoin summary of GSR study](https://www.kucoin.com/news/flash/gsr-study-low-circulation-high-fdv-token-models-underperform-by-80-in-one-year)
- Keyrock (62 airdrop tokens, about Oct 2024): the higher the FDV at launch, the greater the likelihood of a significant price drop, regardless of project type or hype. — [Forklog](https://forklog.com/en/fdv-explained-what-it-is-and-how-it-moves-crypto-prices/)
- Tokenomics.com (500+ 2024 launches): low-float tokens deliver strong returns in the first months, then begin to underperform around day 120. Its claim that "94% fail" has no visible methodology. — [tokenomics.com](https://tokenomics.com/articles/low-float-tokenomics)
- Counterpoint: Crypto Koryo reportedly named low-float/high-FDV among the best-performing sectors in one month (Sept), and suggested unlock events can act as a "strategy". — [search summary via MEXC/Forklog](https://www.mexc.com/news/733642)
- MYX: circulating supply was about 197M of 1B (under 20%) at the peak. Team (20%) and investors (17.5%) were mostly locked. — [Wu Blockchain Substack](https://wublock.substack.com/p/the-myx-affair-a-deliberately-orchestrated) (an opinion piece)
- COAI: about 196M circulating of 1B max (19.6%). Phemex says the top 10 wallets held over 96%, largely team/investor lockups; it names no source. — [Phemex](https://phemex.com/blogs/chainopera-ai-coai-price-prediction-2025). The Block lists about 188M circulating of 1B. — [The Block](https://www.theblock.co/price/chainopera-ai)
- TUT (described as Aug 2026 by a low-reliability site): about 99% of circulating supply sat in the top 100 wallets. — [interactivecrypto](https://interactivecrypto.com/tut-s-183-surge-leverage-frenzy-meets-whale-risk-in-a-bullish-crypto-august-aug-2026)
- Elliptic: concentrated holder distributions raise the feasibility of manipulation, especially when wallets share funding or act in sync. — [Elliptic](https://www.elliptic.co/corpus/gen-2983/fraudulent-trading/detecting-pump-and-dump-schemes-in-crypto-markets-with-on-chain-and-off-chain-signals.html)
- A practitioner screener for Binance perp "squeeze traps" scores the following:
  - OI / circulating market cap: warning above 0.5, danger above 1.0
  - perp/spot volume ratio: warning above 15, danger above 40 (infinite if there is no Binance spot pair)
  - |funding| APR: warning above 100%, danger above 300%
  - listing age: danger under 60 days, warning under 180 days
  - circulating market cap: warning under $100M, danger under $30M
  - not in CoinGecko's top ~2000

  It uses free Binance endpoints and the CoinGecko markets endpoint. The author reports no backtest and no continuation/reversal rates. Squeeze post-mortems "almost always screened hot" on OI/market cap, and trap events cluster in a perp's first two quarters. — [HackerNoon](https://hackernoon.com/that-300percent-funding-apr-is-not-free-money-screening-for-squeeze-traps-on-binance-perpetuals)

### Inferences
- The best-documented mechanism behind the 3-day mega-runners is a cornered float squeezing a perp whose OI is comparable to or larger than the float (see sections 2 and 3). The natural backtestable feature is therefore **OI / circulating market cap at the moment of becoming #1 gainer**, plus listing age and absence of a Binance spot pair. This is untested here.
- The low-float underperformance studies measure 3–12 month horizons from launch. They say little about a 3-day horizon after a coin becomes the top gainer, so "low float = will fade" should not be read into them.
- Data availability (from working knowledge, not sourced above; verify before building):
  - Binance `data.binance.vision` publishes daily USD-M "metrics" files with 5-minute sum OI, OI value and top-trader long/short ratios going back several years. The REST `openInterestHist` endpoint covers only about 30 days.
  - Funding history (`/fapi/v1/fundingRate`) and klines have full history.
  - CoinGecko's free/Demo `market_chart` gives market cap and price history (about 365 days on the free tier). Implied circulating supply = market cap / price. The dedicated circulating-supply-history endpoint is paid.
  - BscScan/Etherscan `tokenholderlist` / top holders are Pro-only. Historical holder concentration means reconstructing balances from free `tokentx` transfer logs, which is slow and rate-limited.
  - Tokenomist/TokenUnlocks historical calendars are mostly paywalled. Unlock schedules can be hand-built from project docs for a small case set.

### Gaps
- No study compares float or concentration between top-gainer runners and faders after the event.
- No reliable top-holder share series (excluding exchange, lock and vesting contracts) was found for MYX, COAI, HIFI, BANANAS31, JELLYJELLY or TUT.

## 2. Case studies of 2025–2026 big runners: reported causes and what was visible

### Takeaway
The documented runners fall into two archetypes:
- **(a) Engineered spot-corner / perp short squeeze in a low-float, cluster-held token:** MYX, COAI, MMT, STO, JELLYJELLY.
- **(b) Binance-delisting futures squeeze:** ALPACA, HIFI.

The visible markers during the move were:
- explosive perp volume and OI relative to market cap
- short liquidations
- Binance switching funding to hourly
- single wallets withdrawing a large share of float from exchanges

The visible markers of the end were:
- a scheduled token unlock or insider exchange deposit
- the delisting date itself
- Bubblemaps/analyst exposure of a cluster, followed by a -50% day

### Cited Findings
**MYX (Aug–Sep 2025)**
- Rose from about $1 to nearly $20 in early Sept 2025. FDV went above $20B. Cointelegraph links the rise partly to a top ranking on Binance Alpha's airdrop/TGE performance list. — [Cointelegraph](https://cointelegraph.com/news/what-is-myx-finance-why-is-it-up-1-400-percent)
- Sept 9: Bubblemaps said one entity with 100 freshly funded wallets (funded with BNB from OKX within minutes, a month before the airdrop) claimed 9.8M MYX, worth about $170M in profit. MYX denied this. — [Cointelegraph](https://cointelegraph.com/news/myx-finance-trading-activity-market-manipulation-report)
- Rena Labs / Insider.Cash: less than a 0.001% probability that the rally was organic. — [Cointelegraph](https://cointelegraph.com/news/myx-finance-trading-activity-market-manipulation-report)
- Timeline:
  - August: interim peak at $2.49 on Aug 8, then a 58% drop after an unlock.
  - Sept 1–8: +1,132%.
  - Sept 8: $14.63M of liquidations, about $11M of them shorts.
  - Sept 9: +291% day, all-time high above $17.
  - Perp turnover was $6–9B/day. Spot volume rose 710% to $354M on Sept 7–8, then $880M at the peak.
  - Binance moved MYX funding settlement to hourly.
  - **The peak coincided with a 39M-token (3.9% of supply) unlock**, and Hack VC reportedly moved MYX to MEXC afterwards.

  — [Wu Blockchain Substack](https://wublock.substack.com/p/the-myx-affair-a-deliberately-orchestrated) (interpretive opinion)
- Rekt: a 52% fall to about $9, and claimed $73M of retail liquidations, which could not be cross-verified. — [Rekt](https://rekt.news/parabolic-mirage). MEXC: a near-75% fall from the high, with selling from Oct 1. — [MEXC](https://www.mexc.com/news/120638)

**COAI (ChainOpera AI, Sep–Oct 2025)**
- Rose from about $0.14 on Sept 26 to an all-time high near $44.90 on Oct 12, then fell about 52% in 24h amid talk of top-wallet profits and coordinated selling. — [99Bitcoins](https://99bitcoins.com/news/altcoins/coai-crypto-drops-after-sprinting-100x-is-chain-opera-ai-run-finished/)
- Phemex instead reports a $5.77 peak on Oct 8. The sources conflict on peak dates and levels. — [Phemex](https://phemex.com/blogs/chainopera-ai-coai-price-prediction-2025)
- Bubblemaps: 60 wallets, over half of the top-100 most profitable holders, were each funded with 1 BNB from Binance on Mar 25 and ran thousands of automated Binance Alpha trades. That suggests one entity with about $13M in profit. Bubblemaps found no proven link to the team. — [CoinCentral](https://coincentral.com/bubblemaps-investigates-chainopera-and-claims-one-entity-controls-coai-profits/)
- A later AI-generated article claims 87.9% of supply sat in 10 wallets and an 88% crash. It is unverified. — [AInvest](https://www.ainvest.com/news/coai-token-scam-fallout-risks-red-flags-emerging-crypto-projects-2512/)

**ALPACA (Apr–May 2025, Binance delisting)**
- Delisting announced Apr 24, 2025. Perps were to close May 1 and spot May 2.
- Price dropped about 30%, then rose about 12x in 3 days ($0.029 to $0.3477).
- **Futures OI was several times the market cap.**
- On Apr 25 Binance shortened the funding settlement cap cycle to hourly (max ±2%), raising the cost of holding shorts.
- Accumulation was suspected around Apr 19 (an allegation).

— [TechFlow](https://www.techflowpost.com/en-US/article/25364); [The Block](https://www.theblock.co/post/355730/alpaca-finance-binance-delisting); [iTiger](https://www.itiger.com/news/2532413020)

**HIFI (Sep 2025, Binance delisting)**
- Delisting announced Sept 3 for Sept 17, grouped with BAKE and SLF.
- HIFI first fell 7%, then went from $0.058 to $0.094 by Sept 10.
- Other outlets cite +600–700% on different baselines.
- HIFI derivatives volume reached $6B, driven by short liquidations. The other delisted tokens (BAKE, SLF) did not squeeze comparably.

— [Invezz](https://invezz.com/news/2025/09/10/binance-delisting-sparks-volatile-rallies-in-bakerytoken-hifi-finance-and-self-chain/); [TradingView](https://www.tradingview.com/symbols/CRYPTO:HIFIUSD)

**JELLYJELLY (Mar 26, 2025)**
- An attacker on Hyperliquid opened a ~$4M short and ~$3M of longs, pulled margin so the HLP vault inherited the short, then pumped spot. Price rose more than 500% ($0.00806 to $0.0517) within an hour.
- 1% market depth averaged only about $72K/day.

— [Kaiko](https://www.kaiko.com/resources/defi-faces-new-test-as-low-liquidity-token-gets-manipulated)

- OKX and Binance listed JELLYJELLY perps (50x) within about 30 minutes. Hyperliquid delisted it and settled at a profit. — [The Defiant](https://thedefiant.io/hyperliquid-token-tanks-as-another-vault-attack-sparks-controversy); [The Block](https://www.theblock.co/post/348314/hyperliquid-delists-jellyjelly-memecoin-amid-whale-manipulation-fiasco)

**STO (StakeStone)**
- The only well-reported pump I found was dated April 2026, not Oct 2025: +500% in 3 days to about $0.84.
- **One wallet withdrew about 25.5M STO (over 11% of circulating) from Binance.** OI rose about 300% to $332M, and derivatives volume rose 500% to $3.44B.
- A 20.17M (2.02%) unlock was scheduled around the rally. An analyst speculated the pump was set up to sell after the unlock.

— [altFINS](https://altfins.com/crypto-news/crypto-news-summary/261844); [MEXC](https://www.mexc.com/news/999743)

**TUT**
- Coverage (dated Aug 2026, low-reliability site) attributes the move to an Aster DEX perp listing that triggered short covering, with $34M of mostly short liquidations on Aug 9.
- The reversal came on Aug 10 with deleveraging and Phemex delisting its TUT futures.

— [interactivecrypto](https://interactivecrypto.com/tut-s-183-surge-leverage-frenzy-meets-whale-risk-in-a-bullish-crypto-august-aug-2026)

**BANANAS31**
- July 2025 move: +240% in 30 days, volume up 2,500% to $383M. Coverage frames it as retail momentum.
- Whale and squeeze claims come only from unverified Binance Square posts.

— [Bitrue](https://www.bitrue.com/blog/bananas31-meme-coin-price-analysis-july-2025); [CCN](https://www.ccn.com/analysis/crypto/bnb-chain-memecoin-banana-for-scale-bananas31-price-spikes/)

**MMT (Momentum, Nov 4, 2025 launch)**
- A commentator alleges a "textbook harvest": spot held around $0.40 while the contract was pushed, with record liquidations within 24h. This is an unverified opinion.

— [AiCoin](https://www.aicoin.com/en/article/498363)

**Market makers**
- An analyst argues that market makers step back after listings and let prices crash after "death-trap" perp listings (ai16z, VVV).

— [The Block](https://www.theblock.co/news/defi/2025-02-06-ai16z-and-vvv-see-double-digit-price-drops-since-death-trap-perps-listings-analyst-says-339220)

### Inferences
- A recurring end-trigger is the **scheduled supply event**. MYX peaked on its unlock date, and its August unlock had already been followed by a -58% drop. STO's pump surrounded an unlock. ALPACA and HIFI ended at the fixed delisting date. A known future unlock or delisting date within the next 1–3 days of a top-gainer event is a plausible **exit or avoid** feature. A distant one may allow running. This is untested.
- Runners tend to be in their first months of perp listing, recently TGE'd or Binance Alpha tokens, or delisting-announced. Long-established mid-caps rarely produce +500% 3-day runs.
- A Binance change of the symbol's funding interval to 1h (or a cap change) is a mechanical, timestamped sign that the exchange sees an extreme squeeze. It is observable from Binance announcements and from `fundingInfo` / funding-rate timestamps. For ALPACA and MYX it came early in the run, not at the top.

### Gaps
- No primary source on cause was found for BANANAS31, and no reliable source for TUT 2025.
- No market-maker deal disclosures (loan or option terms) were found for any of these tokens.
- Exact top-holder percentages at the time of the top-gainer event are not available.

## 3. Binance delisting / monitoring tag / seed tag announcements and futures delisting squeezes

### Takeaway
Monitoring-tag rounds generally produce small-to-double-digit declines, not pumps. Delisting announcements sometimes produce violent futures squeezes, though only in a minority of the delisted names (ALPACA and HIFI, not BAKE or SLF). These cases share three traits: OI far above market cap, an hourly funding switch, and a hard terminal date when futures close. The squeeze runs between the announcement and the futures close, typically 1–2 weeks.

### Cited Findings
- ALPACA: announced Apr 24, perps closed May 1. The run took 3 days after an initial -30%. OI was several times market cap, and funding moved to hourly on Apr 25. — [TechFlow](https://www.techflowpost.com/en-US/article/25364)
- HIFI: announced Sept 3, delisted Sept 17. It first fell 7%, then squeezed by Sept 10, while BAKE fell 20% and SLF fell 25%. — [Invezz](https://invezz.com/news/2025/09/10/binance-delisting-sparks-volatile-rallies-in-bakerytoken-hifi-finance-and-self-chain/)
- Monitoring-tag rounds in 2025 mostly brought immediate declines:
  - June: BIFI, FIS, KMD and MDT fell, some by double digits.
  - July: BAKE -5.6%, IDEX -3.5%, SLF -3.3%.
  - MOVE fell 12% in 24h.

  These are short-window news observations, not event studies. — [crypto.news (MOVE)](https://crypto.news/binance-adds-monitoring-tag-to-move-following-market-maker-scandal-and-rebrand/); [iTiger](https://www.itiger.com/hant/news/2541807819); [CryptoRank](https://cryptorank.io/news/feed/75a75-binance-monitoring-tag-ava-gns-scr)
- The tag flags higher volatility and risk. It does not automatically mean delisting, but recent delisting rounds have drawn from tagged tokens. — [iTiger](https://www.itiger.com/news/2549588158)

### Inferences
- For a delisted token that becomes the #1 gainer, the continuation case rests on how much short OI still has to cover before the futures close date. The futures close is the hard stop: price support disappears once perps settle and spot leaves Binance. A rule such as "hold only while days-to-futures-close > 1 and OI/market cap > 1" is mechanistically motivated but untested.
- Binance announcements (delisting, monitoring tag, funding interval change) are historically retrievable from Binance's public announcement listing (CMS API used by the website), from working knowledge. That makes event timestamps backtestable for free. Verify before building.

### Gaps
- No systematic event study of Binance delisting squeezes (hit rate, size distribution) was found.
- No evidence was found on "seed tag" effects specific to 2024–2026.

## 4. Exchange wallet inflows/outflows and whale transfers as dump-timing signals

### Takeaway
There is no rigorous published evidence that exchange inflows reliably time altcoin dumps. The evidence is vendor heuristics (CryptoQuant, Santiment) and anecdotes, and vendors themselves note counterexamples. Case-level observations fit the idea anyway: large exchange withdrawals of float accompany the start of squeezes (STO), and insider deposits after unlocks accompany tops (MYX, Hack VC). Free historical per-token exchange-flow data for small BSC/ETH tokens is not readily available.

### Cited Findings
- Santiment tested 1,000 assets above $1M market cap on whether inflow spikes are bearish. The results were not retrievable. Its KNC example is a single case. — [Informer Post / Santiment](https://theinformerpost.beehiiv.com/p/massive-whale-transactions-sent-three-altcoins-into-a-spiral-according-to-crypto-analytics-firm-sant)
- CryptoQuant's Exchange Whale Ratio (top 10 deposits / total deposits) uses hand-set thresholds (85–90% signals correction, above 90% a large drop). It is unvalidated, and the same analyst notes the signal cuts both ways. — [CryptoQuant user guide](https://userguide.cryptoquant.com/quickstart/6-minute-feature-guide/top-10-presets/whale-dumping); [Yahoo/CryptoQuant XRP](https://finance.yahoo.com/news/xrp-whale-flows-point-potential-225957013.html)
- STO: one wallet withdrew more than 11% of circulating supply from Binance before or during the +500% run. — [altFINS](https://altfins.com/crypto-news/crypto-news-summary/261844)
- MYX: an investor deposited tokens to MEXC after the unlock at the top. — [Wu Blockchain Substack](https://wublock.substack.com/p/the-myx-affair-a-deliberately-orchestrated)
- COAI and MYX clusters were identified by shared funding source and timing (Bubblemaps). In both, the public exposure came at or after the top, not before the run. — [CoinCentral](https://coincentral.com/bubblemaps-investigates-chainopera-and-claims-one-entity-controls-coai-profits/); [Cointelegraph](https://cointelegraph.com/news/myx-finance-trading-activity-market-manipulation-report)
- Academic work: Karbalaii (arXiv 2504.15790) studied Poloniex pump-and-dumps. 70% of pre-event volume trades within one hour before the announcement, and it separates "pre-accumulation" from "on-the-spot" archetypes. These are small exchange-coordinated pumps, not Binance perp squeezes. — [arXiv](https://arxiv.org/html/2504.15790v1)

### Inferences
- A free proxy for "the float is being cornered" is net transfers into and out of known Binance hot wallets for the token's contract. BscScan/Etherscan free `tokentx` per address is rate-limited but historical. Labelled exchange wallets would need to be hand-curated, because Arkham and Nansen labels are not free via API. This is feasible for a small case set but heavy as a universe-wide backtest.
- Bubblemaps-style exposure posts are a coarse "top is near" signal. They are not historically queryable except by manual timestamping of X posts.

### Gaps
- No free historical exchange-netflow dataset for small-cap BEP-20/ERC-20 tokens was found.
- The Santiment study's results are unknown.

## 5. Korean exchange demand (kimchi premium, Upbit/Bithumb warnings) and social volume

### Takeaway
Korean retail is heavily altcoin-weighted, and the aggregate kimchi premium peaks with altcoin-market tops, which points to a contrarian or market-level timing role rather than a token-level predictor. Upbit/Bithumb "investment warning" or caution designations are associated with restrictions and declines, not runners. No evidence was found that token-level premium or social volume separates runners from faders after a coin becomes the top gainer.

### Cited Findings
- Altcoins are 85% of Korean domestic volume (BTC 9%, ETH 6%). Capital controls keep arbitrageurs out, so domestic retail sets KRW prices. — [4pillars](https://research.4pillars.io/en/data/content/koreans-buy-the-alt-top-not-the-bitcoin-top) (via search summary; fetch blocked)
- The kimchi premium peaked the same week as the altcoin market high, including at the October 2025 top (+0.8% on the top day). In 2026 the premium was negative on 134 of 236 days. — [4pillars](https://research.4pillars.io/en/data/content/koreans-buy-the-alt-top-not-the-bitcoin-top)
- On Upbit listings: tokens already listed globally often react as if newly listed. High volume aids rapid recovery after volatility (Klein Labs 2024). — [Blockmedia/Klein Labs](https://www.blockmedia.co.kr/research-english/892145)
- Upbit listed 54 new tokens in 2025, with about 70% Korean market share. — [MEXC/BitcoinWorld](https://www.mexc.com/news/371504)
- Upbit and Bithumb caution/warning designations in 2025 (SNX deposits suspended, NAP, LWA supply-increase warning) came with declines or no reported surge. — [MEXC/BitcoinWorld](https://www.mexc.com/news/488203); [bitcoinworld](https://bitcoinworld.co.in/upbit-bithumb-coinone-lwa-warning/)
- Social volume: LunarCrush has per-coin social pages (e.g. BANANAS31), but history access via API is paid. No study links social metrics to post-top-gainer continuation. — [LunarCrush](https://lunarcrush.com/en/coin/banana-for-scale-bananas31)

### Inferences
- A token-level kimchi premium is computable for free with history: Upbit public candles API (KRW pairs) versus Binance USDT price, times a USD/KRW rate. Upbit's public notice API and the `market_warning` / caution fields on its market list give designations, though the market list shows only current status, so history must come from the notices. This is from working knowledge; verify. It only applies to tokens also listed on Upbit, and many runners (MYX, COAI) were not Upbit KRW-listed at the time, so coverage is limited.
- Combined with the user's own result that Upbit listing buys lost money (sell the news), Korean flows look like an exhaustion or peak marker more than a continuation signal.

### Gaps
- No event study on Upbit warning designations and price was found.
- No free historical social-volume source with per-token granularity was found. LunarCrush and Santiment social history are paid. X/Telegram scraping is not reliably backtestable.

## Cross-cutting summary for the report writer (inferences, untested)
- **Candidate features that might help separate runners from faders, all free and backtestable from Binance data:**
  - OI / circulating market cap at the time of becoming #1 gainer. Binance data.binance.vision metrics give OI; CoinGecko gives market cap.
  - Perp-to-spot volume ratio, including whether a Binance spot pair exists.
  - Listing age of the perp.
  - Whether Binance has switched the symbol to 1h funding.
  - Days until a known delisting or futures-close date.
- **Mostly not free or not historical:**
  - Holder concentration and exchange netflows (Pro explorer APIs, or labels from Arkham/Nansen)
  - Unlock calendars (Tokenomist paywall)
  - Social volume (LunarCrush paid)
- **The documented end-triggers are scheduled supply events and terminal dates:** unlocks, insider deposits, futures close. Public exposure of wallet clusters tends to come at the top. A "scheduled-event-within-N-days" exit filter is better grounded in case evidence than any entry predictor.
