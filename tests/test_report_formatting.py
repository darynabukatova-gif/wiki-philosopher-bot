import pytest

from wiki_philosopher_bot.report_formatting import (
    format_report_interval_compact,
    format_report_interval_long,
)


@pytest.mark.parametrize(
    "start,end,long,compact",
    [
        ("2025-10-01", "2026-09-30", "October 2025 – September 2026", "Oct 2025 – Sep 2026"),
        ("2025-01-01", "2025-12-31", "2025", "2025"),
        ("2026-03-01", "2026-03-31", "March 2026", "Mar 2026"),
        ("2024-02-01", "2024-02-29", "February 2024", "Feb 2024"),
        ("2025-02-01", "2025-02-28", "February 2025", "Feb 2025"),
    ],
)
def test_complete_calendar_intervals_use_month_labels(start, end, long, compact):
    assert format_report_interval_long(start, end) == long
    assert format_report_interval_compact(start, end) == compact


@pytest.mark.parametrize(
    "start,end",
    [
        ("2025-10-19", "2026-09-30"),
        ("2025-10-01", "2026-09-19"),
        ("2026-03-05", "2026-04-17"),
    ],
)
def test_partial_month_intervals_retain_exact_date_fallback(start, end):
    expected = "{} through {} (inclusive)".format(start, end)
    assert format_report_interval_long(start, end) == expected
    assert format_report_interval_compact(start, end) == expected
