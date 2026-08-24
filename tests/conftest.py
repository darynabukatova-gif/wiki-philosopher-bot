import sys

import pytest
import requests
from pathlib import Path


@pytest.fixture(autouse=True)
def isolate_maintenance_backups(monkeypatch, tmp_path):
    """Never allow command tests to create a backup in the real repository."""
    from wiki_philosopher_bot.cache import DatabaseBackupResult

    def fake_backup(*args, **kwargs):
        return DatabaseBackupResult(
            path=str(tmp_path / "backups" / "database-test.jsonl"),
            sha256="test-sha256",
            size_bytes=1,
            created_at="2026-08-23T00:00:00Z",
            label=kwargs.get("label") or (args[2] if len(args) > 2 else None),
            kind=kwargs.get("kind", "operational"),
            preserve=kwargs.get("preserve", False),
        )

    for name in (
        "refresh_quotes",
        "refresh_wikidata_dates",
        "check_recent_deaths",
        "purge_rejected_quotes",
        "reevaluate_database",
    ):
        module = sys.modules.get(name)
        if module is not None:
            monkeypatch.setattr(module, "create_database_backup", fake_backup)

@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    """Prevent tests from making real HTTP requests."""

    def deny_network(*args, **kwargs):
        raise AssertionError(
            "Network access is blocked in tests. "
            "Use a fake response or monkeypatch the API boundary."
        )

    monkeypatch.setattr(
        requests.sessions.Session,
        "request",
        deny_network,
    )

    monkeypatch.setattr(
        requests,
        "request",
        deny_network,
    )

    monkeypatch.setattr(
        requests,
        "get",
        deny_network,
    )

    monkeypatch.setattr(
        requests,
        "post",
        deny_network,
    )

    monkeypatch.setattr(
        requests.api,
        "request",
        deny_network,
    )

@pytest.fixture
def legacy_source_dir(tmp_path):
    """Create a synthetic legacy-data directory for migration tests."""

    files = {
        "summaries.jsonl": (
            '{"title":"Ada Lovelace",'
            '"summary":"Ada Lovelace was a mathematician."}\n'
        ),
        "entities.jsonl": (
            '{"title":"Ada Lovelace","valid":true,"qid":"Q7259",'
            '"instances":["Q5"],"occupations":["Q170790"],'
            '"birth":1815,"death":1852,'
            '"is_human":true,"is_philosopher":false}\n'
        ),
        "quotes.jsonl": (
            '{"title":"Ada Lovelace",'
            '"quotes":[{"text":"That brain of mine is something more than merely mortal.",'
            '"length":58,"word_count":10,"source":"Wikiquote"}]}\n'
        ),
        "quote_failures.jsonl": (
            '{"title":"Ada Lovelace","reason":"http_404",'
            '"timestamp":1,"retries":1}\n'
        ),
        "results.jsonl": (
            '{"title":"Conflict title","accepted":true,'
            '"reasons":[],"result":null}\n'
        ),
        "processed.jsonl": (
            '{"title":"Conflict title","accepted":false,'
            '"reasons":[],"result":null}\n'
        ),
        "posted.json": '["Ada Lovelace"]\n',
    }

    for filename, content in files.items():
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")

    # Optional extra file to prove the audit does not touch database.jsonl.
    (tmp_path / "database.jsonl").write_text("", encoding="utf-8")

    return tmp_path

@pytest.fixture
def snapshot_bytes():
    """Return a function that captures exact file contents."""

    def take_snapshot(directory):
        directory = Path(directory)

        return {
            file_path.name: file_path.read_bytes()
            for file_path in sorted(directory.iterdir())
            if file_path.is_file()
        }

    return take_snapshot
