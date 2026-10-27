# Academic and quantitative evidence on whether extreme crypto pumps continue or reverse

Scope: what published research says about telling, at or shortly after an extreme short-term rise, coins that keep running from coins that fade. Setting: Binance USD-M perps, coin has just become the #1 24h gainer. Our own 2,533-event test found common price/volume/funding/age features raise P(big run) and P(-30% crash) together (AUC ~0.5 out of sample).

Overall headline (for the report writer): I found **no published study that directly addresses this question** (separating continuation from reversal *conditional on* a coin already being an extreme top gainer, in perps, at horizons of hours to 3 days). The literature splits into (a) pump-and-dump (P&D) detection, which predicts *which coin will be pumped* or *that a pump is happening*, not which pumps last; (b) weekly/monthly cross-sectional anomaly studies (MAX, momentum, skewness, illiquidity), whose horizons and samples (large/mid caps, weekly sorts) are far from our setting; (c) a few event studies of extreme intraday moves in BTC/ETH/LTC. The one signal that the research does show separating "sustained" from "pump-and-dump" reactions is **the timing of social/attention activity relative to the price move** (attention leading the price = manipulation that reverses; attention following the price = organic and more sustained), plus **post-peak volume persistence** — but the latter evidence is partly definitional. Both are weak, small-sample, and not tested out of sample for trading.

## 1. Pump-and-dump detection literature: what predicts the dump and its timing, and does anything predict which pumps last?

### Takeaway
P&D research is good at detecting a pump in seconds (F1 ~94%) or predicting the target coin before the pump (AUC >0.9), using rush orders, volume and price-volatility bursts, and market cap. It says almost nothing about which pumps persist: scheduled Telegram pumps nearly all revert within minutes to hours, while "crowd pumps" (DOGE, XRP 2021) stayed elevated. The only explicit discriminator found between reverting and sustained events is that coordinated social messaging *precedes* the price peak in P&Ds (median -4.3 min) but *follows* it in sustained organic reactions (median +14.5 min).

