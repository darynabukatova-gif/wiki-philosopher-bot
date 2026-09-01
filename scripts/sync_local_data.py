"""Compatibility wrapper for the packaged local-data synchronization CLI."""

from wiki_philosopher_bot.cli.sync_local_data import main


if __name__ == "__main__":
    raise SystemExit(main())
