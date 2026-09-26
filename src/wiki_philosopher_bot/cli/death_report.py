"""Read-only historical philosopher death reporting CLI."""

import argparse
import json
import os
import tempfile
import time
from datetime import date, datetime, timezone
from pathlib import Path

from wiki_philosopher_bot.cache import database_file_sha256, load_database
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    DEATH_REPORT_FOLDER,
)
from wiki_philosopher_bot.date_policy import calendar_date_years_ago
from wiki_philosopher_bot.death_reporting import build_historical_death_report
from wiki_philosopher_bot.run_reporting import save_historical_death_report


DEATH_REPORTS_DIRECTORY = Path(DEATH_REPORT_FOLDER)


def _date_argument(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "date must use valid YYYY-MM-DD form"
        ) from error


def _positive_years(value):
    try:
        years = int(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError(
            "last years must be a positive integer"
        ) from error
    if years <= 0:
        raise argparse.ArgumentTypeError("last years must be a positive integer")
    return years


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a read-only historical philosopher death report."
    )
    interval = parser.add_mutually_exclusive_group(required=True)
    interval.add_argument("--from", dest="interval_from", type=_date_argument)
    interval.add_argument("--last-years", type=_positive_years)
    parser.add_argument("--to", dest="interval_to", type=_date_argument)
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-folder", default=DEATH_REPORT_FOLDER)
    parser.add_argument("--json", dest="json_path", help="write JSON to this exact path")
    args = parser.parse_args(argv)
    if args.interval_from is not None and args.interval_to is None:
        parser.error("--from and --to must be supplied together")
    return args


def resolve_interval(args, today=None):
    if args.last_years is not None:
        interval_to = args.interval_to or today or date.today()
        interval_from = calendar_date_years_ago(interval_to, args.last_years)
        return interval_from, interval_to, "last-years"
    if args.interval_from is None or args.interval_to is None:
        raise ValueError("--from and --to must be supplied together")
    if args.interval_from > args.interval_to:
        raise ValueError("--from must not be after --to")
    return args.interval_from, args.interval_to, "explicit"


def _write_explicit_json(report, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".historical-death-report-",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary_path), str(destination))
        directory_fd = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        if temporary_path.exists():
            temporary_path.unlink()
        raise
    return destination


def format_summary(report, report_path):
    interval = report["interval"]
    population = report["population"]
    results = report["interval_results"]
    completeness = report["date_completeness"]
    return "\n".join((
        "Historical philosopher death report",
        "Interval: {from_date} through {to_date} (inclusive; {source})".format(
            from_date=interval["from"],
            to_date=interval["to"],
            source=interval["source"],
        ),
        "Semantic philosophers: {semantic} of {canonical}".format(
            semantic=population["semantic_philosophers"],
            canonical=population["canonical_records"],
        ),
        "Exact deaths in interval: {}".format(results["exact_deaths"]),
        "Year-only potential matches: {}".format(
            results["year_only_potential_matches"]
        ),
        "Unknown death date: {}".format(results["unknown_death_date"]),
        "Invalid date data: {}".format(results["invalid_date_data"]),
        "Exact birth dates available: {}".format(
            completeness["exact_birth_date_available"]
        ),
        "JSON report: {}".format(report_path),
    ))


def main(argv=None):
    args = parse_args(argv)
    try:
        interval_from, interval_to, source = resolve_interval(args)
    except ValueError as error:
        raise SystemExit(str(error))

    canonical_path = (Path(args.data_folder) / DATABASE_FILE).resolve()
    if args.json_path and Path(args.json_path).resolve() == canonical_path:
        raise SystemExit("--json must not target the canonical database")

    database_hash_before = database_file_sha256(DATABASE_FILE, args.data_folder)
    database = load_database(DATABASE_FILE, args.data_folder)
    database_hash_after = database_file_sha256(DATABASE_FILE, args.data_folder)
    if database_hash_before != database_hash_after:
        raise RuntimeError("canonical database changed while the report was loading")

    started_at = time.time()
    generated_at = datetime.fromtimestamp(started_at, timezone.utc)
    report = build_historical_death_report(
        database,
        interval_from,
        interval_to,
        database_sha256=database_hash_after,
        generated_at=generated_at,
        interval_source=source,
        last_years=args.last_years,
    )
    if args.json_path:
        report_path = _write_explicit_json(report, args.json_path)
    else:
        report_path, diagnostics = save_historical_death_report(
            report, Path(args.report_folder), started_at,
        )
        for diagnostic in diagnostics:
            print("Warning: {}".format(diagnostic))
    print(format_summary(report, report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
