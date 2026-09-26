import argparse
import copy
import json
from datetime import date, datetime, timezone

import pytest

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.cli.death_report as death_report_cli
import wiki_philosopher_bot.telegram_bot as telegram_bot
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)
from wiki_philosopher_bot.death_reporting import (
    EXACT_DEATH_ROW_FIELDS,
    _age_at_death,
    build_historical_death_report,
)


GENERATED_AT = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def record(
    title,
    *,
    status="accepted",
    birth_year=None,
    birth_date=None,
    death_year=None,
    death_date=None,
    posted=False,
    qid="Q1",
):
    value = make_empty_database_entry(title)
    value["evaluation"].update(status=status, algorithm_version=2)
    value["wikidata"].update(
        status="available",
        qid=qid,
        birth_year=birth_year,
        death_year=death_year,
        death_date=death_date,
    )
    if birth_date is not None:
        # Future-compatible reporting input only; Phase 2 does not add this to
        # the canonical schema or production records.
        value["wikidata"]["birth_date"] = birth_date
    value["posting"]["has_been_posted"] = posted
    return value


def build(database, start=date(2019, 9, 26), end=date(2026, 9, 26), **kwargs):
    return build_historical_death_report(
        database,
        start,
        end,
        database_sha256="a" * 64,
        generated_at=GENERATED_AT,
        **kwargs,
    )


def test_explicit_interval_is_inclusive_and_posted_accepted_is_included():
    lower = record("Lower", death_year=2019, death_date="2019-09-26", posted=True)
    upper = record("Upper", death_year=2026, death_date="2026-09-26")
    before = record("Before", death_year=2019, death_date="2019-09-25")
    after = record("After", death_year=2026, death_date="2026-09-27")
    rejected = record("Rejected", status="rejected", death_year=2025, death_date="2025-01-01")
    database = {item["title"]: item for item in (upper, rejected, before, lower, after)}

    report = build(database)

    assert [row["title"] for row in report["interval_results"]["rows"]] == [
        "Lower", "Upper",
    ]
    assert report["interval_results"]["exact_deaths"] == 2
    assert report["interval_results"]["deaths_outside_interval"] == 2
    assert report["population"] == {
        "canonical_records": 5,
        "semantic_philosophers": 4,
        "non_semantic_records_skipped": 1,
    }


def test_last_years_uses_calendar_arithmetic_and_inherits_february_clamp():
    args = death_report_cli.parse_args([
        "--last-years", "7", "--to", "2024-02-29",
    ])
    start, end, source = death_report_cli.resolve_interval(args)
    assert (start, end, source) == (
        date(2017, 2, 28), date(2024, 2, 29), "last-years",
    )


def test_last_years_without_to_uses_injected_today():
    args = death_report_cli.parse_args(["--last-years", "7"])
    assert death_report_cli.resolve_interval(args, today=date(2026, 9, 26)) == (
        date(2019, 9, 26), date(2026, 9, 26), "last-years",
    )


@pytest.mark.parametrize("arguments", [
    ["--from", "2019-09-26"],
    ["--to", "2026-09-26"],
    ["--last-years", "0"],
    ["--last-years", "-1"],
    ["--last-years", "7", "--from", "2019-09-26", "--to", "2026-09-26"],
    ["--from", "not-a-date", "--to", "2026-09-26"],
])
def test_cli_interval_validation_rejects_invalid_combinations(arguments):
    with pytest.raises(SystemExit):
        death_report_cli.parse_args(arguments)


def test_from_after_to_is_rejected():
    args = death_report_cli.parse_args([
        "--from", "2026-09-27", "--to", "2026-09-26",
    ])
    with pytest.raises(ValueError, match="must not be after"):
        death_report_cli.resolve_interval(args)


def test_year_only_overlap_is_never_a_confirmed_exact_death():
    database = {
        "Potential": record("Potential", death_year=2019),
        "Outside": record("Outside", death_year=2018),
    }
    report = build(database)
    assert report["interval_results"]["exact_deaths"] == 0
    assert report["interval_results"]["year_only_potential_matches"] == 1
    assert report["interval_results"]["year_only_rows"] == [
        {"title": "Potential", "death_year": 2019},
    ]
    assert report["interval_results"]["deaths_outside_interval"] == 1
    assert report["deaths_by_year"] == {}


def test_missing_and_malformed_death_data_are_classified_safely():
    missing = record("Missing")
    malformed = record("Malformed", death_year=2024, death_date="2024-02-30")
    report = build({"Missing": missing, "Malformed": malformed})
    assert report["interval_results"]["unknown_death_date"] == 1
    assert report["interval_results"]["invalid_date_data"] == 1
    assert report["interval_results"]["exact_deaths"] == 0
    assert report["interval_results"]["invalid_rows"][0]["title"] == "Malformed"


def test_rows_are_deterministic_and_year_counts_use_exact_rows_only():
    database = {
        "Zulu": record("Zulu", death_year=2025, death_date="2025-03-01"),
        "Alpha": record("Alpha", death_year=2025, death_date="2025-03-01"),
        "Earlier": record("Earlier", death_year=2024, death_date="2024-12-31"),
        "Potential": record("Potential", death_year=2025),
    }
    report = build(database)
    assert [row["title"] for row in report["interval_results"]["rows"]] == [
        "Earlier", "Alpha", "Zulu",
    ]
    assert report["deaths_by_year"] == {"2024": 1, "2025": 2}


