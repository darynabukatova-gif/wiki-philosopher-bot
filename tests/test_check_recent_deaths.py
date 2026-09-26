import copy
import json
from datetime import date, datetime, timezone

import pytest

import wiki_philosopher_bot.cli.check_recent_deaths as check_recent_deaths
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
import wiki_philosopher_bot.recent_death_outbox as recent_death_outbox
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry, make_pending_recent_death_notification,
    serialize_database_entries, transition_recent_death_notification,
)
from wiki_philosopher_bot.telegram_bot import TelegramResult


def monitored_entry(title, qid="Q1", status="accepted", death_year=None):
    entry = make_empty_database_entry(title)
    entry["wikidata"].update({
        "status": "available", "qid": qid, "instances": ["Q5"],
        "occupations": ["Q4964182"], "birth_year": 1932,
        "death_year": death_year, "is_human": True, "is_philosopher": True,
    })
    entry["evaluation"].update({"status": status, "algorithm_version": 2})
    return entry


def write_database(tmp_path, entries):
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries(entries))


def time_claim(time_value, rank="normal", precision=11):
    return {
        "rank": rank,
        "mainsnak": {"snaktype": "value", "datavalue": {"value": {
            "time": time_value, "precision": precision,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }}},
    }


def entity_with_death(*claims):
    return {"claims": {"P570": list(claims)}}


def test_monitoring_eligibility_and_deterministic_selection():
    alpha = monitored_entry("Alpha")
    zulu = monitored_entry("Zulu")
    rejected = monitored_entry("Rejected", status="rejected")
    deceased = monitored_entry("Deceased", death_year=2020)
    database = {entry["title"]: entry for entry in (zulu, alpha, rejected, deceased)}

    assert check_recent_deaths.select_eligible_titles(database) == ["Alpha", "Zulu"]
    assert check_recent_deaths.select_explicit_titles(database, ["Zulu", "Alpha"]) == ["Zulu", "Alpha"]
    with pytest.raises(ValueError, match="duplicate"):
        check_recent_deaths.select_explicit_titles(database, ["Alpha", "Alpha"])
    with pytest.raises(ValueError, match="does not exist"):
        check_recent_deaths.select_explicit_titles(database, ["Missing"])
    with pytest.raises(ValueError, match="not eligible"):
        check_recent_deaths.select_explicit_titles(database, ["Rejected"])
    with pytest.raises(SystemExit):
        check_recent_deaths.parse_args(["--limit", "1", "--title", "Alpha"])


def test_dry_run_has_no_network_or_canonical_write_and_saves_report(monkeypatch, tmp_path):
    write_database(tmp_path, [monitored_entry("Zulu"), monitored_entry("Alpha")])
    database_path = tmp_path / "database.jsonl"
    before = database_path.read_bytes()
    monkeypatch.setattr(
        check_recent_deaths, "RECENT_DEATH_REPORTS_DIRECTORY", tmp_path / "reports/recent-deaths"
    )
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: pytest.fail("dry-run must not fetch"),
    )

    assert check_recent_deaths.main([
        "--data-folder", str(tmp_path), "--dry-run", "--limit", "1",
    ]) == 0

    assert database_path.read_bytes() == before
    report_paths = list((tmp_path / "reports/recent-deaths").glob("*.json"))
    assert len(report_paths) == 1
    report = json.loads(report_paths[0].read_text(encoding="utf-8"))
    assert report["selected"]["titles"] == ["Alpha"]
    assert report["notification"] == {
        "attempted": False, "sent": False, "error": None,
    }


def test_living_no_death_is_checked_without_mutation_and_remains_eligible(monkeypatch, tmp_path):
    entry = monitored_entry("Living")
    database = {"Living": entry}
    write_database(tmp_path, [entry])
    before = copy.deepcopy(entry)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": {"claims": {}}}, None),
    )

    results = check_recent_deaths.run_apply(
        database, ["Living"], str(tmp_path), limiter=object(), today=date(2026, 8, 21),
    )

    assert database["Living"] == before
    assert results["no_death_found"] == 1
    assert check_recent_deaths.recent_death_monitor_needs_processing(database["Living"])


def test_recent_exact_death_updates_only_dates_alerts_and_becomes_ineligible(monkeypatch, tmp_path):
    entry = monitored_entry("Ervin")
    database = {"Ervin": entry}
    write_database(tmp_path, [entry])
    preserved = copy.deepcopy(entry)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(time_claim("+2026-06-29T00:00:00Z"))}, None,
        ),
    )

    results = check_recent_deaths.run_apply(
        database, ["Ervin"], str(tmp_path), limiter=object(), today=date(2026, 8, 21),
    )

    assert database["Ervin"]["wikidata"]["death_year"] == 2026
    assert database["Ervin"]["wikidata"]["death_date"] == "2026-06-29"
    for section in ("summary", "evaluation", "quotes", "posting", "migration"):
        assert database["Ervin"][section] == preserved[section]
    assert len(results["recent_death_updates"]) == 1
    notification = database["Ervin"]["recent_death_notifications"][0]
    assert notification["state"] == "pending"
    assert notification["death_date"] == "2026-06-29"
    assert not check_recent_deaths.recent_death_monitor_needs_processing(database["Ervin"])


