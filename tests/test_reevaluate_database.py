import ast
import copy
import json
import threading
from pathlib import Path

import pytest

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.evaluation as evaluation
import wiki_philosopher_bot.main as main
import wiki_philosopher_bot.cli.reevaluate_database as reevaluate_database
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.config import CURRENT_EVALUATION_ALGORITHM_VERSION
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)


def write_canonical_database(tmp_path, entries):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path


@pytest.mark.parametrize(
    "status, algorithm_version, expected",
    [
        ("unprocessed", None, True),
        ("accepted", CURRENT_EVALUATION_ALGORITHM_VERSION, False),
        ("rejected", CURRENT_EVALUATION_ALGORITHM_VERSION, False),
        ("accepted", None, True),
        ("rejected", None, True),
        ("accepted", CURRENT_EVALUATION_ALGORITHM_VERSION + 1, True),
    ],
)
def test_evaluation_needs_processing_uses_canonical_version_policy(
    status,
    algorithm_version,
    expected,
):
    assert evaluation.evaluation_needs_processing(
        {
            "status": status,
            "algorithm_version": algorithm_version,
        }
    ) is expected


def test_process_title_uses_shared_evaluation_eligibility_helper(monkeypatch):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"].update({
        "status": "accepted",
        "algorithm_version": CURRENT_EVALUATION_ALGORITHM_VERSION,
    })
    calls = []

    def fake_eligibility(evaluation_section):
        calls.append(evaluation_section)
        return False

    def filter_should_not_run(*args, **kwargs):
        pytest.fail("current evaluation must still skip all filters")

    monkeypatch.setattr(
        evaluation,
        "evaluation_needs_processing",
        fake_eligibility,
    )
    monkeypatch.setattr(evaluation, "title_filter", filter_should_not_run)

    result = evaluation.process_title(
        {"title": "Ada Lovelace"},
        {"cached_encountered": 0},
        {"Ada Lovelace": entry},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result is None
    assert calls == [entry["evaluation"]]


def make_entry(title, status, algorithm_version):
    entry = make_empty_database_entry(title)
    entry["evaluation"].update({
        "status": status,
        "algorithm_version": algorithm_version,
    })
    return entry


def test_dry_run_reports_sorted_eligible_canonical_titles(tmp_path, capsys):
    entries = [
        make_entry(
            "Current accepted", "accepted",
            CURRENT_EVALUATION_ALGORITHM_VERSION,
        ),
        make_entry("Zulu historical rejected", "rejected", None),
        make_entry("Alpha unprocessed", "unprocessed", None),
        make_entry(
            "Current rejected", "rejected",
            CURRENT_EVALUATION_ALGORITHM_VERSION,
        ),
        make_entry(
            "Bravo other version", "accepted",
            CURRENT_EVALUATION_ALGORITHM_VERSION + 1,
        ),
        make_entry("Charlie historical accepted", "accepted", None),
    ]
    database_path = write_canonical_database(tmp_path, entries)
    before = database_path.read_bytes()

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--dry-run",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    backup = report.pop("backup")
    assert backup["attempted"] is False
    assert report == {
        "mode": "dry-run",
        "total_canonical_entries": 6,
        "eligible": {
            "total": 4,
            "by_status_version": {
                "accepted_none": 1,
                "accepted_other": 1,
                "rejected_none": 1,
                "rejected_other": 0,
                "unprocessed": 1,
            },
        },
        "selected": {
            "count": 4,
            "limit": None,
            "sample_titles": [
                "Alpha unprocessed",
                "Bravo other version",
                "Charlie historical accepted",
                "Zulu historical rejected",
            ],
        },
    }
    assert database_path.read_bytes() == before


def test_dry_run_limit_selects_sorted_prefix_without_writing(tmp_path, capsys):
    entries = [
        make_entry("Zulu", "unprocessed", None),
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    database_path = write_canonical_database(tmp_path, entries)
    before = database_path.read_bytes()

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--limit", "2",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "dry-run"
    assert report["eligible"]["total"] == 3
    assert report["selected"] == {
        "count": 2,
        "limit": 2,
        "sample_titles": ["Alpha", "Bravo"],
    }
    assert database_path.read_bytes() == before


def test_explicit_title_selection_preserves_requested_order(tmp_path, capsys):
    entries = [
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "rejected", None),
        make_entry("Charlie", "accepted", None),
    ]
    write_canonical_database(tmp_path, entries)

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--dry-run",
        "--title", "Charlie", "--title", "Alpha", "--title", "Bravo",
    ]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["selected"]["sample_titles"] == [
        "Charlie", "Alpha", "Bravo",
    ]
    assert report["selected"]["requested_titles"] == [
        {"title": "Charlie", "status": "accepted", "algorithm_version": None},
        {"title": "Alpha", "status": "unprocessed", "algorithm_version": None},
        {"title": "Bravo", "status": "rejected", "algorithm_version": None},
    ]


