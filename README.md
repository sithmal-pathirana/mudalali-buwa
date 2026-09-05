# mudalali-buwa

A Binance USD-M futures trading bot, built to be **measured before it is trusted**.

*Mudalali* (මුදලාලි) is Sinhala for a shopkeeper or trader — the person who
actually minds the shop.

[![CI](https://github.com/sithmal-pathirana/mudalali-buwa/actions/workflows/ci.yml/badge.svg)](https://github.com/sithmal-pathirana/mudalali-buwa/actions/workflows/ci.yml)

---

## Read this before anything else

**No strategy in this repository has a positive expected return out-of-sample.**

That is a measured result, not modesty. The included tooling produced it:

| Configuration | In-sample | Out-of-sample |
|---|---|---|
| Trend-following, always on | −$0.049/day | −$0.125/day |
| Mean reversion, always on | −$0.265/day | −$0.134/day |
| **Regime-routed (default)** | **+$0.021/day** | **−$0.037/day** |

The routing layer works — gating trend-following to trending markets cut losses
by roughly 70% in both the fitted window *and* an unseen earlier one. The signal
it routes does not clear costs. Those are different findings, and only the first
one is good news.

So this is **research infrastructure that happens to be able to trade**, not a
money printer. Its value is that it can tell you cheaply and honestly whether an
idea works, before you fund it. If you want a bot that makes money, you still
have to bring the edge.

---

## What is actually good here

The risk and operational layers, which is where most retail bots are weakest.

- **Position size follows the stop distance**, never a fixed lot. If the correct
  size is below the symbol's minimum order, the trade is **skipped** rather than
  taken oversized.
- **Protective orders live on the exchange**, not in the bot. If the process
  dies, the VPS reboots or the network drops, your stop is still working.
- **Halting is not shutting down.** A halt stops new entries and leaves an open
  position monitored with its stop intact.
- **The kill switch has defined semantics.** `kill_action: flatten` closes at
  market *first*, then cancels. The one behaviour deliberately excluded is
  cancelling the stop and walking away.
- **The backtester is pessimistic and self-validating.** Stop-priority on
  ambiguous bars, gap-through fills at the open, and a limit entry only fills if
  a later bar actually trades through it.
- **Two independent QA audits** found 27 defects between them. All are fixed,
  each with a named regression test.

```
188 unit tests   ·   37 QA checks across 6 layers   ·   zero third-party
                                                        exchange SDK
```

---

## Quick start

Requires Python 3.11+ and two packages, both in Ubuntu's repositories.

```sh
git clone https://github.com/sithmal-pathirana/mudalali-buwa.git
cd mudalali-buwa
pip install -r requirements.txt          # PyYAML, websockets
cp .env.example .env                     # then fill it in

python3 run.py check                     # what a $2/day target really demands
python3 run.py netcheck                  # can this host reach the exchange?
python3 run.py doctor                    # filters, affordability, credentials
python3 run.py backtest --validate       # prove the harness is honest
python3 tools/oos.py                     # the gate any strategy must clear
python3 run.py trade                     # testnet + dry run by default
```

The shipped config is `mode: testnet` and `dry_run: true`. It sends nothing to
any exchange until you change both.

---

## Commands

| Command | Does |
|---|---|
| `check` | Converts a dollar-per-day target into the return it actually demands |
| `project` | Models an escalating target schedule over time |
| `netcheck` | REST, websockets and geo-restriction, from *this* host |
| `myip` | This host's public IP, for Binance's key restriction |
| `verifykey` | Tests an API key/secret pair before you save it |
| `alerts` | Sends a test alert to phone and email |
| `telegram` | `--setup` finds your chat ID, `--demo` previews the commands |
| `dashboard` | `--demo` previews the web UI with synthetic data |
| `doctor` | Symbol filters, risk-adjusted affordability, account access |
| `backtest` | Measures the configured strategy on real history |
| `trade` | Runs the bot |

### Research tools

| Tool | Answers |
|---|---|
| `tools/qa.py` | Does the code still do what the docs promise? |
| `tools/sweep.py` | Does this strategy work across symbols, or was that one cell luck? |
| `tools/compare.py` | Does routing beat the strategies it routes between? |
| `tools/regime_edge.py` | Does the regime signal carry information at all? |
| `tools/oos.py` | Does the result survive data the rule never saw? |
| `tools/cap_test.py` | Does the daily profit cap help or hurt? |
| `tools/reality_check.py` | Ruin simulation for a fixed daily target |

**`tools/oos.py` is the one that matters.** Every strategy should clear it
before you believe anything else.

---

## How it trades

One decision per **closed bar**, never per tick. Each gate can veto before the
next runs:

```
closed bar
   ├─ risk preflight ......... kill file, halt, equity floor, daily loss limit
   ├─ daily target ........... banked already? stand down until tomorrow
   ├─ position open? ......... then nothing
   ├─ read the market ........ efficiency ratio → trending / ranging / unclear
   ├─ route .................. trending → trend_atr, otherwise stand down
   ├─ averaging-down check ... against the position the EXCHANGE reports
   ├─ size ................... from stop distance; refuse if below minimum
   └─ place .................. maker LIMIT entry + STOP_MARKET + TAKE_PROFIT
```

Ticks arrive once per second and drive three other jobs: proximity alerts, the
kill-switch check, and dry-run fill simulation.

### Strategies

| Name | Description |
|---|---|
| `switcher` | **Default.** Reads the market and routes; stands down when neither applies |
| `trend_atr` | Donchian breakout with an ATR stop. Profitable *only* in trending regimes |
| `mean_reversion` | Z-score fade. Measured negative in every regime — kept as a control |
| `funding_arb` | Delta-neutral funding capture. Refuses to run under-capitalised, by design |

Override the router at runtime from Telegram (`/strategy trend_atr`) or the
dashboard. A manual choice always beats the reading, and the reading keeps
updating so you can see what you are overriding.

---

## Monitoring and control

Two surfaces, both of which **only ever enqueue** — the engine executes on its
own thread, so order placement stays single-threaded.

**Telegram** — outbound-only, so the host needs no inbound port and no tunnel.
`/status` `/pnl` `/target` `/strategy` `/close` `/halt` `/resume`.
Commands are accepted **only** from your configured chat ID; destructive actions
require a confirmation button backed by a single-use nonce.

**Web dashboard** — equity, target progress, position with progress bars toward
TP and SL, live event log, and controls. Binds to `127.0.0.1`; reach it over an
SSH tunnel or Tailscale. Token auth on every request, and a valid token is never
locked out by someone else guessing.

---

## Deployment

Targets an Oracle Cloud Always Free Ampere A1 (ARM64). Nothing compiles.

```sh
scp -r mudalali-buwa ubuntu@<ip>:~/
ssh ubuntu@<ip> 'bash ~/mudalali-buwa/deploy/setup.sh'
```

**The repository name does not affect anything after this point.** `setup.sh`
finds its own source with `$(dirname $0)/..`, so the checkout can be called
anything, and it always installs to **`/opt/trading-bot`** with the service
registered as **`trading-bot`**. Every `systemctl` and `/opt` path in these
docs is therefore correct whatever you named the clone.

Installs `chrony` (signed requests fail on clock drift), creates a locked-down
service user, and installs a hardened `systemd` unit — strict filesystem, syscall
filter, no new privileges, capped at 1 GB and 50% of one core so it stays a good
neighbour on a shared box. The firewall step is **additive** and never resets
existing rules. Pass `--no-firewall` to skip it, or `--venv` to install into
`/opt/trading-bot/.venv` instead of system-wide.

See [deploy/oracle-notes.md](deploy/oracle-notes.md) — in particular that Oracle
reclaims idle Always Free instances, and a bot waiting on bar closes looks idle.

---

## Safety

Configured in `config.yaml`, enforced in `bot/risk.py`, covered by tests.

- `max_leverage: 3` — ruin simulation crosses 90%/year past 5x
- `risk_per_trade_pct: 2.0` — size follows the stop, not a fixed lot
- `daily_loss_limit_pct: 5.0` — halts for the day; recovery does not un-halt
- `allow_averaging_down: false` — the most common cause of a zeroed account
- `touch KILL` — noticed within ~2s, not at the next bar close
- **Stop-order failure is fatal** — the entry is cancelled and the bot halts. A
  naked position is the one state it refuses to sit in.

API keys need **futures enabled, withdrawals disabled, IP-restricted**. Secrets
live in `.env`, which is gitignored. Never commit it.

---

## Contributing

Any new strategy should implement `on_bars` in `bot/strategies/` and clear
`tools/oos.py` before it is taken seriously. Run `python3 tools/qa.py --offline`
before opening a pull request; CI runs the same thing across Python 3.11–3.13.

## License

MIT — see [LICENSE](LICENSE), including the additional notice about financial
risk. This is not financial advice, and leveraged futures can lose more than
you deposit.
