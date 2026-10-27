# Weekday effects (2026-10-27)

Replay: current live settings (gainer 20%, squeeze funding <= −1.5%), realistic
mode, 2024-09-15..2026-09-24. Live: 60 trades from 2026-09-25 to 2026-10-27.

## Replay, gainer, $6 per trade, by entry day (Sri Lanka time)

| day | trades | avg | win % | year 1 | year 2 |
|---|---|---|---|---|---|
| Mon | 120 | −2.45% | 32% | −3.30% | −1.70% |
| Tue | 144 | −0.57% | 38% | −4.09% | +1.74% |
| Wed | 135 | +1.97% | 35% | +3.48% | +0.90% |
| Thu | 149 | +1.75% | 41% | −1.35% | +4.15% |
| Fri | 129 | +0.99% | 42% | +1.65% | +0.65% |
| Sat | 148 | **+4.38%** | 44% | +1.36% | +6.62% |
| Sun | 132 | −0.38% | **33%** | +3.25% | −2.60% |

Buying every new #1 (2,736 trades, UTC): Fri +1.45% and Sat +1.69% (both
positive in both years); Sunday's win rate was the lowest (33%). Squeeze
showed no weekend effect (Sat −0.85%, Sun +4.95%).

## Live, by entry day (Sri Lanka time)

Sat: 15 trades, +$19.84, 8 wins (+$3.28 without IRYS). Sun: 13 trades,
+$2.69, 3 wins (−$15.46 without QNT +$18.15). Mon −$7.99 and Tue −$6.11.

## Test: no new gainer entries on Sunday

| variant | $6 per trade: year1 / year2 / beats current | compounding: median 12m / year1 / year2 |
|---|---|---|
| current | +141 / +105 / – | +2810 / +3247 / +2624 |
| skip Sunday (SL time) | +155 / +136 / 12 of 12 | +1794 / +1775 / +3838 |
| skip Sunday (UTC) | +145 / +116 / 6 of 12 | +1734 / +1702 / +2019 |
| skip Sunday + Monday (SL) | +136 / +113 / 8 of 12 | +1793 / +2714 / +1224 |

**Conclusion.** Per trade, Sunday (Sri Lanka time) entries are weaker, and
skipping them helps a little in both years. The compounding replay
disagrees, and the effect depends on the exact day boundary (UTC is
weaker). Treat it as a real but small, unproven tendency. Not deployed.
