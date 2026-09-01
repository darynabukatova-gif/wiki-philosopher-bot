"""Synchronize the local canonical database from the private authoritative checkout."""

import argparse
import json

from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_BACKUP_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
    PRIVATE_DATA_REPOSITORY_FOLDER,
)
from wiki_philosopher_bot.local_data_sync import synchronize_local_database
from wiki_philosopher_bot.runtime import persistence_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Safely copy the validated private authoritative database into the "
            "local canonical data directory. This never pushes or edits the "
            "private database."
        )
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument(
        "--private-data-repo", default=PRIVATE_DATA_REPOSITORY_FOLDER,
        help="Path to the private authoritative data-repository checkout.",
    )
    parser.add_argument("--backup-folder", default=DATABASE_BACKUP_FOLDER)
    parser.add_argument(
        "--retention-days", type=int, default=OPERATIONAL_BACKUP_RETENTION_DAYS,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Inspect Git/database state and local differences without fetching, backup, or write.",
    )
    parser.add_argument(
        "--update-private-repo", action="store_true",
        help=(
            "Explicitly run git fetch origin and git pull --ff-only before "
            "syncing; ignored during --dry-run."
        ),
    )
    parser.add_argument(
        "--force-replace-local", action="store_true",
        help="Replace despite reported local-only canonical differences after review.",
    )
    args = parser.parse_args(argv)
    if args.retention_days < 0:
        parser.error("--retention-days must not be negative")
    return args


def main(argv=None):
    args = parse_args(argv)
    result = synchronize_local_database(
        private_repository=args.private_data_repo,
        local_data_folder=args.data_folder,
        persistence_lock=persistence_lock,
        backup_folder=args.backup_folder,
        retention_days=args.retention_days,
        dry_run=args.dry_run,
        update_private_repository=args.update_private_repo,
        force_replace_local=args.force_replace_local,
    )
    print(json.dumps(result.as_report(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
