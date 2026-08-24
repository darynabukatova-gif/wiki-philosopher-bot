"""Purge retained quote payloads from canonically rejected entries."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from wiki_philosopher_bot.cache import DatabaseBackupResult, create_database_backup, load_database, update_database_entry
from wiki_philosopher_bot.config import CANONICAL_DATA_FOLDER, DATABASE_FILE, PURGE_REPORT_FOLDER, DATABASE_BACKUP_FOLDER, OPERATIONAL_BACKUP_RETENTION_DAYS
from wiki_philosopher_bot.run_reporting import save_purge_report
from wiki_philosopher_bot.runtime import persistence_lock


PURGE_REPORTS_DIRECTORY = Path(PURGE_REPORT_FOLDER)


def _limit_argument(value):
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Purge quote payloads from canonically rejected entries."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--limit", type=_limit_argument, default=None)
    parser.add_argument("--title", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.title and args.limit is not None:
        parser.error("--title cannot be combined with --limit")
    return args


def quote_item_bytes(items):
    """Return deterministic compact UTF-8 bytes for a quote-item payload."""
    return len(
        json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


def rejected_quotes_need_purge(entry):
    """Whether a rejected canonical entry retains removable quote items."""
    if not isinstance(entry, dict):
        return False
    evaluation = entry.get("evaluation")
    quotes = entry.get("quotes")
    return (
        isinstance(evaluation, dict)
        and evaluation.get("status") == "rejected"
        and isinstance(quotes, dict)
        and isinstance(quotes.get("items"), list)
        and bool(quotes["items"])
    )


def select_eligible_titles(database):
    return sorted(
        title
        for title, entry in database.items()
        if rejected_quotes_need_purge(entry)
    )


def select_explicit_titles(database, requested_titles):
    seen = set()
    for title in requested_titles:
        if title in seen:
            raise ValueError("duplicate --title: {!r}".format(title))
        seen.add(title)
        if title not in database:
            raise ValueError("requested title does not exist: {!r}".format(title))
        if not rejected_quotes_need_purge(database[title]):
            raise ValueError("requested title is not eligible: {!r}".format(title))
    return list(requested_titles)


def _eligible_summary(database):
    entries = [
        entry for entry in database.values()
        if rejected_quotes_need_purge(entry)
    ]
    return {
        "count": len(entries),
        "quote_items": sum(len(entry["quotes"]["items"]) for entry in entries),
        "approx_item_bytes": sum(
            quote_item_bytes(entry["quotes"]["items"]) for entry in entries
        ),
    }


def _selection_details(database, selected_titles):
    return [
        {
            "title": title,
            "quote_count": len(database[title]["quotes"]["items"]),
            "approx_item_bytes": quote_item_bytes(
                database[title]["quotes"]["items"]
            ),
        }
        for title in selected_titles
    ]


def build_dry_run_report(database, selected_titles, limit):
    return {
        "mode": "dry-run",
        "total_canonical_entries": len(database),
        "eligible": _eligible_summary(database),
        "selected": {
            "count": len(selected_titles),
            "limit": limit,
            "titles": list(selected_titles),
        },
        "title_details": _selection_details(database, selected_titles),
    }


def purge_entry_quotes(database, title, data_folder):
    """Transactionally replace only one rejected entry's quotes section."""
    def update_quotes(entry):
        entry["quotes"] = {
            "status": "purged",
            "items": [],
            "failure": None,
            "fetched_at": None,
            "parser_version": None,
        }

    return update_database_entry(
        database,
        title,
        update_quotes,
        DATABASE_FILE,
        data_folder,
        persistence_lock,
    )


def _title_result(title, quotes_before, quotes_after, item_bytes_removed):
    return {
        "title": title,
        "old_quote_status": quotes_before["status"],
        "old_quote_count": len(quotes_before["items"]),
        "new_quote_status": quotes_after["status"],
        "new_quote_count": len(quotes_after["items"]),
        "approx_item_bytes_removed": item_bytes_removed,
    }


def run_apply(database, selected_titles, data_folder):
    """Sequentially purge selected payloads through canonical persistence."""
    results = {
        "successfully_purged": 0,
        "operational_failures": 0,
        "errors": [],
        "total_quote_items_removed": 0,
        "approx_item_bytes_removed": 0,
        "titles": [],
    }
    for title in selected_titles:
        quotes_before = database[title]["quotes"]
        old_quotes = {
            **quotes_before,
            "items": list(quotes_before["items"]),
        }
        removed_bytes = quote_item_bytes(old_quotes["items"])
        try:
            purge_entry_quotes(database, title, data_folder)
        except ValueError:
            raise
        except OSError as error:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title,
                "type": type(error).__name__,
                "message": str(error),
            })
            results["titles"].append(_title_result(
                title, old_quotes, database[title]["quotes"], 0
            ))
            continue

        quotes_after = database[title]["quotes"]
        results["successfully_purged"] += 1
        results["total_quote_items_removed"] += len(old_quotes["items"])
        results["approx_item_bytes_removed"] += removed_bytes
        results["titles"].append(_title_result(
            title, old_quotes, quotes_after, removed_bytes
        ))
    return results


def build_apply_report(database, eligible_before, selected_titles, limit, results):
    return {
        "mode": "apply",
        "total_canonical_entries": len(database),
        "eligible_before": eligible_before,
        "selected": {"count": len(selected_titles), "limit": limit},
        "results": results,
        "remaining_eligible": len(select_eligible_titles(database)),
    }


def _iso_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def add_report_timing(report, started_at, finished_at):
    return {
        "started_at": _iso_timestamp(started_at),
        "finished_at": _iso_timestamp(finished_at),
        "duration_seconds": round(finished_at - started_at, 6),
        **report,
    }


def main(argv=None):
    started_at = time.time()
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    eligible_titles = select_eligible_titles(database)
    try:
        selected_titles = (
            select_explicit_titles(database, args.title)
            if args.title
            else eligible_titles if args.limit is None else eligible_titles[:args.limit]
        )
    except ValueError as error:
        raise SystemExit(str(error))

    backup_result = DatabaseBackupResult().as_report(
        attempted=False, retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
    )
    if args.apply:
        backup = create_database_backup(
            args.data_folder, DATABASE_BACKUP_FOLDER,
            "before-rejected-quote-purge", OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False, kind="operational", persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        backup_result = backup.as_report(True, OPERATIONAL_BACKUP_RETENTION_DAYS)
        if not backup.created:
            print(json.dumps({"mode": "apply", "backup": backup_result,
                "error": "Database backup failed; no canonical mutation was attempted."},
                indent=2, ensure_ascii=False, sort_keys=False))
            return 1
        results = run_apply(database, selected_titles, args.data_folder)
        report = build_apply_report(
            database, len(eligible_titles), selected_titles, args.limit, results
        )
    else:
        report = build_dry_run_report(database, selected_titles, args.limit)

    report["backup"] = backup_result

    report = add_report_timing(report, started_at, time.time())
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    try:
        report_path, diagnostics = save_purge_report(
            report, PURGE_REPORTS_DIRECTORY, started_at
        )
    except OSError as error:
        print("Warning: purge report could not be saved: {}".format(error))
    else:
        print("Saved purge report: {}".format(report_path))
        for diagnostic in diagnostics:
            print("Warning: {}".format(diagnostic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
