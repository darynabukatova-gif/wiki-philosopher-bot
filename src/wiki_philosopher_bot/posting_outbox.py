"""Two-phase durable posting-attempt primitives without deployment coupling."""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import time

import requests

from wiki_philosopher_bot.cache import (
    append_posting_attempt,
    transition_database_posting_attempt,
)
from wiki_philosopher_bot.database_schema import (
    has_unresolved_posting_attempt,
    latest_posting_attempt,
    make_pending_posting_attempt,
    message_fingerprint,
    posting_attempt_by_id,
    sanitize_posting_attempt_resolution_note,
)
from wiki_philosopher_bot.presentation import (
    prepare_philosopher_message,
    select_quote_for_post,
)
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_FAILURE,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION,
    TelegramResult,
    send_message,
)
from wiki_philosopher_bot.utils import get_random_philosopher


@dataclass(frozen=True)
class PostingOperationResult:
    phase: str
    ok: bool
    operation: str = None
    attempt_id: str = None
    title: str = None
    starting_state: str = None
    ending_state: str = None
    telegram_called: bool = False
    telegram_message_id: int = None
    error_kind: str = None
    error_summary: str = None
    persistence_succeeded: bool = False
    manual_reconciliation_required: bool = False
    resolution_note: str = None

    def as_report(self):
        return asdict(self)


def _now_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def unresolved_posting_attempts(database):
    """Return newest unresolved attempts globally, without changing state."""
    unresolved = []
    for title, entry in database.items():
        if has_unresolved_posting_attempt(entry):
            attempt = latest_posting_attempt(entry)
            unresolved.append((title, attempt))
    return unresolved


def prepare_posting_attempt(
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    filename,
    max_quotes,
    limiter=None,
    candidate_chooser=None,
    quote_chooser=None,
    attempt_id=None,
    now=None,
):
    """Select and durably persist one pending attempt without calling Telegram."""
    unresolved = unresolved_posting_attempts(database)
    if unresolved:
        title, attempt = unresolved[0]
        return PostingOperationResult(
            phase="prepare",
            ok=False,
            attempt_id=attempt.get("attempt_id"),
            title=title,
            starting_state=attempt.get("state"),
            ending_state=attempt.get("state"),
            error_kind="unresolved_attempt",
            error_summary="An unresolved posting attempt requires manual reconciliation.",
            manual_reconciliation_required=True,
        )

    candidate_kwargs = {}
    if candidate_chooser is not None:
        candidate_kwargs["chooser"] = candidate_chooser
    philosopher = get_random_philosopher(database, **candidate_kwargs)
    if philosopher is None:
        return PostingOperationResult(
            phase="prepare",
            ok=False,
            error_kind="no_candidate",
            error_summary="No eligible philosopher is available for posting.",
        )

    title = philosopher["title"]
    selected_quote = select_quote_for_post(
        philosopher,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        max_quotes=max_quotes,
        limiter=limiter,
        chooser=quote_chooser,
    )
    if selected_quote is None:
        return PostingOperationResult(
            phase="prepare",
            ok=False,
            title=title,
            error_kind="no_quote",
            error_summary="The selected candidate has no quote available for preparation.",
        )

    prepared = prepare_philosopher_message(philosopher, selected_quote)
    attempt = make_pending_posting_attempt(
        title,
        prepared.selected_quote,
        prepared.message_text,
        attempt_id=attempt_id,
        now=_now_utc(now),
    )
    try:
        append_posting_attempt(
            database,
            title,
            attempt,
            filename,
            data_folder,
            persistence_lock,
        )
    except (OSError, ValueError, KeyError):
        return PostingOperationResult(
            phase="prepare",
            ok=False,
            attempt_id=attempt["attempt_id"],
            title=title,
            starting_state=None,
            ending_state=None,
            error_kind="persistence_error",
            error_summary="The pending posting attempt could not be persisted.",
        )

    return PostingOperationResult(
        phase="prepare",
        ok=True,
        attempt_id=attempt["attempt_id"],
        title=title,
        starting_state=None,
        ending_state="pending",
        persistence_succeeded=True,
    )


def _find_attempt(database, attempt_id):
    matches = []
    for title, entry in database.items():
        attempt = posting_attempt_by_id(entry, attempt_id)
        if attempt is not None:
            matches.append((title, entry, attempt))
    return matches


def show_posting_attempt(database, attempt_id, include_message=False):
    """Return safe read-only attempt data for one exact globally unique ID."""
    matches = _find_attempt(database, attempt_id)
    if len(matches) != 1:
        return None
    title, entry, attempt = matches[0]
    if attempt.get("title") != title:
        return None
    view = {
        "attempt_id": attempt["attempt_id"],
        "title": title,
        "state": attempt["state"],
        "created_at": attempt["created_at"],
        "state_changed_at": attempt["state_changed_at"],
        "quote_fingerprint": attempt["quote_fingerprint"],
        "message_fingerprint": attempt["message_fingerprint"],
        "telegram_message_id": attempt["telegram_message_id"],
        "error_kind": attempt["error_kind"],
        "error_summary": attempt["error_summary"],
        "resolution_note": attempt["resolution_note"],
        "has_been_posted": entry["posting"]["has_been_posted"],
    }
    if include_message:
        view["message_text"] = attempt["message_text"]
    return view


