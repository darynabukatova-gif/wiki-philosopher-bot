import copy
import threading
from datetime import datetime, timezone

import pytest

import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.database_schema as database_schema


NOW = datetime(2026, 8, 25, 12, 34, 56, tzinfo=timezone.utc)
TITLE = "Ada Lovelace"
QUOTE = {
    "text": "That brain of mine is something more than merely mortal.",
    "source": {
        "work": "Letter to Charles Babbage",
        "year": 1843,
        "date": None,
        "details": None,
        "citation": "Letter",
        "url": None,
    },
}
MESSAGE = "Ada Lovelace\n\nThat brain of mine is something more than merely mortal."


def make_attempt(attempt_id="attempt-1"):
    return database_schema.make_pending_posting_attempt(
        TITLE, QUOTE, MESSAGE, attempt_id=attempt_id, now=NOW,
    )


def write_database(tmp_path, entry):
    path = tmp_path / "database.jsonl"
    path.write_bytes(database_schema.serialize_database_entries([entry]))
    return path


def test_legacy_posting_without_attempts_remains_valid_and_queries_as_empty():
    entry = database_schema.make_empty_database_entry(TITLE)
    del entry["posting"]["attempts"]

    assert database_schema.validate_database_entry(entry) == []
    assert database_schema.posting_attempts(entry) == []
    assert database_schema.latest_posting_attempt(entry) is None
    assert database_schema.has_unresolved_posting_attempt(entry) is False


def test_existing_posted_record_without_attempts_remains_valid():
    entry = database_schema.make_empty_database_entry(TITLE)
    del entry["posting"]["attempts"]
    entry["posting"].update({"has_been_posted": True, "posted_at": [1]})

    assert database_schema.validate_database_entry(entry) == []


@pytest.mark.parametrize("state", database_schema.POSTING_ATTEMPT_STATES)
def test_attempt_validator_accepts_every_supported_state(state):
    attempt = make_attempt()
    if state == "sent":
        attempt = database_schema.transition_posting_attempt(
            attempt, state, now=NOW, telegram_message_id=123,
        )
    elif state in ("failed", "unknown"):
        attempt = database_schema.transition_posting_attempt(
            attempt,
            state,
            now=NOW,
            error_kind="telegram_rejected" if state == "failed" else "transport_ambiguous",
            error_summary="Short safe error.",
        )
    elif state == "cancelled":
        attempt = database_schema.transition_posting_attempt(
            attempt, state, now=NOW, resolution_note="Confirmed no dispatch.",
        )

    assert attempt["state"] == state
    assert database_schema.validate_posting_attempt(attempt) == []


@pytest.mark.parametrize(
    "field,value,error_fragment",
    (
        ("state", "mystery", "supported state"),
        ("attempt_id", "", "non-empty string"),
        ("quote_fingerprint", "not-a-fingerprint", "SHA-256"),
        ("telegram_message_id", "99", "positive integer"),
    ),
)
def test_attempt_validator_rejects_malformed_fields(field, value, error_fragment):
    attempt = make_attempt()
    attempt[field] = value

    assert any(error_fragment in error for error in database_schema.validate_posting_attempt(attempt))


def test_attempt_validator_rejects_secret_like_error_summary():
    attempt = database_schema.transition_posting_attempt(
        make_attempt(),
        "failed",
        now=NOW,
        error_kind="telegram_rejected",
        error_summary="Rejected.",
    )
    attempt["error_summary"] = "telegram_token=123456:abcdefghijklmnopqrstuvwxyz"

    assert any("safe short" in error for error in database_schema.validate_posting_attempt(attempt))


def test_attempt_validator_rejects_malformed_attempt_structure():
    assert database_schema.validate_posting_attempt([]) == [
        "posting.attempts item must be an object"
    ]


