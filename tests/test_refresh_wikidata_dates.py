import copy
import json
from datetime import date

import pytest

import refresh_wikidata_dates
import wikipedia_api
from database_schema import make_empty_database_entry, serialize_database_entries


def date_entry(title, birth=650, death=548, qid="Q1"):
    entry = make_empty_database_entry(title)
    entry["wikidata"].update({
        "status": "available",
        "qid": qid,
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "is_human": True,
        "is_philosopher": True,
        "birth_year": birth,
        "death_year": death,
    })
    entry["evaluation"].update({"status": "accepted", "algorithm_version": 2})
    return entry


def write_database(tmp_path, entries):
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries(entries))


def entity_with_dates(birth=None, death=None):
    def claim(time_value):
        return {"mainsnak": {"datavalue": {"value": {"time": time_value}}}}

    claims = {}
    if birth is not None:
        claims["P569"] = [claim(birth)]
    if death is not None:
        claims["P570"] = [claim(death)]
    return {"claims": claims}


def day_death_claim(time_value):
    return {
        "rank": "normal",
        "mainsnak": {"snaktype": "value", "datavalue": {"value": {
            "time": time_value,
            "precision": 11,
            "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
        }}},
    }


def test_date_refresh_eligibility_and_explicit_order():
    alpha = date_entry("Alpha")
    zulu = date_entry("Zulu")
    no_dates = date_entry("No dates", birth=None, death=None)
    unavailable = date_entry("Unavailable")
    unavailable["wikidata"]["status"] = "unavailable"
    database = {
        "Zulu": zulu,
        "Alpha": alpha,
        "No dates": no_dates,
        "Unavailable": unavailable,
    }

    assert refresh_wikidata_dates.select_eligible_titles(database) == [
        "Alpha", "Zulu",
    ]
    assert refresh_wikidata_dates.select_explicit_titles(
        database, ["Zulu", "Alpha"]
    ) == ["Zulu", "Alpha"]
    with pytest.raises(ValueError, match="duplicate"):
        refresh_wikidata_dates.select_explicit_titles(database, ["Alpha", "Alpha"])
    with pytest.raises(ValueError, match="does not exist"):
        refresh_wikidata_dates.select_explicit_titles(database, ["Missing"])
    with pytest.raises(ValueError, match="not eligible"):
        refresh_wikidata_dates.select_explicit_titles(database, ["No dates"])


def test_date_refresh_dry_run_is_network_and_canonical_read_only(monkeypatch, tmp_path):
    write_database(tmp_path, [date_entry("Zulu"), date_entry("Alpha")])
    database_path = tmp_path / "database.jsonl"
    before = database_path.read_bytes()
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "WIKIDATA_DATE_REFRESH_REPORTS_DIRECTORY",
        tmp_path / "reports/wikidata-date-refresh",
    )
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: pytest.fail("dry-run must not fetch"),
    )

    assert refresh_wikidata_dates.main([
        "--data-folder", str(tmp_path), "--dry-run", "--limit", "1",
    ]) == 0

    assert database_path.read_bytes() == before
    reports = list((tmp_path / "reports/wikidata-date-refresh").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["selected"]["titles"] == ["Alpha"]
    assert report["current_date_statistics"]["birth_year"]["count"] == 2


def test_date_refresh_updates_only_signed_life_dates_and_preserves_evaluation(
    monkeypatch, tmp_path,
):
    entry = date_entry("Thales of Miletus")
    old_wikidata = copy.deepcopy(entry["wikidata"])
    preserved = copy.deepcopy({
        key: entry[key]
        for key in ("summary", "evaluation", "quotes", "posting", "migration")
    })
    database = {entry["title"]: entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda qids, limiter=None: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_dates("-0650-00-00T00:00:00Z", "-0548-00-00T00:00:00Z")},
            None,
        ),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Thales of Miletus"], str(tmp_path), limiter=object(),
    )

    assert database["Thales of Miletus"]["wikidata"]["birth_year"] == -650
    assert database["Thales of Miletus"]["wikidata"]["death_year"] == -548
    refreshed_wikidata = copy.deepcopy(
        database["Thales of Miletus"]["wikidata"]
    )
    for field in ("birth_year", "death_year", "death_date"):
        old_wikidata.pop(field)
        refreshed_wikidata.pop(field)
    assert refreshed_wikidata == old_wikidata
    assert {key: database["Thales of Miletus"][key] for key in preserved} == preserved
    assert results["successfully_refreshed"] == 1
    assert results["changed"] == 1
    assert results["birth_sign_corrections"] == 1
    assert results["death_sign_corrections"] == 1
    assert results["titles"] == [{
        "title": "Thales of Miletus",
        "old_birth_year": 650,
        "new_birth_year": -650,
        "old_death_year": 548,
        "new_death_year": -548,
        "old_death_date": None,
        "new_death_date": None,
        "changed": True,
    }]