def test_explicit_title_selection_rejects_missing_title(tmp_path, monkeypatch):
    write_canonical_database(tmp_path, [make_entry("Ada", "unprocessed", None)])
    monkeypatch.setattr(
        reevaluate_database,
        "run_apply",
        lambda *args, **kwargs: pytest.fail("invalid selection must not apply"),
    )

    with pytest.raises(SystemExit, match="does not exist"):
        reevaluate_database.main([
            "--data-folder", str(tmp_path), "--title", "Missing",
        ])


def test_explicit_title_selection_rejects_current_version_terminal(tmp_path):
    write_canonical_database(tmp_path, [
        make_entry(
            "Current", "accepted", CURRENT_EVALUATION_ALGORITHM_VERSION,
        ),
    ])

    with pytest.raises(SystemExit, match="not eligible"):
        reevaluate_database.main([
            "--data-folder", str(tmp_path), "--title", "Current",
        ])


def test_explicit_title_selection_rejects_duplicate_titles(tmp_path):
    write_canonical_database(tmp_path, [make_entry("Ada", "unprocessed", None)])

    with pytest.raises(SystemExit, match="duplicate"):
        reevaluate_database.main([
            "--data-folder", str(tmp_path),
            "--title", "Ada", "--title", "Ada",
        ])


def test_explicit_title_selection_rejects_limit_combination():
    with pytest.raises(SystemExit) as error:
        reevaluate_database.main([
            "--title", "Ada", "--limit", "1",
        ])

    assert error.value.code == 2


def test_explicit_title_dry_run_has_no_network_or_writes(
    monkeypatch,
    tmp_path,
):
    database_path = write_canonical_database(
        tmp_path, [make_entry("Ada", "unprocessed", None)],
    )
    before = database_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("explicit dry-run must not perform runtime work")

    monkeypatch.setattr(wikipedia_api, "safe_request", forbidden)
    monkeypatch.setattr(reevaluate_database, "run_apply", forbidden)
    monkeypatch.setattr(cache, "update_database_entry", forbidden)

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--dry-run", "--title", "Ada",
    ]) == 0
    assert database_path.read_bytes() == before


def test_explicit_title_apply_reuses_existing_sequential_evaluator(
    monkeypatch,
    tmp_path,
    capsys,
):
    entries = [
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)
    patch_entity_lookup_without_network(monkeypatch)
    calls = []

    def fake_process_title(page, *args, **kwargs):
        calls.append(page["title"])
        return flat_result(page["title"])

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--apply",
        "--title", "Bravo", "--title", "Alpha",
    ]) == 0

    assert calls == ["Bravo", "Alpha"]
    report = json.loads(capsys.readouterr().out)
    assert report["selected"] == {"count": 2, "limit": None}


def test_existing_limit_selection_behavior_is_unchanged(tmp_path, capsys):
    entries = [
        make_entry("Zulu", "unprocessed", None),
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--dry-run", "--limit", "2",
    ]) == 0

    assert json.loads(capsys.readouterr().out)["selected"]["sample_titles"] == [
        "Alpha", "Bravo",
    ]


