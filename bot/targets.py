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
from datetime import date, datetime, timezone

log = logging.getLogger("targets")


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
    def progress(self, realized_today: float, today: date | None = None) -> "Progress":
        day = self.day_number(today)
        target = self.target_for(day)
        return Progress(day=day, target=target, realized=realized_today,
                        reached=realized_today >= target,
                        stop_trading=self.stop_when_reached and realized_today >= target)

    def describe(self, equity: float) -> str:
        day = self.day_number()
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
        return (f"day {self.day}  [{self.bar()}]  "
                f"${self.realized:+.2f} / ${self.target:.2f} ({self.pct:.0f}%)")
