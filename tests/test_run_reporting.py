import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from database_schema import make_empty_database_entry
from database_schema import serialize_database_entries
from run_reporting import (
    build_run_report,
    capture_run_baseline,
    format_run_summary,
    save_purge_report,
    save_recent_death_report,
    save_refresh_report,
    save_wikidata_date_refresh_report,
    save_run_report,
)


def make_entry(title, status="unprocessed", version=None, posted=False):
    entry = make_empty_database_entry(title)
    entry["evaluation"].update({
        "status": status,
        "algorithm_version": version,
    })
    entry["posting"]["has_been_posted"] = posted
    return entry


def test_run_report_tracks_new_titles_evaluations_posting_stats_and_errors():
    before = {
        "Old": make_entry("Old", "unprocessed"),
        "Posted": make_entry("Posted", "accepted", 2),
    }
    baseline = capture_run_baseline(before)
    after = {
        "Old": make_entry("Old", "rejected", 2),
        "Posted": make_entry("Posted", "accepted", 2, posted=True),
        "New accepted": make_entry("New accepted", "accepted", 2),
        "New rejected": make_entry("New rejected", "rejected", 2),
    }
    after["Old"]["evaluation"].update({
        "philosopher_confidence": 0,
        "human_confidence": 3,
        "content_confidence": 0,
    })
    after["New accepted"]["evaluation"].update({
        "philosopher_confidence": 4,
        "human_confidence": 3,
        "content_confidence": 2,
    })
    after["New rejected"]["evaluation"].update({
        "philosopher_confidence": 0,
        "human_confidence": -1,
        "content_confidence": 0,
    })

    report = build_run_report(
        baseline,
        after,
        {"cached_summaries": 2, "new_accepted": 1},
        started_at=100.0,
        finished_at=103.25,
        selected_posting_title="Posted",
        telegram_result={"ok": True, "error_reason": None},
        processing_errors=[{"title": "Old", "error": "temporary"}],
    )

    assert report["entries"] == {"before": 2, "after": 4}
    assert report["new_titles"] == {
        "count": 2,
        "titles": ["New accepted", "New rejected"],
        "outcomes": {"accepted": 1, "rejected": 1, "unprocessed": 0},
    }
    assert report["newly_evaluated"] == {
        "count": 3,
        "titles": ["New accepted", "New rejected", "Old"],
        "outcomes": {"accepted": 1, "rejected": 2, "unprocessed": 0},
    }
    assert report["evaluated_titles"] == [
        {
            "title": "New accepted",
            "status": "accepted",
            "algorithm_version": 2,
            "philosopher_confidence": 4,
            "human_confidence": 3,
            "content_confidence": 2,
        },
        {
            "title": "New rejected",
            "status": "rejected",
            "algorithm_version": 2,
            "philosopher_confidence": 0,
            "human_confidence": -1,
            "content_confidence": 0,
        },
        {
            "title": "Old",
            "status": "rejected",
            "algorithm_version": 2,
            "philosopher_confidence": 0,
            "human_confidence": 3,
            "content_confidence": 0,
        },
    ]
    assert report["posting"] == {
        "selected_title": "Posted",
        "telegram": {"ok": True, "error_reason": None},
    }
    assert report["stats"] == {"cached_summaries": 2, "new_accepted": 1}
    assert report["processing_errors"] == [{"title": "Old", "error": "temporary"}]
    assert report["duration_seconds"] == 3.25


def test_run_report_excludes_failed_or_no_result_titles_from_durable_decisions():
    before = {
        "Durable": make_entry("Durable", "unprocessed"),
        "Failed": make_entry("Failed", "unprocessed"),
        "No result": make_entry("No result", "unprocessed"),
    }
    after = {
        "Durable": make_entry("Durable", "accepted", 2),
        "Failed": make_entry("Failed", "unprocessed"),
        "No result": make_entry("No result", "unprocessed"),
    }

    report = build_run_report(
        capture_run_baseline(before), after, {}, 100.0, 101.0,
        processing_errors=[{"title": "Failed", "error": "OSError: disk full"}],
    )

    assert report["evaluated_titles"] == [{
        "title": "Durable",
        "status": "accepted",
        "algorithm_version": 2,
        "philosopher_confidence": None,
        "human_confidence": None,
        "content_confidence": None,
    }]
    assert report["newly_evaluated"] == {
        "count": 1,
        "titles": ["Durable"],
        "outcomes": {"accepted": 1, "rejected": 0, "unprocessed": 0},
    }


