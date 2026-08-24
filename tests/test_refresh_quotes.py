import json

import pytest

import wiki_philosopher_bot.cli.refresh_quotes as refresh_quotes
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.database_schema import make_empty_database_entry, serialize_database_entries


def write_database(tmp_path, entries):
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries(entries))


def stale_entry(title, status="accepted", posted=False, items=None):
    entry = make_empty_database_entry(title)
    entry["evaluation"]["status"] = status
    entry["evaluation"]["algorithm_version"] = 2
    entry["posting"]["has_been_posted"] = posted
    if posted:
        entry["posting"]["posted_at"] = [1]
    entry["quotes"].update({
        "status": "available",
        "items": items if items is not None else [{
            "text": "A sufficiently long historical quote for refresh testing.",
            "length": 57,
            "word_count": 9,
            "source": "Wikiquote",
        }],
        "parser_version": None,
    })
    return entry


def current_quote(text="Current quote"):
    return {
        "text": text,
        "length": len(text),
        "word_count": len(text.split()),
        "source": {
            "work": "A Current Work",
            "year": 1921,
            "date": None,
            "details": None,
            "citation": "A Current Work (1921)",
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }


def current_entry(title, status="accepted", posted=False, quote_status="available"):
    entry = stale_entry(title, status=status, posted=posted, items=[current_quote()])
    entry["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
    entry["quotes"]["status"] = quote_status
    if quote_status != "available":
        entry["quotes"]["items"] = []
    return entry


def test_current_quote_parser_version_is_v10():
    assert CURRENT_QUOTE_PARSER_VERSION == 10


def test_quote_refresh_eligibility_requires_stale_accepted_unposted_available():
    eligible = stale_entry("Eligible")
    assert refresh_quotes.quote_refresh_needs_processing(eligible) is True

    for entry in (
        stale_entry("Rejected", status="rejected"),
        stale_entry("Posted", posted=True),
        stale_entry("Empty", items=[]),
        stale_entry("Current"),
    ):
        if entry["title"] == "Current":
            entry["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
        assert refresh_quotes.quote_refresh_needs_processing(entry) is False


def test_parser_v8_cache_is_stale_and_refresh_eligible_after_v9_rollover():
    entry = stale_entry("Historical v8", items=[current_quote("Historical quote")])
    entry["quotes"]["parser_version"] = 8

    assert refresh_quotes.quote_refresh_needs_processing(entry) is True


def test_failed_refresh_preserves_historical_parser_v8_and_eligibility(monkeypatch, tmp_path):
    entry = stale_entry("Historical v8", items=[current_quote("Historical quote")])
    entry["quotes"]["parser_version"] = 8
    database = {"Historical v8": entry}

    def failed_refresh(title, database, *args, **kwargs):
        database[title]["quotes"]["failure"] = {
            "reason": "request_exception", "timestamp": 1, "retries": 1,
        }
        return []

    monkeypatch.setattr(refresh_quotes, "get_quotes", failed_refresh)
    refresh_quotes.run_apply(database, ["Historical v8"], str(tmp_path))

    assert database["Historical v8"]["quotes"]["parser_version"] == 8
    assert refresh_quotes.quote_refresh_needs_processing(database["Historical v8"]) is True


def test_explicit_quote_refresh_selection_preserves_order_and_rejects_invalid():
    first = stale_entry("First")
    second = stale_entry("Second")
    current = stale_entry("Current")
    current["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
    database = {entry["title"]: entry for entry in (first, second, current)}

    assert refresh_quotes.select_explicit_titles(database, ["Second", "First"]) == [
        "Second", "First",
    ]

    with pytest.raises(ValueError, match="duplicate"):
        refresh_quotes.select_explicit_titles(database, ["First", "First"])
    with pytest.raises(ValueError, match="does not exist"):
        refresh_quotes.select_explicit_titles(database, ["Missing"])
    with pytest.raises(ValueError, match="not eligible"):
        refresh_quotes.select_explicit_titles(database, ["Current"])


def test_repair_current_selection_is_explicit_only_and_preserves_requested_order():
    titles = [
        "Alvin Plantinga", "Andrew Collier (philosopher)",
        "R. G. Collingwood", "Plato", "Albert Camus",
    ]
    database = {title: current_entry(title) for title in titles}

    with pytest.raises(ValueError, match="not eligible"):
        refresh_quotes.select_explicit_titles(database, titles)
    assert refresh_quotes.select_explicit_titles(
        database, list(reversed(titles)), repair_current=True,
    ) == list(reversed(titles))


def test_repair_current_eligibility_excludes_posted_rejected_and_non_available():
    current = current_entry("Current")
    posted = current_entry("Posted", posted=True)
    rejected = current_entry("Rejected", status="rejected")
    failed = current_entry("Failed", quote_status="failed")

    assert refresh_quotes.current_quote_repair_needs_processing(current) is True
    for entry in (posted, rejected, failed):
        assert refresh_quotes.current_quote_repair_needs_processing(entry) is False


def test_repair_current_parse_args_requires_titles_and_rejects_limit():
    with pytest.raises(SystemExit):
        refresh_quotes.parse_args(["--repair-current"])
    with pytest.raises(SystemExit):
        refresh_quotes.parse_args([
            "--repair-current", "--title", "Plato", "--limit", "1",
        ])


def test_repair_current_dry_run_is_read_only_and_marks_report(monkeypatch, tmp_path):
    entries = [current_entry("Plato"), current_entry("Albert Camus")]
    write_database(tmp_path, entries)
    before = (tmp_path / "database.jsonl").read_bytes()
    report_directory = tmp_path / "reports/quote-refresh"
    monkeypatch.setattr(refresh_quotes, "REFRESH_REPORTS_DIRECTORY", report_directory)
    monkeypatch.setattr(
        refresh_quotes, "get_quotes",
        lambda *args, **kwargs: pytest.fail("dry run must not fetch quotes"),
    )

    assert refresh_quotes.main([
        "--data-folder", str(tmp_path), "--dry-run", "--repair-current",
        "--title", "Albert Camus", "--title", "Plato",
    ]) == 0
    assert (tmp_path / "database.jsonl").read_bytes() == before
    report = json.loads(next(report_directory.glob("*.json")).read_text())
    assert report["repair_current"] is True
    assert report["selected"]["titles"] == ["Albert Camus", "Plato"]


def test_successful_current_repair_replaces_items_without_changing_other_sections(monkeypatch, tmp_path):
    entry = current_entry("Plato")
    preserved = {key: entry[key] for key in (
        "summary", "wikidata", "evaluation", "posting", "migration",
    )}
    database = {"Plato": entry}

    def refresh(title, database, *args, **kwargs):
        assert kwargs["refresh_stale"] is True
        assert kwargs["refresh_current"] is True
        database[title]["quotes"].update({
            "status": "available", "items": [current_quote("Repaired quote")],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "failure": None, "fetched_at": 123,
        })

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)
    results = refresh_quotes.run_apply(
        database, ["Plato"], str(tmp_path), repair_current=True,
    )

    assert results["refreshed_current"] == 1
    assert database["Plato"]["quotes"]["items"][0]["text"] == "Repaired quote"
    assert database["Plato"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    for key, value in preserved.items():
        assert database["Plato"][key] is value


def test_failed_current_repair_preserves_current_cache_and_reports_failure(monkeypatch, tmp_path):
    entry = current_entry("Plato")
    old_items = list(entry["quotes"]["items"])
    database = {"Plato": entry}

    def failed_refresh(title, database, *args, **kwargs):
        database[title]["quotes"]["failure"] = {
            "reason": "request_exception", "timestamp": 1, "retries": 1,
        }
        return []

    monkeypatch.setattr(refresh_quotes, "get_quotes", failed_refresh)
    results = refresh_quotes.run_apply(
        database, ["Plato"], str(tmp_path), repair_current=True,
    )

    assert results["refreshed_current"] == 0
    assert results["operational_failures"] == 1
    assert database["Plato"]["quotes"]["status"] == "available"
    assert database["Plato"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    assert database["Plato"]["quotes"]["items"] == old_items


def test_quote_refresh_dry_run_is_read_only_and_sorted(monkeypatch, tmp_path):
    entries = [stale_entry("Zulu"), stale_entry("Alpha"), stale_entry("Rejected", "rejected")]
    write_database(tmp_path, entries)
    before = (tmp_path / "database.jsonl").read_bytes()

    monkeypatch.setattr(
        refresh_quotes,
        "get_quotes",
        lambda *args, **kwargs: pytest.fail("dry run must not fetch quotes"),
    )
    report_directory = tmp_path / "reports/quote-refresh"
    monkeypatch.setattr(refresh_quotes, "REFRESH_REPORTS_DIRECTORY", report_directory)

    assert refresh_quotes.main(["--data-folder", str(tmp_path), "--dry-run"]) == 0
    assert (tmp_path / "database.jsonl").read_bytes() == before
    paths = list(report_directory.glob("*.json"))
    assert len(paths) == 1
    report = json.loads(paths[0].read_text(encoding="utf-8"))
    assert report["mode"] == "dry-run"
    assert report["selected"]["titles"] == ["Alpha", "Zulu"]


def test_quote_refresh_apply_records_current_and_resumes(monkeypatch, tmp_path):
    entry = stale_entry("Ada")
    database = {"Ada": entry}

    def refresh(title, database, *args, **kwargs):
        assert kwargs["refresh_stale"] is True
        database[title]["quotes"].update({
            "status": "available",
            "items": [current_quote()],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "failure": None,
            "fetched_at": 123,
        })
        return database[title]["quotes"]["items"]

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)
    results = refresh_quotes.run_apply(database, ["Ada"], str(tmp_path))

    assert results["refreshed_current"] == 1
    assert results["became_not_found"] == 0
    assert results["operational_failures"] == 0
    assert results["titles"] == [{
        "title": "Ada",
        "status": "available",
        "old_parser_version": None,
        "new_parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "old_quote_count": 1,
        "new_quote_count": 1,
        "quotes_with_source": 1,
        "quotes_with_work": 1,
        "quotes_with_year": 1,
    }]
    assert refresh_quotes.select_eligible_titles(database) == []


def test_successful_refresh_upgrades_historical_parser_v4_to_current(monkeypatch, tmp_path):
    entry = stale_entry("Historical v4", items=[current_quote("Historical quote")])
    entry["quotes"]["parser_version"] = 4
    database = {"Historical v4": entry}

    def refresh(title, database, *args, **kwargs):
        database[title]["quotes"].update({
            "status": "available",
            "items": [current_quote("Refreshed quote")],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "failure": None,
            "fetched_at": 123,
        })

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)
    refresh_quotes.run_apply(database, ["Historical v4"], str(tmp_path))

    assert database["Historical v4"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    assert refresh_quotes.quote_refresh_needs_processing(database["Historical v4"]) is False


def test_priestley_v5_refresh_writes_v6_hierarchical_source_semantics(monkeypatch, tmp_path):
    citation = (
        "Vol. I: Part I: The Being and Attributes of God, § 1: Of the "
        "existence of God, and those attributes which art deduced from his "
        "being considered as uncaused himself, and the cause of every thing "
        "else (1772)"
    )
    entry = stale_entry("Joseph Priestley", items=[current_quote("Old quote")])
    entry["quotes"]["parser_version"] = 5
    database = {"Joseph Priestley": entry}

    def refresh(title, database, *args, **kwargs):
        database[title]["quotes"].update({
            "status": "available",
            "items": [{
                "text": "A refreshed Priestley quote.",
                "length": 28,
                "word_count": 5,
                "source": {
                    "work": "Institutes of Natural and Revealed Religion",
                    "year": 1772,
                    "date": None,
                    "details": "Vol. I, Part I, § 1",
                    "citation": citation,
                    "url": "https://en.wikiquote.org/wiki/Institutes_of_Natural_and_Revealed_Religion",
                },
                "retrieved_from": "Wikiquote",
            }],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "failure": None,
            "fetched_at": 123,
        })

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)
    refresh_quotes.run_apply(database, ["Joseph Priestley"], str(tmp_path))

    source = database["Joseph Priestley"]["quotes"]["items"][0]["source"]
    assert database["Joseph Priestley"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    assert source["work"] == "Institutes of Natural and Revealed Religion"
    assert source["year"] == 1772
    assert source["details"] == "Vol. I, Part I, § 1"
    assert source["citation"] == citation
    assert source["url"] == "https://en.wikiquote.org/wiki/Institutes_of_Natural_and_Revealed_Religion"


def test_quote_refresh_apply_preserves_failed_stale_cache_and_reports_failure(monkeypatch, tmp_path):
    entry = stale_entry("Ada")
    old_items = entry["quotes"]["items"][:]
    database = {"Ada": entry}

    def failed_refresh(title, database, *args, **kwargs):
        database[title]["quotes"]["failure"] = {
            "reason": "request_exception", "timestamp": 1, "retries": 1,
        }
        return []

    monkeypatch.setattr(refresh_quotes, "get_quotes", failed_refresh)
    results = refresh_quotes.run_apply(database, ["Ada"], str(tmp_path))

    assert results["operational_failures"] == 1
    assert database["Ada"]["quotes"]["items"] == old_items
    assert database["Ada"]["quotes"]["status"] == "available"
    assert database["Ada"]["quotes"]["parser_version"] is None
    assert refresh_quotes.select_eligible_titles(database) == ["Ada"]


def test_quote_refresh_apply_reports_not_found_without_touching_other_sections(monkeypatch, tmp_path):
    entry = stale_entry("Ada")
    preserved = {
        key: entry[key]
        for key in ("summary", "wikidata", "evaluation", "posting", "migration")
    }
    database = {"Ada": entry}

    def no_quotes(title, database, *args, **kwargs):
        database[title]["quotes"].update({
            "status": "not_found", "items": [],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "fetched_at": 123,
        })
        return []

    monkeypatch.setattr(refresh_quotes, "get_quotes", no_quotes)
    results = refresh_quotes.run_apply(database, ["Ada"], str(tmp_path))

    assert results["became_not_found"] == 1
    for key, value in preserved.items():
        assert database["Ada"][key] is value


def test_quote_refresh_parse_args_rejects_limit_with_explicit_title():
    with pytest.raises(SystemExit):
        refresh_quotes.parse_args(["--limit", "1", "--title", "Ada"])


def test_quote_refresh_apply_saves_ordered_complete_report(monkeypatch, tmp_path, capsys):
    entry = stale_entry("Ada")
    write_database(tmp_path, [entry])
    report_directory = tmp_path / "reports/quote-refresh"
    monkeypatch.setattr(refresh_quotes, "REFRESH_REPORTS_DIRECTORY", report_directory)
    times = iter((100.0, 103.5))
    monkeypatch.setattr(refresh_quotes.time, "time", lambda: next(times))

    def refresh(title, database, *args, **kwargs):
        database[title]["quotes"].update({
            "status": "available",
            "items": [current_quote("Refreshed quote")],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            "failure": None,
            "fetched_at": 123,
        })

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)

    assert refresh_quotes.main(["--data-folder", str(tmp_path), "--apply"]) == 0
    paths = list(report_directory.glob("*.json"))
    assert len(paths) == 1
    pairs = json.loads(
        paths[0].read_text(encoding="utf-8"),
        object_pairs_hook=lambda items: items,
    )
    as_mapping = dict(pairs)
    assert [key for key, _ in pairs] == [
        "started_at", "finished_at", "duration_seconds", "mode", "repair_current",
        "total_canonical_entries", "eligible_before", "selected", "results",
        "remaining_eligible", "backup",
    ]
    assert dict(as_mapping["backup"])["created"] is True
    result_pairs = as_mapping["results"]
    assert [key for key, _ in result_pairs][-1] == "titles"
    title_pairs = dict(result_pairs)["titles"][0]
    assert [key for key, _ in title_pairs] == [
        "title", "status", "old_parser_version", "new_parser_version",
        "old_quote_count", "new_quote_count", "quotes_with_source",
        "quotes_with_work", "quotes_with_year",
    ]
    stdout = capsys.readouterr().out
    saved = json.loads(paths[0].read_text(encoding="utf-8"))
    assert json.dumps(saved, ensure_ascii=False, indent=2) in stdout
    assert "Saved refresh report:" in stdout


def test_report_write_failure_does_not_roll_back_successful_refresh(monkeypatch, tmp_path, capsys):
    entry = stale_entry("Ada")
    database = {"Ada": entry}
    monkeypatch.setattr(refresh_quotes, "load_database", lambda *args: database)

    def refresh(title, database, *args, **kwargs):
        database[title]["quotes"].update({
            "status": "available",
            "items": [current_quote()],
            "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        })

    monkeypatch.setattr(refresh_quotes, "get_quotes", refresh)
    monkeypatch.setattr(
        refresh_quotes,
        "save_refresh_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert refresh_quotes.main(["--data-folder", str(tmp_path), "--apply"]) == 0
    assert database["Ada"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    assert "Warning: refresh report could not be saved: disk full" in capsys.readouterr().out


def test_refresh_report_keyboard_interrupt_is_not_swallowed(monkeypatch, tmp_path):
    entry = stale_entry("Ada")
    write_database(tmp_path, [entry])
    monkeypatch.setattr(
        refresh_quotes,
        "save_refresh_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        refresh_quotes.main(["--data-folder", str(tmp_path), "--dry-run"])
