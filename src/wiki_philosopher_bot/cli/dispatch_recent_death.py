"""Explicit dispatcher for one checkpointed recent-death notification."""
import argparse
import json
import time
import os
import tempfile
from pathlib import Path

from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.config import CANONICAL_DATA_FOLDER, DATABASE_FILE, RECENT_DEATH_REPORT_FOLDER, get_recent_death_telegram_settings, load_environment
from wiki_philosopher_bot.recent_death_outbox import dispatch_recent_death_notification
from wiki_philosopher_bot.run_reporting import save_recent_death_report
from wiki_philosopher_bot.runtime import persistence_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Dispatch one exact, externally checkpointed recent-death notification.")
    parser.add_argument("--notification-id", required=True)
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--result-json", help="write this dispatch result to an explicit machine-readable JSON file")
    parser.add_argument("--confirm-pending-dispatch", action="store_true", help="acknowledge that a stale pending event must not be retried automatically")
    return parser.parse_args(argv)


def write_result_json(report, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(destination.parent), prefix=".recent-death-dispatch-", suffix=".tmp", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(destination))
        temporary = None
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    args = parse_args(argv)
    if not args.confirm_pending_dispatch:
        print("Refusing dispatch: pending is not proof Telegram was never contacted. Confirm the external checkpoint and pass --confirm-pending-dispatch.")
        return 2
    database = load_database(DATABASE_FILE, args.data_folder)
    load_environment()
    telegram_url, chat_id = get_recent_death_telegram_settings()
    result = dispatch_recent_death_notification(database, args.notification_id, args.data_folder, telegram_url, chat_id, persistence_lock)
    report = {"operation": "recent-death-dispatch", "mode": "dispatch", **result.as_report()}
    if args.result_json:
        write_result_json(report, args.result_json)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=False))
    try:
        save_recent_death_report(report, Path(RECENT_DEATH_REPORT_FOLDER), time.time())
    except OSError as error:
        print("Warning: recent-death dispatch report could not be saved: {}".format(error))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