def test_quote_fingerprint_is_deterministic_and_key_order_independent():
    reordered_quote = {
        "source": {
            "url": None,
            "citation": "Letter",
            "details": None,
            "date": None,
            "year": 1843,
            "work": "Letter to Charles Babbage",
        },
        "text": QUOTE["text"],
    }

    assert database_schema.quote_fingerprint(QUOTE) == database_schema.quote_fingerprint(reordered_quote)


def test_quote_fingerprint_changes_for_text_or_structured_source_change():
    changed_text = copy.deepcopy(QUOTE)
    changed_text["text"] += " Indeed."
    changed_source = copy.deepcopy(QUOTE)
    changed_source["source"]["year"] = 1844

    fingerprint = database_schema.quote_fingerprint(QUOTE)
    assert database_schema.quote_fingerprint(changed_text) != fingerprint
    assert database_schema.quote_fingerprint(changed_source) != fingerprint


def test_message_fingerprint_is_exact_and_deterministic():
    assert database_schema.message_fingerprint(MESSAGE) == database_schema.message_fingerprint(MESSAGE)
    assert database_schema.message_fingerprint(MESSAGE + " ") != database_schema.message_fingerprint(MESSAGE)


def test_pending_attempt_creation_uses_injected_values_exactly():
    attempt = make_attempt("injected-id")

    assert attempt["attempt_id"] == "injected-id"
    assert attempt["created_at"] == "2026-08-25T12:34:56Z"
    assert attempt["state_changed_at"] == attempt["created_at"]
    assert attempt["state"] == "pending"
    assert attempt["message_text"] == MESSAGE
    assert attempt["quote_fingerprint"] == database_schema.quote_fingerprint(QUOTE)


def test_attempt_queries_use_latest_attempt_and_unresolved_definition():
    entry = database_schema.make_empty_database_entry(TITLE)
    pending = make_attempt("pending")
    cancelled = database_schema.transition_posting_attempt(
        pending, "cancelled", now=NOW, resolution_note="No dispatch.",
    )
    entry["posting"]["attempts"] = [pending, cancelled]

    assert database_schema.posting_attempt_by_id(entry, "pending") == pending
    assert database_schema.latest_posting_attempt(entry) == cancelled
    assert database_schema.has_unresolved_posting_attempt(entry) is False


@pytest.mark.parametrize(
    "old_state,new_state,kwargs",
    (
        ("pending", "sent", {"telegram_message_id": 1}),
        ("pending", "failed", {"error_kind": "telegram_rejected", "error_summary": "Rejected."}),
        ("pending", "unknown", {"error_kind": "transport_ambiguous", "error_summary": "Timeout."}),
        ("pending", "cancelled", {"resolution_note": "Not dispatched."}),
        ("failed", "cancelled", {"resolution_note": "Operator approved retry."}),
        ("unknown", "sent", {"telegram_message_id": 2}),
        ("unknown", "cancelled", {"resolution_note": "Operator verified no send."}),
    ),
)
def test_allowed_attempt_transitions(old_state, new_state, kwargs):
    attempt = make_attempt()
    if old_state != "pending":
        attempt = database_schema.transition_posting_attempt(
            attempt,
            old_state,
            now=NOW,
            error_kind="telegram_rejected" if old_state == "failed" else "transport_ambiguous",
            error_summary="Prior outcome.",
        )

    updated = database_schema.transition_posting_attempt(attempt, new_state, now=NOW, **kwargs)

    assert updated["state"] == new_state


@pytest.mark.parametrize(
    "old_state,new_state,kwargs",
    (
        ("pending", "sent", {}),
        ("pending", "failed", {}),
        ("pending", "cancelled", {}),
        ("sent", "pending", {}),
        ("cancelled", "pending", {}),
        ("failed", "sent", {"telegram_message_id": 1}),
    ),
)
def test_prohibited_or_incomplete_attempt_transitions(old_state, new_state, kwargs):
    attempt = make_attempt()
    if old_state == "sent":
        attempt = database_schema.transition_posting_attempt(attempt, "sent", now=NOW, telegram_message_id=1)
    elif old_state == "cancelled":
        attempt = database_schema.transition_posting_attempt(attempt, "cancelled", now=NOW, resolution_note="No dispatch.")
    elif old_state == "failed":
        attempt = database_schema.transition_posting_attempt(
            attempt, "failed", now=NOW, error_kind="telegram_rejected", error_summary="Rejected.",
        )

    with pytest.raises(ValueError):
        database_schema.transition_posting_attempt(attempt, new_state, now=NOW, **kwargs)