def test_run_report_json_uses_human_readable_key_order(tmp_path):
    before = {"Zeno": make_entry("Zeno", "unprocessed")}
    after = {"Zeno": make_entry("Zeno", "accepted", 2)}
    report = build_run_report(
        capture_run_baseline(before), after, {}, 100.0, 101.0,
    )
    path, _ = save_run_report(report, tmp_path, started_at=100.0)

    pairs = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=lambda items: items,
    )
    report_pairs = dict(pairs)
    evaluated_record_pairs = report_pairs["evaluated_titles"][0]

    assert [key for key, _ in pairs] == [
        "started_at",
        "finished_at",
        "duration_seconds",
        "entries",
        "new_titles",
        "newly_evaluated",
        "posting",
        "stats",
        "processing_errors",
        "runtime_error",
        "evaluated_titles",
    ]
    assert [key for key, _ in evaluated_record_pairs] == [
        "title",
        "status",
        "algorithm_version",
        "philosopher_confidence",
        "human_confidence",
        "content_confidence",
    ]


def test_evaluated_title_records_are_sorted_without_changing_aggregates():
    before = {
        "Zulu": make_entry("Zulu", "unprocessed"),
        "Alpha": make_entry("Alpha", "unprocessed"),
    }
    after = {
        "Zulu": make_entry("Zulu", "rejected", 2),
        "Alpha": make_entry("Alpha", "accepted", 2),
    }

    report = build_run_report(
        capture_run_baseline(before), after, {}, 100.0, 101.0,
    )

    assert [record["title"] for record in report["evaluated_titles"]] == [
        "Alpha", "Zulu"
    ]
    assert report["newly_evaluated"]["outcomes"] == {
        "accepted": 1,
        "rejected": 1,
        "unprocessed": 0,
    }


def test_run_report_ordering_does_not_change_canonical_serialization():
    entry = make_entry("Ada", "accepted", 2)
    serialized = serialize_database_entries([entry])

    assert b'"evaluation":{"status":"accepted","algorithm_version":2,' in serialized


def test_run_report_baseline_is_minimal_and_input_is_not_mutated():
    database = {"Ada": make_entry("Ada", "accepted", 2, posted=True)}
    original = json.loads(json.dumps(database))

    baseline = capture_run_baseline(database)

    assert baseline == {
        "titles": {"Ada"},
        "evaluation": {"Ada": {"status": "accepted", "algorithm_version": 2}},
        "posting": {"Ada": True},
    }
    assert database == original


def test_save_run_report_is_atomic_and_prunes_only_old_report_names(tmp_path):
    reports = tmp_path / "reports/runs"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    unrelated = reports / "keep-me.json"
    unrelated.write_text("{}", encoding="utf-8")
    report = {"finished_at": "2026-08-20T12:00:03Z", "value": 1}

    path, diagnostics = save_run_report(
        report,
        reports,
        started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.name == "1970-01-01T00-01-40.json"
    assert json.loads(path.read_text(encoding="utf-8")) == report
    assert diagnostics == []
    assert not old_report.exists()
    assert unrelated.exists()
    assert not list(reports.glob(".*.tmp"))


def test_save_run_report_refuses_to_overwrite_existing_report(tmp_path):
    reports = tmp_path / "reports/runs"
    reports.mkdir(parents=True)
    destination = reports / "1970-01-01T00-01-40.json"
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError):
        save_run_report({"value": 1}, reports, started_at=100.0)

    assert destination.read_text(encoding="utf-8") == "existing"


def test_save_run_report_does_not_touch_canonical_or_legacy_files(tmp_path):
    canonical = tmp_path / "database.jsonl"
    legacy = tmp_path / "posted.json"
    canonical.write_bytes(b"canonical sentinel\n")
    legacy.write_bytes(b"legacy sentinel\n")
    before = {path: path.read_bytes() for path in (canonical, legacy)}

    save_run_report({"value": 1}, tmp_path / "reports/runs", started_at=100.0)

    assert {path: path.read_bytes() for path in before} == before


