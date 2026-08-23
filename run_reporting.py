"""Read-only run accounting plus durable reports for normal bot executions."""

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_RETENTION_DAYS = 90
REPORT_FILENAME_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}\.json$"
)


def _evaluation_state(entry):
    evaluation = entry.get("evaluation", {}) if isinstance(entry, dict) else {}
    if not isinstance(evaluation, dict):
        evaluation = {}
    return {
        "status": evaluation.get("status"),
        "algorithm_version": evaluation.get("algorithm_version"),
    }


def capture_run_baseline(database):
    """Capture only the per-title state needed to describe one normal run."""
    titles = set(database)
    return {
        "titles": titles,
        "evaluation": {
            title: _evaluation_state(entry)
            for title, entry in database.items()
        },
        "posting": {
            title: bool(
                entry.get("posting", {}).get("has_been_posted", False)
            )
            if isinstance(entry, dict)
            else False
            for title, entry in database.items()
        },
    }


def _iso_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _outcome_counts(titles, database):
    counts = {"accepted": 0, "rejected": 0, "unprocessed": 0}
    for title in titles:
        status = _evaluation_state(database[title]).get("status")
        if status in counts:
            counts[status] += 1
    return counts


def _terminal_evaluation_record(title, entry):
    evaluation = entry["evaluation"]
    return {
        "title": title,
        "status": evaluation["status"],
        "algorithm_version": evaluation["algorithm_version"],
        "philosopher_confidence": evaluation["philosopher_confidence"],
        "human_confidence": evaluation["human_confidence"],
        "content_confidence": evaluation["content_confidence"],
    }


def build_run_report(
    baseline,
    database,
    stats,
    started_at,
    finished_at,
    selected_posting_title=None,
    telegram_result=None,
    processing_errors=None,
    runtime_error=None,
):
    """Build a JSON-safe report without changing the runtime database."""
    before_titles = baseline["titles"]
    after_titles = set(database)
    new_titles = sorted(after_titles - before_titles)
    evaluated_titles = [
        _terminal_evaluation_record(title, database[title])
        for title in sorted(after_titles)
        if _evaluation_state(database[title]) != baseline["evaluation"].get(title)
        and _evaluation_state(database[title])["status"] in (
            "accepted", "rejected"
        )
    ]
    newly_evaluated = [record["title"] for record in evaluated_titles]

    return {
        "started_at": _iso_timestamp(started_at),
        "finished_at": _iso_timestamp(finished_at),
        "duration_seconds": round(finished_at - started_at, 6),
        "entries": {"before": len(before_titles), "after": len(after_titles)},
        "new_titles": {
            "count": len(new_titles),
            "titles": new_titles,
            "outcomes": _outcome_counts(new_titles, database),
        },
        "newly_evaluated": {
            "count": len(newly_evaluated),
            "titles": newly_evaluated,
            "outcomes": _outcome_counts(newly_evaluated, database),
        },
        "posting": {
            "selected_title": selected_posting_title,
            "telegram": telegram_result,
        },
        "stats": copy.deepcopy(stats),
        "processing_errors": list(processing_errors or []),
        "runtime_error": runtime_error,
        "evaluated_titles": evaluated_titles,
    }


def _report_filename(started_at):
    return datetime.fromtimestamp(started_at, timezone.utc).strftime(
        "%Y-%m-%dT%H-%M-%S.json"
    )