def test_date_refresh_successful_absent_claims_replace_stale_dates(monkeypatch, tmp_path):
    entry = date_entry("Modern", birth=1951, death=None)
    database = {"Modern": entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_dates()}, None,
        ),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Modern"], str(tmp_path), limiter=object(),
    )

    assert database["Modern"]["wikidata"]["birth_year"] is None
    assert database["Modern"]["wikidata"]["death_year"] is None
    assert results["fields_changed_to_none"] == 1


def test_date_refresh_request_failure_preserves_dates_and_reports_retryable(
    monkeypatch, tmp_path,
):
    entry = date_entry("Socrates", birth=470, death=399)
    database = {"Socrates": entry}
    write_database(tmp_path, [entry])
    before = copy.deepcopy(entry["wikidata"])
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({}, "request_exception"),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Socrates"], str(tmp_path), limiter=object(),
    )

    assert database["Socrates"]["wikidata"] == before
    assert results["operational_failures"] == 1
    assert results["remaining_retryable"] == 1
    assert refresh_wikidata_dates.wikidata_date_refresh_needs_processing(
        database["Socrates"]
    ) is True


def test_date_refresh_malformed_entity_preserves_dates_and_remains_retryable(
    monkeypatch, tmp_path,
):
    entry = date_entry("Aristotle", birth=384, death=322)
    database = {"Aristotle": entry}
    write_database(tmp_path, [entry])
    before = copy.deepcopy(entry["wikidata"])
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": {}}, None),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Aristotle"], str(tmp_path), limiter=object(),
    )

    assert database["Aristotle"]["wikidata"] == before
    assert results["operational_failures"] == 1
    assert results["errors"] == [{
        "title": "Aristotle",
        "type": "MalformedEntity",
        "message": "entity claims are missing or malformed",
    }]


def test_date_refresh_malformed_time_preserves_dates_instead_of_clearing_them(
    monkeypatch, tmp_path,
):
    entry = date_entry("Plato", birth=427, death=347)
    database = {"Plato": entry}
    write_database(tmp_path, [entry])
    before = copy.deepcopy(entry["wikidata"])
    malformed_entity = entity_with_dates("not-a-wikidata-time", "+0347-00-00T00:00:00Z")
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": malformed_entity}, None),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Plato"], str(tmp_path), limiter=object(),
    )

    assert database["Plato"]["wikidata"] == before
    assert results["operational_failures"] == 1
    assert results["errors"][0]["message"] == "P569 time value is malformed"


def test_date_refresh_uses_shared_rank_aware_time_claim_selection():
    deprecated = wikipedia_api_test_claim(
        "+1932-05-12T00:00:00Z", "deprecated",
    )
    normal = wikipedia_api_test_claim("+1932-06-12T00:00:00Z", "normal")

    assert refresh_wikidata_dates.refreshed_life_dates_from_entity({
        "claims": {"P569": [deprecated, normal]},
    }) == (1932, None, None, None)


