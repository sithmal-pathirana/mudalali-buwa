"""
reality_check.py — put numbers on the $2/day target before writing strategy code.

Pure standard library. Run:  python3 tools/reality_check.py

Two questions get answered:
  1. What does $2/day on a given account demand, mechanically?
  2. If you force it with leverage, how long does the account survive?
"""

import math
import random
import statistics
from dataclasses import dataclass

# ---------------------------------------------------------------- assumptions
DAILY_VOL = 0.030          # ~3%/day stdev, typical of BTC/major perps (~57% annualized)
TAKER_ROUNDTRIP = 0.0007   # 0.05% in + 0.02% out, USD-M futures VIP0
MAINT_MARGIN = 0.005       # ~0.5% maintenance margin, low leverage tiers
SIMS = 20000
HORIZON_DAYS = 365


def annualize(daily_rate: float) -> float:
    """Compound a daily return over a year. Returns a growth multiple."""
    return math.exp(math.log1p(daily_rate) * 365)


def required_daily_return(target_usd: float, equity: float) -> float:
    return target_usd / equity


# ------------------------------------------------------------------ section 1
def demand_table(target=2.00):
    print("=" * 74)
    print(f"  WHAT ${target:.2f}/DAY DEMANDS, BY ACCOUNT SIZE")
    print("=" * 74)
    print(f"  {'equity':>9}  {'daily ret':>10}  {'annualized':>14}  {'$43 becomes':>16}")
    print("  " + "-" * 70)
    for equity in (43, 100, 500, 1_000, 5_000, 15_000, 25_000):
        r = required_daily_return(target, equity)
        mult = annualize(r)
        grown = 43 * mult
        if grown > 1e12:
            grown_s = f"${grown:.1e}"
        elif grown > 1e6:
            grown_s = f"${grown/1e6:,.0f}M"
        else:
            grown_s = f"${grown:,.0f}"
        mult_s = f"{mult:,.1f}x" if mult < 1e6 else f"{mult:.1e}x"
        mark = "  <-- you" if equity == 43 else ""
        print(f"  {'$'+str(equity):>9}  {r*100:>9.3f}%  {mult_s:>14}  {grown_s:>16}{mark}")
    print()
    print("  The right-hand column is the tell. A strategy that reliably makes")
    print("  $2/day on $43 and compounds is a strategy that owns the market in")
    print("  under two years. That is not a thing that exists.")
    print()


# ------------------------------------------------------------------ section 2
@dataclass
class Config:
    leverage: float
    stop_mult: float   # stop distance as a multiple of the take-profit distance
    label: str


def simulate(equity0: float, target: float, cfg: Config, sims=SIMS, horizon=HORIZON_DAYS):
    """
    One trade per day, sized to net `target` dollars if it wins.

    Driftless barrier model: with a take-profit at distance tp and a stop at
    distance sl, the probability of touching tp first is sl / (tp + sl).
    That is the standard gambler's-ruin result for a random walk, and it is
    generous here -- it ignores slippage, funding, and gaps through the stop.
    """
    ruined = 0
    days_survived = []
    reached_60 = 0     # made $60 (one month of target) before dying
    finals = []

    liq_move = 1.0 / cfg.leverage - MAINT_MARGIN

    for _ in range(sims):
        equity = equity0
        cum_profit = 0.0
        hit_60 = False
        day = 0
        for day in range(1, horizon + 1):
            notional = equity * cfg.leverage
            if notional < 5.0:            # Binance MIN_NOTIONAL, USD-M futures
                break                     # account too small to place an order
            fee = notional * TAKER_ROUNDTRIP
            tp_move = (target + fee) / notional
            sl_move = min(tp_move * cfg.stop_mult, liq_move)

            if tp_move >= liq_move:
                # the win target is further away than liquidation: unwinnable
                equity = 0.0
                break

            p_win = sl_move / (tp_move + sl_move)
            if random.random() < p_win:
                equity += target
                cum_profit += target
            else:
                equity -= notional * sl_move + fee

            if cum_profit >= 60 and not hit_60:
                hit_60 = True
            if equity < 5.0:
                equity = 0.0
                break

        if equity <= 0.0 or equity < 5.0:
            ruined += 1
            days_survived.append(day)
        else:
            days_survived.append(horizon)
        if hit_60:
            reached_60 += 1
        finals.append(equity)

    return {
        "label": cfg.label,
        "leverage": cfg.leverage,
        "liq_move": liq_move,
        "p_ruin": ruined / sims,
        "median_days": statistics.median(days_survived),
        "p_month": reached_60 / sims,
        "median_final": statistics.median(finals),
    }


def ruin_table(equity0=43.0, target=2.00):
    print("=" * 74)
    print(f"  FORCING ${target:.2f}/DAY OUT OF ${equity0:.0f} WITH LEVERAGE")
    print(f"  one trade/day, {SIMS:,} simulated accounts, {HORIZON_DAYS}-day horizon")
    print("=" * 74)
    configs = [
        Config(5,  1.0, "5x,  stop = target"),
        Config(10, 1.0, "10x, stop = target"),
        Config(20, 1.0, "20x, stop = target"),
        Config(20, 2.0, "20x, stop = 2x target"),
        Config(50, 1.0, "50x, stop = target"),
        Config(75, 1.0, "75x, stop = target"),
    ]
    print(f"  {'config':<22} {'liq dist':>9} {'P(ruin)':>9} {'med. days':>10} {'P($60 mo)':>10}")
    print("  " + "-" * 70)
    for cfg in configs:
        r = simulate(equity0, target, cfg)
        print(f"  {r['label']:<22} {r['liq_move']*100:>8.2f}% {r['p_ruin']*100:>8.1f}% "
              f"{r['median_days']:>10.0f} {r['p_month']*100:>9.1f}%")
    print()
    print("  P(ruin)    = account fell below the $5 minimum order size within a year")
    print("  med. days  = median number of days the account stayed alive")
    print("  P($60 mo)  = odds of banking one month of target before dying")
    print()


# ------------------------------------------------------------------ section 3
def growth_paths(equity0=43.0):
    print("=" * 74)
    print("  THE OTHER ROUTE: COMPOUND A RATE, DON'T EXTRACT A FIXED SUM")
    print("=" * 74)
    print("  $2/day becomes a routine ask once the account is ~$15,000")
    print("  (0.013%/day). Here is how long it takes to get there from $43,")
    print("  assuming you never withdraw and never have a losing month:")
    print()
    print(f"  {'daily rate':>11}  {'annual':>9}  {'days to $15k':>13}  {'verdict':<28}")
    print("  " + "-" * 70)
    rows = [
        (0.0005, "bank-like, realistic"),
        (0.0020, "excellent, ~107%/yr"),
        (0.0050, "world-class, unsustained"),
        (0.0100, "does not exist repeatably"),
    ]
    for rate, verdict in rows:
        days = math.log(15000 / equity0) / math.log1p(rate)
        annual = annualize(rate) - 1
        if days > 365 * 20:
            days_s = f"{days/365:,.0f} yrs"
        elif days > 365:
            days_s = f"{days/365:,.1f} yrs"
        else:
            days_s = f"{days:,.0f} days"
        print(f"  {rate*100:>10.2f}%  {annual*100:>8.0f}%  {days_s:>13}  {verdict:<28}")
    print()
    print("  Even the fantasy row takes over a year. The realistic row takes a")
    print("  lifetime. $43 is research capital, not income capital -- so the bot")
    print("  in this repo is built to be measured, not to be relied on for income.")
    print()


if __name__ == "__main__":
    random.seed(7)
    demand_table()
    ruin_table()
    growth_paths()
