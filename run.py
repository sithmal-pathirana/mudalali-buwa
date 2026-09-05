#!/usr/bin/env python3
"""
Entry point.

    python3 run.py check                 what $2/day means for your account
    python3 run.py doctor                connectivity, filters, what $43 can trade
    python3 run.py backtest --validate   prove the harness is honest
    python3 run.py backtest              measure the strategy on real history
    python3 run.py trade                 run the bot (testnet + dry-run by default)
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def require_working_ssl() -> None:
    """
    Some bundled interpreters (Thonny's, for one) ship an ssl module linked
    against an OpenSSL that is not installed, which makes every https call
    fail with the useless message "unknown url type: https". Catch it here
    rather than three layers down inside an exchange call.
    """
    try:
        import ssl  # noqa: F401
    except ImportError as e:
        exe = sys.executable
        sys.exit(
            f"\n  This interpreter cannot do HTTPS: {e}\n"
            f"    interpreter: {exe}\n"
            f"  Binance is https-only, so nothing here will work under it.\n"
            f"  Run with a system Python instead, for example:\n"
            f"    /usr/bin/python3 {' '.join(sys.argv)}\n")


require_working_ssl()

from bot.config import Config                    # noqa: E402


def setup_logging(verbose: bool = False) -> None:
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = "%(asctime)s %(levelname)-8s %(name)-10s %(message)s"
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format=fmt,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(ROOT / "logs" / "bot.log")],
    )


# ------------------------------------------------------------------- doctor
def cmd_doctor(cfg: Config, equity_override: float | None = None) -> int:
    from bot.binanceapi import Binance, BinanceError
    from bot.filters import SymbolRules
    from bot.strategies import build

    api = Binance(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
    print(f"\n  endpoint      {api.base}")
    try:
        drift = api.sync_clock()
        print(f"  clock drift   {drift} ms  {'OK' if abs(drift) < 1000 else 'FIX YOUR NTP'}")
        info = api.exchange_info()
        print(f"  symbols       {len(info['symbols'])} tradable")
    except BinanceError as e:
        print(f"  FAILED        {e}")
        return 1

    rules = SymbolRules.from_exchange_info(info, cfg.symbol)
    price = float(api.mark_price(cfg.symbol)["markPrice"])
    print(f"\n  {rules.describe(price)}")

    equity = None
    if cfg.api_key:
        try:
            equity = api.usdt_equity()
            print(f"  account       {equity:,.2f} USDT ({cfg.mode})")
        except BinanceError as e:
            print(f"  account       unreadable: {e}")
            if e.help:
                print()
                for line in e.explain().split("\n")[2:]:
                    print(f"  {line}")
                print()
    else:
        print(f"  account       no API key for mode={cfg.mode}; using $43 for sizing math")

    if equity_override is not None:
        # Testnet hands you a large demo balance, which hides exactly the
        # constraint that decides everything live: whether the account can
        # legally place the order at all.
        print(f"\n  PLANNING AGAINST ${equity_override:,.2f} "
              f"(actual balance ignored)")
        equity = equity_override
    equity = equity if equity else 43.0
    strat = build(cfg.strategy, cfg.params)
    ok, note = strat.feasible(equity, price, rules)
    # Control surfaces: the question "alerts work but commands do not" should
    # be answerable here rather than by grepping config on the server.
    print("\n  control surfaces")
    if not cfg.telegram_token:
        print("    telegram      no token -- no alerts, no commands")
    elif not cfg.telegram_chat_id:
        print("    telegram      token set but TELEGRAM_CHAT_ID missing;")
        print("                  control stays disabled (that id is the allowlist)")
    elif cfg.telegram.control:
        print(f"    telegram      alerts ON, commands ON  (chat {cfg.telegram_chat_id})")
    else:
        print(f"    telegram      alerts ON, commands OFF  (chat {cfg.telegram_chat_id})")
        print("                  set `telegram: control: true` in config.yaml,")
        print("                  then restart, to use /status /close /strategy")
    if cfg.dashboard.enabled:
        tok = "set" if cfg.dashboard_token else "generated fresh each restart"
        print(f"    dashboard     {cfg.dashboard.host}:{cfg.dashboard.port}, token {tok}")
    else:
        print("    dashboard     disabled")

    print(f"\n  strategy      {strat.name}: {'RUNNABLE' if ok else 'NOT RUNNABLE'}")
    if note:
        print(f"                {note}")

    # What can an account this size actually place an order in?
    print(f"\n  With ${equity:,.2f} at {cfg.risk.max_leverage}x you can deploy "
          f"${equity * cfg.risk.max_leverage:,.2f} of notional -- but the risk layer "
          f"caps\n  each trade at {cfg.risk.risk_per_trade_pct:.1f}% of equity "
          f"(${equity * cfg.risk.risk_per_trade_pct / 100:.2f}) between entry and stop.")
    print("\n  'Tradable' below is the RISK-ADJUSTED answer -- what the bot will")
    print("  actually accept, not what leverage alone would allow. (QA F16)\n")

    # Use a representative 2% stop to convert the risk budget into notional.
    stop_distance = 0.02
    budget_notional = equity * cfg.risk.risk_per_trade_pct / 100 / stop_distance
    print(f"    {'symbol':<11} {'price':>12} {'min order':>10} {'leverage':>9} {'tradable':>9}")
    print("    " + "-" * 55)
    any_ok = False
    for sym in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT"):
        try:
            r = SymbolRules.from_exchange_info(info, sym)
            px = float(api.mark_price(sym)["markPrice"])
            m = r.min_affordable_notional(px)
            by_leverage = "yes" if m <= equity * cfg.risk.max_leverage else "no"
            tradable = m <= budget_notional
            any_ok = any_ok or tradable
            print(f"    {sym:<11} {px:>12,.4f} {m:>10,.2f} {by_leverage:>9} "
                  f"{'YES' if tradable else 'no':>9}")
        except (KeyError, BinanceError):
            continue
    print(f"\n  At a 2% stop your risk budget funds about ${budget_notional:,.2f} of "
          f"notional.")
    if not any_ok:
        print("  Nothing here is tradable within your risk limit. Lower the symbol")
        print("  price (cheaper contract), or accept a larger risk_per_trade_pct.")
    print()
    return 0


# --------------------------------------------------------------------- scan
def cmd_scan(cfg: Config, args) -> int:
    from bot.binanceapi import Binance, BinanceError
    from bot.filters import SymbolRules
    from bot.scanner import ScanConfig, Scanner

    api = Binance(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
    equity = args.equity
    if equity is None and cfg.api_key:
        try:
            equity = api.usdt_equity()
        except BinanceError:
            equity = None
    equity = equity or 43.0

    # What the risk layer would actually fund, at a representative 2% stop.
    budget = equity * cfg.risk.risk_per_trade_pct / 100 / 0.02

    scan_cfg = ScanConfig(**(cfg.universe or {}))
    scanner = Scanner(api, scan_cfg)
    info = api.exchange_info()
    cache: dict = {}

    def rules_for(sym):
        if sym not in cache:
            cache[sym] = SymbolRules.from_exchange_info(info, sym)
        return cache[sym]

    print(f"\n  SCANNING   equity ${equity:,.2f}   "
          f"risk {cfg.risk.risk_per_trade_pct}%/trade   "
          f"funds ~${budget:,.2f} notional at a 2% stop")
    print(f"  universe: top {scan_cfg.max_symbols} USDT perps "
          f"above ${scan_cfg.min_quote_volume/1e6:.0f}M daily volume\n")

    res = scanner.scan(risk_budget_notional=budget, rules_for=rules_for)

    print(f"  {'symbol':<14} {'score':>8}")
    print("  " + "-" * 74)
    for c in res.ranked[: args.top]:
        print(c.line())
    if not res.ranked:
        print("  nothing passed the filters -- the bot would stand down")

    if args.all and res.rejected:
        print(f"\n  rejected ({len(res.rejected)}):")
        from collections import Counter
        reasons = Counter(c.rejected.split("(")[0].strip() for c in res.rejected)
        for reason, n in reasons.most_common():
            print(f"    {n:>3}  {reason}")

    print(f"\n  {res.summary()}")
    if res.best:
        print(f"\n  the bot would trade {res.best.symbol} next time it is flat")
    print()
    return 0


# ------------------------------------------------------------------- report
def cmd_report(cfg: Config, args) -> int:
    import json as _json

    from bot import report as rp
    from bot.binanceapi import Binance

    if not cfg.api_key:
        print(f"\n  No API key for mode={cfg.mode}. The report reads your account "
              f"history,\n  so credentials are required.\n")
        return 1

    api = Binance(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
    api.sync_clock()
    data = rp.gather(api, cfg.symbol, args.days)
    data["state"] = rp._local_state()
    data["log"] = rp._log_signals(args.days)
    data["mode"] = cfg.mode
    data["strategy"] = cfg.strategy
    data["interval"] = cfg.interval

    text = rp.render(data)
    print()
    print(text)
    print()

    out = Path(args.out) if args.out else ROOT / "logs" / "run-report.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n")
    print(f"  saved to {out}")
    if args.out and args.out.endswith(".json"):
        Path(args.out).write_text(_json.dumps(data, indent=2, default=str))
        print(f"  raw json at {args.out}")
    print("  This contains no keys or account identifiers -- safe to share.\n")
    return 0


# --------------------------------------------------------------------- myip
def public_ip() -> str | None:
    """Whatever the outside world sees this host as. Several sources, in case
    one is down or blocked."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com",
                "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                ip = r.read().decode().strip()
            if ip and len(ip) < 46:
                return ip
        except Exception:
            continue
    return None


def cmd_myip() -> int:
    ip = public_ip()
    if not ip:
        print("\n  Could not determine this host's public IP.\n")
        return 1
    print(f"\n  This host's public IP:   {ip}\n")
    print("  Paste that into Binance when it asks you to restrict the API key")
    print("  to trusted IPs -- but ONLY if you are running this on the machine")
    print("  that will actually trade.\n")
    print("  Run it on the Oracle instance, not your laptop. A key restricted")
    print("  to your home IP will fail from the server, and home IPs usually")
    print("  change without warning.\n")
    return 0


# ---------------------------------------------------------------- verifykey
def cmd_verifykey(cfg: Config, args) -> int:
    """
    Test a key/secret pair against the exchange before committing it to .env.

    Worth its own command because the failure modes are indistinguishable from
    the outside: a key with the wrong permissions, a key for the wrong
    environment and a mistyped secret all look like "it does not work".
    """
    import getpass

    from bot.binanceapi import Binance, BinanceError

    env = "live" if args.live else "testnet"
    print(f"\n  Verifying a {env.upper()} key.\n")
    if not args.live:
        print("  Testnet keys now come from the demo trading site:")
        print("    1. log in at https://demo.binance.com/en/futures")
        print("       (your normal Binance account, not a separate testnet login)")
        print("    2. account icon, top right -> API Management, or go direct to")
        print("       https://demo.binance.com/en/my/settings/api-management")
        print("    3. Create API -> name it -> copy the key and secret")
        print("  The API host is unchanged; only the UI moved.\n")

    try:
        key = input("    API key:    ").strip()
        secret = getpass.getpass("    API secret: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n\n  Cancelled -- nothing was sent or saved.\n")
        return 1
    if not key or not secret:
        print("\n  Both values are required.\n")
        return 1

    api = Binance(key, secret, testnet=not args.live)
    print(f"\n  endpoint    {api.base}")
    try:
        drift = api.sync_clock()
        print(f"  clock       {drift} ms drift")
    except BinanceError as e:
        print(f"  UNREACHABLE {e}\n")
        return 1

    try:
        equity = api.usdt_equity()
    except BinanceError as e:
        print(f"\n  REJECTED    {e}\n")
        if e.help:
            for line in e.help.split("\n"):
                print(f"  {line}")
        print()
        return 1

    print(f"  balance     {equity:,.2f} USDT")
    try:
        positions = api.positions()
        print(f"  positions   {len(positions)} open")
    except BinanceError:
        pass
    print(f"\n  WORKS. Save it in .env as:")
    prefix = "BINANCE_LIVE" if args.live else "BINANCE_TESTNET"
    print(f"    {prefix}_KEY={key[:6]}...      (the full value)")
    print(f"    {prefix}_SECRET=...            (the full value)\n")
    if not args.live and equity == 0:
        print("  Balance is 0. The demo site usually has a faucet or a reset")
        print("  button to top the account back up.\n")
    return 0


# ----------------------------------------------------------------- netcheck
def cmd_netcheck(cfg: Config) -> int:
    """
    Run this on any new host BEFORE deploying. REST working does not imply
    websockets working: on some networks fstream.binance.com completes the
    handshake and then never sends a byte, which looks like a healthy bot
    that simply never trades.
    """
    import asyncio
    import json as _json
    import urllib.error
    import urllib.request

    print("\n  ENDPOINT CHECK\n")
    from bot.binanceapi import LIVE, TESTNET, TESTNET_LEGACY

    rest = [
        ("REST futures live", f"{LIVE}/fapi/v1/time"),
        ("REST testnet (in use)", f"{TESTNET}/fapi/v1/time"),
        ("REST testnet (legacy alias)", f"{TESTNET_LEGACY}/fapi/v1/time"),
        ("Telegram API", "https://api.telegram.org"),
    ]
    geo_blocked = False
    for label, url in rest:
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                r.read(64)
            print(f"    {label:<26} OK")
        except urllib.error.HTTPError as e:
            # Binance answers restricted jurisdictions with 451, or a body
            # naming the restriction. It is a legal block on the IP, not a
            # network fault, and no amount of retrying will clear it.
            body = ""
            try:
                body = e.read().decode(errors="replace")[:300]
            except Exception:
                pass
            if e.code == 451 or "restricted location" in body.lower():
                geo_blocked = True
                print(f"    {label:<26} GEO-BLOCKED  HTTP {e.code}")
                print(f"    {'':<26} this IP's country is not served by Binance")
            else:
                print(f"    {label:<26} FAIL  HTTP {e.code}: {body[:44]}")
        except Exception as e:
            print(f"    {label:<26} FAIL  {type(e).__name__}: {str(e)[:44]}")

    if geo_blocked:
        print("\n    Binance refuses API access from this host's location.")
        print("    Nothing in this repo can work around that, and you should not")
        print("    try to: circumventing an exchange's jurisdiction controls")
        print("    breaches its terms and can freeze the account and its funds.")
        print("    Deploy from a region Binance serves, or use the exchange")
        print("    entity that is licensed where you are.\n")
        return 1

    try:
        import websockets
    except ImportError:
        print("\n    websockets not installed: pip install --user websockets")
        return 1

    async def probe(url, label, timeout=8):
        try:
            async with asyncio.timeout(timeout + 6):
                async with websockets.connect(url, ping_interval=None) as ws:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    d = _json.loads(raw)
                    d = d.get("data", d)
                    print(f"    {label:<26} OK    first event: {d.get('e')}")
                    return True
        except (asyncio.TimeoutError, TimeoutError):
            print(f"    {label:<26} SILENT  handshake fine, no data -- unusable")
        except Exception as e:
            print(f"    {label:<26} FAIL  {type(e).__name__}: {str(e)[:40]}")
        return False

    async def main_probe():
        from bot.stream import WS_LIVE, WS_TESTNET, WS_TESTNET_LEGACY
        live = await probe(f"{WS_LIVE}/ws/btcusdt@aggTrade", "WS futures live")
        test = await probe(f"{WS_TESTNET}/ws/btcusdt@aggTrade", "WS testnet (in use)")
        await probe(f"{WS_TESTNET_LEGACY}/ws/btcusdt@aggTrade", "WS testnet (legacy alias)")
        return live, test

    print()
    live_ok, test_ok = asyncio.run(main_probe())
    print()
    if not live_ok and test_ok:
        print("    Live futures websockets are blocked or silenced on this network.")
        print("    Testnet works, so develop here and re-run netcheck on the server")
        print("    you intend to trade from. Do not go live from a host that fails")
        print("    this check -- the bot will look healthy and never see a price.")
    elif live_ok and test_ok:
        print("    This host can run the bot in real time against live markets.")
    print()
    return 0


# ------------------------------------------------------------------- alerts
def cmd_alerts(cfg: Config) -> int:
    from bot.notify import Event, Notifier

    n = Notifier.from_config(cfg)
    print(f"\n  channels: {', '.join(n.channels)}")
    if n.channels == ["none configured (log only)"]:
        print("  Nothing configured. Fill in TELEGRAM_* and/or SMTP_* in .env.\n")
        return 1
    n.send(Event.TRADE_OPEN,
           "TEST ALERT -- no real trade.\n"
           "BUY 0.01 BTCUSDT @ 80,000.0\n"
           "SL 78,400.0   TP 82,400.0")
    n.send(Event.APPROACH_SL,
           "TEST ALERT -- 82% of the way to the stop.\n"
           "If this reached your phone, proximity warnings work.")
    print("  Two test alerts sent. Check your phone and inbox.\n")
    return 0


# ----------------------------------------------------------------- telegram
def cmd_telegram(cfg: Config, args) -> int:
    import json as _json
    import time as _time
    import urllib.request
    from datetime import datetime, timezone

    from bot.telegram_control import TelegramControl

    if not cfg.telegram_token:
        print("\n  TELEGRAM_TOKEN is not set in .env.\n"
              "  1. Message @BotFather on Telegram, send /newbot, follow the prompts.\n"
              "  2. Put the token it gives you in .env as TELEGRAM_TOKEN.\n"
              "  3. Re-run:  /usr/bin/python3 run.py telegram --setup\n")
        return 1

    def call(method, params=None):
        url = f"https://api.telegram.org/bot{cfg.telegram_token}/{method}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=40) as r:
            return _json.loads(r.read())

    if args.setup:
        print("\n  TELEGRAM SETUP\n")
        try:
            me = call("getMe")
        except Exception as e:
            print(f"    token rejected: {e}\n")
            return 1
        u = me["result"]
        print(f"    bot: @{u.get('username')}  ({u.get('first_name')})")
        if cfg.telegram_chat_id:
            print(f"    chat id already configured: {cfg.telegram_chat_id}")
            print("\n    Sending a test message...")
            try:
                call("sendMessage", {"chat_id": cfg.telegram_chat_id,
                                     "text": "Setup check: this bot can reach you."})
                print("    sent. Check your phone.\n")
            except Exception as e:
                print(f"    FAILED: {e}\n")
                return 1
            return 0

        print(f"\n    Now open Telegram, find @{u.get('username')}, and send it /start")
        print("    Waiting up to 60 seconds...\n")
        deadline = _time.time() + 60
        offset = 0
        while _time.time() < deadline:
            try:
                res = call("getUpdates", {"offset": offset, "timeout": 10})
            except Exception as e:
                print(f"    poll error: {e}")
                break
            for upd in res.get("result", []):
                offset = max(offset, upd["update_id"] + 1)
                chat = ((upd.get("message") or {}).get("chat") or {})
                if chat.get("id"):
                    print(f"    Found you: {chat.get('first_name','')} "
                          f"(@{chat.get('username','?')})")
                    print(f"\n    Add this to .env:\n")
                    print(f"      TELEGRAM_CHAT_ID={chat['id']}\n")
                    print("    That chat id is the only thing stopping a stranger")
                    print("    who finds your bot from issuing commands.\n")
                    return 0
        print("    No message received. Send /start to the bot and try again.\n")
        return 1

    if args.demo:
        if not cfg.telegram_chat_id:
            print("\n  TELEGRAM_CHAT_ID is not set. Run: run.py telegram --setup\n")
            return 1
        tc = TelegramControl(cfg.telegram_token, cfg.telegram_chat_id)
        tc.start()
        print("\n  TELEGRAM DEMO (synthetic data, no exchange calls)\n")
        print("  Try /status, /pnl, /target, /close on your phone. Ctrl-C to stop.\n")
        tc.send("Demo mode. Synthetic data, nothing real.\n\nSend /help.")
        price, realized = 80_000.0, 1.25
        try:
            while True:
                price += random.gauss(0, 20)
                tc.publish({
                    "symbol": "BTCUSDT", "mode": "demo", "dry_run": True,
                    "equity": 43.0, "price": price, "realized_today": realized,
                    "day": 1, "target": 2.0, "target_pct": realized / 2 * 100,
                    "target_reached": False, "stop_when_reached": True,
                    "target_note": "day 1: target $2.00/day on $43.00 equity "
                                   "= 4.65%/day required",
                    "halted": False, "halt_reason": "", "trades_today": 1,
                    "stream_ok": True,
                    "position": {"side": "BUY", "qty": 0.0012, "entry": 79_400.0,
                                 "stop": 78_200.0, "take_profit": 82_400.0,
                                 "unrealized": (price - 79_400.0) * 0.0012,
                                 "to_tp": (price - 79_400.0) / 3000.0,
                                 "to_sl": (79_400.0 - price) / 1200.0},
                })
                for cmd in tc.pop_commands():
                    print(f"  >> command received: {cmd.action}")
                    tc.send(f"(demo) {cmd.action} would execute here. Nothing sent.")
                _time.sleep(1)
        except KeyboardInterrupt:
            tc.stop()
            print("\n  stopped\n")
        return 0

    print("\n  Telegram control runs inside the bot -- `run.py trade` starts it.\n"
          "  --setup   verify the token and find your chat id\n"
          "  --demo    try the commands against synthetic data\n")
    return 0


# ---------------------------------------------------------------- dashboard
def cmd_dashboard(cfg: Config, args) -> int:
    """
    --demo serves a synthetic account so you can check the layout on your phone
    without keys or a running bot. Numbers move; nothing is real.
    """
    import math
    import random
    import time as _time
    from datetime import datetime, timezone

    from bot.dashboard import Dashboard, generate_token

    if not args.demo:
        print("\n  The dashboard runs inside the bot: `run.py trade` starts it and\n"
              "  prints the URL. Use --demo to preview the interface on its own.\n")
        return 1

    host = args.host or cfg.dashboard.host
    port = args.port or cfg.dashboard.port
    token = cfg.dashboard_token or generate_token()
    dash = Dashboard(token, host, port)
    url = dash.start()

    print(f"\n  DEMO DASHBOARD (synthetic data)\n\n    {url}\n")
    if host in ("127.0.0.1", "localhost"):
        print("  Laptop: open the URL above.")
        print("  Phone:  tunnel first, then open it on the phone --")
        print(f"          ssh -L {port}:127.0.0.1:{port} <user>@<this-host>\n")
    print("  Ctrl-C to stop.\n")

    price, entry, t0 = 80_000.0, 79_400.0, _time.time()
    stop, tp = 78_200.0, 82_400.0
    realized, events = 0.0, []

    def add(text):
        events.append({"t": datetime.now(timezone.utc).strftime("%H:%M:%S"), "text": text})
        del events[:-40]

    add("startup: demo mode, synthetic data")
    add("trade opened: BUY 0.0012 BTCUSDT @ 79,400.0")

    try:
        while True:
            elapsed = _time.time() - t0
            price += random.gauss(0, 22) + math.sin(elapsed / 30) * 6
            qty = 0.0012
            unreal = (price - entry) * qty
            to_tp = (price - entry) / (tp - entry)
            to_sl = (entry - price) / (entry - stop)
            if to_tp > 0.8 and not any("take-profit" in e["text"] for e in events):
                add("approaching take-profit: 80% of the way")
            if to_sl > 0.8 and not any("stop" in e["text"] for e in events):
                add("approaching stop-loss: 80% of the way")
            dash.publish({
                "symbol": "BTCUSDT", "mode": "demo", "strategy": "trend_atr",
                "dry_run": True, "equity": 43.0 + realized + unreal, "price": price,
                "realized_today": realized, "day": 1, "target": 2.0,
                "target_pct": realized / 2.0 * 100, "target_reached": realized >= 2.0,
                "stop_when_reached": True,
                "target_note": "day 1: target $2.00/day on $43.00 equity = 4.65%/day required",
                "halted": False, "halt_reason": "", "trades_today": 1,
                "position": {"side": "BUY", "qty": qty, "entry": entry, "stop": stop,
                             "take_profit": tp, "unrealized": unreal,
                             "to_tp": to_tp, "to_sl": to_sl},
                "events": list(events), "stream_ok": True,
            })
            for cmd in dash.pop_commands():
                add(f"command received: {cmd.action} (demo -- nothing sent)")
                print(f"  >> dashboard command: {cmd.action}")
            _time.sleep(1)
    except KeyboardInterrupt:
        dash.stop()
        print("\n  stopped\n")
    return 0


# ----------------------------------------------------------------- backtest
def cmd_backtest(cfg: Config, args) -> int:
    from bot import backtest as bt
    from bot.binanceapi import Binance, BinanceError
    from bot.strategies import build
    from bot.strategies.base import Bar

    if args.validate:
        print("\n  VALIDATING THE BACKTEST HARNESS\n")
        ok = bt.validate()
        print()
        return 0 if ok else 1

    api = Binance(testnet=cfg.testnet)          # public data needs no keys
    try:
        raw = []
        klines = api.klines(cfg.symbol, cfg.interval, limit=1500)
        raw.extend(klines)
    except BinanceError as e:
        print(f"  could not fetch history: {e}")
        return 1

    bars = [Bar.from_kline(k) for k in raw[:-1]]
    strat = build(cfg.strategy, cfg.params)

    # Use the symbol's REAL minimum order size, so the backtest cannot take
    # trades the live account would be too small to place.
    from bot.filters import SymbolRules
    rules = SymbolRules.from_exchange_info(api.exchange_info(), cfg.symbol)
    min_notional = rules.min_affordable_notional(bars[-1].close)

    res = bt.run(bars, strat, equity=args.equity, risk_pct=cfg.risk.risk_per_trade_pct,
                 max_leverage=cfg.risk.max_leverage, min_notional=min_notional)
    print(f"\n  minimum order size enforced: ${min_notional:,.2f}")

    minutes = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}
    per_day = 1440 / minutes.get(cfg.interval, 15)
    days = len(bars) / per_day

    print(f"\n  BACKTEST  {cfg.symbol} {cfg.interval}  strategy={strat.name}\n")
    print(res.report(f"{cfg.symbol} {cfg.interval}", per_day, days))
    print()
    if res.net > 0:
        print(f"  To earn $2.00/day at this measured rate you would need about "
              f"${args.equity * 2.0 / (res.net / days):,.0f} of capital.")
    else:
        print("  This configuration lost money on the sample. Do not run it live.")
    print("\n  One sample is not evidence. Re-run across several symbols and\n"
          "  intervals; if the result flips sign, there is no edge here.\n")
    return 0


