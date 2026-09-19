"""Boundary tests for the hand-written five-field cron parser (Issue #20,
docs/design/04-w5-interfaces.md §2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jones_daemon.scheduler.cron_expr import CronExprError, next_after, parse


def _dt(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=UTC)


# -- parse(): valid syntax --------------------------------------------------------


def test_parse_every_minute():
    s = parse("* * * * *")
    assert s.minutes == frozenset(range(60))
    assert s.hours == frozenset(range(24))
    assert s.days_of_month == frozenset(range(1, 32))
    assert s.months == frozenset(range(1, 13))
    assert s.days_of_week == frozenset(range(7))
    assert s.dom_is_star and s.dow_is_star


def test_parse_single_values():
    s = parse("5 9 15 6 3")
    assert s.minutes == frozenset({5})
    assert s.hours == frozenset({9})
    assert s.days_of_month == frozenset({15})
    assert s.months == frozenset({6})
    assert s.days_of_week == frozenset({3})


def test_parse_list():
    s = parse("0,15,30,45 * * * *")
    assert s.minutes == frozenset({0, 15, 30, 45})


def test_parse_range():
    s = parse("0 9-17 * * *")
    assert s.hours == frozenset(range(9, 18))


def test_parse_step():
    s = parse("*/15 * * * *")
    assert s.minutes == frozenset({0, 15, 30, 45})


def test_parse_range_with_step():
    s = parse("0 0-10/2 * * *")
    assert s.hours == frozenset({0, 2, 4, 6, 8, 10})


def test_parse_combined_list_range_step():
    s = parse("1,5,10-14/2 * * * *")
    assert s.minutes == frozenset({1, 5, 10, 12, 14})


def test_parse_day_of_week_sunday_alias_normalizes_to_zero():
    s = parse("0 0 * * 7")
    assert s.days_of_week == frozenset({0})


# -- parse(): invalid syntax -------------------------------------------------------


@pytest.mark.parametrize(
    "expr",
    [
        "* * * *",  # only 4 fields
        "* * * * * *",  # 6 fields
        "60 * * * *",  # minute out of range
        "* 24 * * *",  # hour out of range
        "* * 32 * *",  # day-of-month out of range
        "* * 0 * *",  # day-of-month below range (1-31)
        "* * * 13 *",  # month out of range
        "* * * 0 *",  # month below range (1-12)
        "* * * * 8",  # day-of-week out of range
        "a * * * *",  # non-numeric
        "*/0 * * * *",  # zero step
        "*/-1 * * * *",  # negative step
        "5-1 * * * *",  # inverted range
        ", * * * *",  # empty component
    ],
)
def test_parse_rejects_invalid_expressions(expr):
    with pytest.raises(CronExprError):
        parse(expr)


# -- next_after(): boundary behavior -----------------------------------------------


def test_next_after_every_minute_advances_exactly_one_minute():
    s = parse("* * * * *")
    assert next_after(s, _dt(2026, 1, 1, 10, 0)) == _dt(2026, 1, 1, 10, 1)


def test_next_after_is_strictly_after_not_equal():
    """Standing exactly on a matching minute must not return that same minute —
    "next" means strictly in the future, or a Cron would refire on every daemon
    restart if `next_run_at` happened to equal `now()` to the second."""
    s = parse("0,30 * * * *")
    assert next_after(s, _dt(2026, 1, 1, 10, 30)) == _dt(2026, 1, 1, 11, 0)


def test_next_after_skips_to_next_hour():
    s = parse("5 * * * *")
    assert next_after(s, _dt(2026, 1, 1, 10, 6)) == _dt(2026, 1, 1, 11, 5)


def test_next_after_crosses_day_boundary():
    s = parse("0 0 * * *")
    assert next_after(s, _dt(2026, 1, 1, 23, 59)) == _dt(2026, 1, 2, 0, 0)


def test_next_after_crosses_month_boundary():
    s = parse("0 0 1 * *")
    assert next_after(s, _dt(2026, 1, 15, 0, 0)) == _dt(2026, 2, 1, 0, 0)


def test_next_after_crosses_year_boundary():
    s = parse("0 0 1 1 *")
    assert next_after(s, _dt(2026, 6, 1, 0, 0)) == _dt(2027, 1, 1, 0, 0)


def test_next_after_respects_month_restriction():
    s = parse("0 0 1 6 *")  # June 1st only
    assert next_after(s, _dt(2026, 1, 1, 0, 0)) == _dt(2026, 6, 1, 0, 0)


def test_next_after_dom_and_dow_are_ored_when_both_restricted():
    """Standard crontab(5) semantics: day-of-month=1 OR day-of-week=Monday, not AND
    — 2026-01-01 is a Thursday, so the 1st still matches even though it isn't a
    Monday, and the following Monday (Jan 5th) also matches even though it isn't
    the 1st."""
    s = parse("0 0 1 * 1")
    first = next_after(s, _dt(2025, 12, 31, 0, 0))
    assert first == _dt(2026, 1, 1, 0, 0)
    second = next_after(s, first)
    assert second == _dt(2026, 1, 5, 0, 0)  # next Monday


def test_next_after_leap_day():
    s = parse("0 0 29 2 *")
    assert next_after(s, _dt(2027, 1, 1, 0, 0)) == _dt(2028, 2, 29, 0, 0)


def test_next_after_business_hours_weekdays():
    s = parse("0 9-17 * * 1-5")
    # Friday evening -> next occurrence is Monday 09:00, not Saturday/Sunday.
    friday_evening = _dt(2026, 1, 2, 18, 0)  # 2026-01-02 is a Friday
    assert next_after(s, friday_evening) == _dt(2026, 1, 5, 9, 0)


def test_next_after_ignores_seconds_and_microseconds_in_input():
    s = parse("* * * * *")
    dt = datetime(2026, 1, 1, 10, 0, 30, 500000, tzinfo=UTC)
    assert next_after(s, dt) == _dt(2026, 1, 1, 10, 1)


def test_next_after_impossible_expression_raises_instead_of_hanging():
    # Feb 31st never exists.
    s = parse("0 0 31 2 *")
    with pytest.raises(CronExprError):
        next_after(s, _dt(2026, 1, 1, 0, 0))
