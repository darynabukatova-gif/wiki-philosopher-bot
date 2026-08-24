"""Create one preserved, verified canonical database checkpoint."""

import argparse

from wiki_philosopher_bot.cache import create_database_backup
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_BACKUP_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
)
from wiki_philosopher_bot.runtime import persistence_lock


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Create a preserved byte-for-byte canonical database backup."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--backup-folder", default=DATABASE_BACKUP_FOLDER)
    parser.add_argument("--label", default="manual")
    parser.add_argument(
        "--retention-days",
        type=int,
        default=OPERATIONAL_BACKUP_RETENTION_DAYS,
    )
    parser.add_argument(
        "--preserve",
        action="store_true",
        default=True,
        help="Preserve this manual checkpoint indefinitely (the default).",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    result = create_database_backup(
        data_folder=args.data_folder,
        backup_folder=args.backup_folder,
        label=args.label,
        retention_days=args.retention_days,
        preserve=args.preserve,
        kind="manual",
        persistence_lock=persistence_lock,
    )
    if not result.created:
        print("Database backup failed: {}".format(result.error_reason))
        return 1
    print("Database backup created:\n{}\nSHA-256: {}".format(
        result.path, result.sha256,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