def test_save_refresh_report_is_atomic_and_prunes_only_refresh_report_names(tmp_path):
    reports = tmp_path / "reports/quote-refresh"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    unrelated_json = reports / "notes.json"
    unrelated_json.write_text("keep", encoding="utf-8")
    unrelated_text = reports / "2025-01-01T00-00-00.txt"
    unrelated_text.write_text("keep", encoding="utf-8")

    path, diagnostics = save_refresh_report(
        {"mode": "dry-run", "selected": {"titles": ["Ada"]}},
        reports,
        started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.name == "1970-01-01T00-01-40.json"
    assert diagnostics == []
    assert not old_report.exists()
    assert unrelated_json.read_text(encoding="utf-8") == "keep"
    assert unrelated_text.read_text(encoding="utf-8") == "keep"
    assert not list(reports.glob(".refresh-report-*.tmp"))


def test_save_refresh_report_refuses_collision_without_overwriting(tmp_path):
    reports = tmp_path / "reports/quote-refresh"
    reports.mkdir(parents=True)
    destination = reports / "1970-01-01T00-01-40.json"
    destination.write_text("existing", encoding="utf-8")

    with pytest.raises(FileExistsError, match="Refresh report already exists"):
        save_refresh_report({"value": 1}, reports, started_at=100.0)

    assert destination.read_text(encoding="utf-8") == "existing"


def test_save_refresh_report_does_not_touch_canonical_or_legacy_files(tmp_path):
    canonical = tmp_path / "database.jsonl"
    legacy = tmp_path / "posted.json"
    canonical.write_bytes(b"canonical sentinel\n")
    legacy.write_bytes(b"legacy sentinel\n")
    before = {path: path.read_bytes() for path in (canonical, legacy)}

    save_refresh_report({"mode": "dry-run"}, tmp_path / "reports/quote-refresh", 100.0)

    assert {path: path.read_bytes() for path in before} == before


def test_save_purge_report_is_atomic_and_keeps_unrelated_files(tmp_path):
    reports = tmp_path / "reports/purge"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    unrelated = reports / "notes.json"
    unrelated.write_text("keep", encoding="utf-8")

    path, diagnostics = save_purge_report(
        {"mode": "dry-run"}, reports, started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.name == "1970-01-01T00-01-40.json"
    assert diagnostics == []
    assert not old_report.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not list(reports.glob(".purge-report-*.tmp"))


def test_save_wikidata_date_refresh_report_is_atomic_and_keeps_unrelated_files(tmp_path):
    reports = tmp_path / "reports/wikidata-date-refresh"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    unrelated = reports / "notes.json"
    unrelated.write_text("keep", encoding="utf-8")

    path, diagnostics = save_wikidata_date_refresh_report(
        {"mode": "dry-run"}, reports, started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.name == "1970-01-01T00-01-40.json"
    assert diagnostics == []
    assert not old_report.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not list(reports.glob(".wikidata-date-refresh-report-*.tmp"))


def test_save_recent_death_report_is_atomic_and_keeps_unrelated_files(tmp_path):
    reports = tmp_path / "reports/recent-deaths"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    unrelated = reports / "notes.json"
    unrelated.write_text("keep", encoding="utf-8")

    path, diagnostics = save_recent_death_report(
        {"mode": "dry-run"}, reports, started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.name == "1970-01-01T00-01-40.json"
    assert diagnostics == []
    assert not old_report.exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert not list(reports.glob(".recent-death-report-*.tmp"))


def test_refresh_pruning_failure_keeps_current_report(monkeypatch, tmp_path):
    reports = tmp_path / "reports/quote-refresh"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_old_unlink(path, *args, **kwargs):
        if path == old_report:
            raise OSError("cannot prune")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    path, diagnostics = save_refresh_report(
        {"value": 1}, reports, started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.exists()
    assert old_report.exists()
    assert diagnostics == [
        "Could not prune old refresh report 2025-01-01T00-00-00.json: cannot prune"
    ]


def test_pruning_failure_is_reported_after_current_report_is_saved(monkeypatch, tmp_path):
    reports = tmp_path / "reports/runs"
    reports.mkdir(parents=True)
    old_report = reports / "2025-01-01T00-00-00.json"
    old_report.write_text("{}", encoding="utf-8")
    real_unlink = Path.unlink

    def fail_old_unlink(path, *args, **kwargs):
        if path == old_report:
            raise OSError("cannot prune")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_old_unlink)
    path, diagnostics = save_run_report(
        {"value": 1}, reports, started_at=100.0,
        now=datetime(2026, 8, 20, tzinfo=timezone.utc),
    )

    assert path.exists()
    assert diagnostics == ["Could not prune old run report 2025-01-01T00-00-00.json: cannot prune"]
    assert old_report.exists()


def test_human_summary_covers_no_candidate_and_telegram_failure():
    report = {
        "entries": {"before": 3, "after": 4},
        "new_titles": {"count": 1},
        "newly_evaluated": {"count": 2, "outcomes": {"accepted": 1, "rejected": 1}},
        "posting": {"selected_title": "Ada", "telegram": {"ok": False, "error_reason": "http_error"}},
        "processing_errors": [{"title": "X", "error": "disk full"}],
        "duration_seconds": 1.2,
    }

    summary = format_run_summary(report, "reports/runs/example.json")

    assert "entries 3 -> 4" in summary
    assert "Telegram failed (http_error)" in summary
    assert "processing errors 1" in summary
    assert "reports/runs/example.json" in summary