def reconcile_posting_attempt(
    database,
    attempt_id,
    operation,
    persistence_lock,
    data_folder,
    filename,
    note=None,
    telegram_message_id=None,
    confirm_unsafe=False,
    now=None,
):
    """Apply one explicit, validated operator reconciliation transition."""
    if not isinstance(attempt_id, str) or not attempt_id:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False,
            error_kind="invalid_attempt", error_summary="A non-empty attempt ID is required.",
        )
    matches = _find_attempt(database, attempt_id)
    if len(matches) != 1:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id,
            error_kind="invalid_attempt", error_summary="The attempt ID must identify exactly one canonical attempt.",
        )
    title, entry, attempt = matches[0]
    if attempt.get("title") != title:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            error_kind="invalid_attempt", error_summary="The attempt title does not match its canonical record.",
        )

    starting_state = attempt.get("state")
    expected_states = {
        "mark_sent": ("pending", "unknown"),
        "cancel_pending": ("pending",),
        "authorize_retry": ("failed",),
        "resolve_unknown_sent": ("unknown",),
        "force_cancel_unknown": ("unknown",),
    }
    if operation not in expected_states:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="invalid_operation", error_summary="Unsupported reconciliation operation.",
        )
    if starting_state not in expected_states[operation]:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="invalid_state", error_summary="This reconciliation operation is not allowed from the current state.",
        )
    if operation == "force_cancel_unknown" and not confirm_unsafe:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="unsafe_confirmation_required",
            error_summary="Cancelling an unknown attempt can cause a duplicate post; --confirm-unsafe is required.",
            manual_reconciliation_required=True,
        )
    if not isinstance(note, str) or not note.strip():
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="validation_error", error_summary="A non-empty operator note or reason is required.",
        )

    if operation in ("mark_sent", "resolve_unknown_sent"):
        next_state = "sent"
        if not isinstance(telegram_message_id, int) or isinstance(telegram_message_id, bool) or telegram_message_id <= 0:
            return PostingOperationResult(
                phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
                starting_state=starting_state, ending_state=starting_state,
                error_kind="validation_error", error_summary="A positive Telegram message ID is required.",
            )
    else:
        next_state = "cancelled"

    # `sent` cannot retain an unresolved error by schema contract. Preserve a
    # concise indication of the former state/error in the durable operator note.
    try:
        resolution_note = sanitize_posting_attempt_resolution_note(note)
    except ValueError:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="validation_error", error_summary="The operator note or reason must be short and safe.",
        )
    if starting_state == "unknown" and attempt.get("error_kind"):
        resolution_note += " [reconciled from unknown: {}]".format(attempt["error_kind"])
    if starting_state == "failed" and attempt.get("error_kind"):
        resolution_note += " [retry authorized after failed: {}]".format(attempt["error_kind"])
    try:
        resolution_note = sanitize_posting_attempt_resolution_note(resolution_note)
    except ValueError:
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            error_kind="validation_error", error_summary="The operator note or reason is too long after audit context.",
        )

    try:
        _persist_terminal_attempt(
            database,
            title,
            attempt_id,
            next_state,
            filename,
            data_folder,
            persistence_lock,
            now,
            telegram_message_id=telegram_message_id,
            resolution_note=resolution_note,
        )
    except (OSError, ValueError, KeyError):
        return PostingOperationResult(
            phase="reconcile", operation=operation, ok=False, attempt_id=attempt_id, title=title,
            starting_state=starting_state, ending_state=starting_state,
            telegram_message_id=telegram_message_id,
            error_kind="persistence_error", error_summary="The reconciliation state could not be persisted.",
            resolution_note=note,
        )

    return PostingOperationResult(
        phase="reconcile", operation=operation, ok=True, attempt_id=attempt_id, title=title,
        starting_state=starting_state, ending_state=next_state,
        telegram_message_id=telegram_message_id,
        persistence_succeeded=True, resolution_note=resolution_note,
    )


def _dispatch_error_result(attempt_id, title, reason):
    return PostingOperationResult(
        phase="dispatch",
        ok=False,
        attempt_id=attempt_id,
        title=title,
        starting_state="pending",
        ending_state="pending",
        error_kind="invalid_attempt",
        error_summary=reason,
    )


def _persist_terminal_attempt(
    database,
    title,
    attempt_id,
    state,
    filename,
    data_folder,
    persistence_lock,
    now,
    telegram_message_id=None,
    error_kind=None,
    error_summary=None,
    resolution_note=None,
):
    posted_at_timestamp = int(_now_utc(now).timestamp()) if now is not None else int(time.time())
    return transition_database_posting_attempt(
        database,
        title,
        attempt_id,
        state,
        filename,
        data_folder,
        persistence_lock,
        now=_now_utc(now),
        posted_at_timestamp=posted_at_timestamp,
        telegram_message_id=telegram_message_id,
        error_kind=error_kind,
        error_summary=error_summary,
        resolution_note=resolution_note,
    )


