# Wiki Scraper Telegram Bot

This project discovers and evaluates philosopher pages using Wikipedia and
Wikidata, extracts structured quotations from Wikiquote, stores canonical
state locally, and can post selected quotations to Telegram.

## Features

- Wikipedia page discovery and summary retrieval
- Wikidata-backed candidate evaluation
- Structured Wikiquote quotation extraction with parser versioning
- A local canonical JSONL database
- Telegram quotation posting
- Recent-death monitoring and maintenance commands
- Verified pre-apply database backups for dangerous maintenance operations
- A pytest test suite

## Requirements

Use Python 3 and the dependencies in [requirements.txt](requirements.txt).
Development and test dependencies are listed in
[requirements-dev.txt](requirements-dev.txt).

## Installation

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project:

```bash
pip install -e .
```

For development and tests, install the development dependencies as well:

```bash
pip install -e ".[dev]"
```

## Configuration

Create a local environment file:

```bash
cp .env.example .env
```

`TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID` configure normal posting.
`RECENT_DEATH_TELEGRAM_CHAT_ID` is optional for a separate notification
destination. `WIKIMEDIA_USER_AGENT_CONTACT` is optional public contact
information included in the Wikimedia User-Agent.

Do not commit `.env`.

## Running

Run the normal bot with:

```bash
python3 -m wiki_philosopher_bot
```

## Tests

```bash
.venv/bin/python -m pytest
```

## Maintenance commands

The installed console commands are:

- `wiki-philosopher-backup`
- `wiki-philosopher-refresh-quotes`
- `wiki-philosopher-refresh-wikidata-dates`
- `wiki-philosopher-check-recent-deaths`
- `wiki-philosopher-reevaluate`
- `wiki-philosopher-purge-rejected-quotes`

Where supported, run `--dry-run` first and use `--apply` only after reviewing
the result. Dangerous maintenance applies create one verified pre-apply
database backup automatically.

Compatibility wrappers remain available under `scripts/`, for example:

```bash
python3 scripts/refresh_quotes.py --dry-run
```

## Data and generated files

Runtime state and generated artifacts are deliberately local and ignored by
Git:

```text
data/
backups/
reports/
analysis/
```

The canonical database is not distributed with this repository. See
[wiki-scraper-tg-bot.txt](wiki-scraper-tg-bot.txt) for detailed technical and
design documentation.

## Project structure

```text
*.py                 application and maintenance commands
tests/               pytest suite
requirements*.txt    dependency declarations
README.md            public project guide
wiki-scraper-tg-bot.txt  detailed technical documentation

data/                local canonical and legacy data (ignored)
backups/             local snapshots (ignored)
reports/             generated maintenance reports (ignored)
analysis/            generated analysis artifacts (ignored)
```

## Data sources and attribution

The program uses Wikipedia, Wikidata, and Wikiquote. Anyone redistributing
content obtained through those services should review the applicable Wikimedia
content licenses and attribution requirements.

## License

This project is licensed under the GNU General Public License v3.0.
See [LICENSE](LICENSE) for details.