# -------------------------------------------------------------------- trade
def cmd_trade(cfg: Config) -> int:
    from bot.engine import Engine

    if not cfg.testnet and not cfg.dry_run:
        print("\n  !! LIVE MODE WITH REAL MONEY !!")
        print(f"     symbol={cfg.symbol}  leverage={cfg.risk.max_leverage}x  "
              f"daily loss limit={cfg.risk.daily_loss_limit_pct}%")
        if input("     Type LIVE to continue: ").strip() != "LIVE":
            print("     aborted\n")
            return 1
    return Engine(cfg).run()


def main() -> int:
    p = argparse.ArgumentParser(description="Binance futures bot")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("-c", "--config", default=str(ROOT / "config.yaml"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="what $2/day demands of your capital")
    sub.add_parser("project", help="model the escalating target schedule")
    sub.add_parser("netcheck", help="probe every endpoint this host needs")
    sub.add_parser("myip", help="this host's public IP, for Binance IP restriction")
    sc = sub.add_parser("scan", help="rank the tradable universe right now")
    sc.add_argument("--equity", type=float, default=None,
                    help="risk budget to filter against (default: account balance)")
    sc.add_argument("--top", type=int, default=15, help="how many to show")
    sc.add_argument("--all", action="store_true", help="also list what was rejected")
    rp = sub.add_parser("report", help="what actually happened, from exchange records")
    rp.add_argument("--days", type=int, default=8, help="how far back (default 8)")
    rp.add_argument("--out", default=None, help="also write the raw JSON here")
    v = sub.add_parser("verifykey", help="test an API key/secret pair before saving it")
    v.add_argument("--live", action="store_true",
                   help="check against live Binance instead of testnet")
    sub.add_parser("alerts", help="send a test alert to phone and email")
    t = sub.add_parser("telegram", help="set up or preview Telegram control")
    t.add_argument("--setup", action="store_true",
                   help="verify the token and discover your chat id")
    t.add_argument("--demo", action="store_true",
                   help="run the command interface against synthetic data")
    d = sub.add_parser("dashboard", help="serve the dashboard")
    d.add_argument("--demo", action="store_true",
                   help="serve synthetic data so you can look at the UI now")
    d.add_argument("--host", default=None, help="override bind address")
    d.add_argument("--port", type=int, default=None)
    doc = sub.add_parser("doctor", help="connectivity, filters, affordability")
    doc.add_argument("--equity", type=float, default=None,
                     help="plan against this equity instead of the account balance "
                          "(e.g. --equity 43 to see what live would do)")
    b = sub.add_parser("backtest", help="measure the strategy on history")
    b.add_argument("--validate", action="store_true", help="prove the harness is honest")
    b.add_argument("--equity", type=float, default=43.0)
    sub.add_parser("trade", help="run the bot")
    args = p.parse_args()

    setup_logging(args.verbose)

    if args.cmd == "check":
        import tools.reality_check as rc
        rc.demand_table(); rc.ruin_table(); rc.growth_paths()
        return 0

    if args.cmd == "project":
        import tools.projection as pj
        pj.show_rate_path(); pj.show_survival()
        return 0

    cfg = Config.load(args.config)
    if args.cmd == "netcheck":
        return cmd_netcheck(cfg)
    if args.cmd == "myip":
        return cmd_myip()
    if args.cmd == "report":
        return cmd_report(cfg, args)
    if args.cmd == "scan":
        return cmd_scan(cfg, args)
    if args.cmd == "verifykey":
        return cmd_verifykey(cfg, args)
    if args.cmd == "alerts":
        return cmd_alerts(cfg)
    if args.cmd == "dashboard":
        return cmd_dashboard(cfg, args)
    if args.cmd == "telegram":
        return cmd_telegram(cfg, args)
    if args.cmd == "doctor":
        return cmd_doctor(cfg, getattr(args, "equity", None))
    if args.cmd == "backtest":
        return cmd_backtest(cfg, args)
    if args.cmd == "trade":
        return cmd_trade(cfg)
    return 1


if __name__ == "__main__":
    sys.exit(main())
