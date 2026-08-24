"""Read-only preview of canonical entries needing current evaluation."""

import argparse
import json

from wiki_philosopher_bot.cache import DatabaseBackupResult, create_database_backup, load_database
from wiki_philosopher_bot.config import CANONICAL_DATA_FOLDER, DATABASE_FILE, RATE_LIMIT, DATABASE_BACKUP_FOLDER, OPERATIONAL_BACKUP_RETENTION_DAYS
from wiki_philosopher_bot.evaluation import (
    evaluation_needs_processing,
    persist_canonical_evaluation,
    process_title,
)
from main import build_entity_lookup, make_initial_stats
from wiki_philosopher_bot.runtime import persistence_lock, stats_lock
from wiki_philosopher_bot.utils import RateLimiter


SAMPLE_SIZE = 10


def _limit_argument(value):
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Preview canonical entries eligible for current evaluation. "
            "This command is read-only."
        )
    )
    parser.add_argument(
        "--data-folder",
        default=CANONICAL_DATA_FOLDER,
        help="Folder containing the canonical database.jsonl",
    )
    parser.add_argument(
        "--limit",
        type=_limit_argument,
        default=None,
        help="Preview at most this many deterministically sorted titles",
    )
    parser.add_argument(
        "--title",
        action="append",
        default=[],
        help="Select one exact eligible canonical title; may be repeated.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview eligible entries without evaluation or writes",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Sequentially reevaluate selected eligible entries",
    )
    args = parser.parse_args(argv)
    if args.title and args.limit is not None:
        parser.error("--title cannot be combined with --limit")
    return args


def select_eligible_titles(database):
    """Return sorted canonical titles needing current evaluation."""
    return sorted(
        title
        for title, entry in database.items()
        if evaluation_needs_processing(
            entry.get("evaluation") if isinstance(entry, dict) else None
        )
    )


def select_explicit_titles(database, requested_titles):
    """Validate and preserve an explicit ordered list of eligible titles."""
    seen = set()
    for title in requested_titles:
        if title in seen:
            raise ValueError("duplicate --title: {!r}".format(title))
        seen.add(title)

        if title not in database:
            raise ValueError("requested title does not exist: {!r}".format(title))

        entry = database[title]
        evaluation = entry.get("evaluation") if isinstance(entry, dict) else None
        if not evaluation_needs_processing(evaluation):
            raise ValueError("requested title is not eligible: {!r}".format(title))

    return list(requested_titles)


def _eligibility_category(evaluation):
    status = evaluation["status"]
    if status == "unprocessed":
        return "unprocessed"

    if evaluation["algorithm_version"] is None:
        return "{}_none".format(status)

    return "{}_other".format(status)


def build_dry_run_report(
    database,
    limit=None,
    selected_titles=None,
    explicit_selection=False,
):
    eligible_titles = select_eligible_titles(database)
    by_status_version = {
        "unprocessed": 0,
        "accepted_none": 0,
        "accepted_other": 0,
        "rejected_none": 0,
        "rejected_other": 0,
    }

    for title in eligible_titles:
        category = _eligibility_category(database[title]["evaluation"])
        by_status_version[category] += 1

    if selected_titles is None:
        selected_titles = (
            eligible_titles
            if limit is None
            else eligible_titles[:limit]
        )

    selected = {
        "count": len(selected_titles),
        "limit": limit,
        "sample_titles": selected_titles[:SAMPLE_SIZE],
    }
    if explicit_selection:
        selected["requested_titles"] = [
            {
                "title": title,
                "status": database[title]["evaluation"]["status"],
                "algorithm_version": (
                    database[title]["evaluation"]["algorithm_version"]
                ),
            }
            for title in selected_titles
        ]

    return {
        "mode": "dry-run",
        "total_canonical_entries": len(database),
        "eligible": {
            "total": len(eligible_titles),
            "by_status_version": by_status_version,
        },
        "selected": selected,
    }


def run_apply(database, selected_titles, data_folder):
    """Sequentially reevaluate titles through the current canonical pipeline."""
    pages = [{"title": title} for title in selected_titles]
    stats = make_initial_stats()
    limiter = RateLimiter(RATE_LIMIT)
    results = {
        "accepted": 0,
        "rejected": 0,
        "no_result": 0,
        "operational_failures": 0,
        "errors": [],
    }

    if pages:
        all_qids, all_entities, wikidata_errors = build_entity_lookup(
            pages,
            database,
            limiter,
        )
    else:
        all_qids, all_entities, wikidata_errors = {}, {}, {}

    for page in pages:
        title = page["title"]

        try:
            result = process_title(
                page,
                stats,
                database,
                all_qids,
                all_entities,
                stats_lock=stats_lock,
                persistence_lock=persistence_lock,
                data_folder=data_folder,
                limiter=limiter,
                wikidata_errors=wikidata_errors,
            )
        except ValueError:
            raise
        except Exception as error:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title,
                "type": type(error).__name__,
                "message": str(error),
            })
            continue

        if result is None:
            results["no_result"] += 1
            continue

        try:
            persist_canonical_evaluation(
                result,
                database,
                stats,
                stats_lock,
                persistence_lock,
                data_folder,
            )
        except OSError as error:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": result.get("title", title),
                "type": type(error).__name__,
                "message": str(error),
            })
            continue

        results[result["status"]] += 1

    return results


def build_apply_report(
    database,
    eligible_before,
    selected_titles,
    limit,
    results,
):
    return {
        "mode": "apply",
        "total_canonical_entries": len(database),
        "eligible_before": eligible_before,
        "selected": {
            "count": len(selected_titles),
            "limit": limit,
        },
        "results": results,
        "remaining_eligible": len(select_eligible_titles(database)),
    }

def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    eligible_titles = select_eligible_titles(database)
    try:
        selected_titles = (
            select_explicit_titles(database, args.title)
            if args.title
            else (
                eligible_titles
                if args.limit is None
                else eligible_titles[:args.limit]
            )
        )
    except ValueError as error:
        raise SystemExit(str(error))

    backup_result = DatabaseBackupResult().as_report(
        attempted=False, retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
    )
    if args.apply:
        backup = create_database_backup(
            args.data_folder, DATABASE_BACKUP_FOLDER,
            "before-reevaluation", OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False, kind="operational", persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        backup_result = backup.as_report(True, OPERATIONAL_BACKUP_RETENTION_DAYS)
        if not backup.created:
            print(json.dumps({"mode": "apply", "backup": backup_result,
                "error": "Database backup failed; no canonical mutation was attempted."},
                indent=2, ensure_ascii=False, sort_keys=True))
            return 1
        results = run_apply(
            database,
            selected_titles,
            args.data_folder,
        )
        report = build_apply_report(
            database,
            len(eligible_titles),
            selected_titles,
            args.limit,
            results,
        )
    else:
        report = build_dry_run_report(
            database,
            limit=args.limit,
            selected_titles=selected_titles,
            explicit_selection=bool(args.title),
        )

    report["backup"] = backup_result

    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
