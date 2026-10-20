"""Shared presentation formatting for historical death reports."""

import calendar
from datetime import date


def _interval_dates(start, end):
    """Parse and validate an inclusive ISO interval for presentation."""
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except (TypeError, ValueError) as error:
        raise ValueError("interval bounds must be ISO dates") from error
    if start_date > end_date:
        raise ValueError("interval start must not be after interval end")
    return start_date, end_date


def _complete_month(start_date, end_date):
    return (
        start_date.day == 1
        and end_date.year == start_date.year
        and end_date.month == start_date.month
        and end_date.day == calendar.monthrange(end_date.year, end_date.month)[1]
    )


def _complete_month_range(start_date, end_date):
    return (
        start_date.day == 1
        and end_date.day == calendar.monthrange(end_date.year, end_date.month)[1]
    )


def format_report_interval_long(start, end):
    """Return a reader-friendly interval label for summaries and captions.

    Exact dates remain the fallback for partial-month intervals. Complete
    calendar years and complete month ranges use human-readable month names.
    """
    start_date, end_date = _interval_dates(start, end)
    if (
        start_date.month == 1 and start_date.day == 1
        and end_date.month == 12 and end_date.day == 31
        and start_date.year == end_date.year
    ):
        return str(start_date.year)
    if _complete_month_range(start_date, end_date):
        if _complete_month(start_date, end_date):
            return start_date.strftime("%B %Y")
        return "{} – {}".format(
            start_date.strftime("%B %Y"), end_date.strftime("%B %Y")
        )
    return "{} through {} (inclusive)".format(start, end)


def format_report_interval_compact(start, end):
    """Return a compact chart/table subtitle for an inclusive interval."""
    start_date, end_date = _interval_dates(start, end)
    if (
        start_date.month == 1 and start_date.day == 1
        and end_date.month == 12 and end_date.day == 31
        and start_date.year == end_date.year
    ):
        return str(start_date.year)
    if _complete_month_range(start_date, end_date):
        if _complete_month(start_date, end_date):
            return start_date.strftime("%b %Y")
        return "{} – {}".format(
            start_date.strftime("%b %Y"), end_date.strftime("%b %Y")
        )
    return "{} through {} (inclusive)".format(start, end)


def compact_percentage(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    rounded = round(float(value), 1)
    return "{}%".format(
        int(rounded) if rounded.is_integer() else "{:.1f}".format(rounded)
    )


def mean_age(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    return "{:.1f}".format(float(value))


def median_age(value, *, missing="n/a"):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return missing
    return "{:g}".format(value)
