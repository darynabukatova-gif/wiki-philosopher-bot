"""Refresh stale section-aware Wikiquote caches sequentially."""

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from cache import DatabaseBackupResult, create_database_backup, load_database
from config import (
    CURRENT_QUOTE_PARSER_VERSION,
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    QUOTE_REFRESH_REPORT_FOLDER,
    DATABASE_BACKUP_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
    RATE_LIMIT,
)
from runtime import persistence_lock, stats_lock
from run_reporting import save_refresh_report
from utils import RateLimiter
from wikipedia_api import get_quotes


REFRESH_REPORTS_DIRECTORY = Path(QUOTE_REFRESH_REPORT_FOLDER)


def _limit_argument(value):
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Refresh stale canonical Wikiquote caches sequentially."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--limit", type=_limit_argument, default=None)
    parser.add_argument("--title", action="append", default=[])
    parser.add_argument(
        "--repair-current",
        action="store_true",
        help="Re-fetch explicitly named current-parser accepted/unposted caches.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.title and args.limit is not None:
        parser.error("--title cannot be combined with --limit")
    if args.repair_current and not args.title:
        parser.error("--repair-current requires one or more --title arguments")
    if args.repair_current and args.limit is not None:
        parser.error("--repair-current cannot be combined with --limit")
    return args


def quote_refresh_needs_processing(entry):
    """Whether an entry is an eligible stale accepted/unposted quote cache."""
    if not isinstance(entry, dict):
        return False
    quotes = entry.get("quotes")
    evaluation = entry.get("evaluation")
    posting = entry.get("posting")
    return (
        isinstance(quotes, dict)
        and isinstance(evaluation, dict)
        and isinstance(posting, dict)
        and evaluation.get("status") == "accepted"
        and posting.get("has_been_posted") is False
        and quotes.get("status") == "available"
        and isinstance(quotes.get("items"), list)
        and bool(quotes["items"])
        and quotes.get("parser_version") != CURRENT_QUOTE_PARSER_VERSION
    )


def current_quote_repair_needs_processing(entry):
    """Whether a current accepted/unposted cache is eligible for named repair."""
    if not isinstance(entry, dict):
        return False
    quotes = entry.get("quotes")
    evaluation = entry.get("evaluation")
    posting = entry.get("posting")
    return (
        isinstance(quotes, dict)
        and isinstance(evaluation, dict)
        and isinstance(posting, dict)
        and evaluation.get("status") == "accepted"
        and posting.get("has_been_posted") is False
        and quotes.get("status") == "available"
        and isinstance(quotes.get("items"), list)
        and bool(quotes["items"])
        and quotes.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION
    )


def select_eligible_titles(database):
    return sorted(
        title
        for title, entry in database.items()
        if quote_refresh_needs_processing(entry)
    )


def select_explicit_titles(database, requested_titles, repair_current=False):
    eligibility = (
        current_quote_repair_needs_processing
        if repair_current else quote_refresh_needs_processing
    )
    seen = set()
    for title in requested_titles:
        if title in seen:
            raise ValueError("duplicate --title: {!r}".format(title))
        seen.add(title)
        if title not in database:
            raise ValueError("requested title does not exist: {!r}".format(title))
        if not eligibility(database[title]):
            raise ValueError("requested title is not eligible: {!r}".format(title))
    return list(requested_titles)


def stale_quote_rollout_counts(database):
    """Read-only rollout counts for parser-version migration planning."""
    counts = {
        "accepted_unposted_stale_available": 0,
        "accepted_posted_stale_available": 0,
        "rejected_stale_available": 0,
        "current_version_available": 0,
    }
    for entry in database.values():
        if not isinstance(entry, dict):
            continue
        quotes = entry.get("quotes")
        evaluation = entry.get("evaluation")
        posting = entry.get("posting")
        if not isinstance(quotes, dict) or quotes.get("status") != "available":
            continue
        if quotes.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION:
            counts["current_version_available"] += 1
            continue
        if not isinstance(evaluation, dict) or not isinstance(posting, dict):
            continue
        if evaluation.get("status") == "accepted":
            key = (
                "accepted_posted_stale_available"
                if posting.get("has_been_posted") is True
                else "accepted_unposted_stale_available"
            )
            counts[key] += 1
        elif evaluation.get("status") == "rejected":
            counts["rejected_stale_available"] += 1
    return counts


def build_dry_run_report(database, selected_titles, limit, repair_current=False):
    return {
        "mode": "dry-run",
        "repair_current": repair_current,
        "total_canonical_entries": len(database),
        "eligible_stale_quotes": len(select_eligible_titles(database)),
        "rollout_counts": stale_quote_rollout_counts(database),
        "selected": {
            "count": len(selected_titles),
            "limit": limit,
            "titles": list(selected_titles),
        },
    }


def _quote_stats():
    return {
        "cached_quotes": 0,
        "downloaded_quotes": 0,
        "failed_quotes": 0,
    }


def _source_diagnostics(items):
    sources = [item.get("source") for item in items if isinstance(item, dict)]
    return {
        "quotes_with_source": sum(
            isinstance(source, dict) and any(value is not None for value in source.values())
            for source in sources
        ),
        "quotes_with_work": sum(isinstance(source, dict) and source.get("work") is not None for source in sources),
        "quotes_with_year": sum(isinstance(source, dict) and source.get("year") is not None for source in sources),
    }


def _title_result(
    title,
    quotes_before,
    quotes_after,
):
    """Return one deliberately ordered, compact refresh diagnostic."""
    return {
        "title": title,
        "status": quotes_after["status"],
        "old_parser_version": quotes_before.get("parser_version"),
        "new_parser_version": quotes_after.get("parser_version"),
        "old_quote_count": len(quotes_before["items"]),
        "new_quote_count": len(quotes_after["items"]),
        **_source_diagnostics(quotes_after["items"]),
    }


def run_apply(database, selected_titles, data_folder, repair_current=False):
    """Refresh selected quote caches sequentially through get_quotes()."""
    stats = _quote_stats()
    limiter = RateLimiter(RATE_LIMIT)
    results = {
        "refreshed_current": 0,
        "became_not_found": 0,
        "operational_failures": 0,
        "errors": [],
        "titles": [],
    }

    for title in selected_titles:
        quotes_before = database[title]["quotes"]
        old_quotes = dict(quotes_before)
        old_quotes["items"] = list(quotes_before["items"])
        try:
            get_quotes(
                title,
                database,
                stats,
                stats_lock,
                persistence_lock,
                data_folder,
                limiter=limiter,
                refresh_stale=True,
                refresh_current=repair_current,
            )
        except ValueError:
            raise
        except Exception as error:
            quotes_after = database[title]["quotes"]
            results["titles"].append(
                _title_result(title, old_quotes, quotes_after)
            )
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title,
                "type": type(error).__name__,
                "message": str(error),
            })
            continue

        quotes_after = database[title]["quotes"]
        detail = _title_result(title, old_quotes, quotes_after)
        results["titles"].append(detail)
        failure = quotes_after.get("failure")
        if repair_current and isinstance(failure, dict):
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title,
                "type": "QuoteRefreshIncomplete",
                "message": failure.get("reason", "current-cache repair failed"),
            })
        elif (
            quotes_after.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION
            and quotes_after.get("status") == "available"
        ):
            results["refreshed_current"] += 1
        elif (
            quotes_after.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION
            and quotes_after.get("status") == "not_found"
        ):
            results["became_not_found"] += 1
        else:
            results["operational_failures"] += 1
            failure = quotes_after.get("failure")
            results["errors"].append({
                "title": title,
                "type": "QuoteRefreshIncomplete",
                "message": (
                    failure.get("reason")
                    if isinstance(failure, dict)
                    else "refresh did not produce a current parser result"
                ),
            })

    return results


