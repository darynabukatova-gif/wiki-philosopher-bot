from copy import deepcopy
from types import SimpleNamespace

import pytest

from wiki_philosopher_bot.database_schema import make_empty_database_entry
from wiki_philosopher_bot.database_schema import serialize_database_entries
import wiki_philosopher_bot.external_links as external_links
from wiki_philosopher_bot.external_links import (
    ExternalLinksApplyValidationError,
    apply_reviewed_external_links,
    audit_external_links,
    validate_reviewed_external_links_apply,
)
from wiki_philosopher_bot.cli import enrich_external_links
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.cache import DatabaseBackupResult, load_database
import threading


def _entry(title, qid=None):
    entry = make_empty_database_entry(title)
    entry["wikidata"]["qid"] = qid
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"].update({
        "status": "available",
        "items": [{
            "text": "A canonical eligible quote.",
            "length": 27,
            "word_count": 4,
            "source": {
                "work": None, "year": None, "date": None,
                "details": None, "citation": None, "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    })
    return entry


def _wikidata_result(data=None, error_reason=None):
    return SimpleNamespace(data=data or {}, error_reason=error_reason)


def _lookup(url=None, reason=None):
    return lambda title, limiter=None: (url, reason)


def _write_database(tmp_path, database):
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries(list(database.values()))
    )


def _reviewed_report(database):
    return audit_external_links(
        database,
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result({
            "Q1": {"sitelinks": {"enwikisource": {"title": "Author:Ada"}}}
        }),
    )


def test_old_record_without_external_links_is_read_only_and_can_receive_proposals():
    entry = _entry("Ada Lovelace", "Q7259")
    del entry["external_links"]
    database = {entry["title"]: entry}
    before = deepcopy(database)

    report = audit_external_links(
        database,
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada_Lovelace"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result({
            "Q7259": {"sitelinks": {"enwikisource": {"title": "Author:Ada Lovelace"}}}
        }),
    )

    assert database == before
    row = report["changes_or_conflicts"][0]
    assert row["proposed"] == {
        "wikiquote": "https://en.wikiquote.org/wiki/Ada_Lovelace",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada_Lovelace",
        "project_gutenberg": None,
    }
    assert report["counts"]["records_receiving_both"] == 1


def test_existing_valid_links_are_preserved_without_lookup():
    entry = _entry("Ada", "Q1")
    entry["external_links"].update({
        "wikiquote": "https://en.wikiquote.org/wiki/Ada",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
        "project_gutenberg": "https://www.gutenberg.org/ebooks/1",
    })
    database = {"Ada": entry}

    report = audit_external_links(
        database,
        wikiquote_lookup=lambda *args, **kwargs: pytest.fail("stored URL is valid"),
        wikidata_lookup=lambda *args, **kwargs: pytest.fail("stored URL is valid"),
    )

    assert report["counts"]["records_already_containing_valid_external_links"] == 1
    assert report["counts"]["records_with_no_proposed_change"] == 1
    assert report["changes_or_conflicts"] == []


def test_wikiquote_redirect_url_is_proposed_only_after_lookup_evidence():
    entry = _entry("Ada")
    report = audit_external_links(
        {"Ada": entry},
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada_(philosopher)"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result(),
    )
    row = report["changes_or_conflicts"][0]
    assert row["proposed"]["wikiquote"] == "https://en.wikiquote.org/wiki/Ada_(philosopher)"


