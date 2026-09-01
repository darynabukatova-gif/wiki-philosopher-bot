import copy
import threading
from datetime import datetime, timezone

import pytest
import requests

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.database_schema as schema
import wiki_philosopher_bot.posting_outbox as outbox
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_AMBIGUOUS,
    TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION,
    TelegramResult,
)


NOW = datetime(2026, 8, 25, 15, 0, 0, tzinfo=timezone.utc)


def postable_entry(title="Ada Lovelace"):
    entry = schema.make_empty_database_entry(title)
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{
            "text": "A complete canonical quote.",
            "word_count": 5,
            "length": 27,
            "source": {
                "work": None, "year": None, "date": None,
                "details": None, "citation": None, "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
    })
    return entry


def write_database(tmp_path, database):
    path = tmp_path / "database.jsonl"
    path.write_bytes(schema.serialize_database_entries(list(database.values())))
    return path


def prepare(database, tmp_path, **kwargs):
    kwargs.setdefault("attempt_id", "attempt-1")
    return outbox.prepare_posting_attempt(
        database=database,
        stats={"cached_quotes": 0, "downloaded_quotes": 0, "failed_quotes": 0},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
        filename="database.jsonl",
        max_quotes=5,
        limiter=None,
        now=NOW,
        **kwargs,
    )


def append_pending(database, tmp_path, title="Ada Lovelace", attempt_id="attempt-1"):
    entry = database[title]
    quote = entry["quotes"]["items"][0]
    attempt = schema.make_pending_posting_attempt(
        title, quote, "Exact prepared payload", attempt_id=attempt_id, now=NOW,
    )
    cache.append_posting_attempt(
        database, title, attempt, "database.jsonl", str(tmp_path), threading.Lock(),
    )
    return attempt