def test_dry_run_does_not_invoke_network_or_runtime_apply_paths(
    monkeypatch,
    tmp_path,
):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    database_path = write_canonical_database(tmp_path, [entry])
    before = database_path.read_bytes()

    def forbidden(*args, **kwargs):
        pytest.fail("dry-run must not enter runtime apply paths")

    monkeypatch.setattr(wikipedia_api, "safe_request", forbidden)
    monkeypatch.setattr(cache, "update_database_entry", forbidden)
    monkeypatch.setattr(main, "build_entity_lookup", forbidden)
    monkeypatch.setattr(main, "evaluate_pages", forbidden)

    assert reevaluate_database.main([
        "--data-folder", str(tmp_path), "--dry-run",
    ]) == 0
    assert database_path.read_bytes() == before


def test_dry_run_missing_or_invalid_canonical_database_fails_visibly(
    tmp_path,
):
    with pytest.raises(FileNotFoundError):
        reevaluate_database.main([
            "--data-folder", str(tmp_path), "--dry-run",
        ])

    (tmp_path / "database.jsonl").write_text("not json\n", encoding="utf-8")

    with pytest.raises(ValueError):
        reevaluate_database.main([
            "--data-folder", str(tmp_path), "--dry-run",
        ])


def test_reevaluation_cli_modes_are_mutually_exclusive():
    with pytest.raises(SystemExit) as error:
        reevaluate_database.main(["--dry-run", "--apply"])

    assert error.value.code == 2


def test_main_has_no_reevaluation_command_dependency():
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    imported_modules = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert "reevaluate_database" not in imported_modules


def flat_result(title, status="accepted"):
    return {
        "title": title,
        "status": status,
        "human_confidence": 2,
        "philosopher_confidence": 3,
        "content_confidence": 1,
        "reasons": [status],
        "last_processed": 123.0,
    }


def patch_entity_lookup_without_network(monkeypatch):
    monkeypatch.setattr(
        reevaluate_database,
        "build_entity_lookup",
        lambda pages, database, limiter: ({}, {}, {}),
    )


def run_apply(tmp_path, capsys, limit=None):
    arguments = ["--data-folder", str(tmp_path), "--apply"]
    if limit is not None:
        arguments.extend(["--limit", str(limit)])

    assert reevaluate_database.main(arguments) == 0
    return json.loads(capsys.readouterr().out)


def test_apply_wikidata_request_failure_leaves_entry_eligible(
    monkeypatch,
    tmp_path,
    capsys,
):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(
        reevaluate_database,
        "build_entity_lookup",
        lambda pages, database, limiter: (
            {},
            {},
            {"Ada Lovelace": "request_exception"},
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(
            human_bonus=1,
            philosopher_bonus=1,
        ),
    )

    report = run_apply(tmp_path, capsys)
    backup = report.pop("backup")
    assert backup["created"] is True
    persisted = cache.load_database("database.jsonl", str(tmp_path))["Ada Lovelace"]

    assert report["results"]["operational_failures"] == 1
    assert report["results"]["accepted"] == 0
    assert report["results"]["rejected"] == 0
    assert report["remaining_eligible"] == 1
    assert persisted["evaluation"]["status"] == "unprocessed"
    assert persisted["evaluation"]["algorithm_version"] is None
    assert persisted["wikidata"]["status"] == "unknown"
    assert persisted["wikidata"]["reason"] is None


def test_apply_persists_current_algorithm_version(monkeypatch, tmp_path, capsys):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )

    run_apply(tmp_path, capsys)

    persisted = cache.load_database("database.jsonl", str(tmp_path))[
        "Ada Lovelace"
    ]["evaluation"]
    assert persisted["status"] == "accepted"
    assert persisted["algorithm_version"] == CURRENT_EVALUATION_ALGORITHM_VERSION


