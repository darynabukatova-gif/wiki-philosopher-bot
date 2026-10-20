"""Calendar-month interval resolution for presentation-oriented reports."""

from calendar import monthrange
from datetime import date, datetime
try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python 3.8 compatibility
    from dateutil.tz import gettz as ZoneInfo


PRESET_MONTHS = {
    "1_year": 12,
    "3_years": 36,
    "5_years": 60,
    "7_years": 84,
    "10_years": 120,
}


def effective_dublin_date(now=None):
    """Return today's date in Europe/Dublin, with an injectable clock."""
    if now is None:
        return datetime.now(ZoneInfo("Europe/Dublin")).date()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            return now.date()
        return now.astimezone(ZoneInfo("Europe/Dublin")).date()
    if isinstance(now, date):
        return now
    raise ValueError("now must be a date or datetime")


def parse_year_month(value, field="month"):
    if not isinstance(value, str) or len(value) != 7 or value[4] != "-":
        raise ValueError("{} must use YYYY-MM format".format(field))
    try:
        year, month = int(value[:4]), int(value[5:])
        if year <= 0 or not 1 <= month <= 12:
            raise ValueError
    except ValueError as error:
        raise ValueError("{} must use a valid YYYY-MM month".format(field)) from error
    return year, month


def month_start(year, month):
    return date(year, month, 1)


def month_end(year, month):
    return date(year, month, monthrange(year, month)[1])


def shift_month(year, month, offset):
    value = year * 12 + (month - 1) + offset
    if value < 12:
        raise ValueError("resulting month must have a positive year")
    return value // 12, value % 12 + 1


def completed_calendar_months(start, end):
    """Count complete calendar months wholly contained in an inclusive interval.

    This supports presentation decisions for arbitrary-date reports without
    treating a partial first or final month as a complete observation period.
    """
    if not isinstance(start, date) or isinstance(start, datetime):
        raise TypeError("start must be a date")
    if not isinstance(end, date) or isinstance(end, datetime):
        raise TypeError("end must be a date")
    if start > end:
        raise ValueError("start must not be after end")

    first_year, first_month = start.year, start.month
    if start.day != 1:
        first_year, first_month = shift_month(first_year, first_month, 1)

    last_year, last_month = end.year, end.month
    if end.day != month_end(last_year, last_month).day:
        last_year, last_month = shift_month(last_year, last_month, -1)

    if (first_year, first_month) > (last_year, last_month):
        return 0
    return (last_year - first_year) * 12 + last_month - first_month + 1


def _completed_month(year, month, current):
    return (year, month) < (current.year, current.month)


def resolve_month_interval(*, mode="preset", preset="1_year", end_month="",
                           from_month="", to_month="", calendar_year="", now=None):
    """Resolve workflow presentation inputs to an inclusive exact-date interval."""
    current = effective_dublin_date(now)
    if mode not in ("preset", "custom"):
        raise ValueError("interval_mode must be preset or custom")

    if mode == "custom":
        if not from_month or not to_month:
            raise ValueError("custom mode requires both from_month and to_month")
        if calendar_year:
            raise ValueError("calendar_year is only valid with preset=calendar_year")
        start_year, start_month = parse_year_month(from_month, "from_month")
        end_year, end_month_value = parse_year_month(to_month, "to_month")
        if (start_year, start_month) > (end_year, end_month_value):
            raise ValueError("from_month must not be after to_month")
        if not _completed_month(end_year, end_month_value, current):
            raise ValueError("to_month must be a completed calendar month")
        return {
            "mode": "custom",
            "preset": None,
            "from_month": "{:04d}-{:02d}".format(start_year, start_month),
            "to_month": "{:04d}-{:02d}".format(end_year, end_month_value),
            "from": month_start(start_year, start_month).isoformat(),
            "to": month_end(end_year, end_month_value).isoformat(),
        }

    if from_month:
        raise ValueError("from_month is only valid in custom mode")
    if preset == "calendar_year":
        if end_month:
            raise ValueError("end_month is not used with preset=calendar_year")
        if calendar_year:
            if len(calendar_year) != 4 or not calendar_year.isdigit():
                raise ValueError("calendar_year must be a positive four-digit year")
            year = int(calendar_year)
            if year <= 0:
                raise ValueError("calendar_year must be a positive four-digit year")
        else:
            year = current.year - 1
        if year >= current.year:
            raise ValueError("calendar_year must be a completed calendar year")
        return {
            "mode": "preset",
            "preset": "calendar_year",
            "calendar_year": str(year),
            "from": date(year, 1, 1).isoformat(),
            "to": date(year, 12, 31).isoformat(),
        }

    if preset not in PRESET_MONTHS:
        raise ValueError("unsupported preset")
    if calendar_year:
        raise ValueError("calendar_year is only valid with preset=calendar_year")
    if end_month:
        end_year, end_month_value = parse_year_month(end_month, "end_month")
        if not _completed_month(end_year, end_month_value, current):
            raise ValueError("end_month must be a completed calendar month")
    else:
        end_year, end_month_value = shift_month(current.year, current.month, -1)
    start_year, start_month = shift_month(
        end_year, end_month_value, -(PRESET_MONTHS[preset] - 1),
    )
    return {
        "mode": "preset",
        "preset": preset,
        "end_month": "{:04d}-{:02d}".format(end_year, end_month_value),
        "from": month_start(start_year, start_month).isoformat(),
        "to": month_end(end_year, end_month_value).isoformat(),
    }
