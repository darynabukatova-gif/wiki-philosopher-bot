from copy import deepcopy
import json
import threading
from types import SimpleNamespace

import pytest

from wiki_philosopher_bot.cache import DatabaseBackupResult, load_database
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)
from wiki_philosopher_bot.external_links import (
    ExternalLinksApplyValidationError,
    PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY,
    apply_reviewed_project_gutenberg_links,
    audit_project_gutenberg_links,
    project_gutenberg_author_identifier_resolution,
    project_gutenberg_author_url,
    validate_reviewed_project_gutenberg_apply,
)
from wiki_philosopher_bot.cli import enrich_external_links


def _entry(title, qid=None, status="accepted"):
    entry = make_empty_database_entry(title)
    entry["evaluation"]["status"] = status
    entry["wikidata"]["qid"] = qid
    return entry


def _result(data=None, error_reason=None):
    return SimpleNamespace(data=data or {}, error_reason=error_reason)


def _p1938(identifier, rank="normal"):
    return {
        "rank": rank,
        "mainsnak": {
            "datavalue": {"value": identifier, "type": "string"},
        },
    }


def _entity(identifier):
    return {"claims": {PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY: [_p1938(identifier)]}}


def _write_database(tmp_path, database):
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries(list(database.values()))
    )


def _reviewed_project_gutenberg_report(database):
    return audit_project_gutenberg_links(
        database,
        wikidata_lookup=lambda qids, limiter=None: _result({
            qid: _entity(str(index + 380))
            for index, qid in enumerate(qids)
        }),
    )


def test_accepted_philosopher_with_p1938_gets_author_page_proposal_without_mutation():
    entry = _entry("William Godwin", "Q1")
    database = {entry["title"]: entry}
    before = deepcopy(database)

    report = audit_project_gutenberg_links(
        database,
        wikidata_lookup=lambda qids, limiter=None: _result({"Q1": _entity("380")}),
    )

    assert database == before
    assert report["operation"] == "project-gutenberg-external-links-audit"
    assert report["counts"]["proposed_new_project_gutenberg_links"] == 1
    row = report["changes_or_conflicts"][0]
    assert row["proposed"] == {
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
    }
    assert row["evidence"] == {
        "wikidata_property": "P1938",
        "raw_project_gutenberg_author_id": "380",
        "formatter_url": "https://gutenberg.org/ebooks/author/$1",
        "constructed_author_url": "https://www.gutenberg.org/ebooks/author/380",
        "verified_final_url": None,
    }


def test_accepted_philosopher_without_p1938_has_no_proposal():
    entry = _entry("Ada", "Q1")
    report = audit_project_gutenberg_links(
        {"Ada": entry},
        wikidata_lookup=lambda qids, limiter=None: _result({"Q1": {"claims": {}}}),
    )

    assert report["counts"]["records_with_no_gutenberg_identifier"] == 1
    assert report["counts"]["proposed_new_project_gutenberg_links"] == 0


def test_rejected_non_philosopher_is_skipped_without_wikidata_lookup():
    rejected = _entry("A Book", "Q1", status="rejected")
    report = audit_project_gutenberg_links(
        {"A Book": rejected},
        wikidata_lookup=lambda *args, **kwargs: pytest.fail("lookup"),
    )

    assert report["semantically_eligible_philosopher_records"] == 0
    assert report["non_eligible_records_skipped"] == 1


def test_missing_qid_is_reported_without_proposal_or_name_lookup():
    entry = _entry("No QID")
    report = audit_project_gutenberg_links(
        {"No QID": entry},
        wikidata_lookup=lambda *args, **kwargs: pytest.fail("lookup"),
    )

    assert report["counts"]["records_missing_usable_wikidata_qid"] == 1
    assert report["counts"]["proposed_new_project_gutenberg_links"] == 0


@pytest.mark.parametrize("identifier", ["0", "0001", "380-1", 380, None])
def test_malformed_p1938_identifier_is_rejected(identifier):
    entry = _entry("Ada", "Q1")
    report = audit_project_gutenberg_links(
        {"Ada": entry},
        wikidata_lookup=lambda qids, limiter=None: _result({"Q1": _entity(identifier)}),
    )

    assert report["counts"]["invalid_identifiers"] == 1
    assert report["counts"]["proposed_new_project_gutenberg_links"] == 0