def wikipedia_api_test_claim(time_value, rank):
    return {
        "rank": rank,
        "mainsnak": {"snaktype": "value", "datavalue": {"value": {
            "time": time_value,
        }}},
    }


def test_detect_recent_death_update_cases():
    today = date(2026, 8, 21)
    assert refresh_wikidata_dates.detect_recent_death_update(
        None, None, 2026, "2026-06-29", today=today,
    ) is True
    assert refresh_wikidata_dates.detect_recent_death_update(
        2026, None, 2026, "2026-06-29", today=today,
    ) is True
    assert refresh_wikidata_dates.detect_recent_death_update(
        2026, "2026-06-29", 2026, "2026-06-29", today=today,
    ) is False
    assert refresh_wikidata_dates.detect_recent_death_update(
        None, None, 2020, "2020-01-01", today=today,
    ) is False
    assert refresh_wikidata_dates.detect_recent_death_update(
        None, None, 2027, "2027-01-01", today=today,
    ) is False


def test_date_refresh_stores_exact_recent_death_and_reports_it(monkeypatch, tmp_path):
    entry = date_entry("Ervin László", birth=1932, death=None)
    database = {entry["title"]: entry}
    write_database(tmp_path, [entry])
    entity = {"claims": {"P570": [day_death_claim("+2026-06-29T00:00:00Z")]}}
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": entity}, None),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Ervin László"], str(tmp_path), limiter=object(),
        today=date(2026, 8, 21),
    )

    assert database["Ervin László"]["wikidata"]["death_year"] == 2026
    assert database["Ervin László"]["wikidata"]["death_date"] == "2026-06-29"
    assert results["recent_death_updates"] == [{
        "title": "Ervin László",
        "death_date": "2026-06-29",
        "old_death_year": None,
        "old_death_date": None,
        "new_death_year": 2026,
        "new_death_date": "2026-06-29",
    }]
    report = refresh_wikidata_dates.build_apply_report(
        database, 1, ["Ervin László"], None, results,
    )
    assert report["recent_death_updates"] == {
        "count": 1,
        "titles": results["recent_death_updates"],
    }


def test_date_refresh_stores_historical_exact_death_without_alert(monkeypatch, tmp_path):
    entry = date_entry("Historical", birth=1900, death=2000)
    database = {entry["title"]: entry}
    write_database(tmp_path, [entry])
    entity = {"claims": {"P570": [day_death_claim("+2000-01-01T00:00:00Z")]}}
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult({"Q1": entity}, None),
    )

    results = refresh_wikidata_dates.run_apply(
        database, ["Historical"], str(tmp_path), limiter=object(),
        today=date(2026, 8, 21),
    )

    assert database["Historical"]["wikidata"]["death_date"] == "2000-01-01"
    assert results["recent_death_updates"] == []


def test_date_refresh_report_failure_does_not_rollback_canonical_update(
    monkeypatch, tmp_path, capsys,
):
    entry = date_entry("Thales of Miletus")
    database = {entry["title"]: entry}
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        refresh_wikidata_dates, "load_database", lambda *args: database,
    )
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Q1": entity_with_dates("-0650-00-00T00:00:00Z", "-0548-00-00T00:00:00Z")},
            None,
        ),
    )
    monkeypatch.setattr(
        refresh_wikidata_dates,
        "save_wikidata_date_refresh_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert refresh_wikidata_dates.main([
        "--data-folder", str(tmp_path), "--apply",
    ]) == 0
    assert database["Thales of Miletus"]["wikidata"]["birth_year"] == -650
    assert "Warning: Wikidata date refresh report could not be saved: disk full" in capsys.readouterr().out


def test_date_refresh_rejects_limit_with_explicit_title():
    with pytest.raises(SystemExit):
        refresh_wikidata_dates.parse_args(["--limit", "1", "--title", "Ada"])
