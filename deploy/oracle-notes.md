# Oracle Cloud Always Free — deployment notes

## Shape and region

Your 2 OCPU / 12 GB instance is an **Ampere A1 (aarch64)**, not x86. Nothing
here compiles, so that costs you nothing — but it is why `setup.sh` installs
PyYAML from `apt` rather than building a wheel.

The bot idles at well under 100 MB and a few percent of one core. The systemd
unit caps it at `MemoryMax=1G` so a leak can never take the box down; you have
roughly 11 GB spare for anything else you want to run there.

**Pick a region close to Binance.** Their matching engines sit in AWS Tokyo, so
`ap-tokyo-1` or `ap-singapore-1` cut round-trip latency to ~10–40 ms versus
~250 ms from Europe or the US. At this strategy's frequency latency is not an
edge, but it does reduce the chance of a stop being placed against a stale price.

## Two things that will bite you

**Idle reclamation.** Oracle reclaims Always Free compute instances that look
idle (under ~10% CPU, plus low network and memory, over a 7-day window). A bot
that sits waiting for bar closes looks exactly like an idle box. If your
instance disappears, this is why. Upgrading the account to Pay As You Go — which
keeps Always Free resources free — exempts you from reclamation and is the only
reliable fix.

**Clock drift.** Every signed Binance request carries a timestamp and is
rejected outside a 5-second window. `setup.sh` installs and enables `chrony`,
and the systemd unit refuses to start before `time-sync.target`. If you ever see
error `-1021`, this is the cause; the client re-syncs automatically but fix NTP.

## Run netcheck on the instance before trusting it

From this laptop, live futures websockets connect and then never deliver data —
the handshake succeeds and nothing arrives. A bot on a host like that looks
perfectly healthy in the logs and simply never trades.

```sh
sudo -u trader /usr/bin/python3 /opt/trading-bot/run.py netcheck
```

`setup.sh` runs it for you. If `WS futures live` comes back `SILENT`, do not go
live from that host. The engine now detects the condition at runtime too — 45
seconds of silence on a connected socket forces a reconnect and alerts you.

## Operating it

```sh
sudo systemctl status trading-bot          # is it up
journalctl -u trading-bot -f               # live logs
journalctl -u trading-bot --since today | grep ALERT
sudo systemctl restart trading-bot         # after a halt, once you know why
sudo -u trader touch /opt/trading-bot/KILL # emergency stop from any shell
```

`KillSignal=SIGINT` matters: systemd sends the same signal as Ctrl-C, so
`systemctl stop` runs the engine's shutdown path — which pulls a resting entry
order but **deliberately leaves the stop and take-profit on the exchange** when
a position is open. With the bot down, that exchange-side stop is the only
thing protecting you.

`TimeoutStopSec=60` is sized for that path: orders are cancelled first, then the
control threads are stopped (Telegram joins for at most 5s). At the old 30s,
with Telegram's long poll in flight, SIGKILL could land before any order was
cancelled.

## Telegram: no tunnel needed

If you set up Telegram control, you do not need any of the tunnelling below for
day-to-day use. The bot polls Telegram outbound, so it works through Oracle's
default deny-all inbound policy untouched — no security-list change, no port
forward, no Tailscale. `/status` and `/close` reach it from anywhere.

```sh
sudo -u trader /usr/bin/python3 /opt/trading-bot/run.py telegram --setup
```

Keep the dashboard for laptop sessions where you want the charts and bars.

## Reaching the dashboard

The bot serves it on `127.0.0.1:8080` inside the instance. UFW blocks inbound
everything but SSH, and that is deliberate — the dashboard can close positions,
so it should not be reachable from the internet.

```sh
# from your laptop
ssh -L 8080:127.0.0.1:8080 ubuntu@<instance-ip>
# then open http://127.0.0.1:8080/?t=<DASHBOARD_TOKEN>
```

On a phone, use an SSH client that forwards ports (Termius, JuiceSSH). For
something you will use daily, Tailscale is less friction: install it on the
instance and the phone, set `dashboard.host: 0.0.0.0` in `config.yaml`, and
reach it at the instance's tailnet IP — still not public, no tunnel to
re-establish each time.

Do **not** open port 8080 in the Oracle security list. If you ever do decide to
expose it, put it behind a reverse proxy with TLS and HTTP basic auth on top of
the bot's own token, and be aware you are publishing an endpoint that closes
trades.

Get the token onto your phone by generating it once and storing it in your
password manager:

```sh
/usr/bin/python3 -c "import secrets;print(secrets.token_urlsafe(24))"
```

## Getting the IP for Binance's key restriction

Binance will not let you enable Futures on a live API key until the key is
restricted to specific IPs. It wants the address of the machine that will
actually trade, so run this **on the instance**, not on your laptop:

```sh
sudo -u trader /usr/bin/python3 /opt/trading-bot/run.py myip
```

A key restricted to your home IP will fail from the server, and home addresses
usually change without warning. If you want to run `doctor` from your laptop as
well, add both addresses -- Binance accepts a list.

This also settles an ordering question: **create the Oracle instance before the
live API key**, because you cannot fill in the restriction without its IP.

## Backups

`data/state.json` holds the schedule start date, the halt flag, and the day's
realised P&L. Losing it resets your day-1 date and therefore the escalation
schedule.

```sh
sudo -u trader crontab -e
# 0 * * * * cp /opt/trading-bot/data/state.json /opt/trading-bot/data/state.$(date +\%H).bak
```