def test_multiple_distinct_p1938_values_are_ambiguous_but_duplicate_value_is_safe():
    ambiguous = project_gutenberg_author_identifier_resolution({
        "claims": {"P1938": [_p1938("380"), _p1938("381")]},
    })
    duplicate = project_gutenberg_author_identifier_resolution({
        "claims": {"P1938": [_p1938("380"), _p1938("380")]},
    })

    assert ambiguous["status"] == "ambiguous"
    assert ambiguous["identifiers"] == ["380", "381"]
    assert duplicate == {
        "status": "available", "identifiers": ["380"], "invalid_values": [],
    }


def test_existing_identical_link_is_preserved_and_different_link_is_reported_as_conflict():
    identical = _entry("Godwin", "Q1")
    identical["external_links"]["project_gutenberg"] = project_gutenberg_author_url("380")
    conflict = _entry("Other", "Q2")
    conflict["external_links"]["project_gutenberg"] = project_gutenberg_author_url("381")

    report = audit_project_gutenberg_links(
        {"Godwin": identical, "Other": conflict},
        wikidata_lookup=lambda qids, limiter=None: _result({
            "Q1": _entity("380"), "Q2": _entity("380"),
        }),
    )

    assert report["counts"]["valid_existing_project_gutenberg_links"] == 1
    assert report["counts"]["conflicts"] == 1
    assert report["conflicts"] == [{
        "title": "Other", "qid": "Q2",
        "current": "https://www.gutenberg.org/ebooks/author/381",
        "observed": "https://www.gutenberg.org/ebooks/author/380",
    }]


def test_audit_has_no_name_matching_path_and_does_not_touch_wikiquote_or_wikisource():
    entry = _entry("A deliberately unrelated title", "Q1")
    entry["external_links"]["wikiquote"] = "https://en.wikiquote.org/wiki/Keep"
    entry["external_links"]["wikisource"] = "https://en.wikisource.org/wiki/Author:Keep"
    before = deepcopy(entry)
    calls = []

    def lookup(qids, limiter=None):
        calls.append(tuple(qids))
        return _result({"Q1": _entity("380")})

    report = audit_project_gutenberg_links({entry["title"]: entry}, wikidata_lookup=lookup)

    assert calls == [("Q1",)]
    assert entry == before
    assert report["changes_or_conflicts"][0]["title"] == entry["title"]


def test_invalid_constructed_url_is_reported_without_proposal(monkeypatch):
    entry = _entry("Ada", "Q1")
    monkeypatch.setattr(
        "wiki_philosopher_bot.external_links.project_gutenberg_author_url",
        lambda identifier: "https://example.invalid/{}".format(identifier),
    )

    report = audit_project_gutenberg_links(
        {"Ada": entry},
        wikidata_lookup=lambda qids, limiter=None: _result({"Q1": _entity("380")}),
    )

    assert report["counts"]["invalid_resulting_urls"] == 1
    assert report["counts"]["proposed_new_project_gutenberg_links"] == 0


def test_cli_project_gutenberg_mode_is_read_only_and_cannot_apply(monkeypatch, tmp_path, capsys):
    database = {"Ada": _entry("Ada", "Q1")}
    monkeypatch.setattr(enrich_external_links, "load_database", lambda *args: database)
    monkeypatch.setattr(enrich_external_links, "audit_project_gutenberg_links", lambda *args, **kwargs: {
        "total_canonical_records": 1,
        "semantically_eligible_philosopher_records": 1,
        "non_eligible_records_skipped": 0,
        "counts": {
            "records_with_authoritative_gutenberg_identifier": 1,
            "proposed_new_project_gutenberg_links": 1,
            "records_with_no_gutenberg_identifier": 0,
            "ambiguous_multiple_identifiers": 0,
            "conflicts": 0,
        },
    })

    assert enrich_external_links.main([
        "--project-gutenberg", "--data-folder", str(tmp_path),
        "--report-folder", str(tmp_path / "reports"),
    ]) == 0
    assert "Project Gutenberg audit" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        enrich_external_links.parse_args([
            "--project-gutenberg", "--apply-report", "reviewed.json",
        ])


