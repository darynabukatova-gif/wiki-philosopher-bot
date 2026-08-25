import json
import threading
from datetime import datetime, timezone

import pytest

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.cli.reconcile_post as reconcile_cli
import wiki_philosopher_bot.database_schema as schema
import wiki_philosopher_bot.posting_outbox as outbox
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION


NOW = datetime(2026, 8, 25, 16, 0, 0, tzinfo=timezone.utc)


def postable_entry(title="Ada Lovelace"):
    entry = schema.make_empty_database_entry(title)
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


def write_database(tmp_path, database):
    (tmp_path / "database.jsonl").write_bytes(
        schema.serialize_database_entries(list(database.values()))
    )


def append_attempt(database, tmp_path, state="pending", attempt_id="attempt-1"):
    entry = database["Ada Lovelace"]
    attempt = schema.make_pending_posting_attempt(
        entry["title"], entry["quotes"]["items"][0], "Exact stored payload",
        attempt_id=attempt_id, now=NOW,
    )
    cache.append_posting_attempt(
        database, entry["title"], attempt, "database.jsonl", str(tmp_path), threading.Lock(),
    )
    if state == "pending":
        return attempt
    transition_args = {}
    if state == "failed":
        transition_args.update(error_kind="telegram_rejected", error_summary="Telegram rejected it.")
    elif state == "unknown":
        transition_args.update(error_kind="transport_ambiguous", error_summary="Delivery is ambiguous.")
    elif state == "cancelled":
        transition_args.update(resolution_note="No dispatch occurred.")
    elif state == "sent":
        transition_args.update(telegram_message_id=7, posted_at_timestamp=7)
    cache.transition_database_posting_attempt(
        database, entry["title"], attempt_id, state, "database.jsonl", str(tmp_path),
        threading.Lock(), now=NOW, **transition_args,
    )
    return attempt


def reconcile(database, tmp_path, operation, **kwargs):
    return outbox.reconcile_posting_attempt(
        database, "attempt-1", operation, threading.Lock(), str(tmp_path),
        "database.jsonl", now=NOW, **kwargs,
    )


def test_show_is_read_only_and_hides_message_by_default(tmp_path):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path)
    before = (tmp_path / "database.jsonl").read_bytes()

    view = outbox.show_posting_attempt(database, "attempt-1")

    assert view["attempt_id"] == "attempt-1"
    assert view["has_been_posted"] is False
    assert "message_text" not in view
    assert "TELEGRAM_TOKEN" not in json.dumps(view)
    assert (tmp_path / "database.jsonl").read_bytes() == before
    assert outbox.show_posting_attempt(database, "attempt-1", include_message=True)["message_text"] == "Exact stored payload"


@pytest.mark.parametrize("initial_state,operation", [
    ("pending", "mark_sent"),
    ("unknown", "resolve_unknown_sent"),
])
def test_evidence_of_delivery_marks_sent_atomically(tmp_path, initial_state, operation):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path, initial_state)

    result = reconcile(database, tmp_path, operation, telegram_message_id=42, note="Verified in Telegram.")

    assert result.ok is True
    posting = database[entry["title"]]["posting"]
    assert posting["attempts"][-1]["state"] == "sent"
    assert posting["attempts"][-1]["telegram_message_id"] == 42
    assert posting["has_been_posted"] is True
    assert posting["posted_at"] == [int(NOW.timestamp())]
    assert outbox.unresolved_posting_attempts(database) == []


@pytest.mark.parametrize("kwargs", [
    {"telegram_message_id": 42}, {"note": "Verified delivery."},
    {"telegram_message_id": 0, "note": "Verified delivery."},
])
def test_mark_sent_requires_positive_message_id_and_note(tmp_path, kwargs):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path)

    result = reconcile(database, tmp_path, "mark_sent", **kwargs)

    assert result.ok is False
    assert database[entry["title"]]["posting"]["attempts"][-1]["state"] == "pending"


