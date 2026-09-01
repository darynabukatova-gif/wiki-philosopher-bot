"""Pure canonical database schema validation and serialization helpers."""

import json
import re
import hashlib
import uuid
from copy import deepcopy
from datetime import date, datetime, timezone
from typing import List
from urllib.parse import urlsplit


DATABASE_SCHEMA_VERSION = 1


EXTERNAL_LINK_KEYS = (
    "wikiquote",
    "wikisource",
    "project_gutenberg",
)


def empty_external_links():
    """Return the additive, source-neutral external reading-link shape."""
    return {key: None for key in EXTERNAL_LINK_KEYS}


def _safe_urlsplit(value):
    try:
        return urlsplit(value)
    except (TypeError, ValueError):
        return None


def _is_canonical_wikimedia_link(value, hostname):
    if not isinstance(value, str) or not value:
        return False
    parsed = _safe_urlsplit(value)
    if parsed is None:
        return False
    return (
        parsed.scheme == "https"
        and parsed.netloc == hostname
        and parsed.path.startswith("/wiki/")
        and len(parsed.path) > len("/wiki/")
        and not parsed.query
        and not parsed.fragment
    )


def is_valid_external_link(key, value):
    """Return whether one optional external-reading link is canonical.

    This is deliberately also usable by read-only enrichment audits.  It does
    not coerce or repair values: a proposed link must already satisfy the
    canonical storage contract before an operator can consider applying it.
    """
    if key == "wikiquote":
        return value is None or _is_canonical_wikimedia_link(
            value, "en.wikiquote.org"
        )
    if key == "wikisource":
        return value is None or _is_canonical_wikimedia_link(
            value, "en.wikisource.org"
        )
    if key == "project_gutenberg":
        if value is None:
            return True
        parsed = _safe_urlsplit(value) if isinstance(value, str) else None
        return bool(
            isinstance(value, str)
            and value
            and parsed is not None
            and parsed.scheme == "https"
            and parsed.netloc
        )
    return False


def _validate_external_links(external_links):
    """Validate an optional canonical record-level external-links section."""
    if not isinstance(external_links, dict):
        return ["external_links must be an object"]

    errors = []
    expected = set(EXTERNAL_LINK_KEYS)
    # Individual reading-link keys are additive too. This lets a carefully
    # scoped enrichment add Wikiquote/Wikisource without manufacturing a
    # Project Gutenberg field for historical records.
    unexpected = set(external_links) - expected
    if unexpected:
        errors.append("external_links has unexpected fields")

    wikiquote = external_links.get("wikiquote")
    if not is_valid_external_link("wikiquote", wikiquote):
        errors.append("external_links.wikiquote must be a canonical English Wikiquote URL or null")

    wikisource = external_links.get("wikisource")
    if not is_valid_external_link("wikisource", wikisource):
        errors.append("external_links.wikisource must be a canonical English Wikisource URL or null")

    gutenberg = external_links.get("project_gutenberg")
    if not is_valid_external_link("project_gutenberg", gutenberg):
        errors.append("external_links.project_gutenberg must be an HTTPS URL or null")
    return errors


POSTING_ATTEMPT_STATES = frozenset(
    ("pending", "sent", "failed", "unknown", "cancelled")
)
UNRESOLVED_POSTING_ATTEMPT_STATES = frozenset(
    ("pending", "failed", "unknown")
)
POSTING_ATTEMPT_ERROR_KINDS = frozenset(
    (
        "telegram_rejected",
        "transport_ambiguous",
        "response_invalid",
        "persistence_error",
        "configuration_error",
    )
)
POSTING_ATTEMPT_ALLOWED_TRANSITIONS = {
    "pending": frozenset(("sent", "failed", "unknown", "cancelled")),
    "failed": frozenset(("cancelled",)),
    "unknown": frozenset(("sent", "cancelled")),
    "sent": frozenset(),
    "cancelled": frozenset(),
}
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
)
_POSTING_ATTEMPT_ERROR_SUMMARY_MAX_LENGTH = 500
_UNSAFE_ATTEMPT_ERROR_PATTERNS = (
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"(?i)\b(?:authorization|bearer)\s+[^\s]+"),
    re.compile(r"(?i)\b(?:telegram_)?token\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(?:telegram_)?chat(?:_|\s|-)?id\s*[:=]\s*-?\d+"),
)


