"""Standard five-field cron expression parsing + "next occurrence" (Issue #20,
docs/design/04-w5-interfaces.md §2: "五段 cron 解析 + next_after(dt)（自己写，
≤150 行，有边界测试；不引第三方）").

Field order: `minute hour day-of-month month day-of-week` (no seconds, no year —
the same fields `crontab(5)` uses). Supported syntax per field: `*`, a single
value, a range `a-b`, a step `*/n` or `a-b/n`, and comma-separated lists combining
any of those (`1,5,10-20/2`).

`day-of-week` follows `crontab(5)`: `0`-`6` with `0` = Sunday (`7` also accepted as
a Sunday alias, both normalized to `0`). When both day-of-month and day-of-week are
restricted (neither is a bare `*`), a date matches if *either* one does — standard
cron semantics, not an AND.

Deliberately minute-resolution; PRD 12.3's Cron acceptance is "分钟级精度", not
sub-minute.

Timezone: `next_after` itself takes no position on timezone — it matches fields
against whatever `datetime` (aware or naive, any tzinfo) the caller passes in, and
returns a result carrying that same tzinfo. `crons.next_run_at` is stored as UTC
ISO-8601 text (00-foundation.md §4.1) regardless, but *storage format* and *which
timezone a user's expression is interpreted in* are two different questions — an
earlier revision of this docstring conflated them (round-1 review #4). The actual
interpretation decision (fields mean the local system timezone — "0 9 * * *" is
this machine's 9am, the intuitive reading for a single-user desktop scheduler, not
UTC 9am) lives in `scheduler/service.py::_next_after_local`, the one place that
converts UTC<->local around calls into this module; see that function's docstring
and 04-w5-interfaces.md §2 for the recorded decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

# Bounded search horizon for `next_after`: a schedule that can never match (e.g.
# "0 0 31 2 *" — Feb 31st never exists) must fail loudly instead of hanging the
# scheduler forever (DEV.md 工程原则 #4: 诚实失败). Four years comfortably covers
# every leap-year/day-of-week combination a valid expression could need.
_MAX_DAYS_SEARCHED = 4 * 366 + 10


class CronExprError(ValueError):
    """Raised for a syntactically or semantically invalid cron expression."""


@dataclass(frozen=True)
class CronSchedule:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]  # normalized: 0=Sunday .. 6=Saturday
    dom_is_star: bool
    dow_is_star: bool


def _parse_field(spec: str, name: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise CronExprError(f"{name}: empty component in {spec!r}")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) <= 0:
                raise CronExprError(f"{name}: invalid step {step_text!r} in {part!r}")
            step = int(step_text)
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            a, b = base.split("-", 1)
            if not (_is_int(a) and _is_int(b)):
                raise CronExprError(f"{name}: invalid range {base!r}")
            start, end = int(a), int(b)
        elif _is_int(base):
            start = end = int(base)
        else:
            raise CronExprError(f"{name}: invalid value {base!r}")
        if start < lo or end > hi or start > end:
            raise CronExprError(f"{name}: {base!r} out of range [{lo},{hi}]")
        values.update(range(start, end + 1, step))
    if not values:
        raise CronExprError(f"{name}: no values parsed from {spec!r}")
    return values


def _is_int(text: str) -> bool:
    return text.lstrip("-").isdigit()


def parse(expr: str) -> CronSchedule:
    fields = expr.split()
    if len(fields) != 5:
        raise CronExprError(
            f"expected 5 space-separated fields (minute hour dom month dow), "
            f"got {len(fields)}: {expr!r}"
        )
    minute_s, hour_s, dom_s, month_s, dow_s = fields
    minutes = _parse_field(minute_s, "minute", 0, 59)
    hours = _parse_field(hour_s, "hour", 0, 23)
    days_of_month = _parse_field(dom_s, "day_of_month", 1, 31)
    months = _parse_field(month_s, "month", 1, 12)
    days_of_week = {v % 7 for v in _parse_field(dow_s, "day_of_week", 0, 7)}
    return CronSchedule(
        minutes=frozenset(minutes),
        hours=frozenset(hours),
        days_of_month=frozenset(days_of_month),
        months=frozenset(months),
        days_of_week=frozenset(days_of_week),
        dom_is_star=dom_s.strip() == "*",
        dow_is_star=dow_s.strip() == "*",
    )


def _date_matches(schedule: CronSchedule, d: datetime) -> bool:
    if d.month not in schedule.months:
        return False
    dom_ok = d.day in schedule.days_of_month
    cron_dow = d.isoweekday() % 7  # Mon=1..Sat=6, Sun=0 (crontab numbering)
    dow_ok = cron_dow in schedule.days_of_week
    if schedule.dom_is_star and schedule.dow_is_star:
        return True
    if schedule.dom_is_star:
        return dow_ok
    if schedule.dow_is_star:
        return dom_ok
    return dom_ok or dow_ok


def next_after(schedule: CronSchedule, dt: datetime) -> datetime:
    """The earliest minute-aligned instant strictly after `dt` that matches
    `schedule`. `dt` may carry any tzinfo (including none); the result carries the
    same tzinfo, truncated to minute resolution.

    Raises `CronExprError` if nothing matches within `_MAX_DAYS_SEARCHED` days —
    an honest failure for an expression that can structurally never fire (e.g. day
    31 in a month that never has one), not a silent hang or a wrong-but-plausible
    guess (DEV.md 工程原则 #4).
    """
    start = dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    candidate_day = start.replace(hour=0, minute=0)
    for _ in range(_MAX_DAYS_SEARCHED):
        if _date_matches(schedule, candidate_day):
            same_day = candidate_day.date() == start.date()
            floor_hour = start.hour if same_day else 0
            floor_minute = start.minute if same_day else 0
            for hour in sorted(schedule.hours):
                if hour < floor_hour:
                    continue
                minute_floor = floor_minute if hour == floor_hour else 0
                for minute in sorted(schedule.minutes):
                    if minute < minute_floor:
                        continue
                    return candidate_day.replace(hour=hour, minute=minute)
        candidate_day = candidate_day + timedelta(days=1)
    raise CronExprError(f"cron expression never matches within {_MAX_DAYS_SEARCHED} days")