def _prune_old_reports(
    report_directory,
    now,
    retention_days,
    keep_path=None,
    report_kind="run",
):
    diagnostics = []
    cutoff = now - timedelta(days=retention_days)
    for path in sorted(report_directory.iterdir()):
        if not path.is_file() or not REPORT_FILENAME_PATTERN.match(path.name):
            continue
        if keep_path is not None and path == keep_path:
            continue
        try:
            report_time = datetime.strptime(
                path.stem, "%Y-%m-%dT%H-%M-%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError as error:
            diagnostics.append(
                "Could not parse {} report timestamp {}: {}".format(
                    report_kind,
                    path.name,
                    error,
                )
            )
            continue
        if report_time >= cutoff:
            continue
        try:
            path.unlink()
        except OSError as error:
            diagnostics.append(
                "Could not prune old {} report {}: {}".format(
                    report_kind,
                    path.name,
                    error,
                )
            )
    return diagnostics


def save_json_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
    report_kind="run",
    temporary_prefix=".report-",
):
    """Atomically save one dated JSON report, then selectively prune it."""
    directory = Path(report_directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / _report_filename(started_at)
    if destination.exists():
        raise FileExistsError(
            "{} report already exists: {}".format(
                report_kind.capitalize(),
                destination,
            )
        )

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=temporary_prefix,
        suffix=".tmp",
        dir=str(directory),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(destination))
        directory_fd = os.open(str(directory), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise

    if now is None:
        now = datetime.now(timezone.utc)
    return destination, _prune_old_reports(
        directory,
        now,
        retention_days,
        keep_path=destination,
        report_kind=report_kind,
    )


def save_run_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
):
    """Atomically save a normal bot run report."""
    return save_json_report(
        report,
        report_directory,
        started_at,
        retention_days=retention_days,
        now=now,
        report_kind="run",
        temporary_prefix=".run-report-",
    )


def save_refresh_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
):
    """Atomically save a quote-refresh report independent of bot runs."""
    return save_json_report(
        report,
        report_directory,
        started_at,
        retention_days=retention_days,
        now=now,
        report_kind="refresh",
        temporary_prefix=".refresh-report-",
    )


def save_purge_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
):
    """Atomically save a rejected-quote purge report."""
    return save_json_report(
        report,
        report_directory,
        started_at,
        retention_days=retention_days,
        now=now,
        report_kind="purge",
        temporary_prefix=".purge-report-",
    )


def save_wikidata_date_refresh_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
):
    """Atomically save a Wikidata life-date maintenance report."""
    return save_json_report(
        report,
        report_directory,
        started_at,
        retention_days=retention_days,
        now=now,
        report_kind="wikidata-date-refresh",
        temporary_prefix=".wikidata-date-refresh-report-",
    )


def save_recent_death_report(
    report,
    report_directory,
    started_at,
    retention_days=DEFAULT_RETENTION_DAYS,
    now=None,
):
    """Atomically save a recent-death monitoring report."""
    return save_json_report(
        report,
        report_directory,
        started_at,
        retention_days=retention_days,
        now=now,
        report_kind="recent-death",
        temporary_prefix=".recent-death-report-",
    )


def format_run_summary(report, report_path=None, diagnostics=None):
    entries = report["entries"]
    evaluated = report["newly_evaluated"]
    outcomes = evaluated["outcomes"]
    posting = report["posting"]
    telegram = posting.get("telegram")

    if posting.get("selected_title") is None:
        posting_text = "no candidate selected"
    elif telegram is None:
        posting_text = "selected {} (not sent)".format(
            posting["selected_title"]
        )
    elif telegram.get("ok"):
        posting_text = "posted {}".format(posting["selected_title"])
    else:
        posting_text = "Telegram failed ({})".format(
            telegram.get("error_reason")
        )

    parts = [
        "Run summary: entries {} -> {}; new titles {}; evaluated {} "
        "(accepted {}, rejected {}); {}; {:.2f}s".format(
            entries["before"],
            entries["after"],
            report["new_titles"]["count"],
            evaluated["count"],
            outcomes["accepted"],
            outcomes["rejected"],
            posting_text,
            report["duration_seconds"],
        )
    ]
    if report["processing_errors"]:
        parts.append("processing errors {}".format(len(report["processing_errors"])))
    if report.get("runtime_error"):
        parts.append("runtime error {}".format(report["runtime_error"]))
    if report_path is not None:
        parts.append("report {}".format(report_path))
    if diagnostics:
        parts.extend(diagnostics)
    return "; ".join(parts)
