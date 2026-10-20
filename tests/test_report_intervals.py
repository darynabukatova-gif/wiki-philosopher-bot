from datetime import date

import pytest

from wiki_philosopher_bot.report_intervals import (
    completed_calendar_months,
    resolve_month_interval,
)


RUN_DATE = date(2026, 10, 20)


@pytest.mark.parametrize(
    ("preset", "start"),
    [
        ("1_year", "2025-10-01"),
        ("3_years", "2023-10-01"),
        ("5_years", "2021-10-01"),
        ("7_years", "2019-10-01"),
        ("10_years", "2016-10-01"),
    ],
)
def test_year_presets_use_complete_calendar_months(preset, start):
    result = resolve_month_interval(preset=preset, now=RUN_DATE)
    assert result["from"] == start
    assert result["to"] == "2026-09-30"


def test_explicit_end_month_and_leap_month_end():
    result = resolve_month_interval(preset="1_year", end_month="2024-02", now=RUN_DATE)
    assert (result["from"], result["to"]) == ("2023-03-01", "2024-02-29")


@pytest.mark.parametrize("value", ["2026-09", "2025-12"])
def test_completed_end_month_is_allowed(value):
    assert resolve_month_interval(preset="1_year", end_month=value, now=RUN_DATE)["to"]


@pytest.mark.parametrize("value", ["2026-10", "2026-11", "2025-13", "bad"])
def test_current_future_or_invalid_end_month_is_rejected(value):
    with pytest.raises(ValueError):
        resolve_month_interval(preset="1_year", end_month=value, now=RUN_DATE)


def test_month_boundary_uses_previous_year_december():
    result = resolve_month_interval(preset="1_year", now=date(2027, 1, 1))
    assert (result["from"], result["to"]) == ("2026-01-01", "2026-12-31")


def test_custom_months_resolve_to_full_month_interval():
    result = resolve_month_interval(
        mode="custom", from_month="2024-03", to_month="2025-08", now=RUN_DATE,
    )
    assert (result["from"], result["to"]) == ("2024-03-01", "2025-08-31")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "custom", "to_month": "2025-08"},
        {"mode": "custom", "from_month": "2024-03"},
        {"mode": "custom", "from_month": "2025-08", "to_month": "2024-03"},
        {"mode": "custom", "from_month": "2024-03", "to_month": "2026-10"},
    ],
)
def test_custom_month_validation(kwargs):
    with pytest.raises(ValueError):
        resolve_month_interval(now=RUN_DATE, **kwargs)


def test_calendar_year_requires_completed_year():
    assert resolve_month_interval(preset="calendar_year", now=RUN_DATE)["from"] == "2025-01-01"
    assert resolve_month_interval(preset="calendar_year", calendar_year="2024", now=RUN_DATE)["to"] == "2024-12-31"
    with pytest.raises(ValueError):
        resolve_month_interval(preset="calendar_year", calendar_year="2026", now=RUN_DATE)
    with pytest.raises(ValueError):
        resolve_month_interval(preset="calendar_year", calendar_year="2027", now=RUN_DATE)
    assert resolve_month_interval(preset="calendar_year", calendar_year="2026", now=date(2027, 1, 1))["to"] == "2026-12-31"


def test_arbitrary_date_engine_remains_separate():
    # This helper only governs presentation intervals; it does not alter the
    # death-report CLI's existing exact-date interval support.
    result = resolve_month_interval(mode="custom", from_month="2024-03", to_month="2025-08", now=RUN_DATE)
    assert result["from"] == "2024-03-01"


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        (date(2024, 10, 1), date(2025, 9, 30), 12),
        (date(2024, 1, 1), date(2025, 12, 31), 24),
        (date(2023, 10, 1), date(2025, 9, 30), 24),
        (date(2024, 10, 19), date(2026, 10, 19), 23),
    ],
)
def test_completed_calendar_months_counts_only_whole_months(start, end, expected):
    assert completed_calendar_months(start, end) == expected
