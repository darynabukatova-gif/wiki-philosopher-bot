"""Durable local outbox primitives for private recent-death notifications."""
from dataclasses import dataclass
from typing import Optional
from datetime import date
import html

from wiki_philosopher_bot.cache import transition_database_recent_death_notification
from wiki_philosopher_bot.database_schema import (
    message_fingerprint,
    recent_death_notification_by_id,
    recent_death_notifications,
    transition_recent_death_notification,
    UNRESOLVED_RECENT_DEATH_NOTIFICATION_STATES,
)
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_AMBIGUOUS,
    TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION,
    send_message_to_chat,
)


@dataclass
class RecentDeathOperationResult:
    ok: bool
    operation: str
    notification_id: Optional[str] = None
    title: Optional[str] = None
    starting_state: Optional[str] = None
    ending_state: Optional[str] = None
    telegram_called: bool = False
    telegram_message_id: Optional[int] = None
    error_kind: Optional[str] = None
    error_summary: Optional[str] = None
    persistence_succeeded: bool = False

    def as_report(self):
        return self.__dict__.copy()


def format_recent_death_notification(entry, death_date):
    """Build the exact, safe private HTML payload from stored canonical data."""
    display_title = entry.get("display_title") or entry.get("title")
    if not isinstance(display_title, str) or not display_title:
        raise ValueError("recent-death notification requires a canonical display title")
    parsed_date = date.fromisoformat(death_date)
    return "\n".join((
        "<b>Recent philosopher death detected</b>",
        "",
        html.escape(display_title),
        "Died: {}".format(parsed_date.strftime("%-d %B %Y")),
    ))


def unresolved_recent_death_notifications(database):
    matches = []
    for title, entry in database.items():
        for notification in recent_death_notifications(entry):
            if notification.get("state") in UNRESOLVED_RECENT_DEATH_NOTIFICATION_STATES:
                matches.append((title, notification))
    return matches


def locate_recent_death_notification(database, notification_id):
    matches = []
    for title, entry in database.items():
        notification = recent_death_notification_by_id(entry, notification_id)
        if notification is not None:
            matches.append((title, entry, notification))
    if len(matches) != 1:
        return None
    return matches[0]


def dispatch_recent_death_notification(
    database, notification_id, data_folder, telegram_url, chat_id, persistence_lock,
    sender=None, now=None,
):
    """Send one exact pending payload at most once and persist its terminal state.

    A caller must only invoke this after its external pending checkpoint. The
    durable state cannot prove a stale pending event was never sent, so a retry
    of an old pending event is deliberately an operator decision, not an
    automatic recovery action.
    """
    located = locate_recent_death_notification(database, notification_id)
    if located is None:
        return RecentDeathOperationResult(False, "dispatch", notification_id=notification_id, error_kind="not_found", error_summary="notification was not found")
    title, _entry, notification = located
    state = notification.get("state")
    result = RecentDeathOperationResult(False, "dispatch", notification_id, title, state)
    if state != "pending":
        result.error_kind = "invalid_state"
        result.error_summary = "only a pending notification can be dispatched"
        return result
    if notification.get("message_fingerprint") != message_fingerprint(notification.get("message_text")):
        result.error_kind = "message_fingerprint_mismatch"
        result.error_summary = "stored notification message fingerprint does not match"
        return result
    if sender is None:
        sender = send_message_to_chat
    telegram_result = sender(notification["message_text"], telegram_url, chat_id)
    result.telegram_called = True
    if telegram_result.outcome == TELEGRAM_OUTCOME_CONFIRMED_SUCCESS and isinstance(telegram_result.message_id, int) and telegram_result.message_id > 0:
        new_state, error_kind, summary, message_id = "sent", None, None, telegram_result.message_id
    elif telegram_result.outcome == TELEGRAM_OUTCOME_DEFINITE_REJECTION:
        new_state, error_kind, summary, message_id = "failed", "telegram_rejected", "Telegram explicitly rejected the notification", None
    else:
        kind = "response_invalid" if telegram_result.error_reason in ("invalid_json", "invalid_response") or telegram_result.ok else "transport_ambiguous"
        new_state, error_kind, summary, message_id = "unknown", kind, "Telegram delivery could not be established", None
    try:
        transition_database_recent_death_notification(
            database, title, notification_id, new_state, "database.jsonl", data_folder,
            persistence_lock, now=now, telegram_message_id=message_id,
            error_kind=error_kind, error_summary=summary,
        )
    except (OSError, ValueError, KeyError) as error:
        result.error_kind = "persistence_error"
        result.error_summary = "terminal notification state could not be persisted"
        result.ending_state = state
        return result
    result.ok = new_state == "sent"
    result.ending_state = new_state
    result.telegram_message_id = message_id
    result.error_kind = error_kind
    result.error_summary = summary
    result.persistence_succeeded = True
    return result


def reconcile_recent_death_notification(
    database, notification_id, operation, data_folder, persistence_lock,
    telegram_message_id=None, note=None, now=None,
):
    located = locate_recent_death_notification(database, notification_id)
    if located is None:
        return RecentDeathOperationResult(False, operation, notification_id=notification_id, error_kind="not_found", error_summary="notification was not found")
    title, _entry, notification = located
    if operation == "show":
        return RecentDeathOperationResult(True, operation, notification_id, title, notification.get("state"), notification.get("state"), persistence_succeeded=True)
    if operation == "mark-sent":
        new_state = "sent"
        if notification.get("state") not in ("pending", "unknown"):
            return RecentDeathOperationResult(False, operation, notification_id, title, notification.get("state"), error_kind="invalid_state", error_summary="only pending or unknown notifications can be marked sent")
        if not note or not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool) or telegram_message_id <= 0:
            return RecentDeathOperationResult(False, operation, notification_id, title, notification.get("state"), error_kind="validation", error_summary="mark-sent requires a positive message ID and note")
    elif operation == "cancel":
        new_state = "cancelled"
        if notification.get("state") not in ("pending", "failed", "unknown") or not note:
            return RecentDeathOperationResult(False, operation, notification_id, title, notification.get("state"), error_kind="validation", error_summary="cancel requires an unresolved notification and note")
    else:
        return RecentDeathOperationResult(False, operation, notification_id, title, notification.get("state"), error_kind="invalid_operation", error_summary="unsupported reconciliation operation")
    try:
        transition_database_recent_death_notification(
            database, title, notification_id, new_state, "database.jsonl", data_folder,
            persistence_lock, now=now, telegram_message_id=telegram_message_id,
            resolution_note=note,
        )
    except (OSError, ValueError, KeyError):
        return RecentDeathOperationResult(False, operation, notification_id, title, notification.get("state"), error_kind="persistence_error", error_summary="notification reconciliation could not be persisted")
    return RecentDeathOperationResult(True, operation, notification_id, title, notification.get("state"), new_state, telegram_message_id=telegram_message_id, persistence_succeeded=True)
