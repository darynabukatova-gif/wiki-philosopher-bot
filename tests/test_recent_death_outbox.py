from datetime import datetime, timezone

import pytest

from wiki_philosopher_bot.cache import append_recent_death_notification
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry, make_pending_recent_death_notification,
    recent_death_notifications, serialize_database_entries,
    validate_database_entry,
)
from wiki_philosopher_bot.recent_death_outbox import (
    dispatch_recent_death_notification, reconcile_recent_death_notification,
    unresolved_recent_death_notifications,
)
from wiki_philosopher_bot.runtime import persistence_lock
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_AMBIGUOUS, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION, TelegramResult,
)


def entry(title="Ada", qid="Q1"):
    value = make_empty_database_entry(title)
    value["wikidata"].update(status="available", qid=qid, is_human=True, is_philosopher=True)
    value["evaluation"].update(status="accepted", algorithm_version=2)
    return value


def write_db(tmp_path, records):
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries(records))


def pending(title="Ada", qid="Q1", death_date="2026-06-29", notification_id="n-1"):
    return make_pending_recent_death_notification(title, qid, death_date, "<b>Ada</b>\nDied: 29 June 2026", notification_id=notification_id, now=datetime(2026, 8, 1, tzinfo=timezone.utc))


def test_historical_record_without_notifications_remains_valid():
    assert validate_database_entry(entry()) == []
    assert recent_death_notifications(entry()) == []


def test_pending_event_is_exact_and_transition_validation_is_conservative():
    event = pending()
    assert event["state"] == "pending"
    assert event["message_fingerprint"]
    with pytest.raises(ValueError):
        make_pending_recent_death_notification("Ada", "not-qid", "2026-06-29", "message")


def test_dispatch_sends_exact_stored_payload_once_and_marks_sent(tmp_path):
    record = entry(); event = pending(); record["recent_death_notifications"] = [event]
    database = {"Ada": record}; write_db(tmp_path, [record]); sent = []
    result = dispatch_recent_death_notification(
        database, "n-1", str(tmp_path), "url", "chat", persistence_lock,
        sender=lambda text, url, chat: sent.append((text, url, chat)) or TelegramResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, 77),
    )
    assert result.ok and result.telegram_called and result.telegram_message_id == 77
    assert sent == [(event["message_text"], "url", "chat")]
    assert database["Ada"]["recent_death_notifications"][0]["state"] == "sent"


@pytest.mark.parametrize("telegram_result, expected_state", [
    (TelegramResult(False, {"ok": False}, "telegram_error", TELEGRAM_OUTCOME_DEFINITE_REJECTION), "failed"),
    (TelegramResult(False, None, "request_exception", TELEGRAM_OUTCOME_AMBIGUOUS), "unknown"),
    (TelegramResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_AMBIGUOUS), "unknown"),
])
def test_dispatch_rejection_and_ambiguity_are_never_sent(tmp_path, telegram_result, expected_state):
    record = entry(); record["recent_death_notifications"] = [pending()]
    database = {"Ada": record}; write_db(tmp_path, [record]); calls=[]
    result = dispatch_recent_death_notification(database, "n-1", str(tmp_path), "url", "chat", persistence_lock, sender=lambda *args: calls.append(args) or telegram_result)
    assert len(calls) == 1 and not result.ok
    assert database["Ada"]["recent_death_notifications"][0]["state"] == expected_state


def test_fingerprint_mismatch_and_terminal_events_never_call_telegram(tmp_path):
    record = entry(); event = pending(); event["message_fingerprint"] = "0" * 64
    # malformed state is intentionally rejected before dispatch by normal load;
    # use an in-memory corrupted event to prove the transport guard too.
    record["recent_death_notifications"] = [event]; database={"Ada": record}; calls=[]
    result = dispatch_recent_death_notification(database, "n-1", str(tmp_path), "url", "chat", persistence_lock, sender=lambda *args: calls.append(args))
    assert not result.ok and not calls


def test_reconcile_mark_sent_and_cancel_are_explicit_and_atomic(tmp_path):
    record = entry(); record["recent_death_notifications"] = [pending()]
    database={"Ada":record}; write_db(tmp_path,[record])
    marked = reconcile_recent_death_notification(database, "n-1", "mark-sent", str(tmp_path), persistence_lock, telegram_message_id=8, note="verified in private chat")
    assert marked.ok and database["Ada"]["recent_death_notifications"][0]["state"] == "sent"
    assert not unresolved_recent_death_notifications(database)
    # terminal notifications cannot reopen or cancel.
    assert not reconcile_recent_death_notification(database, "n-1", "cancel", str(tmp_path), persistence_lock, note="no").ok


def test_unresolved_death_events_are_separate_from_normal_posting_outbox(tmp_path):
    record = entry(); record["recent_death_notifications"] = [pending()]
    database={"Ada":record}; write_db(tmp_path,[record])
    assert unresolved_recent_death_notifications(database)[0][0] == "Ada"
    assert record["posting"].get("attempts", []) == []


def test_append_persistence_failure_does_not_change_memory(tmp_path, monkeypatch):
    record=entry(); database={"Ada":record}; write_db(tmp_path,[record]); before=dict(record)
    import wiki_philosopher_bot.cache as cache
    monkeypatch.setattr(cache, "_rewrite_database_unlocked", lambda *args: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        append_recent_death_notification(database, "Ada", pending(), "database.jsonl", str(tmp_path), persistence_lock)
    assert database["Ada"] == before
