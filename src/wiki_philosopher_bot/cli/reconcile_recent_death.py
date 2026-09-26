"""Conservative operator reconciliation for durable death notifications."""
import argparse
import json
import time
from pathlib import Path

from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.config import CANONICAL_DATA_FOLDER, DATABASE_FILE, RECENT_DEATH_REPORT_FOLDER
from wiki_philosopher_bot.recent_death_outbox import locate_recent_death_notification, reconcile_recent_death_notification
from wiki_philosopher_bot.run_reporting import save_recent_death_report
from wiki_philosopher_bot.runtime import persistence_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Inspect or explicitly reconcile a recent-death notification.")
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    for name in ("show", "mark-sent", "cancel"):
        child = subparsers.add_parser(name)
        child.add_argument("--notification-id", required=True)
        if name == "mark-sent":
            child.add_argument("--telegram-message-id", type=int, required=True)
            child.add_argument("--note", required=True)
        elif name == "cancel":
            child.add_argument("--note", required=True)
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    if args.operation == "show":
        located = locate_recent_death_notification(database, args.notification_id)
        if located is None:
            print("Notification not found: {}".format(args.notification_id)); return 1
        title, entry, event = located
        safe = {key: event.get(key) for key in ("notification_id", "title", "qid", "death_date", "state", "created_at", "state_changed_at", "message_fingerprint", "telegram_message_id", "error_kind", "error_summary", "resolution_note")}
        safe["title_marked_posted"] = entry.get("posting", {}).get("has_been_posted")
        print(json.dumps(safe, ensure_ascii=False, indent=2)); return 0
    result = reconcile_recent_death_notification(database, args.notification_id, args.operation, args.data_folder, persistence_lock, telegram_message_id=getattr(args, "telegram_message_id", None), note=getattr(args, "note", None))
    report = {"operation": "recent-death-reconcile", "action": args.operation, **result.as_report()}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if result.persistence_succeeded:
        try:
            save_recent_death_report(report, Path(RECENT_DEATH_REPORT_FOLDER), time.time())
        except OSError as error:
            print("Warning: reconciliation report could not be saved: {}".format(error))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
