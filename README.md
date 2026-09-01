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

The package entry point does not post by default. The legacy one-process flow
is intentionally explicit because it has no external persistence checkpoint:

```bash
python3 -m wiki_philosopher_bot --unsafe-direct-post
```

For future unattended posting, prepare and dispatch are separate commands:

```bash
wiki-philosopher-prepare-post
# Persist the changed database.jsonl to the authoritative store.
wiki-philosopher-dispatch-post --attempt-id <attempt-id>
# Persist the resulting database.jsonl again.
```

The application does not perform that external checkpoint itself. A
GitHub Actions workflow can run one posting attempt manually or automatically
once per day at 16:19 Europe/Dublin time.
It checks out `database.jsonl` from a separate private authoritative data
repository, runs prepare, pushes the pending checkpoint, dispatches the exact
stored attempt once, then pushes the terminal checkpoint.

The workflow expects a repository variable named `DATA_REPOSITORY` with the
value `owner/private-data-repository`, plus the `DATA_REPO_TOKEN`,
`TELEGRAM_TOKEN`, and `TELEGRAM_CHAT_ID` repository secrets. The private data
repository contains only `database.jsonl`.

If a workflow fails after the pending checkpoint, do **not** simply rerun the
dispatch step or a new post run: the authoritative database may still contain
`pending`, `unknown`, or `failed` state. Inspect and reconcile the exact
attempt first.

### Posting-attempt reconciliation

Posting attempts are deliberately never resent automatically after an
ambiguous outcome. Inspect an attempt before taking any action:

```bash
wiki-philosopher-reconcile-post show --attempt-id <attempt-id>
```

`pending` requires investigation; `failed` records a definite Telegram
rejection and may be deliberately closed with `authorize-retry`; `unknown`
must not be retried automatically; `sent` is terminal; and `cancelled` closes
the prior attempt so a later prepare may select a fresh one. Only use
`mark-sent` or `resolve-unknown-sent` with external delivery evidence and a
Telegram message ID. `force-cancel-unknown --confirm-unsafe` is intentionally
hazardous because it can make a duplicate possible if Telegram actually
received the original message. These commands do not send Telegram messages.

## Tests

```bash
.venv/bin/python -m pytest
```

## Maintenance commands

The installed console commands are:

- `wiki-philosopher-backup`
- `wiki-philosopher-refresh-quotes`
- `wiki-philosopher-refresh-wikidata-dates`
- `wiki-philosopher-enrich-external-links` defaults to a read-only, networked
  audit of positively evidenced Wikiquote and English Wikisource links. Apply
  only an explicitly reviewed report, for example:

  ```bash
  wiki-philosopher-enrich-external-links \
    --apply-report reports/external-links/REVIEWED.json
  ```

  Apply performs no network lookup, validates every proposal against current
  state, takes one verified pre-operation backup, and atomically writes only
  the reviewed Wikiquote/Wikisource fields.

  Project Gutenberg author-link discovery uses only the canonical Wikidata
  QID's P1938 author identifier—never a name search. Review its read-only
  audit first, then apply only that exact reviewed report. The apply performs
  no network lookup, validates the complete proposal set before taking one
  verified backup, and atomically changes only
  `external_links.project_gutenberg`:

  ```bash
  wiki-philosopher-enrich-external-links --project-gutenberg

  wiki-philosopher-enrich-external-links \
    --apply-project-gutenberg-report reports/external-links/REVIEWED.json
  ```
- `wiki-philosopher-check-recent-deaths`
- `wiki-philosopher-reevaluate`
- `wiki-philosopher-purge-rejected-quotes`
- `wiki-philosopher-prepare-post`
- `wiki-philosopher-dispatch-post`
- `wiki-philosopher-reconcile-post`

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
