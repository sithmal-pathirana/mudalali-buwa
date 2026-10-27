# New methods for runners and full slots (2026-10-09, untested ideas)

None of these were found in published research or common practice (searched
2026-10-09). All directions are hypotheses, fixed BEFORE any data is seen.

## Picking runners: live-only signals (cannot be backtested, collect forward)
1. Binance risk desk: poll /fapi/v1/leverageBracket (signed) for the top 10.
   Max leverage / notional cut during the pump -> fade. Also watch the funding
   interval switching to 1h and funding cap changes -> squeeze under way.
2. Liquidation fuel: !forceOrder@arr stream, short-liquidation $ over the last
   hour vs long-liquidation $ and vs OI. Rising price while short liquidations dry
   up -> fuel spent -> fade. (Stream sends at most 1 per symbol per second; use as a
   relative measure.)
3. Ask refill ratio: depth@100ms for 10 minutes after the coin becomes #1. Ask size
   within +2% eaten and re-appearing at the same prices (a hidden seller) -> fade;
   asks eaten and not replaced (supply gone) -> run.
4. Leader churn: number of distinct coins that were #1 in the last 24h. High
   churn = rotation regime (leaders fade fast); low churn = trend regime.
5. Copy-trading crowding: number of Binance lead traders opening the coin after
   it becomes #1 (late money -> fade). Check the data is available first.

## Full slots: allocation methods
6. Shared budget with squeeze: one pool; the gainer borrows idle squeeze budget
   and hands it back before the next funding settlement (known times 00/08/16 UTC).
7. Probe and promote: when full, open a 3% probe. If the probe arms (+10%) and a
   main position has not, close that main position and promote the probe to 20%.
   Exposure stays the same; capital moves to whichever trade proves itself.
8. Harvest on arm: sell 1/3 when a trade arms; that cash is reserved for the
   next leader. The rest rides with its stop at entry. Count it at a stressed 15%.
9. Slot auction (survival model): estimate E[remaining return | age, current %,
   best % so far, hours since last high] for each held trade, from the bot's own
   trades. Swap when it is below a new leader's E (~ +0.15%) minus 0.3% cost.
   This generalises the stale-swap rule.
10. Churn-aware slots: 3 slots x 13% in low-churn (trend) regimes, 2 x 20% normally.

## Validation without past data
Shadow mode: at each #1 event, log 1-5 and the would-be decisions of 6-10. Score
after about 150-300 leaders (6-8 weeks) with the direction fixed now. Pass = the
skip/take split differs by > 3 points of average return per trade AND keeps the
same sign in both halves of the sample.