@pytest.mark.parametrize(
    "title, claim, expected_outcome, expected_date",
    [
        ("Historical", time_claim("+2000-01-01T00:00:00Z"), "historical_death", "2000-01-01"),
        ("Imprecise", time_claim("+2020-00-00T00:00:00Z", precision=9), "imprecise_death", None),
    ],
)
def test_old_exact_and_year_only_deaths_are_stored_without_recent_alert(
    monkeypatch, tmp_path, title, claim, expected_outcome, expected_date,
):
    entry = monitored_entry(title)
    database = {title: entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(claim)}, None,
        ),
    )

    results = check_recent_deaths.run_apply(
        database, [title], str(tmp_path), limiter=object(), today=date(2026, 8, 21),
    )

    assert database[title]["wikidata"]["death_date"] == expected_date
    assert results["title_details"][0]["outcome"] == expected_outcome
    assert results["recent_death_updates"] == []


def test_future_exact_death_is_reported_suspicious_without_persistence(monkeypatch, tmp_path):
    entry = monitored_entry("Future")
    database = {"Future": entry}
    write_database(tmp_path, [entry])
    before = copy.deepcopy(entry)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(time_claim("+2027-01-01T00:00:00Z"))}, None,
        ),
    )

    results = check_recent_deaths.run_apply(
        database, ["Future"], str(tmp_path), limiter=object(), today=date(2026, 8, 21),
    )

    assert database["Future"] == before
    assert results["suspicious_future_deaths"] == 1
    assert results["title_details"][0]["outcome"] == "future_death_suspicious"


def test_rank_selection_and_malformed_preferred_use_valid_normal_claim(monkeypatch, tmp_path):
    entry = monitored_entry("Ranked")
    database = {"Ranked": entry}
    write_database(tmp_path, [entry])
    malformed_preferred = {"rank": "preferred", "mainsnak": {"snaktype": "value"}}
    deprecated = time_claim("+2026-05-01T00:00:00Z", rank="deprecated")
    normal = time_claim("+2026-06-29T00:00:00Z")
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(deprecated, malformed_preferred, normal)}, None,
        ),
    )

    check_recent_deaths.run_apply(
        database, ["Ranked"], str(tmp_path), limiter=object(), today=date(2026, 8, 21),
    )
    assert database["Ranked"]["wikidata"]["death_date"] == "2026-06-29"


def test_failures_preserve_state_and_value_error_propagates(monkeypatch, tmp_path):
    alpha = monitored_entry("Alpha")
    zulu = monitored_entry("Zulu", qid="Q2")
    database = {"Alpha": alpha, "Zulu": zulu}
    write_database(tmp_path, [alpha, zulu])
    before = copy.deepcopy(database["Alpha"])
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({}, "request_exception"),
    )
    results = check_recent_deaths.run_apply(database, ["Alpha", "Zulu"], str(tmp_path), limiter=object())
    assert database["Alpha"] == before
    assert results["operational_failures"] == 2
    assert check_recent_deaths.recent_death_monitor_needs_processing(database["Alpha"])

    monkeypatch.setattr(
        check_recent_deaths, "record_recent_death_discovery",
        lambda *args: (_ for _ in ()).throw(ValueError("invalid")),
    )
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(time_claim("+2026-06-29T00:00:00Z"))}, None,
        ),
    )
    failed = check_recent_deaths.run_apply(database, ["Alpha"], str(tmp_path), limiter=object())
    assert failed["operational_failures"] == 1

    monkeypatch.setattr(
        check_recent_deaths, "record_recent_death_discovery",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        check_recent_deaths.run_apply(database, ["Alpha"], str(tmp_path), limiter=object())


def test_persistence_oserror_preserves_failed_title_and_continues(monkeypatch, tmp_path):
    alpha = monitored_entry("Alpha")
    zulu = monitored_entry("Zulu", qid="Q2")
    database = {"Alpha": alpha, "Zulu": zulu}
    write_database(tmp_path, [alpha, zulu])
    original = check_recent_deaths.record_recent_death_discovery

    def update_with_failure(database_value, title, *args):
        if title == "Alpha":
            raise OSError("disk full")
        return original(database_value, title, *args)

    monkeypatch.setattr(check_recent_deaths, "record_recent_death_discovery", update_with_failure)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({
            "Q1": entity_with_death(time_claim("+2026-06-29T00:00:00Z")),
            "Q2": entity_with_death(time_claim("+2026-06-29T00:00:00Z")),
        }, None),
    )

    results = check_recent_deaths.run_apply(
        database, ["Alpha", "Zulu"], str(tmp_path), limiter=object(),
    )

    assert results["operational_failures"] == 1
    assert database["Alpha"]["wikidata"]["death_year"] is None
    assert database["Zulu"]["wikidata"]["death_year"] == 2026