def _utc_timestamp_text(now=None):
    """Return the canonical UTC timestamp representation for attempt metadata."""
    if now is None:
        now = datetime.now(timezone.utc)
    if not isinstance(now, datetime):
        raise TypeError("now must be a datetime")
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_utc_timestamp(value):
    if not isinstance(value, str) or not _UTC_TIMESTAMP_RE.fullmatch(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_sha256_hex(value):
    return isinstance(value, str) and _SHA256_HEX_RE.fullmatch(value) is not None


def sanitize_posting_attempt_error_summary(value):
    """Return a short report-safe error summary without common secret forms."""
    if not isinstance(value, str):
        raise ValueError("error_summary must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _POSTING_ATTEMPT_ERROR_SUMMARY_MAX_LENGTH:
        raise ValueError("error_summary must be a short single-line string")
    if any(pattern.search(normalized) for pattern in _UNSAFE_ATTEMPT_ERROR_PATTERNS):
        raise ValueError("error_summary must not contain credentials or chat identifiers")
    return normalized


def sanitize_posting_attempt_resolution_note(value):
    """Return a short report-safe operator note without common secret forms."""
    if not isinstance(value, str):
        raise ValueError("resolution_note must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _POSTING_ATTEMPT_ERROR_SUMMARY_MAX_LENGTH:
        raise ValueError("resolution_note must be a short single-line string")
    if any(pattern.search(normalized) for pattern in _UNSAFE_ATTEMPT_ERROR_PATTERNS):
        raise ValueError("resolution_note must not contain credentials or chat identifiers")
    return normalized


def _canonical_quote_identity(selected_quote):
    if not isinstance(selected_quote, dict):
        raise ValueError("selected_quote must be an object")
    text = selected_quote.get("text")
    source = selected_quote.get("source")
    if not isinstance(text, str) or not text:
        raise ValueError("selected_quote.text must be a non-empty string")
    if not isinstance(source, dict):
        raise ValueError("selected_quote.source must be a structured object")
    return {"source": source, "text": text}


def quote_fingerprint(selected_quote):
    """Return a stable SHA-256 identity for one quote's text and structured source."""
    identity = _canonical_quote_identity(selected_quote)
    try:
        canonical = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except TypeError as error:
        raise ValueError("selected_quote must be JSON-serializable") from error
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def message_fingerprint(message_text):
    """Return a SHA-256 fingerprint for the exact UTF-8 outbound message."""
    if not isinstance(message_text, str) or not message_text:
        raise ValueError("message_text must be a non-empty string")
    return hashlib.sha256(message_text.encode("utf-8")).hexdigest()


def _validate_posting_attempt(attempt) -> List[str]:
    """Validate one future durable Telegram posting attempt."""
    errors = []
    if not isinstance(attempt, dict):
        return ["posting.attempts item must be an object"]

    required_keys = (
        "attempt_id", "title", "quote_fingerprint", "message_fingerprint",
        "message_text", "created_at", "state", "state_changed_at",
        "telegram_message_id", "error_kind", "error_summary", "resolution_note",
    )
    for key in required_keys:
        if key not in attempt:
            errors.append("posting.attempts item missing {}".format(key))

    if not isinstance(attempt.get("attempt_id"), str) or not attempt.get("attempt_id").strip():
        errors.append("posting.attempts.attempt_id must be a non-empty string")
    if not isinstance(attempt.get("title"), str) or not attempt.get("title").strip():
        errors.append("posting.attempts.title must be a non-empty string")
    for field_name in ("quote_fingerprint", "message_fingerprint"):
        if not _is_sha256_hex(attempt.get(field_name)):
            errors.append("posting.attempts.{} must be a SHA-256 hex string".format(field_name))
    if not isinstance(attempt.get("message_text"), str) or not attempt.get("message_text"):
        errors.append("posting.attempts.message_text must be a non-empty string")
    for field_name in ("created_at", "state_changed_at"):
        if not _is_utc_timestamp(attempt.get(field_name)):
            errors.append("posting.attempts.{} must be a UTC timestamp".format(field_name))

    state = attempt.get("state")
    if state not in POSTING_ATTEMPT_STATES:
        errors.append("posting.attempts.state must be a supported state")

    message_id = attempt.get("telegram_message_id")
    if message_id is not None and not (_is_int_not_bool(message_id) and message_id > 0):
        errors.append("posting.attempts.telegram_message_id must be a positive integer or null")

    error_kind = attempt.get("error_kind")
    if error_kind is not None and error_kind not in POSTING_ATTEMPT_ERROR_KINDS:
        errors.append("posting.attempts.error_kind must be a supported value or null")

    error_summary = attempt.get("error_summary")
    if error_summary is not None:
        try:
            sanitized_error_summary = sanitize_posting_attempt_error_summary(error_summary)
        except ValueError:
            errors.append("posting.attempts.error_summary must be a safe short single-line string or null")
        else:
            if sanitized_error_summary != error_summary:
                errors.append("posting.attempts.error_summary must already be normalized")

    resolution_note = attempt.get("resolution_note")
    if resolution_note is not None:
        try:
            sanitized_resolution_note = sanitize_posting_attempt_resolution_note(
                resolution_note
            )
        except ValueError:
            errors.append(
                "posting.attempts.resolution_note must be a safe short single-line string or null"
            )
        else:
            if sanitized_resolution_note != resolution_note:
                errors.append("posting.attempts.resolution_note must already be normalized")

    if state == "sent":
        if message_id is None:
            errors.append("posting.attempts.sent requires telegram_message_id")
        if error_kind is not None or error_summary is not None:
            errors.append("posting.attempts.sent must not retain an unresolved error")

    if state in ("failed", "unknown") and error_kind is None:
        errors.append("posting.attempts.{} requires error_kind".format(state))

    if state == "cancelled" and not resolution_note:
        errors.append("posting.attempts.cancelled requires resolution_note")

    return errors


def validate_posting_attempt(attempt) -> List[str]:
    """Public validation entry point for one posting-attempt object."""
    return _validate_posting_attempt(attempt)


def make_pending_posting_attempt(
    title,
    selected_quote,
    message_text,
    attempt_id=None,
    now=None,
):
    """Construct, but do not persist, one validated pending posting attempt."""
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be a non-empty string")
    if attempt_id is None:
        attempt_id = str(uuid.uuid4())
    timestamp = _utc_timestamp_text(now)
    attempt = {
        "attempt_id": attempt_id,
        "title": title,
        "quote_fingerprint": quote_fingerprint(selected_quote),
        "message_fingerprint": message_fingerprint(message_text),
        "message_text": message_text,
        "created_at": timestamp,
        "state": "pending",
        "state_changed_at": timestamp,
        "telegram_message_id": None,
        "error_kind": None,
        "error_summary": None,
        "resolution_note": None,
    }
    errors = validate_posting_attempt(attempt)
    if errors:
        raise ValueError("\n".join(errors))
    return attempt


def posting_attempts(entry):
    """Return the posting attempts list, treating old records as an empty list."""
    posting = entry.get("posting") if isinstance(entry, dict) else None
    attempts = posting.get("attempts", []) if isinstance(posting, dict) else []
    return attempts if isinstance(attempts, list) else []


def latest_posting_attempt(entry):
    attempts = posting_attempts(entry)
    return attempts[-1] if attempts else None


def posting_attempt_by_id(entry, attempt_id):
    for attempt in posting_attempts(entry):
        if attempt.get("attempt_id") == attempt_id:
            return attempt
    return None


def has_unresolved_posting_attempt(entry):
    attempt = latest_posting_attempt(entry)
    return attempt is not None and attempt.get("state") in UNRESOLVED_POSTING_ATTEMPT_STATES


def transition_posting_attempt(
    attempt,
    new_state,
    now=None,
    telegram_message_id=None,
    error_kind=None,
    error_summary=None,
    resolution_note=None,
):
    """Return a validated copy of an attempt in one allowed next state."""
    errors = validate_posting_attempt(attempt)
    if errors:
        raise ValueError("\n".join(errors))
    current_state = attempt["state"]
    if new_state not in POSTING_ATTEMPT_ALLOWED_TRANSITIONS[current_state]:
        raise ValueError("Unsupported posting attempt transition: {} -> {}".format(current_state, new_state))

    if new_state == "sent":
        if not (_is_int_not_bool(telegram_message_id) and telegram_message_id > 0):
            raise ValueError("sent posting attempts require a positive telegram_message_id")
        if error_kind is not None or error_summary is not None:
            raise ValueError("sent posting attempts must not retain an unresolved error")
    elif new_state in ("failed", "unknown"):
        if error_kind not in POSTING_ATTEMPT_ERROR_KINDS:
            raise ValueError("{} posting attempts require a supported error_kind".format(new_state))
    elif new_state == "cancelled":
        if not isinstance(resolution_note, str) or not resolution_note.strip():
            raise ValueError("cancelled posting attempts require a resolution_note")

    if error_summary is not None:
        error_summary = sanitize_posting_attempt_error_summary(error_summary)
    if resolution_note is not None:
        resolution_note = sanitize_posting_attempt_resolution_note(resolution_note)

    updated = deepcopy(attempt)
    updated["state"] = new_state
    updated["state_changed_at"] = _utc_timestamp_text(now)
    if new_state == "sent":
        updated["telegram_message_id"] = telegram_message_id
        updated["error_kind"] = None
        updated["error_summary"] = None
    elif new_state in ("failed", "unknown"):
        updated["telegram_message_id"] = None
        updated["error_kind"] = error_kind
        updated["error_summary"] = error_summary
    if resolution_note is not None:
        updated["resolution_note"] = resolution_note

    errors = validate_posting_attempt(updated)
    if errors:
        raise ValueError("\n".join(errors))
    return updated


EVALUATION_SERIALIZATION_ORDER = (
    "status",
    "algorithm_version",
    "philosopher_confidence",
    "human_confidence",
    "content_confidence",
    "reasons",
    "processed_at",
    "legacy_result",
)


def make_empty_database_entry(title: str) -> dict:
    """Return a fresh canonical database entry for one exact title."""
    if not isinstance(title, str) or not title:
        raise ValueError("Database entry title must be a non-empty string")

    display_title = re.sub(r"\s*\([^)]*\)", "", title).strip() or title

    return {
        "schema_version": DATABASE_SCHEMA_VERSION,
        "title": title,
        "display_title": display_title,
        "external_links": empty_external_links(),
        "summary": {
            "text": None,
            "source": "Wikipedia",
            "fetched_at": None,
        },
        "wikidata": {
            "status": "unknown",
            "reason": None,
            "qid": None,
            "instances": [],
            "occupations": [],
            "birth_year": None,
            "death_year": None,
            "death_date": None,
            "is_human": None,
            "is_philosopher": None,
            "fetched_at": None,
        },
        "quotes": {
            "status": "unknown",
            "items": [],
            "failure": None,
            "fetched_at": None,
            "parser_version": None,
        },
        "evaluation": {
            "status": "unprocessed",
            "algorithm_version": None,
            "human_confidence": None,
            "philosopher_confidence": None,
            "content_confidence": None,
            "reasons": [],
            "legacy_result": None,
            "processed_at": None,
        },
        "posting": {
            "has_been_posted": False,
            "posted_at": [],
            "legacy_posted_without_timestamp": False,
            "attempts": [],
        },
        "migration": {
            "legacy_sources": [],
            "conflicts": [],
        },
    }


def _is_int_not_bool(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_not_bool(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


def _require_keys(
    section_name,
    section,
    required_keys,
):
    errors = []

    if not isinstance(section, dict):
        return [
            "{} must be an object".format(
                section_name
            )
        ]

    for key in required_keys:
        if key not in section:
            errors.append(
                "Missing required field: {}.{}".format(
                    section_name,
                    key,
                )
            )

    return errors


def validate_database_entry(entry: dict) -> List[str]:
    errors = []

    if not isinstance(entry, dict):
        return ["Entry must be an object"]

    required_top_level = (
        "schema_version",
        "title",
        "display_title",
        "summary",
        "wikidata",
        "quotes",
        "evaluation",
        "posting",
        "migration",
    )

    for key in required_top_level:
        if key not in entry:
            errors.append(
                "Missing required top-level field: {}".format(key)
            )

    if errors:
        return errors

    if entry["schema_version"] != DATABASE_SCHEMA_VERSION:
        errors.append(
            "schema_version must be {}".format(
                DATABASE_SCHEMA_VERSION
            )
        )

    if (
        not isinstance(entry["title"], str)
        or not entry["title"]
    ):
        errors.append(
            "title must be a non-empty string"
        )

    if (
        not isinstance(entry["display_title"], str)
        or not entry["display_title"]
    ):
        errors.append(
            "display_title must be a non-empty string"
        )

    # This section was introduced additively. Historical records that omit it
    # remain valid and behave as if all external links were unavailable.
    if "external_links" in entry:
        errors.extend(_validate_external_links(entry["external_links"]))

    summary = entry["summary"]

    errors.extend(
        _require_keys(
            "summary",
            summary,
            (
                "text",
                "source",
                "fetched_at",
            ),
        )
    )

    if isinstance(summary, dict):
        text = summary.get("text")

        if text is not None and not isinstance(text, str):
            errors.append(
                "summary.text must be a string or null"
            )

        if summary.get("source") != "Wikipedia":
            errors.append(
                "summary.source must be 'Wikipedia'"
            )

        fetched_at = summary.get("fetched_at")

        if (
            fetched_at is not None
            and not _is_int_not_bool(fetched_at)
        ):
            errors.append(
                "summary.fetched_at must be an integer or null"
            )

    wikidata = entry["wikidata"]

    errors.extend(
        _require_keys(
            "wikidata",
            wikidata,
            (
                "status",
                "reason",
                "qid",
                "instances",
                "occupations",
                "birth_year",
                "death_year",
                "is_human",
                "is_philosopher",
                "fetched_at",
            ),
        )
    )

    if isinstance(wikidata, dict):
        if wikidata.get("status") not in (
            "unknown",
            "available",
            "unavailable",
        ):
            errors.append(
                "wikidata.status has invalid value"
            )

        reason = wikidata.get("reason")

        if reason is not None and not isinstance(reason, str):
            errors.append(
                "wikidata.reason must be a string or null"
            )

        qid = wikidata.get("qid")

        if qid is not None and not isinstance(qid, str):
            errors.append(
                "wikidata.qid must be a string or null"
            )

        for field_name in (
            "instances",
            "occupations",
        ):
            value = wikidata.get(field_name)

            if (
                not isinstance(value, list)
                or not all(
                    isinstance(item, str)
                    for item in value
                )
            ):
                errors.append(
                    "wikidata.{} must be a list of strings".format(
                        field_name
                    )
                )

        for field_name in (
            "birth_year",
            "death_year",
        ):
            value = wikidata.get(field_name)

            if (
                value is not None
                and not _is_int_not_bool(value)
            ):
                errors.append(
                    "wikidata.{} must be an integer or null".format(
                        field_name
                    )
                )

        # Historical canonical entries predate exact death-date storage, so a
        # missing key is deliberately equivalent to a null value.
        if "death_date" in wikidata:
            death_date = wikidata.get("death_date")
            valid_death_date = isinstance(death_date, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", death_date
            )
            if valid_death_date:
                try:
                    date.fromisoformat(death_date)
                except ValueError:
                    valid_death_date = False
            if death_date is not None and not valid_death_date:
                errors.append(
                    "wikidata.death_date must be an ISO date or null"
                )

        for field_name in (
            "is_human",
            "is_philosopher",
        ):
            value = wikidata.get(field_name)

            if (
                value is not None
                and not isinstance(value, bool)
            ):
                errors.append(
                    "wikidata.{} must be boolean or null".format(
                        field_name
                    )
                )

        fetched_at = wikidata.get("fetched_at")

        if (
            fetched_at is not None
            and not _is_int_not_bool(fetched_at)
        ):
            errors.append(
                "wikidata.fetched_at must be an integer or null"
            )

    quotes = entry["quotes"]

    errors.extend(
        _require_keys(
            "quotes",
            quotes,
            (
                "status",
                "items",
                "failure",
                "fetched_at",
            ),
        )
    )

    if isinstance(quotes, dict):
        if quotes.get("status") not in (
            "unknown",
            "available",
            "not_found",
            "failed",
            "purged",
        ):
            errors.append(
                "quotes.status has invalid value"
            )

        items = quotes.get("items")

        structured_parser_quote = (
            isinstance(quotes.get("parser_version"), int)
            and not isinstance(quotes.get("parser_version"), bool)
            and quotes["parser_version"] >= 2
        )

        if not isinstance(items, list):
            errors.append("quotes.items must be a list")
        else:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(
                        "quotes.items[{}] must be an object".format(index)
                    )
                    continue

                for key in (
                    "text",
                    "length",
                    "word_count",
                    "source",
                ):
                    if key not in item:
                        errors.append(
                            "quotes.items[{}] missing {}".format(index, key)
                        )

                if (
                    "text" in item
                    and (
                        not isinstance(item["text"], str)
                        or not item["text"]
                    )
                ):
                    errors.append(
                        "quotes.items[{}].text must be a non-empty string".format(
                            index
                        )
                    )

                for key in ("length", "word_count"):
                    if (
                        key in item
                        and not _is_int_not_bool(item[key])
                    ):
                        errors.append(
                            "quotes.items[{}].{} must be an integer".format(
                                index,
                                key,
                            )
                        )

                if structured_parser_quote:
                    expected_item_keys = {
                        "text", "length", "word_count", "source", "retrieved_from",
                    }
                    if set(item) != expected_item_keys:
                        errors.append(
                            "quotes.items[{}] must use the current parser item shape".format(index)
                        )
                    if "retrieved_from" not in item:
                        errors.append(
                            "quotes.items[{}] missing retrieved_from".format(index)
                        )
                    elif item.get("retrieved_from") != "Wikiquote":
                        errors.append(
                            "quotes.items[{}].retrieved_from must be 'Wikiquote'".format(index)
                        )

                    source = item.get("source")
                    if not isinstance(source, dict):
                        errors.append(
                            "quotes.items[{}].source must be an object for current parser data".format(index)
                        )
                        continue

                    expected_source_keys = (
                        "work", "year", "date", "details", "citation", "url",
                    )
                    for key in expected_source_keys:
                        if key not in source:
                            errors.append(
                                "quotes.items[{}].source missing {}".format(index, key)
                            )
                    unexpected_keys = set(source) - set(expected_source_keys)
                    if unexpected_keys:
                        errors.append(
                            "quotes.items[{}].source has unexpected fields".format(index)
                        )
                    for key in ("work", "date", "details", "citation", "url"):
                        if key in source and source[key] is not None and not isinstance(source[key], str):
                            errors.append(
                                "quotes.items[{}].source.{} must be a string or null".format(index, key)
                            )
                    if (
                        "year" in source
                        and source["year"] is not None
                        and (
                            not _is_int_not_bool(source["year"])
                            or source["year"] < 0
                        )
                    ):
                        errors.append(
                            "quotes.items[{}].source.year must be a non-negative integer or null".format(index)
                        )
                elif not isinstance(item.get("source"), str):
                    errors.append(
                        "quotes.items[{}].source must be a string for historical data".format(index)
                    )

        failure = quotes.get("failure")

        if failure is not None:
            errors.extend(
                _require_keys(
                    "quotes.failure",
                    failure,
                    ("reason", "timestamp", "retries"),
                )
            )

            if isinstance(failure, dict):
                if not isinstance(failure.get("reason"), str):
                    errors.append("quotes.failure.reason must be a string")

                if not _is_number_not_bool(failure.get("timestamp")):
                    errors.append("quotes.failure.timestamp must be a number")

                if not _is_int_not_bool(failure.get("retries")):
                    errors.append("quotes.failure.retries must be an integer")
            else:
                errors.append("quotes.failure must be an object or null")

        fetched_at = quotes.get("fetched_at")

        if fetched_at is not None and not _is_int_not_bool(fetched_at):
            errors.append("quotes.fetched_at must be an integer or null")

        # Historical canonical entries predate parser-version provenance, so
        # omission remains valid and is interpreted by callers as stale.
        if "parser_version" in quotes:
            parser_version = quotes["parser_version"]
            if (
                parser_version is not None
                and (
                    not _is_int_not_bool(parser_version)
                    or parser_version <= 0
                )
            ):
                errors.append(
                    "quotes.parser_version must be a positive integer or null"
                )

        if quotes.get("status") == "purged":
            if quotes.get("items") != []:
                errors.append("quotes.purged items must be an empty list")
            if quotes.get("failure") is not None:
                errors.append("quotes.purged failure must be null")
            if quotes.get("fetched_at") is not None:
                errors.append("quotes.purged fetched_at must be null")
            if (
                "parser_version" not in quotes
                or quotes.get("parser_version") is not None
            ):
                errors.append("quotes.purged parser_version must be null")

    evaluation = entry["evaluation"]

    errors.extend(
        _require_keys(
            "evaluation",
            evaluation,
            (
                "status",
                "algorithm_version",
                "human_confidence",
                "philosopher_confidence",
                "content_confidence",
                "reasons",
                "legacy_result",
                "processed_at",
            ),
        )
    )

    if isinstance(evaluation, dict):
        if evaluation.get("status") not in (
            "unprocessed",
            "accepted",
            "rejected",
        ):
            errors.append(
                "evaluation.status has invalid value"
            )

        if (
            isinstance(quotes, dict)
            and quotes.get("status") == "purged"
            and evaluation.get("status") != "rejected"
        ):
            errors.append("quotes.purged requires evaluation.status rejected")

        algorithm_version = evaluation.get(
            "algorithm_version"
        )

        if (
            algorithm_version is not None
            and (
                not _is_int_not_bool(
                    algorithm_version
                )
                or algorithm_version <= 0
            )
        ):
            errors.append(
                "evaluation.algorithm_version "
                "must be a positive integer or null"
            )

        for field_name in (
            "human_confidence",
            "philosopher_confidence",
            "content_confidence",
        ):
            value = evaluation.get(field_name)

            if (
                value is not None
                and not _is_int_not_bool(value)
            ):
                errors.append(
                    "evaluation.{} must be an integer or null".format(
                        field_name
                    )
                )

        reasons = evaluation.get("reasons")

        if (
            not isinstance(reasons, list)
            or not all(
                isinstance(reason, str)
                for reason in reasons
            )
        ):
            errors.append(
                "evaluation.reasons must be a list of strings"
            )

        processed_at = evaluation.get(
            "processed_at"
        )

        if (
            processed_at is not None
            and not _is_number_not_bool(
                processed_at
            )
        ):
            errors.append(
                "evaluation.processed_at must be a number or null"
            )

        legacy_result = evaluation.get("legacy_result")

        if (
            legacy_result is not None
            and not isinstance(legacy_result, dict)
        ):
            errors.append(
                "evaluation.legacy_result must be an object or null"
            )

    posting = entry["posting"]

    errors.extend(
        _require_keys(
            "posting",
            posting,
            (
                "has_been_posted",
                "posted_at",
                "legacy_posted_without_timestamp",
            ),
        )
    )

    if isinstance(posting, dict):
        has_been_posted = posting.get(
            "has_been_posted"
        )
        posted_at = posting.get("posted_at")
        legacy_marker = posting.get(
            "legacy_posted_without_timestamp"
        )

        if not isinstance(has_been_posted, bool):
            errors.append(
                "posting.has_been_posted must be boolean"
            )

        if not isinstance(posted_at, list):
            errors.append(
                "posting.posted_at must be a list"
            )
        elif not all(
            _is_int_not_bool(timestamp)
            for timestamp in posted_at
        ):
            errors.append(
                "posting.posted_at must contain integer timestamps"
            )

        if not isinstance(legacy_marker, bool):
            errors.append(
                "posting.legacy_posted_without_timestamp "
                "must be boolean"
            )

        if (
            isinstance(posted_at, list)
            and posted_at
            and has_been_posted is not True
        ):
            errors.append(
                "posting.has_been_posted must be true "
                "when posted_at is non-empty"
            )

        if (
            legacy_marker is True
            and has_been_posted is not True
        ):
            errors.append(
                "posting.has_been_posted must be true "
                "when legacy_posted_without_timestamp is true"
            )

        # `attempts` is deliberately optional: historical canonical records
        # remain schema-valid without an offline rewrite.
        attempts = posting.get("attempts", [])
        if not isinstance(attempts, list):
            errors.append("posting.attempts must be a list when present")
        else:
            seen_attempt_ids = set()
            for index, attempt in enumerate(attempts):
                for attempt_error in validate_posting_attempt(attempt):
                    errors.append(
                        "posting.attempts[{}]: {}".format(index, attempt_error)
                    )
                if isinstance(attempt, dict):
                    attempt_title = attempt.get("title")
                    if attempt_title != entry["title"]:
                        errors.append(
                            "posting.attempts[{}].title must match entry.title".format(index)
                        )
                    attempt_id = attempt.get("attempt_id")
                    if isinstance(attempt_id, str):
                        if attempt_id in seen_attempt_ids:
                            errors.append("posting.attempts attempt_id values must be unique")
                        else:
                            seen_attempt_ids.add(attempt_id)
                    if (
                        attempt.get("state") == "sent"
                        and has_been_posted is not True
                    ):
                        errors.append(
                            "posting.has_been_posted must be true when an attempt is sent"
                        )

    return errors


def validate_database_dataset(
    entries: List[dict],
) -> List[str]:
    errors = []
    seen_titles = set()

    for index, entry in enumerate(entries):
        entry_errors = validate_database_entry(
            entry
        )

        for error in entry_errors:
            errors.append(
                "entry[{}]: {}".format(
                    index,
                    error,
                )
            )

        if isinstance(entry, dict):
            title = entry.get("title")

            if isinstance(title, str):
                if title in seen_titles:
                    errors.append(
                        "Duplicate database title: {!r}".format(
                            title
                        )
                    )
                else:
                    seen_titles.add(title)

    return errors


def serialize_database_entries(
    entries: List[dict],
) -> bytes:
    ordered_entries = sorted(
        entries,
        key=lambda entry: entry["title"],
    )

    serialized_lines = []

    for entry in ordered_entries:
        serialized_lines.append(
            json.dumps(
                _canonicalize_for_serialization(entry),
                ensure_ascii=False,
                sort_keys=False,
                separators=(",", ":"),
            )
        )

    if not serialized_lines:
        return b""

    text = "\n".join(serialized_lines) + "\n"

    return text.encode("utf-8")


def _canonicalize_for_serialization(value, is_evaluation=False):
    """Return recursively deterministic JSON data with ordered evaluation keys."""
    if isinstance(value, dict):
        if is_evaluation:
            keys = [
                key for key in EVALUATION_SERIALIZATION_ORDER
                if key in value
            ]
            keys.extend(
                sorted(
                    key for key in value
                    if key not in EVALUATION_SERIALIZATION_ORDER
                )
            )
        else:
            keys = sorted(value)

        return {
            key: _canonicalize_for_serialization(
                value[key],
                is_evaluation=(key == "evaluation"),
            )
            for key in keys
        }

    if isinstance(value, list):
        return [
            _canonicalize_for_serialization(item)
            for item in value
        ]

    return value
