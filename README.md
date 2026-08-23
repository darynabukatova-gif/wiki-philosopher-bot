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

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

For development and tests:

```bash
pip install -r requirements-dev.txt
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
python3 main.py
```

## Tests

```bash
.venv/bin/python -m pytest
```

## Maintenance commands

The project includes:

- `backup_database.py`
- `refresh_quotes.py`
- `refresh_wikidata_dates.py`
- `check_recent_deaths.py`
- `reevaluate_database.py`
- `purge_rejected_quotes.py`

Where supported, run `--dry-run` first and use `--apply` only after reviewing
the result. Dangerous maintenance applies create one verified pre-apply
database backup automatically.

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
