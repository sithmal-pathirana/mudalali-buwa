"""
Daily profit targets, and the escalation schedule.

What this module can do:
  * compute today's target ($2/day, rising to $3/day after day 15)
  * STOP TRADING once the target is banked, which protects the day's gain
  * report progress toward the target for alerts

What no module can do: make the target arrive. A target is a stopping rule,
not a guarantee -- it can cap a good day, it cannot manufacture one. Days that
finish below target, and days that finish red, are a normal part of the
distribution and the risk layer is what bounds them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

log = logging.getLogger("targets")


def seconds_to_day_end(now: datetime | None = None) -> float:
    """
    Seconds until the trading day rolls.

    The day boundary is UTC midnight, because that is where
    State.roll_day_if_needed puts it -- that is the moment realized_today,
    trades_today and the daily loss limit all reset, so it is the deadline the
    daily target is actually running against.
    """
    now = now or datetime.now(timezone.utc)
    end = (datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
           + timedelta(days=1))
    return max(0.0, (end - now).total_seconds())


def format_duration(seconds: float) -> str:
    """Compact enough for one status line: "4h 12m", "47m", "under a minute"."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m"
    return "under a minute"


@dataclass
class TargetStep:
    from_day: int          # 1-based day of operation this step begins on
    usd_per_day: float


@dataclass
class TargetSchedule:
    steps: list[TargetStep]
    stop_when_reached: bool = True
    start_date: str = ""             # ISO date of day 1; set on first run

    @classmethod
    def from_config(cls, raw: dict) -> "TargetSchedule":
        steps = [TargetStep(int(s["from_day"]), float(s["usd_per_day"]))
                 for s in raw.get("schedule", [{"from_day": 1, "usd_per_day": 2.0}])]
        steps.sort(key=lambda s: s.from_day)
        if steps[0].from_day != 1:
            raise ValueError("target schedule must start at from_day: 1")
        return cls(steps=steps, stop_when_reached=bool(raw.get("stop_when_reached", True)))

    # ----------------------------------------------------------------- days
    def day_number(self, today: date | None = None) -> int:
        today = today or datetime.now(timezone.utc).date()
        if not self.start_date:
            return 1
        started = date.fromisoformat(self.start_date)
        return (today - started).days + 1

    def target_for(self, day: int) -> float:
        current = self.steps[0].usd_per_day
        for step in self.steps:
            if day >= step.from_day:
                current = step.usd_per_day
            else:
                break
        return current

    def today_target(self, today: date | None = None) -> float:
        return self.target_for(self.day_number(today))

    def escalates_today(self, today: date | None = None) -> TargetStep | None:
        day = self.day_number(today)
        for step in self.steps:
            if step.from_day == day and step.from_day != 1:
                return step
        return None

    # ------------------------------------------------------------- progress
    def progress(self, realized_today: float, today: date | None = None,
                 equity: float = 0.0,
                 since_restart: float | None = None) -> "Progress":
        """`equity` is the day's opening equity and `since_restart` the net
        realised P&L since the process started; both only change how the
        progress READS, never what it decides."""
        day = self.day_number(today)
        target = self.target_for(day)
        return Progress(day=day, target=target, realized=realized_today,
                        reached=realized_today >= target,
                        stop_trading=self.stop_when_reached and realized_today >= target,
                        enforced=self.stop_when_reached, equity=equity,
                        since_restart=since_restart)

    def describe(self, equity: float) -> str:
        day = self.day_number()
        if not self.stop_when_reached:
            # With stop_when_reached off the dollar target decides nothing, and
            # quoting "$2.00/day required" read as a rule the bot was following.
            return (f"day {day}: daily target off (targets.stop_when_reached "
                    f"false); P&L is reported against ${equity:,.2f} equity")
        target = self.target_for(day)
        pct = target / equity * 100 if equity > 0 else float("inf")
        nxt = next((s for s in self.steps if s.from_day > day), None)
        line = (f"day {day}: target ${target:.2f}/day on ${equity:,.2f} equity "
                f"= {pct:.2f}%/day required")
        if nxt:
            line += (f"; rises to ${nxt.usd_per_day:.2f}/day on day {nxt.from_day} "
                     f"(~{nxt.usd_per_day / (equity + target * (nxt.from_day - day)) * 100:.2f}%/day "
                     f"if every day hits target)")
        return line


@dataclass
class Progress:
    day: int
    target: float
    realized: float
    reached: bool
    stop_trading: bool
    #: False when targets.stop_when_reached is off: the dollar target decides
    #: nothing, so the P&L is read against equity instead of against it.
    enforced: bool = True
    #: The day's opening equity, for the percent reading. 0 = unknown.
    equity: float = 0.0
    #: Net realised P&L since the process last started; None = not tracked.
    since_restart: float | None = None

    @property
    def equity_pct(self) -> float:
        return self.realized / self.equity * 100 if self.equity > 0 else 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.target - self.realized)

    @property
    def pct(self) -> float:
        return (self.realized / self.target * 100) if self.target else 0.0

    def bar(self, width: int = 20) -> str:
        filled = max(0, min(width, int(self.pct / 100 * width)))
        return "#" * filled + "." * (width - filled)

    def __str__(self) -> str:
        if self.enforced:
            line = (f"day {self.day}  [{self.bar()}]  "
                    f"${self.realized:+.2f} / ${self.target:.2f} ({self.pct:.0f}%)")
        elif self.equity > 0:
            line = (f"day {self.day}  today {self.realized:+.2f} USDT "
                    f"({self.equity_pct:+.2f}% of ${self.equity:,.2f})")
        else:
            line = f"day {self.day}  today {self.realized:+.2f} USDT"
        if self.since_restart is not None:
            line += f"\nsince restart {self.since_restart:+.2f} USDT"
        return line
