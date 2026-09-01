"""Prepare one durable pending philosopher-post attempt without Telegram I/O."""

import argparse
import json
import time

from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    MAX_QUOTES,
    POSTING_ATTEMPT_REPORT_FOLDER,
    RATE_LIMIT,
)
from wiki_philosopher_bot.main import make_initial_stats
from wiki_philosopher_bot.posting_outbox import prepare_posting_attempt
from wiki_philosopher_bot.run_reporting import (
    build_posting_phase_report,
    save_posting_phase_report,
)
from wiki_philosopher_bot.runtime import persistence_lock, stats_lock
from wiki_philosopher_bot.utils import RateLimiter


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Persist one pending philosopher-post attempt without sending Telegram."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-folder", default=POSTING_ATTEMPT_REPORT_FOLDER)
    parser.add_argument(
        "--title",
        help=(
            "Prepare this exact canonical philosopher title if it currently "
            "satisfies normal posting eligibility; without --title, normal "
            "candidate selection is used."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    started_at = time.time()
    database = load_database(DATABASE_FILE, args.data_folder)
    result = prepare_posting_attempt(
        database=database,
        stats=make_initial_stats(),
        stats_lock=stats_lock,
        persistence_lock=persistence_lock,
        data_folder=args.data_folder,
        filename=DATABASE_FILE,
        max_quotes=MAX_QUOTES,
        limiter=RateLimiter(RATE_LIMIT),
        title=args.title,
    )
    report = build_posting_phase_report(result, started_at, time.time())
    try:
        report_path, diagnostics = save_posting_phase_report(
            report, args.report_folder, started_at,
        )
    except OSError as error:
        report_path, diagnostics = None, ["Posting-phase report failed: {}".format(error)]
    print(json.dumps(result.as_report(), ensure_ascii=False, sort_keys=True))
    if report_path is not None:
        print("Report: {}".format(report_path))
    for diagnostic in diagnostics:
        print(diagnostic)
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