def test_reviewed_project_gutenberg_apply_only_changes_gutenberg_and_preserves_other_fields(tmp_path):
    ada = _entry("Ada", "Q1")
    ada["external_links"]["wikiquote"] = "https://en.wikiquote.org/wiki/Ada"
    ada["external_links"]["wikisource"] = "https://en.wikisource.org/wiki/Author:Ada"
    ada["summary"]["text"] = "Keep this exact summary."
    other = _entry("Other", "Q2", status="rejected")
    database = {"Ada": ada, "Other": other}
    report = _reviewed_project_gutenberg_report(database)
    before_other = deepcopy(other)
    _write_database(tmp_path, database)
    loaded = load_database("database.jsonl", str(tmp_path))

    result = apply_reviewed_project_gutenberg_links(
        loaded, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )

    assert result["records_updated"] == 1
    assert result["project_gutenberg_links_written"] == 1
    assert result["applied_changes"] == [{
        "title": "Ada", "qid": "Q1", "previous_project_gutenberg": None,
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
    }]
    assert loaded["Ada"]["external_links"] == {
        "wikiquote": "https://en.wikiquote.org/wiki/Ada",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
    }
    assert loaded["Ada"]["summary"]["text"] == "Keep this exact summary."
    assert loaded["Other"] == before_other
    assert load_database("database.jsonl", str(tmp_path)) == loaded


def test_project_gutenberg_apply_handles_absent_external_links_without_unrelated_keys(tmp_path):
    ada = _entry("Ada", "Q1")
    del ada["external_links"]
    database = {"Ada": ada}
    report = _reviewed_project_gutenberg_report(database)
    _write_database(tmp_path, database)
    loaded = load_database("database.jsonl", str(tmp_path))

    apply_reviewed_project_gutenberg_links(
        loaded, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )

    assert loaded["Ada"]["external_links"] == {
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
    }