def test_apply_reprocesses_unprocessed_entry(monkeypatch, tmp_path, capsys):
    entry = make_entry("Unprocessed", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    calls = []

    def fake_process_title(page, *args, **kwargs):
        calls.append(page["title"])
        return flat_result(page["title"], status="rejected")

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    report = run_apply(tmp_path, capsys)

    assert calls == ["Unprocessed"]
    assert report["results"]["rejected"] == 1


def test_apply_reprocesses_historical_none_version_entry(
    monkeypatch,
    tmp_path,
    capsys,
):
    entry = make_entry("Historical", "rejected", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )

    run_apply(tmp_path, capsys)

    persisted = cache.load_database("database.jsonl", str(tmp_path))[
        "Historical"
    ]["evaluation"]
    assert persisted["algorithm_version"] == CURRENT_EVALUATION_ALGORITHM_VERSION


def test_apply_skips_current_version_terminal_entry(monkeypatch, tmp_path, capsys):
    entry = make_entry(
        "Current", "accepted", CURRENT_EVALUATION_ALGORITHM_VERSION,
    )
    database_path = write_canonical_database(tmp_path, [entry])
    before = database_path.read_bytes()
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda *args, **kwargs: pytest.fail("current entry must not run"),
    )

    report = run_apply(tmp_path, capsys)

    assert report["selected"]["count"] == 0
    assert report["remaining_eligible"] == 0
    assert database_path.read_bytes() == before


def test_apply_limit_uses_sorted_titles(monkeypatch, tmp_path, capsys):
    entries = [
        make_entry("Zulu", "unprocessed", None),
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)
    patch_entity_lookup_without_network(monkeypatch)
    calls = []

    def fake_process_title(page, *args, **kwargs):
        calls.append(page["title"])
        return flat_result(page["title"])

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    report = run_apply(tmp_path, capsys, limit=2)

    assert calls == ["Alpha", "Bravo"]
    assert report["selected"] == {"count": 2, "limit": 2}
    assert report["remaining_eligible"] == 1


def test_apply_rerun_skips_completed_entry(monkeypatch, tmp_path, capsys):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    calls = []

    def fake_process_title(page, *args, **kwargs):
        calls.append(page["title"])
        return flat_result(page["title"])

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    first = run_apply(tmp_path, capsys)
    second = run_apply(tmp_path, capsys)

    assert calls == ["Ada Lovelace"]
    assert first["remaining_eligible"] == 0
    assert second["selected"]["count"] == 0


def test_apply_preserves_legacy_result_migration_and_non_evaluation_sections(
    monkeypatch,
    tmp_path,
    capsys,
):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    entry["evaluation"]["legacy_result"] = {"old": "result"}
    entry["migration"]["conflicts"] = [{
        "field": "evaluation.status",
        "values": [],
        "resolution": "unprocessed",
    }]
    entry["summary"]["text"] = "Saved summary"
    entry["wikidata"]["status"] = "unavailable"
    entry["wikidata"]["reason"] = "no_qid"
    entry["quotes"]["status"] = "available"
    entry["quotes"]["items"] = [{
        "text": "Quote",
        "length": 5,
        "word_count": 1,
        "source": "Wikiquote",
    }]
    entry["posting"]["has_been_posted"] = True
    before_non_evaluation = {
        key: copy.deepcopy(entry[key])
        for key in ("summary", "wikidata", "quotes", "posting", "migration")
    }
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )

    run_apply(tmp_path, capsys)

    persisted = cache.load_database("database.jsonl", str(tmp_path))["Ada Lovelace"]
    assert persisted["evaluation"]["legacy_result"] == {"old": "result"}
    for key, expected in before_non_evaluation.items():
        assert persisted[key] == expected