def test_prepare_persists_one_exact_pending_attempt_without_telegram(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    quote = entry["quotes"]["items"][0]
    prepared = type("Prepared", (), {
        "selected_quote": quote,
        "message_text": "Exact prepared payload",
    })()
    monkeypatch.setattr(outbox, "select_quote_for_post", lambda *args, **kwargs: quote)
    monkeypatch.setattr(outbox, "prepare_philosopher_message", lambda *args, **kwargs: prepared)

    result = prepare(database, tmp_path)

    assert result.ok is True
    assert result.telegram_called is False
    attempt = database["Ada Lovelace"]["posting"]["attempts"][-1]
    assert attempt["attempt_id"] == "attempt-1"
    assert attempt["state"] == "pending"
    assert attempt["message_text"] == "Exact prepared payload"
    assert attempt["quote_fingerprint"] == schema.quote_fingerprint(quote)
    assert attempt["message_fingerprint"] == schema.message_fingerprint("Exact prepared payload")


@pytest.mark.parametrize("state", ("pending", "unknown", "failed"))
def test_prepare_globally_blocks_new_attempt_for_unresolved_latest_state(tmp_path, state):
    first = postable_entry("Ada Lovelace")
    second = postable_entry("Zeno")
    database = {first["title"]: first, second["title"]: second}
    write_database(tmp_path, database)
    attempt = append_pending(database, tmp_path)
    if state != "pending":
        cache.transition_database_posting_attempt(
            database, "Ada Lovelace", attempt["attempt_id"], state, "database.jsonl", str(tmp_path), threading.Lock(),
            now=NOW, error_kind="transport_ambiguous" if state == "unknown" else "telegram_rejected",
            error_summary="Prior outcome.",
        )

    result = prepare(database, tmp_path, attempt_id="attempt-2")

    assert result.ok is False
    assert result.error_kind == "unresolved_attempt"
    assert result.manual_reconciliation_required is True
    assert len(database["Zeno"]["posting"]["attempts"]) == 0


def test_prepare_is_idempotently_blocked_after_pending_persistence(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    quote = entry["quotes"]["items"][0]
    monkeypatch.setattr(outbox, "select_quote_for_post", lambda *args, **kwargs: quote)
    monkeypatch.setattr(
        outbox, "prepare_philosopher_message",
        lambda *args, **kwargs: type("Prepared", (), {"selected_quote": quote, "message_text": "Exact"})(),
    )

    first = prepare(database, tmp_path)
    second = prepare(database, tmp_path, attempt_id="attempt-2")

    assert first.ok is True
    assert second.ok is False
    assert second.error_kind == "unresolved_attempt"
    assert len(database["Ada Lovelace"]["posting"]["attempts"]) == 1


def test_sent_does_not_independently_block_and_cancelled_allows_new_prepare(tmp_path, monkeypatch):
    sent = postable_entry("Ada Lovelace")
    available = postable_entry("Zeno")
    database = {sent["title"]: sent, available["title"]: available}
    write_database(tmp_path, database)
    first = append_pending(database, tmp_path)
    cache.transition_database_posting_attempt(
        database, "Ada Lovelace", first["attempt_id"], "sent", "database.jsonl", str(tmp_path), threading.Lock(),
        now=NOW, posted_at_timestamp=1, telegram_message_id=2,
    )
    cancelled = schema.make_pending_posting_attempt(
        "Zeno", available["quotes"]["items"][0], "Old payload", attempt_id="cancelled", now=NOW,
    )
    cancelled = schema.transition_posting_attempt(cancelled, "cancelled", now=NOW, resolution_note="No dispatch.")
    cache.append_posting_attempt(database, "Zeno", cancelled, "database.jsonl", str(tmp_path), threading.Lock())
    quote = available["quotes"]["items"][0]
    monkeypatch.setattr(outbox, "select_quote_for_post", lambda *args, **kwargs: quote)
    monkeypatch.setattr(outbox, "prepare_philosopher_message", lambda *args, **kwargs: type("Prepared", (), {"selected_quote": quote, "message_text": "New payload"})())

    result = prepare(database, tmp_path, attempt_id="attempt-2")

    assert result.ok is True
    assert result.title == "Zeno"


def test_prepare_persistence_failure_leaves_no_attempt(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    monkeypatch.setattr(outbox, "append_posting_attempt", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))

    result = prepare(database, tmp_path)

    assert result.ok is False
    assert result.error_kind == "persistence_error"
    assert entry["posting"]["attempts"] == []


def test_prepare_no_candidate_is_a_clear_noop(tmp_path):
    database = {}
    write_database(tmp_path, database)

    result = prepare(database, tmp_path)

    assert result.ok is False
    assert result.error_kind == "no_candidate"


def test_dispatch_sends_exact_stored_payload_once_without_repreparing_or_link_lookup(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    attempt = append_pending(database, tmp_path)
    messages = []
    monkeypatch.setattr(
        outbox, "prepare_philosopher_message",
        lambda *args, **kwargs: pytest.fail("dispatch must not reprepare the message"),
    )
    monkeypatch.setattr(
        outbox, "select_quote_for_post",
        lambda *args, **kwargs: pytest.fail("dispatch must not reselect a quote"),
    )

    result = outbox.dispatch_posting_attempt(
        database, attempt["attempt_id"], threading.Lock(), str(tmp_path), "database.jsonl",
        now=NOW,
        send=lambda message: messages.append(message) or TelegramResult(
            True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, 123,
        ),
    )

    assert result.ok is True
    assert messages == ["Exact prepared payload"]
    posting = database["Ada Lovelace"]["posting"]
    assert posting["attempts"][0]["state"] == "sent"
    assert posting["has_been_posted"] is True
    assert posting["posted_at"] == [int(NOW.timestamp())]


@pytest.mark.parametrize("state", ("sent", "failed", "unknown", "cancelled"))
def test_dispatch_rejects_non_pending_attempt_without_telegram(tmp_path, state):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    attempt = append_pending(database, tmp_path)
    if state == "sent":
        cache.transition_database_posting_attempt(database, entry["title"], attempt["attempt_id"], state, "database.jsonl", str(tmp_path), threading.Lock(), now=NOW, posted_at_timestamp=1, telegram_message_id=2)
    elif state == "cancelled":
        cache.transition_database_posting_attempt(database, entry["title"], attempt["attempt_id"], state, "database.jsonl", str(tmp_path), threading.Lock(), now=NOW, resolution_note="No dispatch.")
    else:
        cache.transition_database_posting_attempt(database, entry["title"], attempt["attempt_id"], state, "database.jsonl", str(tmp_path), threading.Lock(), now=NOW, error_kind="telegram_rejected" if state == "failed" else "transport_ambiguous", error_summary="Prior outcome.")

    result = outbox.dispatch_posting_attempt(database, attempt["attempt_id"], threading.Lock(), str(tmp_path), "database.jsonl", send=lambda *_: pytest.fail("must not send"))

    assert result.ok is False
    assert result.telegram_called is False


def test_dispatch_rejects_missing_stale_or_fingerprint_mismatched_attempt(tmp_path):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    first = append_pending(database, tmp_path, attempt_id="first")
    second = append_pending(database, tmp_path, attempt_id="second")

    for attempt_id in ("missing", first["attempt_id"]):
        result = outbox.dispatch_posting_attempt(database, attempt_id, threading.Lock(), str(tmp_path), "database.jsonl", send=lambda *_: pytest.fail("must not send"))
        assert result.ok is False
        assert result.telegram_called is False

    database["Ada Lovelace"]["posting"]["attempts"][-1]["message_fingerprint"] = "0" * 64
    result = outbox.dispatch_posting_attempt(database, second["attempt_id"], threading.Lock(), str(tmp_path), "database.jsonl", send=lambda *_: pytest.fail("must not send"))
    assert result.ok is False
    assert result.telegram_called is False


def test_dispatch_definite_rejection_and_ambiguous_failure_persist_terminal_state(tmp_path):
    for outcome, expected_state, error_kind in (
        (TELEGRAM_OUTCOME_DEFINITE_REJECTION, "failed", "telegram_rejected"),
        (TELEGRAM_OUTCOME_AMBIGUOUS, "unknown", "transport_ambiguous"),
    ):
        directory = tmp_path / expected_state
        directory.mkdir()
        entry = postable_entry()
        database = {entry["title"]: entry}
        write_database(directory, database)
        attempt = append_pending(database, directory)
        calls = []
        result = outbox.dispatch_posting_attempt(
            database, attempt["attempt_id"], threading.Lock(), str(directory), "database.jsonl", now=NOW,
            send=lambda message: calls.append(message) or TelegramResult(False, None, "request_exception", outcome),
        )
        assert calls == ["Exact prepared payload"]
        assert result.ending_state == expected_state
        assert database["Ada Lovelace"]["posting"]["attempts"][0]["error_kind"] == error_kind
        assert database["Ada Lovelace"]["posting"]["has_been_posted"] is False


def test_dispatch_request_exception_is_ambiguous_and_calls_transport_once(tmp_path):
    entry = postable_entry()
    database = {entry["title"]: entry}
    write_database(tmp_path, database)
    attempt = append_pending(database, tmp_path)
    calls = []

    def timeout_once(message):
        calls.append(message)
        raise requests.Timeout("timeout")

    result = outbox.dispatch_posting_attempt(
        database, attempt["attempt_id"], threading.Lock(), str(tmp_path), "database.jsonl",
        now=NOW, send=timeout_once,
    )

    assert calls == ["Exact prepared payload"]
    assert result.ending_state == "unknown"
    assert result.manual_reconciliation_required is True


def test_post_send_persistence_failure_leaves_pending_and_never_resends(tmp_path, monkeypatch):
    entry = postable_entry()
    database = {entry["title"]: entry}
    path = write_database(tmp_path, database)
    attempt = append_pending(database, tmp_path)
    before_disk = path.read_bytes()
    calls = []
    monkeypatch.setattr(outbox, "_persist_terminal_attempt", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("full")))

    result = outbox.dispatch_posting_attempt(
        database, attempt["attempt_id"], threading.Lock(), str(tmp_path), "database.jsonl", now=NOW,
        send=lambda message: calls.append(message) or TelegramResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, 77),
    )

    assert calls == ["Exact prepared payload"]
    assert result.manual_reconciliation_required is True
    assert database["Ada Lovelace"]["posting"]["attempts"][0]["state"] == "pending"
    assert database["Ada Lovelace"]["posting"]["has_been_posted"] is False
    assert path.read_bytes() == before_disk