def test_cancel_pending_and_authorize_retry_close_only_the_allowed_state(tmp_path):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path)

    cancelled = reconcile(database, tmp_path, "cancel_pending", note="Workflow stopped before dispatch.")

    assert cancelled.ok is True
    assert database[entry["title"]]["posting"]["attempts"][-1]["state"] == "cancelled"
    assert outbox.unresolved_posting_attempts(database) == []
    append_attempt(database, tmp_path, "failed", attempt_id="attempt-2")
    retried = outbox.reconcile_posting_attempt(
        database, "attempt-2", "authorize_retry", threading.Lock(), str(tmp_path),
        "database.jsonl", note="Telegram explicitly rejected the request.", now=NOW,
    )
    assert retried.ok is True
    assert database[entry["title"]]["posting"]["attempts"][-1]["state"] == "cancelled"


def test_unknown_cancellation_requires_explicit_hazard_confirmation(tmp_path):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path, "unknown")

    refused = reconcile(database, tmp_path, "force_cancel_unknown", note="Evidence of non-delivery.")
    accepted = reconcile(
        database, tmp_path, "force_cancel_unknown", note="Evidence of non-delivery.",
        confirm_unsafe=True,
    )

    assert refused.error_kind == "unsafe_confirmation_required"
    assert accepted.ok is True
    assert database[entry["title"]]["posting"]["attempts"][-1]["state"] == "cancelled"


@pytest.mark.parametrize("state", ("sent", "cancelled"))
def test_terminal_attempts_cannot_be_reconciled(tmp_path, state):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path, state)

    result = reconcile(database, tmp_path, "cancel_pending", note="Never allowed.")

    assert result.ok is False
    assert result.error_kind == "invalid_state"


def test_reconciliation_persistence_failure_leaves_memory_and_file_unchanged(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path)
    before_file = (tmp_path / "database.jsonl").read_bytes()
    before_attempt = json.loads(json.dumps(database[entry["title"]]["posting"]["attempts"][-1]))
    monkeypatch.setattr(
        outbox, "_persist_terminal_attempt",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = reconcile(database, tmp_path, "mark_sent", telegram_message_id=42, note="Verified delivery.")

    assert result.error_kind == "persistence_error"
    assert database[entry["title"]]["posting"]["attempts"][-1] == before_attempt
    assert database[entry["title"]]["posting"]["has_been_posted"] is False
    assert (tmp_path / "database.jsonl").read_bytes() == before_file


def test_cli_show_and_force_unknown_warning_never_call_telegram(tmp_path, monkeypatch, capsys):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path, "unknown")
    monkeypatch.setattr(reconcile_cli, "save_posting_phase_report", lambda *args, **kwargs: (tmp_path / "report.json", []))

    assert reconcile_cli.main(["show", "--attempt-id", "attempt-1", "--data-folder", str(tmp_path)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "message_text" not in shown
    assert reconcile_cli.main([
        "--data-folder", str(tmp_path), "force-cancel-unknown", "--attempt-id", "attempt-1",
        "--reason", "Confirmed no delivery.", "--confirm-unsafe",
    ]) == 0
    assert "WARNING:" in capsys.readouterr().out


def test_cli_invalid_state_has_predictable_exit_code(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path, "sent")
    monkeypatch.setattr(reconcile_cli, "save_posting_phase_report", lambda *args, **kwargs: (tmp_path / "report.json", []))

    assert reconcile_cli.main([
        "--data-folder", str(tmp_path), "cancel-pending", "--attempt-id", "attempt-1",
        "--reason", "This cannot reopen sent state.",
    ]) == 2


def test_successful_reconciliation_survives_report_write_failure(tmp_path, monkeypatch, capsys):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    append_attempt(database, tmp_path)
    monkeypatch.setattr(
        reconcile_cli, "save_posting_phase_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("reports unavailable")),
    )

    assert reconcile_cli.main([
        "--data-folder", str(tmp_path), "mark-sent", "--attempt-id", "attempt-1",
        "--telegram-message-id", "42", "--note", "Verified delivery.",
    ]) == 0
    persisted = cache.load_database("database.jsonl", str(tmp_path))[entry["title"]]
    assert persisted["posting"]["attempts"][-1]["state"] == "sent"
    assert "reports unavailable" in capsys.readouterr().out
