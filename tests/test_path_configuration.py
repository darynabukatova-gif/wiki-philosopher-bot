from pathlib import Path
from datetime import datetime, timezone

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.cli.check_recent_deaths as check_recent_deaths
import wiki_philosopher_bot.config as config
import wiki_philosopher_bot.main as main
import wiki_philosopher_bot.migration as migration
import wiki_philosopher_bot.cli.purge_rejected_quotes as purge_rejected_quotes
import wiki_philosopher_bot.cli.refresh_quotes as refresh_quotes
import wiki_philosopher_bot.cli.refresh_wikidata_dates as refresh_wikidata_dates


def test_repository_storage_paths_are_separated_and_used_by_callers():
    assert config.CANONICAL_DATA_FOLDER == "data"
    assert config.LEGACY_DATA_FOLDER == "data/legacy"
    assert config.DATABASE_BACKUP_FOLDER == "backups/database"
    assert config.MIGRATION_BACKUP_FOLDER == "backups/migration"
    assert config.DATABASE_FILE == "database.jsonl"
    assert cache.default_database_backup_path(
        datetime(2026, 8, 23, 12, 34, 56, tzinfo=timezone.utc)
    ) == Path("backups/database/database-2026-08-23T12-34-56Z.jsonl")

    assert main.RUN_REPORTS_DIRECTORY == Path(config.RUN_REPORT_FOLDER)
    assert refresh_quotes.REFRESH_REPORTS_DIRECTORY == Path(
        config.QUOTE_REFRESH_REPORT_FOLDER
    )
    assert refresh_wikidata_dates.WIKIDATA_DATE_REFRESH_REPORTS_DIRECTORY == Path(
        config.WIKIDATA_DATE_REFRESH_REPORT_FOLDER
    )
    assert purge_rejected_quotes.PURGE_REPORTS_DIRECTORY == Path(
        config.PURGE_REPORT_FOLDER
    )
    assert check_recent_deaths.RECENT_DEATH_REPORTS_DIRECTORY == Path(
        config.RECENT_DEATH_REPORT_FOLDER
    )
    assert migration.read_legacy_sources.__defaults__ == (
        config.LEGACY_DATA_FOLDER,
    )
