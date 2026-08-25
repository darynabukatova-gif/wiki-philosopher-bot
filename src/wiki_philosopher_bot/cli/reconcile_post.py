"""Safely reconcile one durable philosopher-post attempt without Telegram I/O."""

import argparse
import json
import time

from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    POSTING_ATTEMPT_REPORT_FOLDER,
)
from wiki_philosopher_bot.posting_outbox import (
    reconcile_posting_attempt,
    show_posting_attempt,
)
from wiki_philosopher_bot.run_reporting import (
    build_posting_phase_report,
    save_posting_phase_report,
)
from wiki_philosopher_bot.runtime import persistence_lock


def _operation_parser(subparsers, name, help_text, *, message_id=False,
                      confirmation=False):
    parser = subparsers.add_parser(name, help=help_text)
    _add_path_overrides(parser)
    parser.add_argument("--attempt-id", required=True)
    if message_id:
        parser.add_argument("--telegram-message-id", required=True, type=int)
        parser.add_argument("--note", required=True)
    else:
        parser.add_argument("--reason", required=True)
    if confirmation:
        parser.add_argument("--confirm-unsafe", action="store_true")
    return parser


def _add_path_overrides(parser):
    """Accept path overrides before or after a reconciliation subcommand."""
    parser.add_argument("--data-folder", default=argparse.SUPPRESS)
    parser.add_argument("--report-folder", default=argparse.SUPPRESS)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly reconcile one durable posting attempt."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-folder", default=POSTING_ATTEMPT_REPORT_FOLDER)
    subparsers = parser.add_subparsers(dest="command", required=True)

    show = subparsers.add_parser("show", help="Show safe state for one attempt.")
    _add_path_overrides(show)
    show.add_argument("--attempt-id", required=True)
    show.add_argument("--show-message", action="store_true")
    _operation_parser(
        subparsers, "mark-sent",
        "Record externally confirmed Telegram delivery for pending or unknown state.",
        message_id=True,
    )
    _operation_parser(
        subparsers, "cancel-pending",
        "Cancel pending only when dispatch definitely never occurred.",
    )
    _operation_parser(
        subparsers, "authorize-retry",
        "Close a definitely rejected failed attempt; it does not resend it.",
    )
    _operation_parser(
        subparsers, "resolve-unknown-sent",
        "Record externally confirmed delivery for an unknown attempt.",
        message_id=True,
    )
    _operation_parser(
        subparsers, "force-cancel-unknown",
        "Hazardously cancel unknown only with evidence of non-delivery.",
        confirmation=True,
    )
    return parser.parse_args(argv)


def _exit_code(result):
    if result.ok:
        return 0
    if result.error_kind == "persistence_error":
        return 3
    if result.error_kind in {
        "invalid_attempt", "invalid_operation", "invalid_state",
        "validation_error", "unsafe_confirmation_required",
    }:
        return 2
    return 1


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    if args.command == "show":
        view = show_posting_attempt(
            database, args.attempt_id, include_message=args.show_message,
        )
        if view is None:
            print("Attempt not found or ambiguous: {}".format(args.attempt_id))
            return 2
        print(json.dumps(view, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "force-cancel-unknown" and args.confirm_unsafe:
        print(
            "WARNING: cancelling an unknown attempt can cause a duplicate post "
            "if Telegram actually received it."
        )

    message_id = getattr(args, "telegram_message_id", None)
    note = getattr(args, "note", None) or getattr(args, "reason", None)
    started_at = time.time()
    result = reconcile_posting_attempt(
        database=database,
        attempt_id=args.attempt_id,
        operation=args.command.replace("-", "_"),
        persistence_lock=persistence_lock,
        data_folder=args.data_folder,
        filename=DATABASE_FILE,
        note=note,
        telegram_message_id=message_id,
        confirm_unsafe=getattr(args, "confirm_unsafe", False),
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
    return _exit_code(result)


if __name__ == "__main__":
    raise SystemExit(main())