def test_missing_birth_date_never_infers_age_from_years():
    database = {
        "No exact birth": record(
            "No exact birth", birth_year=1950, death_year=2025,
            death_date="2025-01-01",
        ),
    }
    report = build(database)
    row = report["interval_results"]["rows"][0]
    assert row["birth_date"] is None
    assert row["age_at_death"] is None
    assert report["age_statistics"] == {
        "exact_ages_available": 0,
        "insufficient_age_data": 1,
        "mean": None,
        "median": None,
        "minimum": None,
        "maximum": None,
    }


def test_age_is_calculated_only_when_both_exact_dates_exist():
    database = {
        "Exact": record(
            "Exact", birth_year=1950, birth_date="1950-06-02",
            death_year=2025, death_date="2025-06-01",
        ),
    }
    report = build(database)
    row = report["interval_results"]["rows"][0]
    assert row["age_at_death"] == 74
    assert report["age_statistics"]["mean"] == 74.0
    assert report["age_statistics"]["median"] == 74


def test_report_structure_is_stable_and_input_is_not_mutated():
    item = record("Ada", death_year=2025, death_date="2025-01-01")
    database = {"Ada": item}
    before = copy.deepcopy(database)
    report = build(database)
    assert list(report) == [
        "operation", "mode", "generated_at", "database_sha256", "interval",
        "population", "date_completeness", "interval_results",
        "deaths_by_year", "age_statistics",
    ]
    assert tuple(report["interval_results"]["rows"][0]) == EXACT_DEATH_ROW_FIELDS
    assert report["operation"] == "historical-death-report"
    assert report["mode"] == "read-only"
    assert report["generated_at"] == "2026-09-26T12:00:00Z"
    assert database == before


def test_cli_is_read_only_and_writes_only_explicit_report(monkeypatch, tmp_path, capsys):
    item = record("Ada", death_year=2025, death_date="2025-01-01")
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([item]))
    before = database_path.read_bytes()
    destination = tmp_path / "output" / "report.json"

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only report attempted an external or mutating operation")

    monkeypatch.setattr(cache, "create_database_backup", forbidden)
    monkeypatch.setattr(wikipedia_api, "get_wikidata_entities_batch", forbidden)
    monkeypatch.setattr(telegram_bot, "send_message_to_chat", forbidden)

    assert death_report_cli.main([
        "--from", "2019-09-26", "--to", "2026-09-26",
        "--data-folder", str(tmp_path), "--json", str(destination),
    ]) == 0
    assert database_path.read_bytes() == before
    report = json.loads(destination.read_text(encoding="utf-8"))
    assert report["interval_results"]["exact_deaths"] == 1
    assert "Exact deaths in interval: 1" in capsys.readouterr().out
    assert not (tmp_path / "backups").exists()



def test_cli_default_output_uses_durable_report_directory(tmp_path):
    item = record("Ada", death_year=2025, death_date="2025-01-01")
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries([item]))
    report_folder = tmp_path / "reports" / "deaths"
    assert death_report_cli.main([
        "--last-years", "7", "--to", "2026-09-26",
        "--data-folder", str(tmp_path),
        "--report-folder", str(report_folder),
    ]) == 0
    reports = list(report_folder.glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["interval"] == {
        "from": "2019-09-26", "to": "2026-09-26", "inclusive": True,
        "source": "last-years", "last_years": 7,
    }



def test_explicit_json_cannot_target_canonical_database(tmp_path):
    item = record("Ada", death_year=2025, death_date="2025-01-01")
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([item]))
    before = database_path.read_bytes()
    with pytest.raises(SystemExit, match="must not target"):
        death_report_cli.main([
            "--from", "2019-09-26", "--to", "2026-09-26",
            "--data-folder", str(tmp_path), "--json", str(database_path),
        ])
    assert database_path.read_bytes() == before



@pytest.mark.parametrize(
    "death_date, expected_age",
    [
        ("2021-06-14", 20),
        ("2021-06-15", 21),
        ("2021-06-16", 21),
    ],
)
def test_ordinary_age_uses_completed_anniversaries(death_date, expected_age):
    item = record(
        "Ordinary", birth_year=2000, birth_date="2000-06-15",
        death_year=2021, death_date=death_date,
    )
    row = build({"Ordinary": item})["interval_results"]["rows"][0]
    assert row["age_at_death"] == expected_age


@pytest.mark.parametrize(
    "death_date, expected_age",
    [
        ("2021-02-27", 20),
        ("2021-02-28", 21),
        ("2024-02-28", 23),
        ("2024-02-29", 24),
    ],
)
def test_february_29_birth_uses_clamped_calendar_anniversary(
    death_date, expected_age,
):
    item = record(
        "Leap", birth_year=2000, birth_date="2000-02-29",
        death_year=int(death_date[:4]), death_date=death_date,
    )
    row = build({"Leap": item})["interval_results"]["rows"][0]
    assert row["age_at_death"] == expected_age


def test_birth_after_death_has_no_age():
    item = record(
        "Invalid chronology", birth_year=2022, birth_date="2022-01-01",
        death_year=2021, death_date="2021-12-31",
    )
    row = build({"Invalid chronology": item})["interval_results"]["rows"][0]
    assert row["age_at_death"] is None



def test_missing_either_exact_date_has_no_age():
    assert _age_at_death(None, date(2021, 1, 1)) is None
    assert _age_at_death(date(2000, 1, 1), None) is None
