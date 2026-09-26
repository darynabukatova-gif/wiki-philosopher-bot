from datetime import date

import pytest

from wiki_philosopher_bot.date_policy import (
    LEAP_DAY_POLICY,
    calendar_anniversary,
    calendar_date_years_ago,
    completed_calendar_years,
    date_is_within_interval,
    date_is_within_recent_policy,
    recent_death_interval,
    recent_death_policy,
)


def test_seven_calendar_years_are_not_seven_times_365_days():
    start, end = recent_death_interval(date(2026, 9, 26), 7)
    assert (start, end) == (date(2019, 9, 26), date(2026, 9, 26))
    assert (end - start).days != 7 * 365


def test_interval_is_inclusive_and_excludes_adjacent_and_future_dates():
    start, end = recent_death_interval(date(2026, 9, 26), 7)
    assert date_is_within_interval(start, start, end)
    assert date_is_within_interval(end, start, end)
    assert not date_is_within_interval(date(2019, 9, 25), start, end)
    assert not date_is_within_interval(date(2026, 9, 27), start, end)


def test_leap_day_clamps_to_last_day_of_target_month():
    assert calendar_date_years_ago(date(2024, 2, 29), 7) == date(2017, 2, 28)
    policy = recent_death_policy(date(2024, 2, 29), years=7)
    assert policy == {
        "kind": "calendar-years",
        "value": 7,
        "run_date": "2024-02-29",
        "inclusive_from": "2017-02-28",
        "inclusive_to": "2024-02-29",
        "leap_day_policy": LEAP_DAY_POLICY,
    }


def test_fixed_days_policy_preserves_legacy_inclusive_cutoff():
    policy = recent_death_policy(date(2026, 8, 21), days=30)
    assert policy["kind"] == "fixed-days"
    assert policy["inclusive_from"] == "2026-07-22"
    assert date_is_within_recent_policy("2026-07-22", policy)
    assert not date_is_within_recent_policy("2026-07-21", policy)


@pytest.mark.parametrize("kwargs", [{"years": 0}, {"years": -1}, {"days": 0}, {"days": -1}])
def test_policy_overrides_require_positive_integers(kwargs):
    with pytest.raises(ValueError, match="positive integer"):
        recent_death_policy(date(2026, 9, 26), **kwargs)


def test_policy_rejects_simultaneous_year_and_day_overrides():
    with pytest.raises(ValueError, match="mutually exclusive"):
        recent_death_policy(date(2026, 9, 26), years=7, days=30)



def test_calendar_anniversary_and_completed_years_share_leap_day_clamping():
    birth = date(2000, 2, 29)
    assert calendar_anniversary(birth, 2021) == date(2021, 2, 28)
    assert completed_calendar_years(birth, date(2021, 2, 27)) == 20
    assert completed_calendar_years(birth, date(2021, 2, 28)) == 21
