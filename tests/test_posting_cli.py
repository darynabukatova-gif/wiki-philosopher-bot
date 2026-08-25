import json
import threading

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.cli.dispatch_post as dispatch_cli
import wiki_philosopher_bot.cli.prepare_post as prepare_cli
import wiki_philosopher_bot.database_schema as schema
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TelegramResult,
)


def postable_entry():
    entry = schema.make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{
            "text": "A complete canonical quote.", "word_count": 5, "length": 27,
            "source": {"work": None, "year": None, "date": None, "details": None, "citation": None, "url": None},
            "retrieved_from": "Wikiquote",
        }],
    })
    return entry


def write_database(tmp_path, entry):
    (tmp_path / "database.jsonl").write_bytes(schema.serialize_database_entries([entry]))


def test_prepare_cli_persists_pending_and_prints_safe_structured_result(tmp_path, monkeypatch, capsys):
    entry = postable_entry()
    write_database(tmp_path, entry)
    monkeypatch.setattr(prepare_cli, "save_posting_phase_report", lambda *args, **kwargs: (tmp_path / "report.json", []))

    assert prepare_cli.main(["--data-folder", str(tmp_path), "--report-folder", str(tmp_path)]) == 0

    output = json.loads(capsys.readouterr().out.splitlines()[0])
    assert output["phase"] == "prepare"
    assert output["ending_state"] == "pending"
    database = cache.load_database("database.jsonl", str(tmp_path))
    assert len(database["Ada Lovelace"]["posting"]["attempts"]) == 1


def test_dispatch_cli_keeps_sent_state_when_report_write_fails(tmp_path, monkeypatch, capsys):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, entry)
    attempt = schema.make_pending_posting_attempt(
        entry["title"], entry["quotes"]["items"][0], "Exact stored payload", attempt_id="attempt-1",
    )
    cache.append_posting_attempt(database, entry["title"], attempt, "database.jsonl", str(tmp_path), threading.Lock())
    monkeypatch.setattr(dispatch_cli, "load_environment", lambda: None)
    monkeypatch.setattr(
        dispatch_cli,
        "send_message",
        lambda message: TelegramResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, 99),
    )
    monkeypatch.setattr(dispatch_cli, "save_posting_phase_report", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("reports unavailable")))

    assert dispatch_cli.main(["--attempt-id", "attempt-1", "--data-folder", str(tmp_path), "--report-folder", str(tmp_path)]) == 0

    persisted = cache.load_database("database.jsonl", str(tmp_path))[entry["title"]]
    assert persisted["posting"]["attempts"][0]["state"] == "sent"
    assert persisted["posting"]["has_been_posted"] is True
    assert "reports unavailable" in capsys.readouterr().out