### Cited Findings
- La Morgia, Mei, Sassi, Stefa, "The Doge of Wall Street" (arXiv 2021; ACM Trans. Internet Technology 2023): detector flags a Binance pump within 25 seconds of its start with 94.5% F1, versus prior state of the art of 30 minutes and 62.7% F1. Trained on 317 Binance P&D events drawn from 902 operations across four exchanges. — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Same paper, features (Random-Forest Gini importance): StdRushOrders 0.251, AvgRushOrders 0.123, AvgVolumes 0.081, StdTrades 0.073, StdVolumes 0.073, AvgPriceMax 0.055, AvgPrice 0.032, time-of-day features small (0.003-0.031). "Rush orders" = bursts of aggressive market buys grouped in the trade tape. Best F1 used 25-second chunks with a 7-hour moving window; fastest used 5-second chunks with a 35-minute window. — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Same paper, persistence: standard Telegram pumps last a few minutes (Big Pump Signal: ~5 min average), and the price "returns to its natural level when the event ends"; YoBit coins returned to their original state within hours. "Crowd pumps" (Reddit/WSB-driven DOGE, XRP, Jan-Feb 2021) lasted hours to days; DOGE was still ~500% above pre-pump value on 2 March 2021, XRP ~100% higher. The XRP pump failed to extend because price had already risen before the scheduled time. No per-event sustain/dump statistics beyond these cases. — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Same paper, target size: of 264 targets with market-cap data, 71% were below $20M and 22% below $1M; median target market cap on Binance was ~$25.6M (vs ~$2.6M on YoBit/Bittrex). — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Xu & Livshits, "The Anatomy of a Cryptocurrency Pump-and-Dump Scheme" (USENIX Security 2019): 412 Telegram-organised P&Ds (June 2018 - Feb 2019). Model predicts pump likelihood of every listed coin *before* the pump with AUC >0.9 both in-sample and out-of-sample; a trading strategy returned up to 60% in 2.5 months (preprint version: up to 80% in 3 weeks on a narrower sample). Predictors: market cap and unusual pre-pump price and volume fluctuations. — [USENIX](https://www.usenix.org/conference/usenixsecurity19/presentation/xu-jiahua); [Imperial summary](https://imperial.ac.uk/news/189526/cryptocurrency-manipulation-schemes-could-found-foiled)
- Nghiem, Muric, Morstatter, Ferrara (Expert Systems with Applications, 2021): predicting the pump target before announcement; market signals predicted better than social signals; performance drifted as market conditions shifted. — [ISI](https://www.isi.edu/results/publications/12609/detecting-cryptocurrency-pump-and-dump-frauds-using-market-and-social-signals/); [datalearner listing](https://datalearner.com/academic/journal-papers/0957-4174/volumes-and-issues/386/paper-detail/17225)
- Moura, Paoletti, Ferreira, Almeida, "Don't You Know, Pump it Up!" (arXiv Sept 2026, accepted ICWSM 2027): 14,499 public Telegram channels, 20M+ messages, 1 year, aligned with market data for 17,000+ coins. Found 47 P&D-like events and 73 "Sustained Market Reactions" (SMR). In P&Ds, the median message precedes the price peak by 4.3 min; in SMRs, discussion follows the peak by a median 14.5 min (KS stat 0.272, p = 0.0006). Message *text* was linguistically indistinguishable between the two. — [arXiv 2609.01176](https://arxiv.org/pdf/2609.01176)
- Same paper, magnitudes: P&D mean max rise +9.98% then -14.84% for peak buyers; mean pump phase 14.6 min, mean dump phase 32.1 min. SMR mean rise +18.56% then only -5.36% drawdown. SMRs cluster in higher-liquidity coins ($828.8M aggregate volume vs $234.8M for P&Ds); P&Ds target low-cap, low-liquidity coins. Caveat: the P&D vs SMR split is *defined* using post-event Reversal Ratio (>0.7 = P&D) and Volume Decay (<0.3 = P&D), so the volume-decay "difference" is partly by construction; small N; not an out-of-sample trading test. — [arXiv 2609.01176](https://arxiv.org/pdf/2609.01176)
- Bolz et al. (Univ. Zurich, 2025, "Machine learning-based detection of pump-and-dump schemes in real time"): NLP classification of Telegram messages identified 2,079 past pump events; in a Poloniex case study the target coin ranked in the model's top 5 (out of 50 random coins) in 24 of 43 events (55.8%). — [arXiv 2412.18848](https://arxiv.org/html/2412.18848v2); [UZH](https://www.blockchain.uzh.ch/publication/machine-learning-based-detection-of-pump-and-dump-schemes-in-real-time/)
- Karbalaii (arXiv April 2025, Poloniex Aug 2024 - Feb 2025, 485 events with minute data): 69.3% of events showed pre-pump accumulation volume (average span ~36 h); two archetypes, "pre-accumulated" vs "on-the-spot". Simulated insider profits median ~92-127% depending on exit rule. No analysis of which events sustain. — [arXiv 2504.15790](https://arxiv.org/html/2504.15790v1)
- Li, Shin, Wang (JFQA, version Dec 2024): P&Ds produce short-lived rises in price, volume and volatility "followed by quick reversals"; P&Ds reduce coin liquidity and prices afterwards. — [JFQA PDF](https://jfqa.org/wp-content/uploads/2025/01/24639_Cryptocurrency-Pump-and-Dump.pdf)
- Long-run: pumped coins fell ~30% relative to the market after 365 days. — [arXiv 2309.06608](https://arxiv.org/pdf/2309.06608)
- Hamrick et al. (IP&M 2021): Telegram/Discord pumps are modestly successful at short-term price rises, and the effect diminished over time; 10% of Telegram pumps raised price >18% within 5 minutes. — [Finance Magnates summary](https://www.financemagnates.com/cryptocurrency/news/telegram-is-a-hub-of-crypto-pump-and-dump-schemes-study-shows/)

### Inferences
- Classic Telegram P&Ds are a different animal from our events: they are minutes-long, +10-20% moves in micro-caps; a coin that becomes the #1 Binance-futures 24h gainer (typically +30-100%+) has already survived far beyond a classic pump's life. So P&D "dump timing" findings (dump within ~30 min) mostly describe events that never reach our trigger.
- The rush-order feature is about *detecting* aggressive coordinated buying; the literature does not show it separates runners from faders. If anything, rush-order intensity is a symptom of both.
- The single most transferable idea is the **lead-lag of attention vs price**: price moves *preceded* by coordinated attention bursts revert; moves that *attract* attention afterwards persist more. A futures-only analogue using free data: whether volume/taker-buy surges and open-interest build *precede* the price peak (pre-positioned, exit-liquidity seeking) or keep arriving *after* it (new participants). This is our inference, not a tested result.
- "Pre-accumulation" (volume rising hours/days before the pump) marks insiders positioned to sell; in principle a reversal flag, but no study tests it as a continuation discriminator.

### Gaps
- No P&D paper I found predicts *which* pumps last or the size of the eventual dump from features available at the peak. Nghiem et al. 2021 reportedly estimated pump peak values with neural nets (cited secondhand in the Telegram paper) — not verified.
- Social data (Telegram/X) is not free historically for 2024-2026 at scale; the lead-lag signal therefore cannot be replicated directly with Binance-only data.

## 2. Short-horizon momentum vs reversal, momentum crashes, overreaction, MAX/lottery effect: what conditions favour continuation?

### Takeaway
In crypto, unlike stocks, extreme past winners on weekly/monthly horizons tend to keep winning ("MAX momentum"): high-MAX coins beat low-MAX by ~1.5-3%/week. Cross-sectional momentum is strongest at ~1-2 week formation/holding and turns into reversal at 4-6 weeks; it is crash-prone (a single coin can wipe out the premium). Intraday studies on BTC/ETH/LTC find same-day continuation after abnormal moves, but next-day results are mixed. None of these condition on an already-extreme +50%+ day in small perps, so they are weak guides.

### Cited Findings
- Özdamar, Akdeniz, Şensoy (Financial Innovation / Bilkent, 2021): prior-month maximum daily return (MAX) *positively* predicts crypto returns; top-minus-bottom MAX decile = 3.03%/week raw, 1.99%/week risk-adjusted; survives controls for size, momentum, short-term reversal, sentiment. Weekly sorts, 2014-2020. In-sample portfolio sorts. — [Bilkent repository](https://repository.bilkent.edu.tr/items/78167f5c-d3e2-4f07-b4bc-5e8a3ed0b2c0); [tables](https://jfin-swufe.springeropen.com/articles/10.1186/s40854-021-00291-9/tables/13)
- Li, Urquhart, Wang, Zhang (Journal of Banking & Finance, 2021): "MAX momentum" — coins with higher max daily returns earn higher future returns; strength varies with market conditions and sentiment. — [IDEAS](https://ideas.repec.org/a/eee/finana/v77y2021ics1057521921001630.html)
- Grobys & Junttila (2021): low-MAX minus high-MAX quintile difference >1.5%/week, i.e. the *opposite* (reversal) sign, robust to Bitcoin risk and microstructure controls. Direct conflict with the two papers above; differences in MAX window (1 week vs 1 month), sorts and samples are the likely cause. — [Univ. of Vaasa news](https://www.uwasa.fi/en/newshub/news/new-study-detects-lottery-behavior-cryptocurrency-markets); [JYX](https://jyx.jyu.fi/handle/123456789/74624)
- Gorman et al. (2021, US stocks): high-MAX days are followed by underperformance consistent with overreaction (equity benchmark for comparison). — [paper](https://cfr.ivo-welch.info/published/papers/gorman2021could.pdf)
- Liu, Tsyvinski, Wu, "Common Risk Factors in Cryptocurrency" (Journal of Finance 2022): crypto market, size and momentum explain the cross-section; ten characteristics form profitable weekly long-short strategies. Liu & Tsyvinski ("Risks and Returns of Cryptocurrency", RFS 2021): strong time-series momentum, and investor-attention proxies strongly forecast returns. — [SSRN](https://papers.ssrn.com/abstract=3379131); [NBER w25882](https://www.nber.org/papers/w25882.pdf); [NBER w24877](https://www.nber.org/system/files/working_papers/w24877/w24877.pdf)
- Dobrynskaya (HSE, ~2,000 coins >$1M cap, 2014-2020): momentum peaks at ~2-week sorting/holding; reversal from 4-6 weeks; short-sample/short-interval results are very unstable. — [HSE news](https://www.hse.ru/en/news/research/559533243.html)
- Grobys, Kolari, Sandretto, Shahzad, Äijö (Financial Markets & Portfolio Mgmt, 2025): top-30 coins, weekly 2016-2023; crypto momentum suffers severe crashes, a single coin can make momentum returns insignificant; volatility-managed momentum mitigates crashes. — [Springer](https://link.springer.com/article/10.1007/s11408-025-00474-9); [Vaasa repository](https://osuva.uwasa.fi/handle/10024/20018)
- Caporale & Plastun (FMPM 2020; working paper 2019): BTC/ETH/LTC 2015-2019, on days with abnormal moves hourly returns keep moving in the direction of the move until the day's close (same-day momentum, exploitable in the working paper); next day mostly continuation in the working paper, but the published version reports contrarian effects in two cases (BTC after positive abnormal days, ETH after negative). — [Brunel WP](https://www.brunel.ac.uk/economics-finance-and-accounting/research/pdf/1917-Oct-GMC-Momentum-effect-in-the-Cryptocurrency-Market-after-One-day-Abnormal-Returns.pdf); [Springer](https://link.springer.com/doi/10.1007/s11408-020-00357-1)
- Momentum-illiquidity double sorts (14-day momentum x 14-day Amihud) used in crypto. — [arXiv 1904.00890](https://arxiv.org/pdf/1904.00890)

### Inferences
- The positive MAX/momentum effect in crypto implies that, *on average across coins and weeks*, extreme winners keep outperforming the cross-section slightly. But a ~2-3%/week mean spread is tiny relative to our 3-day outcome tails (+50% vs -30%); it cannot separate runners from crashers and is consistent with our finding that features raise both tails.
- Lottery-demand theory predicts exactly what we found: the same attention that drives runs drives crashes. Positive skew raises both upside and downside dispersion; variance is predictable, direction is not.
- Momentum crash research suggests the edge, if any, is in *sizing* (volatility scaling) rather than in *selection*.

### Gaps
- I found no study of crypto continuation conditional on a +50%-or-more 24h move, nor any using perpetual futures top-gainer lists.
- Post-publication decay of the crypto MAX/momentum effects was not tested in any source I found.

## 3. Volatility-of-volatility, skewness, intraday patterns, liquidity (Amihud), market cap/float as predictors after extreme gains

### Takeaway
Cross-sectionally, higher skewness predicts *lower* future crypto returns (driven by idiosyncratic skew), and illiquid coins earn higher returns (Amihud). Small caps are right-skewed. Extreme intraday moves come with sharp rises in volume, spreads and depth. None of these is tested as a runner-vs-fader discriminator after an extreme gain; they predict means over weeks, not 3-day tail outcomes.

### Cited Findings
- Liu & Chen (International Review of Financial Analysis, 2024), "Skewness risk and the cross-section of cryptocurrency returns": negative relationship between skewness and future returns at portfolio and coin level; skewness mostly idiosyncratic; large caps left-skewed, small caps right-skewed. Effect sizes not in the abstract. — [IDEAS](https://ideas.repec.org/a/eee/finana/v96y2024ipas1057521924005581.html)
- Zhang & Li (Int. J. Finance & Economics, 2023): negative liquidity-return relationship (illiquid coins earn more), robust to alternative measures; no intertemporal liquidity effect for the 3 largest coins. — [IDEAS](https://ideas.repec.org/a/wly/ijfiec/v28y2023i1p472-492.html)
- Desagre, Mazza, Petitjean: extreme intraday price movements in 8 coins on 16 venues (2015-2018) come with sharp rises in volume, spreads and depth. — [AUT PDF](https://acfr.aut.ac.nz/__data/assets/pdf_file/0006/331656/desagre_mazza_petitjean_cryptocurrencies.pdf)
- Saef et al. (tick data): jumps on up to 58% of days, negative jumps more frequent and larger; jumps influence intraday returns. Aysan, Caporin, Cepni (JIFMIM 2024): news releases raise jump probability, with emotional and fundamental sentiment most potent. — [Padova](https://www.research.unipd.it/handle/11577/3510029); [HBKU](https://elmi.hbku.edu.qa/en/publications/not-all-words-are-equal-sentiment-and-jumps-in-the-cryptocurrency/)
- A media-index study of 64,000 news items found headlines have no forecasting power for BTC and media follows price (industry, not peer-reviewed). — [MEXC summary](https://www.mexc.com/it-IT/news/984901)
- Open interest: no academic study found linking OI to altcoin returns; Giagkiozis & Said (arXiv 2023) show some large exchanges systematically misquote BTC perp OI. — [arXiv 2310.14973](https://ar5iv.labs.arxiv.org/html/2310.14973)
- Funding/liquidations: only industry blogs found (e.g. claims that persistent high funding precedes corrections) with no stated methodology; I could not find a peer-reviewed test. — [Amberdata](https://blog.amberdata.io/leverage-liquidations-the-31b-deleveraging)
- Binance spot listing effect: original "Binance effect" positive (~41% spike); later analyst counts say ~80% of 2024 listings and ~89% of 2025 listings fell after listing (samples of ~27 tokens, unverified methods, confounded by market trend); pre-listing run-ups. — [Yahoo](https://malaysia.news.yahoo.com/binance-effect-means-41-price-173526978.html); [FXStreet](https://www.fxstreet.com/cryptocurrencies/news/binance-effect-fades-less-than-20-tokens-are-profitable-six-months-after-listing-202405201006); [BeInCrypto](https://beincrypto.com/binance-listed-tokens-negative-return); [RockawayX](https://rockawayx.com/insights/bullish-or-bearish-what-really-happens-when-tokens-list-on-a-cex)

### Inferences
- A news-vs-no-news split (catalyst-driven moves continue, no-news moves revert) is well established for equities in my background knowledge (not verified in this session), and the Telegram SMR-vs-P&D result is a crypto analogue. A Binance-only proxy for "news" is weak; Binance announcement timestamps (listings, delistings, Launchpool, spot listing of a futures-only coin) are the most practical catalyst flags.
- Our feature set already included liquidity and size proxies; academic illiquidity/skew premia are weekly mean effects and would not be expected to move AUC on 3-day tails.
- Volatility-of-volatility: I found no crypto paper using vol-of-vol to predict post-spike continuation.

### Gaps
- Effect sizes for skewness and idiosyncratic volatility papers (full text paywalled).
- No study on float/unlock/circulating-supply share as a post-pump predictor (token-unlock data is not on Binance endpoints anyway).
- No peer-reviewed test of funding rate, OI change or liquidations as continuation predictors for altcoin perps.

## 4. Which signals have genuine out-of-sample or post-publication evidence, and how large?

### Takeaway
Out-of-sample evidence exists only for *detection/targeting* (Xu & Livshits AUC >0.9 OOS for predicting the pump target; La Morgia 94.5% F1 for detecting a pump in 25s; Bolz et al. 55.8% top-5 hit rate). The cross-sectional anomalies (MAX, momentum, skew, illiquidity) are in-sample portfolio sorts, with conflicting signs and documented crashes; no post-publication decay test found. Nothing found has out-of-sample evidence for separating continuing from reversing pumps.

### Cited Findings
- Xu & Livshits: AUC >0.9 in and out of sample, for predicting *which coin* is pumped, not its persistence. — [USENIX](https://www.usenix.org/conference/usenixsecurity19/presentation/xu-jiahua); [Imperial](https://imperial.ac.uk/news/189526/cryptocurrency-manipulation-schemes-could-found-foiled)
- La Morgia et al.: 94.5% F1 detection, cross-validated on 317 Binance events; detection only. — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Nghiem et al.: performance degraded as market regimes shifted (non-stationarity). — [datalearner](https://datalearner.com/academic/journal-papers/0957-4174/volumes-and-issues/386/paper-detail/17225)
- Hamrick et al.: pump effectiveness itself decayed over time. — [Finance Magnates](https://www.financemagnates.com/cryptocurrency/news/telegram-is-a-hub-of-crypto-pump-and-dump-schemes-study-shows/)
- MAX effect sign conflict (positive 1.5-3%/week in Özdamar et al. and Li et al.; negative >1.5%/week in Grobys & Junttila). — [Bilkent](https://repository.bilkent.edu.tr/items/78167f5c-d3e2-4f07-b4bc-5e8a3ed0b2c0); [Vaasa](https://www.uwasa.fi/en/newshub/news/new-study-detects-lottery-behavior-cryptocurrency-markets)
- Momentum crashes make plain crypto momentum insignificant (2016-2023). — [Springer 2025](https://link.springer.com/article/10.1007/s11408-025-00474-9)
- Moura et al. lead-lag result: KS p = 0.0006 on 47 vs 73 events, in-sample, no trading test. — [arXiv 2609.01176](https://arxiv.org/pdf/2609.01176)

### Inferences
- Our AUC ~0.5 result agrees with the literature: published crypto short-horizon effects are means over many coins, sign-unstable, and crash-prone; nobody has shown out of sample that price, volume or funding features separate runners from crashers among extreme gainers.
- Candidate signals not yet tested by us, ranked by research support, all weak: (1) attention/flow timing relative to the price peak (do volume, taker buying and OI keep growing *after* the high, or were they front-loaded?); (2) pre-pump accumulation (volume or OI build-up 1-36 h before the move); (3) external catalyst present (Binance announcement); (4) liquidity/market depth class (organic sustained events skew to more liquid coins).

### Gaps
- No post-publication (out-of-time) replication found for any crypto MAX, skewness or illiquidity effect.

## 5. Machine-learning studies predicting crypto extreme moves: out-of-sample accuracy and important features

### Takeaway
ML on crypto works for *detection* (pumps) and *volatility* (magnitude), not reliably for *direction* of extreme moves. Order-flow features (rush orders, trade-size/direction images, LOB inputs) dominate, and feature engineering matters more than model complexity. I found no ML study predicting post-pump continuation with a reported OOS metric.

### Cited Findings
- La Morgia et al.: rush-order dispersion is by far the top feature (Gini 0.251) for pump detection. — [arXiv 2105.00733](https://arxiv.org/html/2105.00733v2)
- Lensky & Hao (2023): CNN on order-flow images predicts BTC short-term realized volatility, RMSPE 0.85 vs 1.4 for the naive current-volatility forecast; one month of data (Jan 2021) only. — [arXiv 2304.02472](https://arxiv.org/pdf/2304.02472)
- Wang (2025): LOB models at 100ms-1s horizons; "better inputs matter more than stacking another hidden layer". — [arXiv 2506.05764](https://arxiv.org/html/2506.05764v2)
- Fang et al. (2021): universal cross-coin LOB features beat asset-specific models; long input sequences do not help; 78% live accuracy on next mid-price tick direction for BTC/USD (tick horizon, not extreme moves). — [UCL](https://discovery-pp.ucl.ac.uk/10132227/1/Ascertaining%20price%20formation%20in%20cryptocurrency%20markets%20with%20machine%20learning.pdf)
- Moura et al.: text content of pump messages is indistinguishable from organic chatter; timing is what differs. — [arXiv 2609.01176](https://arxiv.org/pdf/2609.01176)

### Inferences
- The ML pattern (volatility predictable, direction not) matches our finding: features predict the *size* of the next move (both tails up together) not its *sign*. That argues for using such features in position sizing / stop width / exit timing rather than entry selection.
- Data availability (from my knowledge of Binance's public data, not verified in this session; check before building): data.binance.vision publishes USD-M futures 1m klines, aggTrades and raw trades (enough to build rush-order, taker-imbalance and Amihud features), funding-rate history, mark/index/premium klines, and daily "metrics" files with 5-minute open interest, long/short ratios and taker buy/sell volume. The REST open-interest-history endpoint only covers about the last 30 days. Order-book depth history and liquidation history are limited or discontinued. Social data (Telegram/X) and token-unlock/float data are not available from Binance.

### Gaps
- No ML paper found that reports out-of-sample AUC/accuracy for predicting continuation vs reversal after an extreme crypto gain.
- The industry quant blogs that were suggested (Kaiko, Glassnode, Galaxy) turned up no published study of top-gainer continuation in these searches. They were not searched exhaustively.
