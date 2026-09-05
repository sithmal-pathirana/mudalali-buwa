"""
The trading loop, event-driven over websockets.

Event sources, all arriving on one queue:
    Tick          every second -- drives proximity alerts to TP/SL
    BarClosed     drives strategy decisions
    OrderUpdate   real fills from the user-data stream
    StreamStale   connected-but-silent socket, treated as an outage

Ordering discipline is unchanged from the polling version: reconcile, gate on
risk, only then ask the strategy, then size, then place entry AND stop. A
position is never opened without its protective stop.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from datetime import datetime, timezone

from .binanceapi import Binance, BinanceError
from .dashboard import Dashboard, generate_token
from .filters import SymbolRules
from .notify import Event, Notifier
from .portfolio import allocate
from .positions import ActivePosition
from .risk import KILL_FILE, RiskManager
from .state import State, client_order_id, reconcile
from .telegram_control import TelegramControl
from .stream import BarClosed, Disconnected, MarketStream, OrderUpdate, StreamStale, Tick
from .strategies import build
from .strategies.base import Bar
from .targets import TargetSchedule

log = logging.getLogger("engine")


# The KILL file used to raise a KillSwitch exception from emergency_check().
# That was worse than the bug it replaced: KillSwitch subclasses Exception, and
# the trading loop catches `Exception` per iteration to survive transient
# errors -- so the kill would have been logged as "unhandled error, continuing"
# and the bot would have kept trading. The kill is now plain state, acted on
# inline and checked by the loop. (QA R2)

RECONCILE_SECONDS = 60


class Engine:
    # Class-level defaults for the tracking counters. Test harnesses (ours and
    # the QA probes) build a partial Engine with object.__new__ to drive one
    # method in isolation; without these, reconcile_position and the dry-run
    # simulator raise AttributeError instead of exercising the logic. They are
    # plain immutable defaults, so no state is shared between instances.
    _flat_reconciles = 0
    _entry_placed_at = 0.0
    _dry_pending: dict = None
    _last_guard = 0.0
    _last_publish = 0.0
    _last_heartbeat = 0.0
    _last_reconcile = 0.0
    _stopping = False
    _stop_reason = ""
    _seq = 0
    position_amt = 0.0
    last_prices: dict = None
    scanner = None
    _rules_cache: dict = None
    _exchange_info = None

    def __init__(self, cfg):
        self.cfg = cfg
        self.api = Binance(cfg.api_key, cfg.api_secret, testnet=cfg.testnet)
        self.state = State.load()
        self.risk = RiskManager(cfg, self.state)
        self.strategy = build(cfg.strategy, cfg.params)
        self.notify = Notifier.from_config(cfg)
        self.schedule = TargetSchedule.from_config(cfg.targets)
        self.rules: SymbolRules | None = None
        self.stream: MarketStream | None = None
        #: symbol -> ActivePosition. The single-position path is the one-entry
        #: case of this, so there is no parallel code for the two modes.
        self._book: dict[str, ActivePosition] = {}
        self._rules_cache: dict = {}
        self._exchange_info = None
        self.scanner = None
        self.last_price = 0.0
        self.equity = 0.0
        self.bars: list[Bar] = []
        self._seq = self.state.total_trades
        self._last_reconcile = 0.0
        self._last_heartbeat = time.time()
        self._last_publish = 0.0
        self._last_guard = 0.0
        self.events: deque = deque(maxlen=40)
        self.position_amt = 0.0
        self.last_prices: dict[str, float] = {}
        self._entry_placed_at = 0.0
        #: symbol -> resting dry-run entry. A single slot dropped every entry
        #: but the last as soon as the portfolio opened more than one.
        self._dry_pending: dict[str, ActivePosition] = {}
        self._flat_reconciles = 0
        self._stopping = False
        self._stop_reason = ""
        self.dashboard: Dashboard | None = None
        self.telegram: TelegramControl | None = None
        self.notify.listener = self._record_event
        self.notify.context = self.mode_line

    # ------------------------------------------------------------ dashboard
    # ------------------------------------------------------------- the book
    #: A mutable class attribute would be SHARED by every Engine built with
    #: object.__new__, so the default is a sentinel and the property
    #: materialises a per-instance dict on first use.
    _book: dict | None = None

    @property
    def book(self) -> dict:
        if self._book is None:
            self._book = {}
        return self._book

    @book.setter
    def book(self, value: dict) -> None:
        self._book = dict(value or {})

    @property
    def active(self) -> "ActivePosition | None":
        """
        The single open position, or None.

        Kept as a view over the book so every existing call site and test goes
        on working untouched while the book is the real storage underneath.
        With more than one position open it returns the first, which only
        happens in portfolio mode where callers use the book directly.
        """
        return next(iter(self.book.values()), None)

    @active.setter
    def active(self, position) -> None:
        if position is None:
            self.book = {}
        else:
            self.book = {position.symbol: position}

    def position_for(self, symbol: str) -> "ActivePosition | None":
        return self.book.get(symbol)

    def release(self, symbol: str) -> None:
        """Forget ONE position and free its slot. Never touches the others."""
        p = self.book.pop(symbol, None)
        if p is not None:
            self.notify.clear_position_alerts(p.tag)
        if self.stream is not None and symbol != self.cfg.symbol:
            self.stream.remove_symbol(symbol)

    def rules_for(self, symbol: str):
        """Per-symbol filters, cached. exchangeInfo is one request for all."""
        if symbol not in self._rules_cache:
            if self._exchange_info is None:
                self._exchange_info = self.api.exchange_info()
            self._rules_cache[symbol] = SymbolRules.from_exchange_info(
                self._exchange_info, symbol)
        return self._rules_cache[symbol]

    def mode_line(self) -> str:
        """
        One line naming what is running: risk profile, strategy, and how the
        strategy was chosen. Prefixed to every alert so no message is ambiguous
        about which configuration produced it.
        """
        profile = "AGGRESSIVE" if getattr(self.cfg, "aggressive_on", False) else "safe"
        name = self.strategy.name
        mode = getattr(self.strategy, "mode", None)
        if mode and mode != "auto":
            name = f"{name}/{mode.replace('manual/', '')}"
        elif mode == "auto":
            name = f"{name}/auto"
        bits = [profile, name, self.cfg.mode]
        if self.cfg.dry_run:
            bits.append("dry-run")
        return "[" + " · ".join(bits) + "]"

    def _record_event(self, event, subject, body) -> None:
        self.events.append({
            "t": datetime.now(timezone.utc).strftime("%H:%M:%S"),
            "text": f"{event.value}: " + body.replace("\n", " · "),
        })

    def snapshot(self) -> dict:
        prog = self.schedule.progress(self.state.realized_today)
        pos = None
        if self.active is not None:
            p = self.active
            pos = {
                "side": p.side, "qty": p.qty, "entry": p.entry,
                "stop": p.stop, "take_profit": p.take_profit,
                "price": (self.last_prices or {}).get(sym, self.last_price),
                "unrealized": p.unrealized(px) if px else 0.0,
                "to_tp": p.progress_to_tp(px) if px else 0.0,
                "to_sl": p.progress_to_stop(px) if px else 0.0,
            }
        return {
            "symbol": self.cfg.symbol, "mode": self.cfg.mode,
            "strategy": self.strategy.name, "dry_run": self.cfg.dry_run,
            "equity": self.equity, "price": self.last_price,
            "realized_today": self.state.realized_today,
            "day": prog.day, "target": prog.target, "target_pct": prog.pct,
            "target_reached": prog.reached,
            "stop_when_reached": self.schedule.stop_when_reached,
            "target_note": self.schedule.describe(self.equity),
            "halted": self.state.halted, "halt_reason": self.state.halt_reason,
            "trades_today": self.state.trades_today,
            "position": pos, "events": list(self.events),
            "stream_ok": bool(self.stream and self.stream.connected.is_set()),
            "regime": self._regime_note(),
            "strategy_mode": getattr(self.strategy, "mode", "fixed"),
            "strategy_choices": getattr(self.strategy, "choices", []),
            "mode_line": self.mode_line(),
            "positions": self._position_book(),
            "config": self._config_summary(),
            "scan": self._scan_summary(),
        }

    def _position_book(self) -> list[dict]:
        """Every open position. A list even when there is one, so the control
        surfaces need no special case once portfolio mode lands."""
        book = []
        for sym, p in self._positions().items():
            # Each position is valued at ITS OWN last price, not a single
            # global one -- with several symbols open, one shared price would
            # report every position's P&L against the wrong market.
            px = (self.last_prices or {}).get(sym, 0.0)
            book.append({
                "symbol": sym, "side": p.side, "qty": p.qty, "entry": p.entry,
                "stop": p.stop, "take_profit": p.take_profit,
                "price": (self.last_prices or {}).get(sym, self.last_price),
                "unrealized": p.unrealized(px) if px else 0.0,
                "to_tp": p.progress_to_tp(px) if px else 0.0,
                "to_sl": p.progress_to_stop(px) if px else 0.0,
            })
        return book

    def _positions(self) -> dict:
        return self.book

    def _config_summary(self) -> dict:
        """The settings that actually decide behaviour, for /config."""
        r = self.cfg.risk
        return {
            "mode": self.cfg.mode,
            "dry_run": self.cfg.dry_run,
            "symbol": self.cfg.symbol,
            "interval": self.cfg.interval,
            "strategy": self.cfg.strategy,
            "max_leverage": f"{r.max_leverage}x",
            "risk_per_trade": f"{r.risk_per_trade_pct}%",
            "daily_loss_limit": f"{r.daily_loss_limit_pct}%",
            "kill_action": r.kill_action,
            "portfolio": ("on" if self.cfg.portfolio.enabled else "off"),
            "target": (f"${self.schedule.today_target():.2f}/day"
                       if self.schedule.stop_when_reached else "not enforced"),
        }

    def _scan_summary(self) -> dict:
        scanner = getattr(self, "scanner", None)
        last = getattr(scanner, "last", None) if scanner else None
        if last is None:
            return {}
        age = int(time.time() - last.scanned_at)
        return {
            "considered": last.considered,
            "passed": len(last.ranked),
            "age": f"{age // 60}m" if age >= 60 else f"{age}s",
            "top": [{"symbol": c.symbol, "score": round(c.score, 3)}
                    for c in last.ranked[:10]],
        }

    def _regime_note(self) -> str:
        """Which strategy the router is currently willing to run, if any."""
        reading = getattr(self.strategy, "last_reading", None)
        if reading is None:
            return ""
        target = getattr(self.strategy, "routes", {}).get(reading.regime.value)
        return f"{reading} -> {target or 'standing down'}"

    @property
    def controllers(self) -> list:
        """Everything that can observe state and issue commands."""
        return [c for c in (self.dashboard, self.telegram) if c is not None]

    def publish(self) -> None:
        if not self.controllers:
            return
        now = time.time()
        if now - self._last_publish < 0.5:
            return
        self._last_publish = now
        snap = self.snapshot()
        for c in self.controllers:
            c.publish(snap)

    def process_commands(self) -> None:
        """
        One handler for every control surface. Both the dashboard and Telegram
        only ever enqueue; execution happens here, on the engine thread, so
        order placement stays single-threaded regardless of where the command
        came from.
        """
        for controller in self.controllers:
            for cmd in controller.pop_commands():
                origin = cmd.note or type(controller).__name__
                log.info("executing command %s (%s)", cmd.action, origin)
                if cmd.action == "close":
                    target = (cmd.value or "").strip().upper()
                    if target in ("", "ALL"):
                        self.close_all(f"closed manually ({origin})")
                    elif target in self.book:
                        self.close_position(f"closed manually ({origin})",
                                            symbol=target)
                    else:
                        held = ", ".join(sorted(self.book)) or "nothing"
                        self.notify.send(Event.ERROR,
                                         f"Not holding {target}. Currently: {held}")
                elif cmd.action == "halt":
                    self.state.halt(f"halted manually ({origin})")
                    self.notify.send(Event.HALT, f"Halted ({origin}). "
                                                 f"Open positions are untouched.")
                elif cmd.action == "strategy":
                    self.set_strategy(cmd.value, origin)
                elif cmd.action == "resume":
                    previous = self.state.halt_reason
                    self.state.halted = False
                    self.state.halt_reason = ""
                    self.state.save()
                    self.notify.send(Event.STARTUP,
                                     f"Resumed ({origin}).\nCleared halt: {previous}")

    def set_strategy(self, name: str, origin: str = "") -> str:
        """
        Manual override of automatic strategy selection.

        Only meaningful for the router; a bot configured with a single strategy
        has nothing to switch between, and says so rather than failing quietly.
        """
        if not hasattr(self.strategy, "set_override"):
            msg = (f"strategy is {self.strategy.name}, which does not route. "
                   f"Set `strategy: switcher` in config.yaml to enable "
                   f"automatic selection and manual overrides.")
            log.warning(msg)
            self.notify.send(Event.ERROR, msg)
            return msg
        try:
            result = self.strategy.set_override(name or "auto")
        except ValueError as e:
            log.warning("rejected strategy override: %s", e)
            self.notify.send(Event.ERROR, str(e))
            return str(e)

        self.state.strategy_override = self.strategy.override or ""
        self.state.save()
        log.info("strategy selection -> %s (%s)", result, origin or "local")
        reading = getattr(self.strategy, "last_reading", None)
        self.notify.send(Event.STARTUP,
                         f"Strategy selection changed ({origin or 'local'}):\n{result}"
                         + (f"\n\nCurrent market reading: {reading}" if reading else ""))
        return result

    def close_all(self, reason: str) -> int:
        """
        Flatten every open position, one at a time.

        Sequential and individually reported on purpose: if the third close
        fails, the first two are already flat and the remaining ones still
        hold their exchange-side stops. A batch that aborts halfway must never
        leave a position unprotected.
        """
        symbols = list(self.book) or [self.cfg.symbol]
        closed, failed = 0, []
        for sym in symbols:
            try:
                self.close_position(reason, symbol=sym)
                closed += 1
            except Exception as e:
                failed.append(f"{sym}: {e}")
                log.exception("close_all: %s failed", sym)
        if failed:
            self.notify.send(Event.ERROR,
                             f"close_all ({reason}): {closed} closed, "
                             f"{len(failed)} FAILED\n" + "\n".join(failed)
                             + "\nThe failures still hold their stops.")
        else:
            self.notify.send(Event.DAILY_SUMMARY,
                             f"closed {closed} position(s): {reason}")
        return closed

    def close_position(self, reason: str, symbol: str | None = None) -> None:
        """Cancel resting orders, then flatten with a reduce-only market order."""
        symbol = symbol or (self.active.symbol if self.active else self.cfg.symbol)
        if self.cfg.dry_run:
            log.info("dry_run: would close %s (%s)", symbol, reason)
            self.notify.send(Event.SL_HIT, f"DRY RUN -- would close {symbol}: {reason}")
            self.release(symbol)
            return
        try:
            live = self.api.positions(symbol)
        except BinanceError as e:
            log.error("could not read position to close: %s", e)
            self.notify.send(Event.ERROR, f"Close failed reading position: {e}")
            return

        if not live:
            log.info("close requested but %s is already flat", symbol)
            self.notify.send(Event.DAILY_SUMMARY, f"Close requested; {symbol} already flat.")
            self.release(symbol)
            return

        amt = float(live[0]["positionAmt"])
        side = "SELL" if amt > 0 else "BUY"
        # The kill path runs here. It must not be able to crash on a missing
        # filter table -- the exchange already reported an exact position size,
        # so fall back to it verbatim rather than failing to close.
        if self.rules is not None:
            qty = self.rules.round_qty(abs(amt))
        else:
            qty = f"{abs(amt)}"
            log.warning("closing without symbol filters; using exchange qty %s", qty)
        # CLOSE FIRST, cancel second. Cancelling the protective orders before
        # the market order opens a window -- however brief -- where the
        # position is live with no stop, and if the close then fails the
        # account sits naked. reduceOnly can only shrink a position, so it
        # cannot conflict with a stop that fires concurrently. (QA R2)
        try:
            self._seq += 1
            self.api.order(symbol=symbol, side=side, type="MARKET",
                           quantity=qty, reduceOnly="true",
                           newClientOrderId=client_order_id("x", self._seq))
            log.info("flattened %s %s (%s)", side, qty, reason)
        except BinanceError as e:
            log.error("close failed: %s", e)
            self.notify.send(Event.ERROR,
                             f"CLOSE FAILED: {e}\nPosition is still open and its "
                             f"protective orders have NOT been touched.")
            return

        try:
            self.api.cancel_all(symbol)
        except BinanceError as e:
            log.error("close succeeded but cancelling leftovers failed: %s", e)
        self.notify.send(Event.SL_HIT,
                         f"{symbol} closed: {side} {qty} at market.\n{reason}")
        # release(symbol), NEVER `self.active = None`. The latter goes through
        # the property setter, which replaces the whole book -- so closing one
        # of twenty-five positions would drop tracking for the other
        # twenty-four while they were still open on the exchange.
        self.release(symbol)

    # ------------------------------------------------------------------ boot
    def startup(self) -> bool:
        log.info("mode=%s dry_run=%s realtime=%s symbol=%s strategy=%s",
                 self.cfg.mode, self.cfg.dry_run, self.cfg.realtime,
                 self.cfg.symbol, self.strategy.name)
        self.warn_if_foreground()
        log.info("alert channels: %s", ", ".join(self.notify.channels))

        for problem in self.cfg.validate():
            log.warning("config: %s", problem)

        self.api.sync_clock()
        info = self.api.exchange_info()
        self.rules = SymbolRules.from_exchange_info(info, self.cfg.symbol)
        self.last_price = float(self.api.mark_price(self.cfg.symbol)["markPrice"])
        log.info(self.rules.describe(self.last_price))

        if not self.cfg.api_key:
            log.error("no API credentials for mode=%s. Copy .env.example to .env.", self.cfg.mode)
            self.notify.send(Event.HALT, f"Refused to start: no API credentials "
                                         f"for mode={self.cfg.mode}.")
            return False

        snap = reconcile(self.api, self.cfg.symbol)
        self.equity = snap["equity"]

        ok, note = self.strategy.feasible(self.equity, self.last_price, self.rules)
        log.info("feasibility: %s", note or "ok")
        if not ok:
            log.error("strategy %s cannot run on this account: %s", self.strategy.name, note)
            self.notify.send(Event.HALT, f"Refused to start: strategy "
                                         f"{self.strategy.name} is not viable here.\n{note}")
            return False

        if self.state.strategy_override and hasattr(self.strategy, "set_override"):
            try:
                log.info("restoring saved override: %s",
                         self.strategy.set_override(self.state.strategy_override))
            except ValueError as e:
                log.warning("saved override %r is no longer valid (%s); "
                            "reverting to automatic", self.state.strategy_override, e)
                self.state.strategy_override = ""

        self.state.roll_day_if_needed(self.equity)
        self.schedule.start_date = self.state.schedule_start_date
        log.info("targets: %s", self.schedule.describe(self.equity))

        if self.state.halted:
            log.critical("halted from a previous run: %s", self.state.halt_reason)
            self.notify.send(Event.HALT, f"Refused to start -- still halted from a "
                                         f"previous run:\n{self.state.halt_reason}")
            return False

        if snap["position_amt"] != 0.0:
            log.warning("resuming with an existing position of %s; the bot did not "
                        "open it in this run, so it has no stop it knows about. "
                        "Close it by hand or restart flat.", snap["position_amt"])

        if not self.cfg.dry_run:
            self.api.set_margin_type(self.cfg.symbol, "ISOLATED")
            self.api.set_leverage(self.cfg.symbol, self.cfg.risk.max_leverage)

        # Warm the bar history once over REST; the stream keeps it current.
        klines = self.api.klines(self.cfg.symbol, self.cfg.interval,
                                 limit=self.strategy.warmup + 50)
        self.bars = [Bar.from_kline(k) for k in klines[:-1]]
        log.info("warmed %d bars of %s history", len(self.bars), self.cfg.interval)

        if self.cfg.portfolio.enabled:
            from .scanner import ScanConfig, Scanner
            self.scanner = Scanner(self.api, ScanConfig(**(self.cfg.universe or {})))
            log.info("portfolio mode ON: scanning up to %d symbols every %ds",
                     self.scanner.cfg.max_symbols, self.scanner.cfg.rescan_seconds)

        if self.cfg.telegram.control and self.cfg.telegram_token and self.cfg.telegram_chat_id:
            self.telegram = TelegramControl(self.cfg.telegram_token,
                                            self.cfg.telegram_chat_id)
            self.telegram.start()
            self.telegram.send(
                "Bot starting.\n"
                f"{self.cfg.symbol} · {self.cfg.mode}"
                + (" · dry run" if self.cfg.dry_run else "")
                + "\n\nSend /help for commands.")
            log.info("telegram control enabled for chat %s", self.cfg.telegram_chat_id)
        elif self.cfg.telegram_token and not self.cfg.telegram.control:
            # A warning, not info: alerts arriving while commands are ignored
            # looks like a broken bot rather than a config choice, and the one
            # channel that could explain it is the one that is switched off.
            log.warning("TELEGRAM COMMANDS ARE OFF. Alerts will arrive, but "
                        "/status, /close and /strategy will be IGNORED.")
            log.warning("  to enable, in %s:", self.cfg.config_path or "config.yaml")
            log.warning("      telegram:")
            log.warning("        control: true")
            log.warning("  then: sudo systemctl restart trading-bot")

        if self.cfg.dashboard.enabled:
            token = self.cfg.dashboard_token or generate_token()
            if not self.cfg.dashboard_token:
                log.warning("DASHBOARD_TOKEN not set in .env -- generated a temporary "
                            "one that changes on every restart")
            self.dashboard = Dashboard(token, self.cfg.dashboard.host,
                                       self.cfg.dashboard.port)
            url = self.dashboard.start()
            log.info("dashboard URL (contains the token, treat it as a password):")
            log.info("  %s", url)
            self.publish()

        if self.cfg.realtime:
            self.stream = MarketStream(self.cfg.symbol, self.cfg.interval,
                                       api=self.api if not self.cfg.dry_run else None,
                                       testnet=self.cfg.testnet)
            try:
                self.stream.start()
            except RuntimeError as e:
                # Missing websockets. Fail with the fix rather than a traceback,
                # and do NOT silently drop to polling -- a trading bot quietly
                # changing how it sees the market is a surprise you find later.
                self.stream = None
                log.error("%s", e)
                log.error("install it, then re-run:")
                log.error("    sudo apt-get install -y python3-websockets")
                log.error("  or, if that package is unavailable on this release:")
                log.error("    /usr/bin/python3 -m pip install --break-system-packages websockets")
                log.error("  or set `realtime: false` in config.yaml to use REST polling.")
                self.notify.send(Event.HALT,
                                 "Refused to start: the websockets package is missing.\n"
                                 "sudo apt-get install -y python3-websockets")
                return False

        step = self.schedule.escalates_today()
        if step:
            self.notify.send(Event.TARGET_RAISED,
                             f"Day {self.schedule.day_number()}: target is now "
                             f"${step.usd_per_day:.2f}/day.\n"
                             f"{self.schedule.describe(self.equity)}",
                             dedupe_key=f"raise:{self.state.day}")

        if self.telegram is not None:
            commands = "Commands are ON -- send /help."
        elif self.cfg.telegram_token:
            commands = ("Commands are OFF (telegram.control: false in config.yaml). "
                        "This bot will send alerts but ignore /status and /close.")
        else:
            commands = ""

        self.notify.send(Event.STARTUP,
                         f"{self.cfg.symbol} {self.strategy.name} "
                         f"({self.cfg.mode}{', dry-run' if self.cfg.dry_run else ''})\n"
                         f"equity ${self.equity:,.2f}\n"
                         f"{self.schedule.describe(self.equity)}"
                         + (f"\n\n{commands}" if commands else ""))

        # startup() has just reconciled, so start the periodic clock now.
        # Leaving it at 0 made the first periodic() fire on the very next loop
        # iteration, duplicating that reconcile a second or two later -- wasted
        # request weight on signed endpoints, on every single start.
        self._last_reconcile = time.time()
        return True

    # ------------------------------------------------------------------ loop
    def run(self) -> int:
        """Returns a process exit code: 0 clean stop, 1 refused to start."""
        if not self.startup():
            # startup() may have already opened the dashboard port and the
            # Telegram poller before hitting the problem. Release them, or the
            # next attempt collides with a port that is still held.
            self.stop_controllers()
            return 1
        try:
            while not self._stopping:
                try:
                    if self.stream is not None:
                        ev = self.stream.get(timeout=1.0)
                        if ev is not None:
                            self.handle(ev)
                    else:
                        self.poll_once()
                        time.sleep(self.cfg.poll_seconds)
                    self.process_commands()
                    self.publish()
                    self.periodic()
                except BinanceError as e:
                    log.error("exchange error: %s", e)
                    if e.code in (-1021, -1022):
                        self.api.sync_clock()
                except Exception:
                    log.exception("unhandled error -- continuing")
        except KeyboardInterrupt:
            self.shutdown(reason="interrupted")
            return 0
        # Left the loop because trigger_kill() set the flag: the exchange work
        # is already done, so shutdown only tears down threads.
        self.shutdown(reason=self._stop_reason or "stopped", exchange_done=True)
        return 0

    def trigger_kill(self, reason: str) -> None:
        """
        Act on the KILL file immediately and idempotently.

        `kill_action: flatten` closes the position at market FIRST, then
        cancels everything -- so the account is never left holding a position
        with its stop removed. `protect` leaves the position and its stop on
        the exchange untouched. Doing neither is the outcome to avoid. (QA R2)
        """
        if self._stopping:
            return
        self._stopping = True
        self._stop_reason = reason
        flatten = self.cfg.risk.kill_action == "flatten"
        log.critical("KILL: %s (action=%s)", reason, self.cfg.risk.kill_action)
        if not self.state.halted:
            self.state.halt(reason)

        self.notify.send(
            Event.HALT,
            f"KILL: {reason}\n\n"
            + ("Closing the position at market, then cancelling everything."
               if flatten else
               "Stopping. The position and its stop are LEFT ON THE EXCHANGE."),
            dedupe_key="kill")

        if self.cfg.dry_run:
            log.info("dry_run: kill sequence not sent to the exchange")
            return
        if flatten:
            self.close_all("kill switch")
        self.cancel_orders_safely(keep_protective=not flatten)

    @staticmethod
    def warn_if_foreground() -> bool:
        """
        Running from an interactive shell means the bot dies when the shell
        does -- closing an SSH session sends SIGHUP and the process goes with
        it. Easy to hit, and the symptom (a bot that was "running fine" and is
        now silent) points nowhere useful, so say it up front.
        """
        import os
        import sys

        if os.environ.get("INVOCATION_ID") or os.environ.get("JOURNAL_STREAM"):
            return False              # started by systemd; it survives logout
        if not sys.stdout.isatty():
            return False              # redirected or nohup'd; the user chose that
        log.warning("running in the foreground -- this process will STOP when "
                    "you close this shell or SSH session.")
        log.warning("  for anything longer than a quick look, install the service:")
        log.warning("      bash deploy/setup.sh && sudo systemctl start trading-bot")
        log.warning("  to keep this run alive after logout instead:")
        log.warning("      tmux new -s bot   (then Ctrl-b d to detach)")
        return True

    def stop_controllers(self) -> None:
        """Tear down anything startup() opened. Safe to call more than once."""
        for c in self.controllers:
            try:
                c.stop()
            except Exception as e:
                log.warning("failed to stop %s: %s", type(c).__name__, e)
        self.dashboard = None
        self.telegram = None
        if self.stream is not None:
            try:
                self.stream.stop()
            except Exception:
                pass
            self.stream = None

    def shutdown(self, reason: str = "", exchange_done: bool = False) -> None:
        """
        Exchange work happens FIRST, then the control surfaces are stopped.

        The old order stopped Telegram first, whose join could outlast
        systemd's TimeoutStopSec -- SIGKILL then landed before any order was
        cancelled, so `systemctl stop` silently left resting orders behind.
        (QA R3)
        """
        log.info("shutting down: %s", reason or "clean stop")
        if not self.cfg.dry_run and not exchange_done:
            self.cancel_orders_safely(keep_protective=True)
        for c in self.controllers:
            c.stop()
        if self.stream:
            self.stream.stop()
        self.state.save()
        self.notify.send(Event.STARTUP, f"bot stopped ({reason or 'clean stop'})")

    def cancel_orders_safely(self, keep_protective: bool = True) -> None:
        """
        Never remove a stop from a position that is still open.

        Flat account -> cancel everything, nothing is at risk. Position open
        and keep_protective -> pull only the resting entry, so the stop and
        take-profit stay on the book with no bot watching them. That is the
        whole point of exchange-side protective orders. (QA R1)
        """
        try:
            live = self.api.positions(self.cfg.symbol)
        except BinanceError as e:
            log.error("could not read position before cancelling: %s", e)
            return

        if not live:
            try:
                self.api.cancel_all(self.cfg.symbol)
                log.info("flat: all open orders cancelled")
            except BinanceError as e:
                log.error("cancel failed: %s", e)
            return

        if not keep_protective:
            try:
                self.api.cancel_all(self.cfg.symbol)
                log.info("all open orders cancelled")
            except BinanceError as e:
                log.error("cancel failed: %s", e)
            return

        if self.active is None:
            log.warning("position open but untracked -- leaving all orders in place")
            return
        try:
            self.api.cancel_order(self.cfg.symbol, self.active.entry_order_id)
            log.info("pulled resting entry %s; protective orders left on the book",
                     self.active.entry_order_id)
        except BinanceError as e:
            log.info("entry %s not cancellable (%s) -- likely already filled or gone",
                     self.active.entry_order_id, e)
        self.notify.send(Event.HALT,
                         "Bot stopping with a position still open.\n"
                         "The stop and take-profit are LEFT ON THE EXCHANGE so the "
                         "position stays protected while the bot is down.")

    # -------------------------------------------------------------- handlers
    def handle(self, ev) -> None:
        if isinstance(ev, Tick):
            self.on_tick(ev)
        elif isinstance(ev, BarClosed):
            self.on_bar(ev)
        elif isinstance(ev, OrderUpdate):
            self.on_order(ev)
        elif isinstance(ev, StreamStale):
            self.notify.send(Event.ERROR,
                             f"Market data went silent for {ev.seconds_silent:.0f}s. "
                             f"Reconnecting. No new trades while blind.",
                             dedupe_key=f"stale:{int(time.time() // 300)}")
        elif isinstance(ev, Disconnected):
            log.warning("stream disconnected: %s", ev.reason)

    def on_tick(self, tick: Tick) -> None:
        self.last_price = tick.mark_price
        if self.last_prices is None:
            self.last_prices = {}
        self.last_prices[tick.symbol] = tick.mark_price
        self._tick_guard()
        if self.cfg.dry_run:
            self.simulate_entry(tick.mark_price)

        # Route to the position for THIS symbol; a tick for a symbol we do not
        # hold is still useful for the price cache and nothing else.
        pos = self.book.get(tick.symbol)
        if pos is None:
            return
        threshold = self.cfg.alerts.approach_pct / 100.0
        price = tick.mark_price

        to_tp = pos.progress_to_tp(price)
        if to_tp >= threshold:
            self.notify.send(
                Event.APPROACH_TP,
                f"{to_tp*100:.0f}% of the way to take-profit.\n{pos.status_line(price)}",
                dedupe_key=f"tp:{pos.tag}")

        to_sl = pos.progress_to_stop(price)
        if to_sl >= threshold:
            self.notify.send(
                Event.APPROACH_SL,
                f"{to_sl*100:.0f}% of the way to the stop.\n{pos.status_line(price)}",
                dedupe_key=f"sl:{pos.tag}")

        if self.cfg.dry_run:
            self.simulate_exit(price, tick.symbol)

    def simulate_entry(self, price: float, symbol: str | None = None) -> None:
        """
        Fill resting dry-run entries only when price reaches them, and expire
        them on the same clock the live path uses. (QA R4)

        Keyed by symbol so a portfolio of resting entries resolves
        independently -- one filling must not disturb the others.
        """
        if not self._dry_pending:
            return
        symbols = [symbol] if symbol else list(self._dry_pending)
        minutes = self.cfg.risk.entry_expiry_minutes

        for sym in symbols:
            pending = self._dry_pending.get(sym)
            if pending is None:
                continue

            if minutes > 0 and self._entry_placed_at:
                age = (time.time() - self._entry_placed_at) / 60.0
                if age >= minutes:
                    del self._dry_pending[sym]
                    log.info("dry_run: %s entry expired unfilled after %.1f min",
                             sym, age)
                    self.notify.send(Event.DAILY_SUMMARY,
                                     f"DRY RUN -- {sym} entry expired unfilled "
                                     f"after {age:.0f} min. No trade.")
                    continue

            px = (self.last_prices or {}).get(sym, price)
            reached = px <= pending.entry if pending.is_long else px >= pending.entry
            if not reached:
                continue

            del self._dry_pending[sym]
            self.book[sym] = pending
            self.risk.record_fill()
            log.info("dry_run: %s entry filled at %.6f", sym, px)
            self.notify.send(Event.TRADE_OPEN,
                             f"DRY RUN -- {sym} entry filled at {px:,.4f}\n"
                             f"{pending.status_line(px)}")

    def simulate_exit(self, price: float, symbol: str | None = None) -> None:
        """
        Resolve a dry-run position against real tick prices, so stops, targets,
        P&L accounting and the daily-target logic all run end to end with
        nothing sent to the exchange. (QA F8)
        """
        pos = self.book.get(symbol) if symbol else self.active
        if pos is None:
            return
        hit_stop = price <= pos.stop if pos.is_long else price >= pos.stop
        hit_tp = bool(pos.take_profit) and (
            price >= pos.take_profit if pos.is_long else price <= pos.take_profit)
        if not (hit_stop or hit_tp):
            return

        exit_px = pos.stop if hit_stop else pos.take_profit
        gross = (exit_px - pos.entry) * pos.qty
        if not pos.is_long:
            gross = -gross
        fees = (pos.entry * pos.qty * 0.0002) + (exit_px * pos.qty * 0.0005)
        pnl = gross - fees

        self.state.realized_today += pnl
        self.risk.record_fill()
        self.equity += pnl
        event = Event.TP_HIT if pnl >= 0 else Event.SL_HIT
        prog = self.schedule.progress(self.state.realized_today)
        self.notify.send(event,
                         f"DRY RUN -- simulated close at {exit_px:,.4f} "
                         f"for {pnl:+.2f} USDT\n{prog}")
        self.release(pos.symbol)
        self._entry_placed_at = 0.0
        self.check_target_reached()

    def _tick_guard(self) -> bool:
        """Cheap safety checks that must not wait for a bar close. (QA F3)"""
        now = time.time()
        if now - self._last_guard < 2.0:
            return False
        self._last_guard = now
        return self.emergency_check()      # may set the stopping flag

    def on_bar(self, bar: BarClosed) -> None:
        self.bars.append(Bar(bar.open_time, bar.open, bar.high, bar.low, bar.close, bar.volume))
        self.bars = self.bars[-(self.strategy.warmup + 200):]
        self.decide()

    def on_order(self, upd: OrderUpdate) -> None:
        if upd.symbol != self.cfg.symbol:
            return
        log.info("order update %s %s %s rp=%.4f",
                 upd.client_order_id, upd.order_type, upd.status, upd.realized_pnl)

        # A partially filled entry means the real position is smaller than the
        # size we asked for. Track it rather than dropping the event. (QA F10)
        if upd.status == "PARTIALLY_FILLED":
            if self.active is not None and upd.client_order_id == self.active.entry_order_id:
                self.active.qty = upd.cumulative_qty or self.active.qty
                if upd.avg_price:
                    self.active.entry = upd.avg_price
                log.info("entry partially filled: %s of %s at %s",
                         upd.cumulative_qty, self.active.qty, upd.avg_price)
            return

        if upd.status != "FILLED":
            return

        # A stop or take-profit filling means the position is closed.
        closing = upd.order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET") or \
            (self.active is not None and upd.client_order_id == self.active.stop_order_id)

        if closing and self.active is not None:
            pnl = upd.realized_pnl
            self.state.realized_today += pnl
            self.state.save()
            event = Event.TP_HIT if pnl >= 0 else Event.SL_HIT
            prog = self.schedule.progress(self.state.realized_today)
            self.notify.send(
                event,
                f"closed at {upd.avg_price:,.4f} for {pnl:+.2f} USDT\n"
                f"{prog}\nequity ${self.equity:,.2f}")
            self.notify.clear_position_alerts(self.active.tag)
            self.active = None
            self.check_target_reached()

        elif self.active is not None and upd.client_order_id == self.active.entry_order_id:
            # Adopt the ACTUAL fill. Everything downstream -- unrealised P&L,
            # both progress bars, the 80% proximity thresholds -- was being
            # computed against the limit price we asked for rather than the
            # price we got. (QA F9)
            asked = self.active.entry          # capture BEFORE overwriting (QA R6)
            if upd.avg_price:
                self.active.entry = upd.avg_price
            if upd.cumulative_qty:
                self.active.qty = upd.cumulative_qty
            self.risk.record_fill()
            slip = (upd.avg_price - asked) if upd.avg_price else 0.0
            self.notify.send(
                Event.TRADE_OPEN,
                f"entry filled at {upd.avg_price:,.4f} (asked {asked:,.4f}, "
                f"slippage {slip:+.4f})\n"
                f"{self.active.status_line(upd.avg_price)}")

    # -------------------------------------------------------------- periodic
    def periodic(self) -> None:
        now = time.time()
        if now - self._last_reconcile < RECONCILE_SECONDS:
            return
        self._last_reconcile = now

        self.emergency_check()

        snap = reconcile(self.api, self.cfg.symbol)
        self.equity = snap["equity"]

        if self.state.roll_day_if_needed(self.equity):
            self.schedule.start_date = self.state.schedule_start_date
            step = self.schedule.escalates_today()
            if step:
                self.notify.send(Event.TARGET_RAISED,
                                 f"Day {self.schedule.day_number()}: target raised to "
                                 f"${step.usd_per_day:.2f}/day.\n"
                                 f"{self.schedule.describe(self.equity)}")
            self.notify.send(Event.DAILY_SUMMARY,
                             f"new day. equity ${self.equity:,.2f}\n"
                             f"{self.schedule.describe(self.equity)}")

        self.position_amt = snap["position_amt"]
        self.reconcile_position(snap)

        hb = self.cfg.alerts.heartbeat_minutes
        if hb and now - self._last_heartbeat > hb * 60:
            self._last_heartbeat = now
            prog = self.schedule.progress(self.state.realized_today)
            self.notify.send(Event.DAILY_SUMMARY,
                             f"{prog}\nequity ${self.equity:,.2f}  "
                             f"price {self.last_price:,.4f}\n"
                             f"position: {'yes' if self.active else 'flat'}  "
                             f"trades today {self.state.trades_today}")

    def reconcile_position(self, snap: dict) -> None:
        """
        Decide whether a tracked position is really gone.

        A flat `positionAmt` is NOT proof of that: entries are LIMIT GTC, so
        between placement and fill the exchange legitimately reports zero while
        our entry rests in the book. Clearing `active` there let the next bar
        open a SECOND entry on top of the first, doubling the position the risk
        layer approved. So we only forget a trade when the account is flat AND
        none of that trade's orders are still open. (QA F1)
        """
        if self.active is None or self.cfg.dry_run:
            return                      # dry-run positions are simulated locally
        if snap["position_amt"] != 0.0:
            self._entry_placed_at = self._entry_placed_at or time.time()
            return

        ours = {self.active.entry_order_id, self.active.stop_order_id,
                self.active.tp_order_id}
        still_open = ours & snap.get("open_order_ids", set())

        # A resting ENTRY is a live trade waiting to happen -- keep tracking it,
        # subject to its own expiry.
        if self.active.entry_order_id in still_open:
            self.expire_stale_entry(snap)
            return

        # Anything else still listed is a leftover STOP or TAKE_PROFIT. Both
        # carry closePosition:true, so with the account flat the trade is over
        # and the order is stale. Holding `active` here used to park the bot
        # forever: it believed it held a position it did not have and quietly
        # stopped trading, with no halt, no alert and nothing in the logs.
        # (QA R5)
        if still_open:
            log.warning("flat but protective order(s) %s still listed; "
                        "cancelling them and releasing tracking", sorted(still_open))
            if not self.cfg.dry_run:
                try:
                    self.api.cancel_all(self.cfg.symbol)
                except BinanceError as e:
                    log.error("could not cancel stale protective orders: %s", e)
            self.notify.send(
                Event.DAILY_SUMMARY,
                f"Account is flat but {len(still_open)} protective order(s) were "
                f"still listed. Cancelled them and released tracking, so the bot "
                f"does not sit idle believing it holds a position.")
            self.notify.clear_position_alerts(self.active.tag)
            self.active = None
            self._entry_placed_at = 0.0
            return

        log.info("position closed exchange-side and no orders remain; clearing")
        self.notify.clear_position_alerts(self.active.tag)
        self.active = None
        self._entry_placed_at = 0.0

    def expire_stale_entry(self, snap: dict) -> None:
        """
        An entry that never fills would otherwise hold a slot forever and keep
        the bot out of the market. Cancel it after entry_expiry_minutes.
        """
        if self.active is None or not self._entry_placed_at:
            return
        if self.active.entry_order_id not in snap.get("open_order_ids", set()):
            return
        minutes = self.cfg.risk.entry_expiry_minutes
        if minutes <= 0:
            return
        age = (time.time() - self._entry_placed_at) / 60.0
        if age < minutes:
            return
        log.info("entry %s unfilled after %.1f min -- cancelling",
                 self.active.entry_order_id, age)
        if not self.cfg.dry_run:
            try:
                self.api.cancel_all(self.cfg.symbol)
            except BinanceError as e:
                log.error("could not cancel stale entry: %s", e)
                return
        self.notify.send(Event.DAILY_SUMMARY,
                         f"Entry expired unfilled after {age:.0f} min; cancelled.")
        self.notify.clear_position_alerts(self.active.tag)
        self.active = None
        self._entry_placed_at = 0.0

    def emergency_check(self) -> bool:
        """
        Checked off the bar-close path, every tick and every reconcile. (QA F3)

        Returns True when NO NEW TRADES should open. It does not stop the
        process: halting and shutting down are different things.

        They used to be the same thing, and the consequence was severe --
        pressing Halt raised KeyboardInterrupt, which reached shutdown(), which
        cancelled every order including the protective stop, and exited. The
        button a worried user reaches for was the one that stripped their
        protection. Halting now keeps the loop alive so the position stays
        monitored and its stop stays on the book. (QA R1)

        Only the KILL file stops the process, and it does so through kill(),
        which never leaves a naked position. (QA R2)
        """
        if KILL_FILE.exists():
            self.trigger_kill(f"kill file present at {KILL_FILE.resolve()}")
            return True
        if self.state.halted:
            self.notify.send(Event.HALT,
                             f"{self.state.halt_reason}\n\n"
                             f"No new trades. Any open position and its stop are "
                             f"untouched and still monitored.",
                             dedupe_key=f"halt:{self.state.halt_reason[:40]}")
            return True
        return False

    def poll_once(self) -> None:
        """Fallback path when realtime is disabled."""
        klines = self.api.klines(self.cfg.symbol, self.cfg.interval,
                                 limit=self.strategy.warmup + 10)
        bars = [Bar.from_kline(k) for k in klines[:-1]]
        if bars and bars[-1].open_time != self.state.last_bar_open_ms:
            self.state.last_bar_open_ms = bars[-1].open_time
            self.bars = bars
            self.decide()

    # -------------------------------------------------------------- decision
    def check_target_reached(self) -> bool:
        prog = self.schedule.progress(self.state.realized_today)
        if prog.reached and not self.state.target_reached_today:
            self.state.target_reached_today = True
            self.state.save()
            self.notify.send(
                Event.TARGET_REACHED,
                f"Day {prog.day} target of ${prog.target:.2f} banked "
                f"(${prog.realized:+.2f}).\n"
                + ("Closing every open position and standing down for the day."
                   if prog.stop_trading else "Continuing to trade."))
            # Standing down is not enough once several positions are open: the
            # day's gain can still evaporate while they run. Flatten. (D3)
            if prog.stop_trading and self.book:
                self.close_all(f"daily target of ${prog.target:.2f} reached")
        return prog.stop_trading

    # ---------------------------------------------------------- portfolio
    def portfolio_cycle(self) -> int:
        """
        Scan, allocate, and fill whatever slots are free.

        Allocation is recomputed from the number of coins that qualified, so a
        day with one candidate sizes differently from a day with fifty. Every
        candidate then goes through the SAME per-trade sizing and portfolio
        gate as a single-symbol trade -- this layer decides how many and how
        big, never whether a position gets a stop.
        """
        pf = self.cfg.portfolio
        if not pf.enabled or self.scanner is None:
            return 0

        if self.scanner.due() or self.scanner.last is None:
            budget = (self.equity * self.cfg.risk.risk_per_trade_pct / 100
                      / pf.stop_distance)
            self.scanner.scan(risk_budget_notional=budget,
                              rules_for=self.rules_for)

        result = self.scanner.last
        if result is None or not result.ranked:
            return 0

        alloc = allocate(self.equity, len(result.ranked),
                         portfolio_risk_pct=pf.portfolio_risk_pct,
                         single_position_cap_pct=pf.single_position_cap_pct,
                         max_leverage=self.cfg.risk.max_leverage,
                         stop_distance=pf.stop_distance,
                         hard_cap=pf.hard_cap)
        pf.resolved_slots = alloc.slots
        pf.resolved_risk_pct = alloc.per_position_risk_pct
        if not alloc.slots:
            log.info("portfolio: %s", alloc)
            return 0

        free = alloc.slots - len(self.book)
        if free <= 0:
            return 0
        log.info("portfolio: %s | %d held, %d free", alloc, len(self.book), free)

        opened = 0
        for cand in result.ranked:
            if opened >= free:
                break
            if cand.symbol in self.book:
                continue
            signal = self.strategy.on_bars(cand.bars, 0.0)
            if signal is None:
                continue

            sized = self.risk.size_position(
                self.equity, signal.entry, signal.stop,
                self.rules_for(cand.symbol),
                risk_pct_override=alloc.per_position_risk_pct)
            if not sized:
                log.info("portfolio: %s rejected by risk -- %s",
                         cand.symbol, sized.reason)
                continue

            gate = self.risk.check_portfolio(self.book, self.equity,
                                             sized.qty_notional, cand.symbol, pf)
            if not gate:
                log.info("portfolio: %s refused -- %s", cand.symbol, gate.reason)
                continue

            self.place(signal, sized.qty_notional, sized.reason, symbol=cand.symbol)
            if cand.symbol in self.book and self.stream is not None:
                self.stream.add_symbol(cand.symbol)
            opened += 1

        if opened:
            log.info("portfolio: opened %d position(s), %d held",
                     opened, len(self.book))
        return opened

    def decide(self) -> None:
        if not self.bars:
            return
        gate = self.risk.preflight(self.equity)
        if not gate:
            log.info("no trading: %s", gate.reason)
            return

        if self.check_target_reached():
            log.info("daily target already met; standing down until tomorrow")
            return

        if self.cfg.portfolio.enabled:
            self.portfolio_cycle()
            return

        if self.active is not None:
            return

        signal = self.strategy.on_bars(self.bars, self.position_amt)
        if signal is None:
            log.info("bar close %.4f equity %.2f -- no signal",
                     self.bars[-1].close, self.equity)
            return

        # Checked against the position the EXCHANGE reports, not local tracking,
        # so the averaging-down block is enforced in production and not only in
        # its unit test. (QA F7)
        adds = self.risk.check_add_to_position(self.position_amt, signal.side)
        if not adds:
            log.info("signal ignored: %s", adds.reason)
            return

        sized = self.risk.size_position(self.equity, signal.entry, signal.stop, self.rules)
        if not sized:
            log.warning("signal rejected by risk: %s", sized.reason)
            return

        self.place(signal, sized.qty_notional, sized.reason)

    # ---------------------------------------------------------------- orders
    def place(self, signal, notional: float, risk_note: str,
              symbol: str | None = None) -> None:
        symbol = symbol or self.cfg.symbol
        rules = self.rules if symbol == self.cfg.symbol else self.rules_for(symbol)
        sized = rules.size_for_notional(notional, signal.entry)
        if sized is None:
            log.warning("order failed filter checks at $%.2f notional", notional)
            return
        qty, price = sized
        stop_price = rules.round_price(signal.stop)
        tp_price = rules.round_price(signal.take_profit) if signal.take_profit else "0"
        exit_side = "SELL" if signal.side == "BUY" else "BUY"

        log.info("SIGNAL %s %s %s @ %s stop %s tp %s | %s | %s",
                 signal.side, qty, symbol, price, stop_price, tp_price,
                 risk_note, signal.reason)

        if self.cfg.dry_run:
            # Track the position locally anyway. Returning early here meant
            # `active` stayed None, so proximity alerts never fired, the
            # dashboard panel never populated and the close button stayed
            # disabled -- i.e. dry run could not validate the things it exists
            # to validate. Nothing is sent to the exchange. (QA F8)
            self._seq += 1
            tag = f"dry-{self._seq}"
            # The entry RESTS until a tick trades through it, exactly as the
            # backtester was taught in F11. Booking an instant fill at the
            # asked price is the same optimism, in the mode the README tells
            # you to live in for days -- and on a breakout strategy the entries
            # that actually fill are disproportionately the reversals. (QA R4)
            self._dry_pending = self._dry_pending or {}
            self._dry_pending[symbol] = ActivePosition(
                symbol=symbol, side=signal.side, entry=float(price),
                stop=float(stop_price), take_profit=float(tp_price),
                qty=float(qty), entry_order_id=tag, tag=tag)
            self._entry_placed_at = time.time()
            self.risk.record_attempt()
            log.info("dry_run: entry resting at %s; waiting for the market to "
                     "trade through it", price)
            self.notify.send(Event.TRADE_OPEN,
                             f"DRY RUN -- limit entry resting (not filled):\n"
                             f"{signal.side} {qty} @ {price}\nSL {stop_price}  TP {tp_price}\n"
                             f"{risk_note}")
            return

        self._seq += 1
        entry_id = client_order_id("e", self._seq)
        stop_id = client_order_id("s", self._seq)
        tp_id = client_order_id("t", self._seq)

        entry = self.api.order(symbol=symbol, side=signal.side, type="LIMIT",
                               timeInForce="GTC", quantity=qty, price=price,
                               newClientOrderId=entry_id)
        log.info("entry placed %s status=%s", entry_id, entry.get("status"))

        try:
            self.api.order(symbol=symbol, side=exit_side, type="STOP_MARKET",
                           stopPrice=stop_price, closePosition="true",
                           workingType="MARK_PRICE", newClientOrderId=stop_id)
            if signal.take_profit:
                self.api.order(symbol=symbol, side=exit_side,
                               type="TAKE_PROFIT_MARKET", stopPrice=tp_price,
                               closePosition="true", workingType="MARK_PRICE",
                               newClientOrderId=tp_id)
        except BinanceError as e:
            log.critical("PROTECTIVE ORDER FAILED (%s) -- cancelling entry", e)
            self.api.cancel_all(symbol)
            self.state.halt(f"could not place protective stop on {symbol}: {e}")
            self.notify.send(Event.HALT, f"Could not place a stop: {e}\n"
                                         "Entry cancelled, bot halted. Nothing is open.")
            return

        self.book[symbol] = ActivePosition(
            symbol=symbol, side=signal.side, entry=float(price),
            stop=float(stop_price), take_profit=float(tp_price), qty=float(qty),
            entry_order_id=entry_id, stop_order_id=stop_id, tp_order_id=tp_id,
            tag=entry_id)
        self.state.entry_order_id = entry_id
        self.state.stop_order_id = stop_id
        self.risk.record_attempt()
        self._entry_placed_at = time.time()

        prog = self.schedule.progress(self.state.realized_today)
        self.notify.send(
            Event.TRADE_OPEN,
            f"{signal.side} {qty} {symbol} @ {price}\n"
            f"SL {stop_price}   TP {tp_price}\n{risk_note}\n"
            f"{signal.reason}\n{prog}")
