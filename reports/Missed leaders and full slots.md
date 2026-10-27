# Missed leaders and full slots (2026-10-09)

All runs: live config, realistic mode (ladder on closes, 1% stop slip), funding
counted, squeeze on, no Funding transfers (compounding), $50 + $50/30 days.
Rolling = 12 separate 12-month accounts starting 2024-10 .. 2025-09.
Reports: /home/ubuntu/backtest-data/reports/{every-leader-real,capacity,capacity-windows,capacity-rules}.
Outside research: research_capacity.md (agent, 2026-10-09).

## Who was missed, and were they better?

Each skipped leader was scored with the live exits (from "buy every #1 at $6").

| group | n | avg/trade | win % | runners >= +30% |
|---|---|---|---|---|
| taken by the bot (year 1) | 224 | +1.67% | 40% | 6.7% |
| missed: slots/exposure full | 843 | -0.97% | 32% | 5.6% |
| missed: drawdown pause | 1,407 | +0.35% | 38% | 7.3% |
| every #1 | 2,736 | +0.15% | - | - |

- Leaders missed because slots were full were NOT better than the trades
  taken. Runners exist among them at the same rate as duds.
- The big leak is the drawdown pause: after the Sep 2025 peak the compounding
  account stayed >20% under its high for about a year and took only 11 gainer
  trades in year 2 (1,294 leaders and 96 runners skipped).

## Rolling 12-month results (median profit, windows losing, windows beating current)

| variant | median | min | losing | beats current | year1 | year2 |
|---|---|---|---|---|---|---|
| current | +281 | -35 | 1/12 | - | +2239 | +900 |
| pause resets after 14d | +498 | +84 | 0/12 | 8/12 | +2334 | +1818 |
| pause resets after 21d | +552 | +26 | 0/12 | 7/12 | +2239 | +1460 |
| pause resets after 30d | +346 | +141 | 0/12 | 8/12 | +2239 | +1837 |
| pause resets after 10d / 45d | +488 / +168 | -67 / -119 | 2/12 | 7 / 2 | | |
| stale swap, min hold 24h | +479 | +62 | 0/12 | 9/12 | +2051 | +2031 |
| stale swap 12h / 6h | +567 / +99 | +16 / -246 | 0 / 4 | 5 / 4 | | |
| armed position frees its slot | +213 | -38 | 2/12 | 4/12 | +955 | +7090 |
| time stop 24h (instead of 48h) | +566 | +40 | 0/12 | 6/12 | +1579 | +5116 |
| time stop 12/18/36h | ~+260 | | | 5-6/12 | | |
| delayed entry 15/30/60 min | +238/+217/+410 | | 1/12 | 4/4/10 | | |
| pause off | +273 | -255 | 4/12 | 5/12 | | |

Combinations (reset + stale swap, reset + time stop, reset + armed frees) did
not improve on the single changes, which is a sign of noise.

## Conclusions

1. Selection is still unsolved: nothing known at the time separates the missed
   runners from missed duds (see "Picking runners among top gainers.md").
2. Best change: a drawdown pause that RESETS its high after 14-30 days
   refused (`test.dd_rebase_days`). All neighbouring values hold up and it fixes a
   real lock-out. Not in the bot yet.
3. Second: when full, sell a gainer position held >= 24h that never armed
   (`test.when_full=replace_stale`, `replace_min_hold_hours=24`). Good at 24h,
   weak at 12h, bad at 6h, so fragile. Shadow-log before going live.
4. Rejected: freeing slots for armed positions (unstable), more/overflow slots,
   replace oldest/worst/losing (lost earlier and again), pause off.
5. Compounding windows overlap and are path-dependent; treat them as
   indicative, not proof.