def test_project_gutenberg_apply_never_calls_network(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_project_gutenberg_report(database)
    _write_database(tmp_path, database)
    loaded = load_database("database.jsonl", str(tmp_path))
    monkeypatch.setattr(
        "wiki_philosopher_bot.external_links.get_wikidata_entities_batch",
        lambda *args, **kwargs: pytest.fail("network"),
    )

    apply_reviewed_project_gutenberg_links(
        loaded, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )


@pytest.mark.parametrize("mutation, expected", [
    (lambda database, report: database.pop("Ada"), "no longer exists"),
    (lambda database, report: database["Ada"]["wikidata"].__setitem__("qid", "Q99"), "QID differs"),
    (lambda database, report: database["Ada"]["external_links"].__setitem__("project_gutenberg", "https://www.gutenberg.org/ebooks/author/999"), "no longer null"),
    (lambda database, report: database["Ada"]["evaluation"].__setitem__("status", "rejected"), "no longer semantically eligible"),
    (lambda database, report: report.__setitem__("operation", "wrong-operation"), "not a Project Gutenberg"),
    (lambda database, report: report.__setitem__("mode", "apply"), "Only a dry-run"),
    (lambda database, report: report["changes_or_conflicts"][0]["evidence"].__setitem__("raw_project_gutenberg_author_id", "0001"), "invalid P1938 evidence"),
    (lambda database, report: report["changes_or_conflicts"][0]["proposed"].__setitem__("project_gutenberg", "https://www.gutenberg.org/ebooks/author/381"), "invalid P1938 evidence"),
    (lambda database, report: report.__setitem__("conflicts", [{"title": "Ada"}]), "inconsistent conflicts"),
    (lambda database, report: report.__setitem__("lookup_failures_by_reason", {"wikidata_http_429": 1}), "inconsistent identifier counts"),
])
def test_project_gutenberg_apply_rejects_stale_or_invalid_report_before_write(mutation, expected):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_project_gutenberg_report(database)
    mutation(database, report)

    with pytest.raises(ExternalLinksApplyValidationError, match=expected):
        validate_reviewed_project_gutenberg_apply(database, report)


def test_project_gutenberg_apply_rejects_duplicate_title_before_write():
    ada = _entry("Ada", "Q1")
    beth = _entry("Beth", "Q2")
    database = {"Ada": ada, "Beth": beth}
    report = _reviewed_project_gutenberg_report(database)
    duplicate = deepcopy(report["changes_or_conflicts"][0])
    report["changes_or_conflicts"].append(duplicate)

    with pytest.raises(ExternalLinksApplyValidationError, match="duplicate proposal title"):
        validate_reviewed_project_gutenberg_apply(database, report)


def test_project_gutenberg_apply_is_all_or_nothing_and_second_apply_is_stale(tmp_path):
    ada = _entry("Ada", "Q1")
    beth = _entry("Beth", "Q2")
    database = {"Ada": ada, "Beth": beth}
    report = _reviewed_project_gutenberg_report(database)
    database["Beth"]["evaluation"]["status"] = "rejected"
    _write_database(tmp_path, database)
    before = (tmp_path / "database.jsonl").read_bytes()

    with pytest.raises(ExternalLinksApplyValidationError, match="no longer semantically eligible"):
        apply_reviewed_project_gutenberg_links(
            database, report, "database.jsonl", str(tmp_path), threading.Lock(),
        )
    assert (tmp_path / "database.jsonl").read_bytes() == before
    assert database["Ada"]["external_links"]["project_gutenberg"] is None

    database["Beth"]["evaluation"]["status"] = "accepted"
    apply_reviewed_project_gutenberg_links(
        database, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )
    with pytest.raises(ExternalLinksApplyValidationError, match="no longer null"):
        validate_reviewed_project_gutenberg_apply(database, report)


def test_cli_project_gutenberg_apply_creates_one_backup_only_after_validation(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_project_gutenberg_report(database)
    source = tmp_path / "reviewed.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    _write_database(tmp_path, database)
    backups = []
    monkeypatch.setattr(
        enrich_external_links,
        "create_database_backup",
        lambda **kwargs: backups.append(kwargs) or DatabaseBackupResult(
            path=str(tmp_path / "backup.jsonl"), sha256="a" * 64, size_bytes=1,
        ),
    )

    assert enrich_external_links.main([
        "--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports"),
        "--apply-project-gutenberg-report", str(source),
    ]) == 0
    assert len(backups) == 1
    assert backups[0]["label"] == "before-project-gutenberg-external-links-enrichment"
    assert load_database("database.jsonl", str(tmp_path))["Ada"]["external_links"]["project_gutenberg"] == "https://www.gutenberg.org/ebooks/author/380"
    apply_reports = list((tmp_path / "reports").glob("*.json"))
    assert len(apply_reports) == 1
    apply_report = json.loads(apply_reports[0].read_text(encoding="utf-8"))
    assert apply_report["operation"] == "project-gutenberg-external-links-apply"
    assert apply_report["source_reviewed_report"] == str(source)
    assert apply_report["reviewed_proposal_count"] == 1
    assert apply_report["records_updated"] == 1
    assert apply_report["project_gutenberg_links_written"] == 1
    assert apply_report["backup_path"] == str(tmp_path / "backup.jsonl")
    assert apply_report["backup_sha256"] == "a" * 64
    assert apply_report["pre_write_database_sha256"]
    assert apply_report["post_write_database_sha256"] == apply_report["database_sha256"]


def test_cli_project_gutenberg_stale_report_creates_no_backup(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_project_gutenberg_report(database)
    source = tmp_path / "reviewed.json"
    source.write_text(json.dumps(report), encoding="utf-8")
    database["Ada"]["external_links"]["project_gutenberg"] = "https://www.gutenberg.org/ebooks/author/380"
    _write_database(tmp_path, database)
    monkeypatch.setattr(
        enrich_external_links, "create_database_backup", lambda **kwargs: pytest.fail("backup"),
    )

    assert enrich_external_links.main([
        "--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports"),
        "--apply-project-gutenberg-report", str(source),
    ]) == 1
