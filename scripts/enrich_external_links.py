"""Compatibility wrapper for the packaged external-links audit CLI."""

from wiki_philosopher_bot.cli.enrich_external_links import main


if __name__ == "__main__":
    raise SystemExit(main())