def test_apply_guaranteed_rejection_does_not_fetch_quotes(
    monkeypatch,
    tmp_path,
    capsys,
):
    entry = make_entry("Known non-philosopher", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: evaluation.FilterResult(
            philosopher_bonus=1,
            reasons=["title reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "get_quotes",
        lambda *args, **kwargs: pytest.fail("quote fetch must be skipped"),
    )

    report = run_apply(tmp_path, capsys)

    assert report["results"]["rejected"] == 1


def test_apply_process_exception_is_reported_and_later_title_continues(
    monkeypatch,
    tmp_path,
    capsys,
):
    entries = [
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)
    patch_entity_lookup_without_network(monkeypatch)

    def fake_process_title(page, *args, **kwargs):
        if page["title"] == "Alpha":
            raise RuntimeError("temporary processing failure")
        return flat_result(page["title"])

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    report = run_apply(tmp_path, capsys)

    assert report["results"]["accepted"] == 1
    assert report["results"]["operational_failures"] == 1
    assert report["results"]["errors"] == [{
        "title": "Alpha",
        "type": "RuntimeError",
        "message": "temporary processing failure",
    }]
    persisted = cache.load_database("database.jsonl", str(tmp_path))
    assert persisted["Alpha"]["evaluation"]["status"] == "unprocessed"
    assert persisted["Bravo"]["evaluation"]["algorithm_version"] == CURRENT_EVALUATION_ALGORITHM_VERSION


def test_apply_persistence_oserror_is_reported_and_later_title_continues(
    monkeypatch,
    tmp_path,
    capsys,
):
    entries = [
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )
    original_persist = reevaluate_database.persist_canonical_evaluation

    def fail_alpha(result, *args, **kwargs):
        if result["title"] == "Alpha":
            raise OSError("disk full")
        return original_persist(result, *args, **kwargs)

    monkeypatch.setattr(
        reevaluate_database,
        "persist_canonical_evaluation",
        fail_alpha,
    )

    report = run_apply(tmp_path, capsys)

    assert report["results"]["accepted"] == 1
    assert report["results"]["operational_failures"] == 1
    persisted = cache.load_database("database.jsonl", str(tmp_path))
    assert persisted["Alpha"]["evaluation"]["status"] == "unprocessed"
    assert persisted["Bravo"]["evaluation"]["algorithm_version"] == CURRENT_EVALUATION_ALGORITHM_VERSION


def test_apply_value_error_propagates(monkeypatch, tmp_path, capsys):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )
    monkeypatch.setattr(
        reevaluate_database,
        "persist_canonical_evaluation",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ValueError("evaluation contract bug")
        ),
    )

    with pytest.raises(ValueError, match="evaluation contract bug"):
        run_apply(tmp_path, capsys)


def test_apply_keyboard_interrupt_propagates(monkeypatch, tmp_path, capsys):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        run_apply(tmp_path, capsys)


def test_apply_does_not_touch_legacy_files(monkeypatch, tmp_path, capsys):
    entry = make_entry("Ada Lovelace", "unprocessed", None)
    write_canonical_database(tmp_path, [entry])
    legacy_files = {
        "summaries.jsonl": b"summary sentinel\n",
        "entities.jsonl": b"entity sentinel\n",
        "quotes.jsonl": b"quote sentinel\n",
        "quote_failures.jsonl": b"failure sentinel\n",
        "results.jsonl": b"result sentinel\n",
        "processed.jsonl": b"processed sentinel\n",
        "posted.json": b"posted sentinel\n",
    }
    for filename, contents in legacy_files.items():
        (tmp_path / filename).write_bytes(contents)
    before = {
        filename: (tmp_path / filename).read_bytes()
        for filename in legacy_files
    }
    patch_entity_lookup_without_network(monkeypatch)
    monkeypatch.setattr(
        reevaluate_database,
        "process_title",
        lambda page, *args, **kwargs: flat_result(page["title"]),
    )

    run_apply(tmp_path, capsys)

    assert {
        filename: (tmp_path / filename).read_bytes()
        for filename in legacy_files
    } == before


def test_apply_report_contains_counts_and_remaining_eligible(
    monkeypatch,
    tmp_path,
    capsys,
):
    entries = [
        make_entry("Alpha", "unprocessed", None),
        make_entry("Bravo", "unprocessed", None),
    ]
    write_canonical_database(tmp_path, entries)
    patch_entity_lookup_without_network(monkeypatch)

    def fake_process_title(page, *args, **kwargs):
        if page["title"] == "Alpha":
            raise RuntimeError("temporary failure")
        return flat_result(page["title"], status="rejected")

    monkeypatch.setattr(reevaluate_database, "process_title", fake_process_title)

    report = run_apply(tmp_path, capsys)
    backup = report.pop("backup")
    assert backup["created"] is True

    assert report == {
        "mode": "apply",
        "total_canonical_entries": 2,
        "eligible_before": 2,
        "selected": {"count": 2, "limit": None},
        "results": {
            "accepted": 0,
            "rejected": 1,
            "no_result": 0,
            "operational_failures": 1,
            "errors": [{
                "title": "Alpha",
                "type": "RuntimeError",
                "message": "temporary failure",
            }],
        },
        "remaining_eligible": 1,
    }