def test_wikisource_absence_and_missing_qid_are_normal_not_errors():
    ada = _entry("Ada", "Q1")
    no_qid = _entry("No QID")
    report = audit_external_links(
        {"Ada": ada, "No QID": no_qid},
        wikiquote_lookup=_lookup(None, "http_404"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result({"Q1": {"sitelinks": {}}}),
    )
    assert report["counts"]["records_without_usable_wikidata_qid"] == 1
    assert report["wikisource_lookup_failures_by_reason"] == {}
    assert report["records_without_usable_wikidata_qid"][0]["title"] == "No QID"


def test_transport_failures_and_invalid_existing_links_are_reported_without_overwrite():
    entry = _entry("Ada", "Q1")
    entry["external_links"]["wikiquote"] = "https://example.invalid/Ada"
    report = audit_external_links(
        {"Ada": entry},
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result({}, "timeout"),
    )
    assert report["wikiquote_lookup_failures_by_reason"] == {}
    assert report["wikisource_lookup_failures_by_reason"] == {"wikidata_timeout": 1}
    assert report["invalid_existing_external_links"][0]["invalid"] == {
        "wikiquote": "https://example.invalid/Ada"
    }
    assert report["conflicts"] == [{
        "title": "Ada", "qid": "Q1", "kind": "wikiquote",
        "current": "https://example.invalid/Ada",
        "observed": "https://en.wikiquote.org/wiki/Ada",
    }]
    assert entry["external_links"]["wikiquote"] == "https://example.invalid/Ada"


def test_wikiquote_transport_and_parse_failures_stay_distinct_in_report():
    transport = _entry("Transport")
    parsed = _entry("Parsed")

    def lookup(title, limiter=None):
        return (None, "timeout" if title == "Transport" else "parsing_error")

    report = audit_external_links(
        {"Transport": transport, "Parsed": parsed},
        wikiquote_lookup=lookup,
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result(),
    )
    assert report["wikiquote_lookup_failures_by_reason"] == {
        "parsing_error": 1, "timeout": 1,
    }


def test_project_gutenberg_is_reported_but_never_proposed_or_touched():
    entry = _entry("Ada")
    entry["external_links"]["project_gutenberg"] = "https://www.gutenberg.org/ebooks/1"
    database = {"Ada": entry}
    report = audit_external_links(
        database,
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result(),
    )
    row = report["changes_or_conflicts"][0]
    assert row["current"]["project_gutenberg"] == "https://www.gutenberg.org/ebooks/1"
    assert row["proposed"]["project_gutenberg"] is None
    assert database["Ada"]["external_links"]["project_gutenberg"] == "https://www.gutenberg.org/ebooks/1"
    assert report["project_gutenberg_existing_values"] == [{
        "title": "Ada", "qid": None,
        "project_gutenberg": "https://www.gutenberg.org/ebooks/1",
    }]


def test_cli_is_audit_only_and_saves_a_report(monkeypatch, tmp_path, capsys):
    database = {"Ada": _entry("Ada")}
    monkeypatch.setattr(enrich_external_links, "load_database", lambda *args: database)
    monkeypatch.setattr(enrich_external_links, "audit_external_links", lambda *args, **kwargs: {
        "total_canonical_records": 1,
        "post_eligible_records": 1,
        "non_post_eligible_records_skipped": 0,
        "counts": {
            "proposed_new_wikiquote_links": 0,
            "proposed_new_wikisource_links": 0,
            "records_receiving_both": 0,
            "records_with_no_proposed_change": 1,
            "invalid_existing_external_link_values": 0,
            "conflicts": 0,
        },
    })

    assert enrich_external_links.main(["--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports")]) == 0
    assert list((tmp_path / "reports").glob("*.json"))
    assert "External-links audit" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        enrich_external_links.parse_args(["--apply"])


def test_non_post_eligible_topic_or_book_is_skipped_without_any_lookup():
    philosopher = _entry("Ada", "Q1")
    topic = _entry("Philosophy", "Q2")
    topic["evaluation"]["status"] = "rejected"
    book = _entry("A Book", "Q3")
    book["posting"]["has_been_posted"] = True
    database = {"Ada": philosopher, "Philosophy": topic, "A Book": book}
    calls = []

    def wikiquote_lookup(title, limiter=None):
        calls.append(("wikiquote", title))
        return "https://en.wikiquote.org/wiki/{}".format(title), None

    def wikidata_lookup(qids, limiter=None):
        calls.append(("wikidata", tuple(qids)))
        return _wikidata_result({"Q1": {"sitelinks": {}}})

    report = audit_external_links(
        database,
        wikiquote_lookup=wikiquote_lookup,
        wikidata_lookup=wikidata_lookup,
    )

    assert report["total_canonical_records"] == 3
    assert report["post_eligible_records"] == 1
    assert report["non_post_eligible_records_skipped"] == 2
    assert report["skipped_non_post_eligible_records"] == [
        {"title": "A Book"}, {"title": "Philosophy"},
    ]
    assert calls == [("wikiquote", "Ada"), ("wikidata", ("Q1",))]
    assert report["changes_or_conflicts"][0]["title"] == "Ada"


