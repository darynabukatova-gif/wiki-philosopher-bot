"""Pure calendar policy helpers for exact recent-death dates."""

import calendar
from datetime import date, datetime, timedelta


LEAP_DAY_POLICY = "clamp-to-last-day-of-month"


def _positive_integer(value, name):
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("{} must be a positive integer".format(name))
    return value


def _calendar_date(value, name):
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError("{} must be a date".format(name))
    return value


def calendar_anniversary(value, year):
    """Return ``value``'s anniversary in ``year``, clamped at month end."""
    value = _calendar_date(value, "value")
    if (
        not isinstance(year, int)
        or isinstance(year, bool)
        or not 1 <= year <= 9999
    ):
        raise ValueError("year must be an integer from 1 through 9999")
    target_day = min(
        value.day,
        calendar.monthrange(year, value.month)[1],
    )
    return date(year, value.month, target_day)


def completed_calendar_years(start, end):
    """Return completed clamped calendar anniversaries from start through end."""
    start = _calendar_date(start, "start")
    end = _calendar_date(end, "end")
    if start > end:
        raise ValueError("start must not be after end")
    years = end.year - start.year
    if end < calendar_anniversary(start, end.year):
        years -= 1
    return years


def calendar_date_years_ago(run_date, years):
    """Return ``years`` before ``run_date``, clamping invalid month-end days."""
    run_date = _calendar_date(run_date, "run_date")
    years = _positive_integer(years, "years")
    return calendar_anniversary(run_date, run_date.year - years)


def recent_death_interval(run_date, years):
    """Return the inclusive calendar-year interval ending on ``run_date``."""
    run_date = _calendar_date(run_date, "run_date")
    return calendar_date_years_ago(run_date, years), run_date


def date_is_within_interval(value, start, end):
    """Whether an ISO date/date value lies in the inclusive interval."""
    start = _calendar_date(start, "start")
    end = _calendar_date(end, "end")
    if start > end:
        raise ValueError("start must not be after end")
    if isinstance(value, str):
        try:
            value = date.fromisoformat(value)
        except ValueError:
            return False
    if not isinstance(value, date) or isinstance(value, datetime):
        return False
    return start <= value <= end


def recent_death_policy(run_date, *, years=None, days=None, default_years=7):
    """Build a structured inclusive recent-death policy."""
    run_date = _calendar_date(run_date, "run_date")
    if years is not None and days is not None:
        raise ValueError("years and days are mutually exclusive")
    if days is not None:
        days = _positive_integer(days, "days")
        start = run_date - timedelta(days=days)
        return {
            "kind": "fixed-days",
            "value": days,
            "run_date": run_date.isoformat(),
            "inclusive_from": start.isoformat(),
            "inclusive_to": run_date.isoformat(),
        }
    years = default_years if years is None else years
    years = _positive_integer(years, "years")
    start, end = recent_death_interval(run_date, years)
    return {
        "kind": "calendar-years",
        "value": years,
        "run_date": run_date.isoformat(),
        "inclusive_from": start.isoformat(),
        "inclusive_to": end.isoformat(),
        "leap_day_policy": LEAP_DAY_POLICY,
    }


def date_is_within_recent_policy(value, policy):
    """Evaluate a date against a structured policy from ``recent_death_policy``."""
    return date_is_within_interval(
        value,
        date.fromisoformat(policy["inclusive_from"]),
        date.fromisoformat(policy["inclusive_to"]),
    )