def test_report_order_and_report_write_failure_does_not_rollback(monkeypatch, tmp_path, capsys):
    entry = monitored_entry("Ada")
    database = {"Ada": entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(check_recent_deaths, "load_database", lambda *args: database)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_death(time_claim("+2026-06-29T00:00:00Z"))}, None,
        ),
    )
    monkeypatch.setattr(
        check_recent_deaths, "save_recent_death_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert check_recent_deaths.main(["--data-folder", str(tmp_path), "--apply"]) == 0
    assert database["Ada"]["wikidata"]["death_date"] == "2026-06-29"
    output = capsys.readouterr().out
    assert "Warning: recent-death report could not be saved: disk full" in output
    report = check_recent_deaths.build_apply_report(database, 1, ["Ada"], None, {
        "successfully_checked": 1, "no_death_found": 0, "newly_deceased": 1,
        "recent_deaths": 1, "historical_deaths": 0, "imprecise_deaths": 0,
        "suspicious_future_deaths": 0, "operational_failures": 0, "errors": [],
        "recent_death_updates": [], "title_details": [],
    })
    assert list(report)[-1] == "title_details"



def test_prepare_never_uses_telegram_or_combined_notification(monkeypatch, tmp_path):
    entry = monitored_entry("Ervin")
    database = {"Ervin": entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(check_recent_deaths, "get_wikidata_entities_batch", lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": entity_with_death(time_claim("+2026-06-29T00:00:00Z"))}, None))
    monkeypatch.setattr(recent_death_outbox, "send_message_to_chat", lambda *args, **kwargs: pytest.fail("prepare must not notify"))
    check_recent_deaths.run_apply(database, ["Ervin"], str(tmp_path), limiter=object(), today=date(2026, 8, 21))
    assert database["Ervin"]["recent_death_notifications"][0]["state"] == "pending"


def test_recent_days_is_configurable(monkeypatch, tmp_path):
    entry = monitored_entry("Window")
    database = {"Window": entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(check_recent_deaths, "get_wikidata_entities_batch", lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": entity_with_death(time_claim("+2025-09-01T00:00:00Z"))}, None))
    short = check_recent_deaths.run_apply(database, ["Window"], str(tmp_path), limiter=object(), today=date(2026, 8, 21), recent_days=30)
    assert short["historical_deaths"] == 1
    assert "recent_death_notifications" not in database["Window"]


def test_prepare_report_exposes_only_newly_created_notification_ids(tmp_path):
    report = check_recent_deaths.build_apply_report(
        {"Ada": monitored_entry("Ada")}, 1, ["Ada"], None,
        {
            "successfully_checked": 1, "no_death_found": 0, "newly_deceased": 1,
            "recent_deaths": 1, "historical_deaths": 0, "imprecise_deaths": 0,
            "suspicious_future_deaths": 0, "operational_failures": 0, "errors": [],
            "pending_notifications": 1, "already_notified": 0,
            "blocked_existing_notification": 0, "correction_review_required": 0,
            "recent_death_updates": [{"title": "Ada", "death_date": "2026-06-29", "notification_id": "new-id"}],
            "title_details": [],
        },
    )
    assert report["new_notification_ids"] == ["new-id"]
    destination = tmp_path / "result.json"
    check_recent_deaths.write_result_json(report, destination)
    assert json.loads(destination.read_text(encoding="utf-8"))["new_notification_ids"] == ["new-id"]



def test_recent_window_cli_overrides_are_positive_and_mutually_exclusive():
    assert check_recent_deaths.parse_args(["--recent-years", "3"]).recent_years == 3
    assert check_recent_deaths.parse_args(["--recent-days", "30"]).recent_days == 30
    for arguments in (["--recent-years", "0"], ["--recent-days", "-1"],
                      ["--recent-years", "7", "--recent-days", "30"]):
        with pytest.raises(SystemExit):
            check_recent_deaths.parse_args(arguments)


def test_default_report_uses_structured_seven_calendar_year_policy():
    policy = check_recent_deaths.recent_death_policy(
        date(2026, 9, 26),
        default_years=check_recent_deaths.RECENT_DEATH_WINDOW_YEARS,
    )
    report = check_recent_deaths.build_dry_run_report({}, [], None, policy)
    assert report["recent_window"] == {
        "kind": "calendar-years",
        "value": 7,
        "run_date": "2026-09-26",
        "inclusive_from": "2019-09-26",
        "inclusive_to": "2026-09-26",
        "leap_day_policy": "clamp-to-last-day-of-month",
    }


def test_new_death_calendar_boundaries_control_notification_creation(monkeypatch, tmp_path):
    entries = [monitored_entry("Lower", qid="Q1"), monitored_entry("Before", qid="Q2")]
    database = {entry["title"]: entry for entry in entries}
    write_database(tmp_path, entries)
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({
            "Q1": entity_with_death(time_claim("+2019-09-26T00:00:00Z")),
            "Q2": entity_with_death(time_claim("+2019-09-25T00:00:00Z")),
        }, None),
    )
    results = check_recent_deaths.run_apply(
        database, ["Lower", "Before"], str(tmp_path), limiter=object(),
        today=date(2026, 9, 26),
    )
    assert database["Lower"]["recent_death_notifications"][0]["state"] == "pending"
    assert "recent_death_notifications" not in database["Before"]
    assert results["pending_notifications"] == 1
    assert results["historical_deaths"] == 1


def test_known_year_receiving_exact_date_never_creates_event_and_is_review_only(monkeypatch, tmp_path):
    entry = monitored_entry("Known", death_year=2025)
    database = {"Known": entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({
            "Q1": entity_with_death(time_claim("+2025-09-01T00:00:00Z")),
        }, None),
    )
    results = check_recent_deaths.run_apply(
        database, ["Known"], str(tmp_path), limiter=object(),
        today=date(2026, 9, 26),
    )
    assert database["Known"]["wikidata"]["death_date"] == "2025-09-01"
    assert "recent_death_notifications" not in database["Known"]
    assert results["historical_deaths"] == 1
    policy = check_recent_deaths.recent_death_policy(date(2026, 9, 26), years=7)
    assert check_recent_deaths.policy_backfill_review(database, policy) == {
        "count": 1,
        "records": [{
            "title": "Known", "death_date": "2025-09-01",
            "reason": "known-before-current-policy-window",
        }],
    }


def test_known_death_entering_widened_window_is_review_only():
    entry = monitored_entry("Known", death_year=2020)
    entry["wikidata"]["death_date"] = "2020-01-01"
    database = {"Known": entry}
    narrow = check_recent_deaths.recent_death_policy(date(2026, 9, 26), years=3)
    wide = check_recent_deaths.recent_death_policy(date(2026, 9, 26), years=7)
    assert check_recent_deaths.policy_backfill_review(database, narrow)["count"] == 0
    assert check_recent_deaths.policy_backfill_review(database, wide)["records"][0]["title"] == "Known"
    assert check_recent_deaths.select_eligible_titles(database) == []
    assert "recent_death_notifications" not in entry


def test_sent_notification_suppresses_policy_backfill_duplicate():
    entry = monitored_entry("Sent", death_year=2026)
    entry["wikidata"]["death_date"] = "2026-06-29"
    pending = make_pending_recent_death_notification(
        "Sent", "Q1", "2026-06-29", "message", notification_id="sent-id",
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    entry["recent_death_notifications"] = [transition_recent_death_notification(
        pending, "sent", telegram_message_id=10,
        now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )]
    policy = check_recent_deaths.recent_death_policy(date(2026, 9, 26), years=7)
    assert check_recent_deaths.policy_backfill_review({"Sent": entry}, policy) == {
        "count": 0, "records": [],
    }


def test_unresolved_notification_still_blocks_apply_before_backup(monkeypatch, tmp_path):
    entry = monitored_entry("Blocked", death_year=2026)
    entry["wikidata"]["death_date"] = "2026-06-29"
    entry["recent_death_notifications"] = [make_pending_recent_death_notification(
        "Blocked", "Q1", "2026-06-29", "message", notification_id="pending-id",
        now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )]
    monkeypatch.setattr(check_recent_deaths, "load_database", lambda *args: {"Blocked": entry})
    monkeypatch.setattr(
        check_recent_deaths, "create_database_backup",
        lambda *args, **kwargs: pytest.fail("blocked apply must not create a backup"),
    )
    monkeypatch.setattr(
        check_recent_deaths, "get_wikidata_entities_batch",
        lambda *args, **kwargs: pytest.fail("blocked apply must not query Wikidata"),
    )
    assert check_recent_deaths.main([
        "--data-folder", str(tmp_path), "--apply",
    ]) == 1