def test_genuinely_post_eligible_philosopher_is_audited_normally():
    ada = _entry("Ada", "Q1")
    report = audit_external_links(
        {"Ada": ada},
        wikiquote_lookup=_lookup("https://en.wikiquote.org/wiki/Ada"),
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result({
            "Q1": {"sitelinks": {"enwikisource": {"title": "Author:Ada"}}}
        }),
    )
    assert report["post_eligible_records"] == 1
    assert report["non_post_eligible_records_skipped"] == 0
    assert report["counts"]["proposed_new_wikiquote_links"] == 1
    assert report["counts"]["proposed_new_wikisource_links"] == 1


def test_new_audit_reports_include_positive_evidence_fields():
    report = _reviewed_report({"Ada": _entry("Ada", "Q1")})
    row = report["changes_or_conflicts"][0]
    assert report["audit_schema_version"] == 2
    assert row["evidence"] == {
        "wikiquote": {
            "final_response_url": "https://en.wikiquote.org/wiki/Ada",
            "successful_quote_parse": True,
        },
        "wikisource": {
            "qid": "Q1",
            "enwikisource_sitelink_title": "Author:Ada",
        },
    }


def test_apply_accepts_the_reviewed_pre_evidence_audit_shape():
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    report.pop("audit_schema_version")
    report["changes_or_conflicts"][0].pop("evidence")

    proposal = validate_reviewed_external_links_apply(database, report)

    assert proposal[0]["title"] == "Ada"


def test_successful_apply_only_changes_reviewed_links_and_preserves_gutenberg_and_unrelated_fields(tmp_path):
    ada = _entry("Ada", "Q1")
    del ada["external_links"]
    ada["summary"]["text"] = "Keep this summary."
    other = _entry("Other", "Q2")
    other["evaluation"]["status"] = "rejected"
    database = {"Ada": ada, "Other": other}
    report = _reviewed_report(database)
    before_other = deepcopy(other)
    _write_database(tmp_path, database)
    loaded = load_database("database.jsonl", str(tmp_path))

    result = apply_reviewed_external_links(
        loaded, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )

    assert result["records_updated"] == 1
    assert result["wikiquote_links_written"] == 1
    assert result["wikisource_links_written"] == 1
    assert loaded["Ada"]["external_links"] == {
        "wikiquote": "https://en.wikiquote.org/wiki/Ada",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
    }
    assert "project_gutenberg" not in loaded["Ada"]["external_links"]
    assert loaded["Ada"]["summary"]["text"] == "Keep this summary."
    assert loaded["Other"] == before_other
    assert load_database("database.jsonl", str(tmp_path)) == loaded