def build_apply_report(
    database,
    eligible_before,
    selected_titles,
    limit,
    results,
    repair_current=False,
):
    return {
        "mode": "apply",
        "repair_current": repair_current,
        "total_canonical_entries": len(database),
        "eligible_before": eligible_before,
        "selected": {"count": len(selected_titles), "limit": limit},
        "results": results,
        "remaining_eligible": (
            sum(current_quote_repair_needs_processing(entry) for entry in database.values())
            if repair_current else len(select_eligible_titles(database))
        ),
    }


def _iso_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def add_report_timing(report, started_at, finished_at):
    """Add common ordered timing fields without changing report contents."""
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
    repair_eligible_titles = sorted(
        title for title, entry in database.items()
        if current_quote_repair_needs_processing(entry)
    )
    try:
        selected_titles = (
            select_explicit_titles(
                database, args.title, repair_current=args.repair_current,
            )
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
            data_folder=args.data_folder,
            backup_folder=DATABASE_BACKUP_FOLDER,
            label="before-quote-refresh",
            retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False,
            kind="operational",
            persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        backup_result = backup.as_report(
            attempted=True, retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
        )
        if not backup.created:
            report = {
                "mode": "apply",
                "backup": backup_result,
                "error": "Database backup failed; no canonical mutation was attempted.",
            }
            report = add_report_timing(report, started_at, time.time())
            print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
            return 1
        results = run_apply(
            database, selected_titles, args.data_folder,
            repair_current=args.repair_current,
        )
        report = build_apply_report(
            database,
            len(repair_eligible_titles) if args.repair_current else len(eligible_titles),
            selected_titles,
            args.limit,
            results,
            repair_current=args.repair_current,
        )
    else:
        report = build_dry_run_report(
            database, selected_titles, args.limit,
            repair_current=args.repair_current,
        )
    report["backup"] = backup_result
    report = add_report_timing(report, started_at, time.time())
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    try:
        report_path, diagnostics = save_refresh_report(
            report,
            REFRESH_REPORTS_DIRECTORY,
            started_at,
        )
    except OSError as error:
        print("Warning: refresh report could not be saved: {}".format(error))
    else:
        print("Saved refresh report: {}".format(report_path))
        for diagnostic in diagnostics:
            print("Warning: {}".format(diagnostic))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