def dispatch_posting_attempt(
    database,
    attempt_id,
    persistence_lock,
    data_folder,
    filename,
    send=send_message,
    now=None,
):
    """Send exactly one stored pending payload and persist its terminal state."""
    if not isinstance(attempt_id, str) or not attempt_id:
        return _dispatch_error_result(attempt_id, None, "A non-empty attempt ID is required.")
    matches = _find_attempt(database, attempt_id)
    if len(matches) != 1:
        return _dispatch_error_result(attempt_id, None, "The attempt ID must identify exactly one canonical attempt.")
    title, entry, attempt = matches[0]
    if attempt.get("state") != "pending":
        return _dispatch_error_result(attempt_id, title, "Only a pending posting attempt may be dispatched.")
    latest = latest_posting_attempt(entry)
    if latest is None or latest.get("attempt_id") != attempt_id:
        return _dispatch_error_result(attempt_id, title, "Only the newest posting attempt may be dispatched.")
    try:
        if message_fingerprint(attempt["message_text"]) != attempt.get("message_fingerprint"):
            return _dispatch_error_result(attempt_id, title, "Stored message fingerprint verification failed.")
    except (KeyError, ValueError):
        return _dispatch_error_result(attempt_id, title, "Stored message payload is invalid.")

    # One call only.  No retry is permitted here because a transport failure
    # can leave Telegram delivery ambiguous.
    try:
        telegram_result = send(attempt["message_text"])
    except requests.RequestException:
        telegram_result = TelegramResult(
            ok=False,
            response_data=None,
            error_reason="request_exception",
        )

    if not isinstance(telegram_result, TelegramResult):
        telegram_result = TelegramResult(
            ok=False,
            response_data=None,
            error_reason="invalid_result",
        )

    if (
        telegram_result.outcome == TELEGRAM_OUTCOME_CONFIRMED_SUCCESS
        and telegram_result.ok is True
        and isinstance(telegram_result.message_id, int)
        and not isinstance(telegram_result.message_id, bool)
        and telegram_result.message_id > 0
    ):
        try:
            _persist_terminal_attempt(
                database, title, attempt_id, "sent", filename, data_folder,
                persistence_lock, now, telegram_message_id=telegram_result.message_id,
            )
        except (OSError, ValueError, KeyError):
            return PostingOperationResult(
                phase="dispatch", ok=False, attempt_id=attempt_id, title=title,
                starting_state="pending", ending_state="pending", telegram_called=True,
                telegram_message_id=telegram_result.message_id,
                error_kind="persistence_error",
                error_summary="Telegram accepted the message but sent state could not be persisted.",
                manual_reconciliation_required=True,
            )
        return PostingOperationResult(
            phase="dispatch", ok=True, attempt_id=attempt_id, title=title,
            starting_state="pending", ending_state="sent", telegram_called=True,
            telegram_message_id=telegram_result.message_id, persistence_succeeded=True,
        )

    if telegram_result.outcome in (
        TELEGRAM_OUTCOME_DEFINITE_REJECTION,
        TELEGRAM_OUTCOME_DEFINITE_FAILURE,
    ):
        next_state = "failed"
        error_kind = (
            "configuration_error"
            if telegram_result.outcome == TELEGRAM_OUTCOME_DEFINITE_FAILURE
            else "telegram_rejected"
        )
        error_summary = "Telegram rejected the posting request." if error_kind == "telegram_rejected" else "Telegram configuration is unavailable."
    else:
        next_state = "unknown"
        error_kind = (
            "response_invalid"
            if telegram_result.error_reason in ("invalid_json", "invalid_response", "invalid_result")
            else "transport_ambiguous"
        )
        error_summary = "Telegram delivery outcome is ambiguous."

    try:
        _persist_terminal_attempt(
            database, title, attempt_id, next_state, filename, data_folder,
            persistence_lock, now, error_kind=error_kind, error_summary=error_summary,
        )
    except (OSError, ValueError, KeyError):
        return PostingOperationResult(
            phase="dispatch", ok=False, attempt_id=attempt_id, title=title,
            starting_state="pending", ending_state="pending", telegram_called=True,
            error_kind="persistence_error",
            error_summary="Posting outcome could not be persisted; manual reconciliation is required.",
            manual_reconciliation_required=True,
        )
    return PostingOperationResult(
        phase="dispatch", ok=False, attempt_id=attempt_id, title=title,
        starting_state="pending", ending_state=next_state, telegram_called=True,
        error_kind=error_kind, error_summary=error_summary, persistence_succeeded=True,
        manual_reconciliation_required=(next_state == "unknown"),
    )