def test_apply_does_not_call_network_lookups(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    _write_database(tmp_path, database)
    loaded = load_database("database.jsonl", str(tmp_path))
    monkeypatch.setattr(external_links, "lookup_wikiquote_external_link", lambda *a, **k: pytest.fail("network"))
    monkeypatch.setattr(external_links, "get_wikidata_entities_batch", lambda *a, **k: pytest.fail("network"))

    apply_reviewed_external_links(
        loaded, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )


@pytest.mark.parametrize("mutation, expected", [
    (lambda database: database.pop("Ada"), "no longer exists"),
    (lambda database: database["Ada"].__setitem__("title", "Wrong Ada"), "does not match reviewed title"),
    (lambda database: database["Ada"]["wikidata"].__setitem__("qid", "Q99"), "QID differs"),
    (lambda database: database["Ada"]["evaluation"].__setitem__("status", "rejected"), "no longer post eligible"),
    (lambda database: database["Ada"]["external_links"].__setitem__("wikiquote", "https://en.wikiquote.org/wiki/Already"), "no longer null"),
])
def test_apply_rejects_stale_title_qid_eligibility_or_target(mutation, expected):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    mutation(database)

    with pytest.raises(ExternalLinksApplyValidationError, match=expected):
        validate_reviewed_external_links_apply(database, report)


def test_apply_rejects_conflicting_or_malformed_report_before_any_write(tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    _write_database(tmp_path, database)
    before = (tmp_path / "database.jsonl").read_bytes()
    report["conflicts"] = [{"title": "Ada"}]

    with pytest.raises(ExternalLinksApplyValidationError, match="contains conflicts"):
        apply_reviewed_external_links(
            database, report, "database.jsonl", str(tmp_path), threading.Lock(),
        )
    assert (tmp_path / "database.jsonl").read_bytes() == before

    malformed = _reviewed_report(database)
    malformed["operation"] = "wrong-operation"
    with pytest.raises(ExternalLinksApplyValidationError, match="not an external-links"):
        validate_reviewed_external_links_apply(database, malformed)


def test_apply_write_failure_keeps_in_memory_and_durable_database_unchanged(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    _write_database(tmp_path, database)
    before_memory = deepcopy(database)
    before_bytes = (tmp_path / "database.jsonl").read_bytes()
    monkeypatch.setattr(
        external_links, "rewrite_database",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    with pytest.raises(OSError, match="write failed"):
        apply_reviewed_external_links(
            database, report, "database.jsonl", str(tmp_path), threading.Lock(),
        )
    assert database == before_memory
    assert (tmp_path / "database.jsonl").read_bytes() == before_bytes


def test_apply_is_all_or_nothing_when_one_of_two_reviewed_rows_is_stale(tmp_path):
    ada = _entry("Ada", "Q1")
    beth = _entry("Beth", "Q2")
    database = {"Ada": ada, "Beth": beth}

    def lookup(title, limiter=None):
        return "https://en.wikiquote.org/wiki/{}".format(title), None

    report = audit_external_links(
        database, wikiquote_lookup=lookup,
        wikidata_lookup=lambda qids, limiter=None: _wikidata_result(),
    )
    database["Beth"]["evaluation"]["status"] = "rejected"
    _write_database(tmp_path, database)
    before = (tmp_path / "database.jsonl").read_bytes()

    with pytest.raises(ExternalLinksApplyValidationError, match="no longer post eligible"):
        apply_reviewed_external_links(
            database, report, "database.jsonl", str(tmp_path), threading.Lock(),
        )
    assert (tmp_path / "database.jsonl").read_bytes() == before
    assert database["Ada"]["external_links"]["wikiquote"] is None


def test_second_application_of_same_reviewed_report_fails_safely_as_stale(tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    _write_database(tmp_path, database)
    apply_reviewed_external_links(
        database, report, "database.jsonl", str(tmp_path), threading.Lock(),
    )

    with pytest.raises(ExternalLinksApplyValidationError, match="no longer null"):
        apply_reviewed_external_links(
            database, report, "database.jsonl", str(tmp_path), threading.Lock(),
        )


def test_cli_apply_requires_valid_explicit_report_and_creates_one_backup(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    source_report = tmp_path / "audit.json"
    source_report.write_text(__import__("json").dumps(report), encoding="utf-8")
    _write_database(tmp_path, database)
    backups = []
    monkeypatch.setattr(enrich_external_links, "create_database_backup", lambda **kwargs: backups.append(kwargs) or DatabaseBackupResult(
        path=str(tmp_path / "backup.jsonl"), sha256="a" * 64, size_bytes=1,
    ))

    assert enrich_external_links.main([
        "--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports"),
        "--apply-report", str(source_report),
    ]) == 0
    assert len(backups) == 1
    assert backups[0]["label"] == "before-external-links-enrichment"
    assert load_database("database.jsonl", str(tmp_path))["Ada"]["external_links"]["wikiquote"] is not None


def test_cli_apply_missing_report_fails_without_backup(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    _write_database(tmp_path, database)
    monkeypatch.setattr(enrich_external_links, "create_database_backup", lambda **kwargs: pytest.fail("backup"))

    assert enrich_external_links.main([
        "--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports"),
        "--apply-report", str(tmp_path / "missing.json"),
    ]) == 1


def test_cli_backup_failure_prevents_apply(monkeypatch, tmp_path):
    database = {"Ada": _entry("Ada", "Q1")}
    report = _reviewed_report(database)
    source_report = tmp_path / "audit.json"
    source_report.write_text(__import__("json").dumps(report), encoding="utf-8")
    _write_database(tmp_path, database)
    before = (tmp_path / "database.jsonl").read_bytes()
    monkeypatch.setattr(
        enrich_external_links, "create_database_backup",
        lambda **kwargs: DatabaseBackupResult(error_reason="copy failed"),
    )
    monkeypatch.setattr(enrich_external_links, "apply_reviewed_external_links", lambda *args: pytest.fail("apply"))

    assert enrich_external_links.main([
        "--data-folder", str(tmp_path), "--report-folder", str(tmp_path / "reports"),
        "--apply-report", str(source_report),
    ]) == 1
    assert (tmp_path / "database.jsonl").read_bytes() == before