def test_attempt_append_and_sent_transition_are_atomic(tmp_path):
    entry = database_schema.make_empty_database_entry(TITLE)
    write_database(tmp_path, entry)
    database = {TITLE: entry}
    attempt = make_attempt()

    cache.append_posting_attempt(database, TITLE, attempt, "database.jsonl", str(tmp_path), threading.Lock())
    cache.transition_database_posting_attempt(
        database, TITLE, attempt["attempt_id"], "sent", "database.jsonl", str(tmp_path), threading.Lock(),
        now=NOW, posted_at_timestamp=123, telegram_message_id=456,
    )

    persisted = cache.load_database("database.jsonl", str(tmp_path))[TITLE]
    assert persisted["posting"]["attempts"][0]["state"] == "sent"
    assert persisted["posting"]["has_been_posted"] is True
    assert persisted["posting"]["posted_at"] == [123]
    assert persisted == database[TITLE]


def test_append_rejects_duplicate_id_and_title_mismatch(tmp_path):
    entry = database_schema.make_empty_database_entry(TITLE)
    write_database(tmp_path, entry)
    database = {TITLE: entry}
    attempt = make_attempt()
    cache.append_posting_attempt(database, TITLE, attempt, "database.jsonl", str(tmp_path), threading.Lock())

    with pytest.raises(ValueError, match="Duplicate posting attempt ID"):
        cache.append_posting_attempt(database, TITLE, attempt, "database.jsonl", str(tmp_path), threading.Lock())

    mismatch = make_attempt("other-id")
    mismatch["title"] = "Different title"
    with pytest.raises(ValueError, match="title must match"):
        cache.append_posting_attempt(database, TITLE, mismatch, "database.jsonl", str(tmp_path), threading.Lock())

    with pytest.raises(KeyError, match="missing title"):
        cache.append_posting_attempt(database, "Missing", make_attempt("missing-id"), "database.jsonl", str(tmp_path), threading.Lock())


def test_attempt_write_failure_leaves_memory_and_disk_unchanged(tmp_path, monkeypatch):
    entry = database_schema.make_empty_database_entry(TITLE)
    path = write_database(tmp_path, entry)
    database = {TITLE: entry}
    before_memory = copy.deepcopy(database)
    before_disk = path.read_bytes()

    monkeypatch.setattr(cache, "_rewrite_database_unlocked", lambda *args: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError, match="full"):
        cache.append_posting_attempt(database, TITLE, make_attempt(), "database.jsonl", str(tmp_path), threading.Lock())

    assert database == before_memory
    assert path.read_bytes() == before_disk


def test_sent_transition_write_failure_leaves_all_posting_fields_unchanged(tmp_path, monkeypatch):
    entry = database_schema.make_empty_database_entry(TITLE)
    entry["posting"]["attempts"].append(make_attempt())
    path = write_database(tmp_path, entry)
    database = {TITLE: entry}
    before_memory = copy.deepcopy(database)
    before_disk = path.read_bytes()

    monkeypatch.setattr(cache, "_rewrite_database_unlocked", lambda *args: (_ for _ in ()).throw(OSError("full")))

    with pytest.raises(OSError, match="full"):
        cache.transition_database_posting_attempt(
            database, TITLE, "attempt-1", "sent", "database.jsonl", str(tmp_path), threading.Lock(),
            now=NOW, posted_at_timestamp=123, telegram_message_id=456,
        )

    assert database == before_memory
    assert path.read_bytes() == before_disk
