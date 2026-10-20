"""Preparation-only durable outbox for public statistical-report publication.

This phase deliberately has no Telegram, database, or reconciliation code. It
freezes an already-sent private report-post bundle into an independent bundle
that a later dispatch phase can consume byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import hashlib
import json
import os
import shutil
import tempfile
import uuid

from wiki_philosopher_bot.report_post_outbox import (
    ReportPostError,
    list_attempts as list_report_post_attempts,
    load_attempt as load_report_post_attempt,
    verify_bundle as verify_report_post_bundle,
)
from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.database_schema import (
    recent_death_notifications,
    validate_recent_death_notification,
)
from wiki_philosopher_bot.config import get_channel_telegram_settings
from wiki_philosopher_bot.telegram_bot import (
    TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
    TELEGRAM_OUTCOME_DEFINITE_REJECTION,
    send_message_to_chat,
    send_media_group_to_chat,
)

SCHEMA_VERSION = 1
SOURCE_TYPE = "statistical_report"
RECENT_DEATH_SOURCE_TYPE = "recent_death"
SUPPORTED_SOURCE_TYPES = frozenset((SOURCE_TYPE, RECENT_DEATH_SOURCE_TYPE))
PAYLOAD_KIND = "media_group"
TEXT_PAYLOAD_KIND = "text"
STATES = frozenset(("pending", "sent", "failed", "unknown", "cancelled"))
UNRESOLVED_STATES = frozenset(("pending", "failed", "unknown"))
TRANSITIONS = {
    "pending": frozenset(("sent", "failed", "unknown", "cancelled")),
    "failed": frozenset(("cancelled",)),
    "unknown": frozenset(("sent", "cancelled")),
    "sent": frozenset(),
    "cancelled": frozenset(),
}
SHA256_LENGTH = 64
MAX_MEDIA_ITEMS = 10


class ChannelPublicationError(ValueError):
    """A validation, source-integrity, or atomic-publication failure."""


@dataclass(frozen=True)
class ChannelPublicationResult:
    ok: bool
    operation: str = "prepare-channel-publication"
    publication_id: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    payload_kind: Optional[str] = None
    ending_state: Optional[str] = None
    source_payload_fingerprint: Optional[str] = None
    publication_fingerprint: Optional[str] = None
    telegram_called: bool = False
    persistence_succeeded: bool = False
    error_kind: Optional[str] = None
    error_summary: Optional[str] = None
    bundle_path: Optional[str] = None

    def as_report(self):
        return self.__dict__.copy()


@dataclass(frozen=True)
class ChannelDispatchResult:
    ok: bool
    operation: str = "dispatch-channel-publication"
    publication_id: Optional[str] = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    payload_kind: Optional[str] = None
    starting_state: Optional[str] = None
    ending_state: Optional[str] = None
    telegram_called: bool = False
    telegram_message_ids: Optional[list] = None
    persistence_succeeded: bool = False
    manual_reconciliation_required: bool = False
    error_kind: Optional[str] = None
    error_summary: Optional[str] = None

    def as_report(self):
        value = self.__dict__.copy()
        if value["telegram_message_ids"] is None:
            value["telegram_message_ids"] = []
        return value


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value) -> bool:
    return (
        isinstance(value, str)
        and len(value) == SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_uuid(value) -> bool:
    try:
        return str(uuid.UUID(str(value))) == value
    except (TypeError, ValueError, AttributeError):
        return False


def _safe_filename(value) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and Path(value).name == value
        and value not in {".", ".."}
    )


def _timestamp(now=None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_fingerprint(value) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return _sha_bytes(encoded)


def _atomic_json(path: Path, value: dict) -> None:
    path = Path(path)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".channel-publication-attempt-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_bytes(path: Path, data: bytes) -> None:
    with Path(path).open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _media_identity(media):
    return [
        {
            "position": item["position"],
            "kind": item["kind"],
            "filename": item["filename"],
            "sha256": item["sha256"],
            **({"page": item["page"]} if "page" in item else {}),
        }
        for item in media
    ]


def source_payload_fingerprint(caption_html=None, media=None, *, message_html=None, payload_kind=None) -> str:
    """Identify the exact caption and ordered frozen media content."""
    if payload_kind == TEXT_PAYLOAD_KIND or message_html is not None:
        return _json_fingerprint({"payload_kind": TEXT_PAYLOAD_KIND, "message_html": message_html})
    return _json_fingerprint({"caption_html": caption_html, "media": _media_identity(media or [])})


def publication_fingerprint(attempt: dict) -> str:
    """Identify this source's prepared publication, excluding volatile fields."""
    value = {
        "source_type": attempt.get("source_type"),
        "source_id": attempt.get("source_id"),
        "source_payload_fingerprint": attempt.get("source_payload_fingerprint"),
        "payload_kind": attempt.get("payload_kind"),
        "caption_html": attempt.get("caption_html"),
        "media": _media_identity(attempt.get("media", [])),
    }
    if attempt.get("payload_kind") == TEXT_PAYLOAD_KIND:
        value["message_html"] = attempt.get("message_html")
    return _json_fingerprint(value)


def is_legal_transition(start_state: str, end_state: str) -> bool:
    return end_state in TRANSITIONS.get(start_state, frozenset())


def validate_publication(attempt: dict) -> dict:
    if not isinstance(attempt, dict) or attempt.get("schema_version") != SCHEMA_VERSION:
        raise ChannelPublicationError("unsupported channel-publication schema")
    if not _valid_uuid(attempt.get("publication_id")):
        raise ChannelPublicationError("publication_id must be a canonical UUID")
    source_type = attempt.get("source_type")
    if source_type not in SUPPORTED_SOURCE_TYPES:
        raise ChannelPublicationError("unsupported source_type")
    if not _valid_uuid(attempt.get("source_id")):
        raise ChannelPublicationError("source_id must be a canonical UUID")
    if attempt.get("state") not in STATES:
        raise ChannelPublicationError("invalid publication state")
    payload_kind = attempt.get("payload_kind")
    if (source_type == SOURCE_TYPE and payload_kind != PAYLOAD_KIND) or (source_type == RECENT_DEATH_SOURCE_TYPE and payload_kind != TEXT_PAYLOAD_KIND):
        raise ChannelPublicationError("unsupported payload_kind")
    for field_name in ("created_at", "state_changed_at"):
        if not isinstance(attempt.get(field_name), str) or not attempt[field_name]:
            raise ChannelPublicationError("{} is required".format(field_name))
    for field_name in ("source_payload_fingerprint", "publication_fingerprint"):
        if not _is_sha256(attempt.get(field_name)):
            raise ChannelPublicationError("{} must be a SHA-256 hex string".format(field_name))

    caption = attempt.get("caption_html")
    media = attempt.get("media")
    message_html = attempt.get("message_html")
    if source_type == SOURCE_TYPE:
        if not isinstance(caption, str) or not caption:
            raise ChannelPublicationError("caption_html must be a non-empty string")
        if message_html is not None:
            raise ChannelPublicationError("media publication cannot contain message_html")
        if not isinstance(media, list) or not media or len(media) > MAX_MEDIA_ITEMS:
            raise ChannelPublicationError("media must contain 1 to {} items".format(MAX_MEDIA_ITEMS))
    else:
        if caption is not None:
            raise ChannelPublicationError("text publication caption_html must be null")
        if not isinstance(message_html, str) or not message_html:
            raise ChannelPublicationError("message_html must be a non-empty string")
        if media != []:
            raise ChannelPublicationError("text publication media must be empty")
        media = []
    names = set()
    for expected_position, item in enumerate(media, 1):
        if not isinstance(item, dict) or item.get("position") != expected_position:
            raise ChannelPublicationError("media positions must be contiguous from 1")
        if not _safe_filename(item.get("filename")):
            raise ChannelPublicationError("media filename must be a safe basename")
        if item["filename"] in names:
            raise ChannelPublicationError("media filenames must be unique")
        names.add(item["filename"])
        if not isinstance(item.get("kind"), str) or not item["kind"]:
            raise ChannelPublicationError("media kind must be a non-empty string")
        if not _is_sha256(item.get("sha256")):
            raise ChannelPublicationError("media sha256 must be a SHA-256 hex string")
        if "page" in item and (
            not isinstance(item["page"], int) or isinstance(item["page"], bool) or item["page"] <= 0
        ):
            raise ChannelPublicationError("media page must be a positive integer")

    message_ids = attempt.get("telegram_message_ids")
    if not isinstance(message_ids, list) or any(
        not isinstance(value, int) or isinstance(value, bool) or value <= 0
        for value in message_ids
    ):
        raise ChannelPublicationError("telegram_message_ids must contain positive integers")
    if attempt["state"] == "sent" and len(message_ids) != (len(media) if source_type == SOURCE_TYPE else 1):
        raise ChannelPublicationError("sent publication requires complete message IDs")
    if attempt["state"] != "sent" and message_ids:
        raise ChannelPublicationError("non-sent publication cannot contain message IDs")
    for field_name in ("error_kind", "error_summary", "resolution_note"):
        if attempt.get(field_name) is not None and not isinstance(attempt[field_name], str):
            raise ChannelPublicationError("{} must be a string or null".format(field_name))

    expected_source_fp = source_payload_fingerprint(caption, media, message_html=message_html, payload_kind=payload_kind)
    if attempt["source_payload_fingerprint"] != expected_source_fp:
        raise ChannelPublicationError("source_payload_fingerprint does not match payload")
    if attempt["publication_fingerprint"] != publication_fingerprint(attempt):
        raise ChannelPublicationError("publication_fingerprint does not match payload")
    return attempt


def load_publication(state_root, publication_id):
    bundle = Path(state_root) / str(publication_id)
    attempt_path = bundle / "attempt.json"
    if not attempt_path.is_file():
        raise ChannelPublicationError("publication not found")
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ChannelPublicationError("attempt.json is unreadable or malformed") from error
    return validate_publication(attempt), bundle


def list_publications(state_root):
    root = Path(state_root)
    if not root.exists():
        return []
    publications = []
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and (child / "attempt.json").is_file():
            publications.append(load_publication(root, child.name)[0])
    return publications


def validate_publication_bundle(bundle, attempt=None):
    """Validate every frozen media byte and return payload metadata."""
    bundle = Path(bundle)
    if attempt is None:
        attempt = load_publication(bundle.parent, bundle.name)[0]
    validate_publication(attempt)
    payload = []
    for media in attempt["media"]:
        path = bundle / media["filename"]
        if not path.is_file():
            raise ChannelPublicationError("frozen media is missing: {}".format(media["filename"]))
        data = path.read_bytes()
        if _sha_bytes(data) != media["sha256"]:
            raise ChannelPublicationError("frozen media hash mismatch: {}".format(media["filename"]))
        payload.append({"filename": media["filename"], "bytes": data})
    return payload


def _load_source_by_id(report_post_root, source_id):
    if not _valid_uuid(source_id):
        raise ChannelPublicationError("source_id must be a canonical UUID")
    bundle = Path(report_post_root) / source_id
    try:
        attempt = load_report_post_attempt(bundle)
    except (OSError, ReportPostError) as error:
        raise ChannelPublicationError("report-post source is invalid: {}".format(error)) from error
    return bundle, attempt


def _eligible_source(bundle, attempt):
    if attempt.get("state") != "sent":
        raise ChannelPublicationError("report-post source is not sent")
    media = attempt.get("media")
    if not isinstance(media, list) or len(attempt.get("telegram_message_ids", [])) != len(media):
        raise ChannelPublicationError("report-post source has incomplete Telegram message IDs")
    try:
        payload = verify_report_post_bundle(bundle, attempt)
    except (OSError, ReportPostError) as error:
        raise ChannelPublicationError("report-post source bundle failed integrity validation: {}".format(error)) from error
    return attempt["caption_html"], media, payload


def _all_eligible_sources(report_post_root):
    try:
        attempts = list_report_post_attempts(report_post_root)
    except (OSError, ReportPostError) as error:
        raise ChannelPublicationError("report-post source scan failed: {}".format(error)) from error
    sources = []
    for bundle, attempt in attempts:
        if attempt.get("state") != "sent":
            continue
        caption, media, payload = _eligible_source(bundle, attempt)
        sources.append({
            "bundle": bundle,
            "attempt": attempt,
            "source_id": attempt["attempt_id"],
            "caption": caption,
            "media": media,
            "payload": payload,
        })
    return sources


def _load_recent_database(database_path):
    path = Path(database_path)
    if not path.is_file():
        raise ChannelPublicationError("recent-death database does not exist")
    try:
        return load_database(path.name, str(path.parent))
    except (OSError, ValueError) as error:
        raise ChannelPublicationError("recent-death database is invalid: {}".format(error)) from error


def _recent_notification_sources(database):
    """Return valid sent notifications, rejecting ambiguous/corrupt records."""
    by_id = {}
    for title, entry in database.items():
        for notification in recent_death_notifications(entry):
            errors = validate_recent_death_notification(notification)
            if errors:
                raise ChannelPublicationError("invalid recent-death notification for {}: {}".format(title, "; ".join(errors)))
            if notification.get("title") != title:
                raise ChannelPublicationError("recent-death notification title does not match canonical record")
            stored_qid = (entry.get("wikidata") or {}).get("qid")
            if stored_qid and notification.get("qid") != stored_qid:
                raise ChannelPublicationError("recent-death notification QID does not match canonical record")
            identifier = notification["notification_id"]
            if not _valid_uuid(identifier):
                raise ChannelPublicationError("recent-death notification_id must be a canonical UUID")
            by_id.setdefault(identifier, []).append((title, notification))
    if any(len(matches) != 1 for matches in by_id.values()):
        raise ChannelPublicationError("ambiguous duplicate recent-death notification_id")
    sources = []
    for identifier, matches in by_id.items():
        title, notification = matches[0]
        if notification["state"] != "sent":
            continue
        if not isinstance(notification.get("telegram_message_id"), int) or notification["telegram_message_id"] <= 0:
            raise ChannelPublicationError("sent recent-death notification lacks a positive Telegram message ID")
        message = notification.get("message_text")
        if not isinstance(message, str) or not message:
            raise ChannelPublicationError("sent recent-death notification lacks message text")
        sources.append({"source_id": identifier, "title": title, "notification": notification, "message": message})
    return sources


def _select_recent_source(database, source_id=None, published_source_ids=None):
    sources = _recent_notification_sources(database)
    if source_id is not None:
        matches = [source for source in sources if source["source_id"] == source_id]
        if not matches:
            raise ChannelPublicationError("recent-death source is not an eligible sent notification")
        return matches[0]
    published_source_ids = published_source_ids or set()
    sources = [source for source in sources if source["source_id"] not in published_source_ids]
    if not sources:
        raise ChannelPublicationError("no eligible unpublished sent recent-death notification found")
    return max(sources, key=lambda source: (source["notification"].get("state_changed_at", ""), source["source_id"]))


def discover_sent_recent_death_sources(database_path, state_root=None):
    database = _load_recent_database(database_path)
    published = set()
    if state_root is not None:
        published = {a["source_id"] for a in list_publications(state_root)
                     if a["source_type"] == RECENT_DEATH_SOURCE_TYPE and a["state"] == "sent"}
    return [source for source in _recent_notification_sources(database) if source["source_id"] not in published]


def _existing_publication_ids(existing, source_type):
    return {a["source_id"] for a in existing if a["source_type"] == source_type and a["state"] == "sent"}


def _publish_bundle(root, identifier, attempt, media_payload, publish):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    final = root / identifier
    if final.exists():
        raise ChannelPublicationError("publication bundle already exists")
    temporary = Path(tempfile.mkdtemp(prefix=".channel-publication-{}-".format(identifier), dir=str(root)))
    try:
        for item, payload in zip(attempt["media"], media_payload):
            _write_bytes(temporary / item["filename"], payload["bytes"])
        _atomic_json(temporary / "attempt.json", attempt)
        publish(str(temporary), str(final))
        temporary = None
    finally:
        if temporary is not None and temporary.exists():
            shutil.rmtree(str(temporary))
    return final


def _select_source(report_post_root, state_root, source_id=None, published_source_ids=None):
    if source_id is not None:
        bundle, attempt = _load_source_by_id(report_post_root, source_id)
        caption, media, payload = _eligible_source(bundle, attempt)
        return {
            "bundle": bundle,
            "attempt": attempt,
            "source_id": attempt["attempt_id"],
            "caption": caption,
            "media": media,
            "payload": payload,
        }

    sources = _all_eligible_sources(report_post_root)
    published_source_ids = published_source_ids or set()
    sources = [source for source in sources if source["source_id"] not in published_source_ids]
    if not sources:
        raise ChannelPublicationError("no eligible unpublished sent report-post source found")
    return max(
        sources,
        key=lambda source: (
            source["attempt"].get("state_changed_at", ""),
            source["source_id"],
        ),
    )


def discover_sent_report_sources(report_post_root, state_root=None):
    """Return read-only eligible sent sources for diagnostics/selection tests."""
    published = set()
    if state_root is not None:
        published = {
            attempt["source_id"]
            for attempt in list_publications(state_root)
            if attempt["source_type"] == SOURCE_TYPE and attempt["state"] == "sent"
        }
    return [
        source for source in _all_eligible_sources(report_post_root)
        if source["source_id"] not in published
    ]


def prepare_channel_publication_from_report(
    report_post_root,
    state_root,
    source_id=None,
    publication_id=None,
    now=None,
    publish=os.replace,
):
    """Freeze one eligible sent statistical report-post into a pending bundle."""
    try:
        existing = list_publications(state_root)
        unresolved = [attempt for attempt in existing if attempt["state"] in UNRESOLVED_STATES]
        if unresolved:
            raise ChannelPublicationError("unresolved channel publication blocks preparation")
        published_source_ids = {
            attempt["source_id"]
            for attempt in existing
            if attempt["source_type"] == SOURCE_TYPE and attempt["state"] == "sent"
        }
        source = _select_source(
            report_post_root,
            state_root,
            source_id=source_id,
            published_source_ids=published_source_ids,
        )
        if source["source_id"] in published_source_ids:
            raise ChannelPublicationError("source report-post is already channel-published")

        identifier = str(uuid.UUID(str(publication_id))) if publication_id else str(uuid.uuid4())
        stamp = _timestamp(now)
        media = []
        for source_item in source["media"]:
            item = {
                "position": source_item["position"],
                "kind": source_item["kind"],
                "filename": source_item["filename"],
                "sha256": source_item["sha256"],
            }
            if "page" in source_item:
                item["page"] = source_item["page"]
            media.append(item)

        attempt = {
            "schema_version": SCHEMA_VERSION,
            "publication_id": identifier,
            "created_at": stamp,
            "state": "pending",
            "state_changed_at": stamp,
            "source_type": SOURCE_TYPE,
            "source_id": source["source_id"],
            "payload_kind": PAYLOAD_KIND,
            "source_payload_fingerprint": source_payload_fingerprint(source["caption"], media),
            "publication_fingerprint": "",
            "caption_html": source["caption"],
            "message_html": None,
            "media": media,
            "telegram_message_ids": [],
            "error_kind": None,
            "error_summary": None,
            "resolution_note": None,
        }
        attempt["publication_fingerprint"] = publication_fingerprint(attempt)
        validate_publication(attempt)

        final = _publish_bundle(state_root, identifier, attempt, source["payload"], publish)

        return ChannelPublicationResult(
            ok=True,
            publication_id=identifier,
            source_type=SOURCE_TYPE,
            source_id=source["source_id"],
            payload_kind=PAYLOAD_KIND,
            ending_state="pending",
            source_payload_fingerprint=attempt["source_payload_fingerprint"],
            publication_fingerprint=attempt["publication_fingerprint"],
            telegram_called=False,
            persistence_succeeded=True,
            bundle_path=str(final),
        )
    except (OSError, ValueError, ReportPostError, ChannelPublicationError) as error:
        return ChannelPublicationResult(
            ok=False,
            source_type=SOURCE_TYPE,
            source_id=source_id,
            telegram_called=False,
            error_kind="validation_or_persistence",
            error_summary=str(error),
        )


def prepare_channel_publication_from_recent_death(
    database_path,
    state_root,
    source_id=None,
    publication_id=None,
    now=None,
    publish=os.replace,
):
    """Freeze one already-sent recent-death notification without rebuilding it."""
    try:
        existing = list_publications(state_root)
        unresolved = [a for a in existing if a["state"] in UNRESOLVED_STATES]
        if unresolved:
            raise ChannelPublicationError("unresolved channel publication blocks preparation")
        published = _existing_publication_ids(existing, RECENT_DEATH_SOURCE_TYPE)
        source = _select_recent_source(_load_recent_database(database_path), source_id, published)
        if source["source_id"] in published:
            raise ChannelPublicationError("recent-death source is already channel-published")
        identifier = str(uuid.UUID(str(publication_id))) if publication_id else str(uuid.uuid4())
        stamp = _timestamp(now)
        attempt = {
            "schema_version": SCHEMA_VERSION,
            "publication_id": identifier,
            "created_at": stamp,
            "state": "pending",
            "state_changed_at": stamp,
            "source_type": RECENT_DEATH_SOURCE_TYPE,
            "source_id": source["source_id"],
            "payload_kind": TEXT_PAYLOAD_KIND,
            "source_payload_fingerprint": source_payload_fingerprint(message_html=source["message"], payload_kind=TEXT_PAYLOAD_KIND),
            "publication_fingerprint": "",
            "caption_html": None,
            "message_html": source["message"],
            "media": [],
            "telegram_message_ids": [],
            "error_kind": None,
            "error_summary": None,
            "resolution_note": None,
        }
        attempt["publication_fingerprint"] = publication_fingerprint(attempt)
        validate_publication(attempt)
        final = _publish_bundle(state_root, identifier, attempt, [], publish)
        return ChannelPublicationResult(
            ok=True, publication_id=identifier, source_type=RECENT_DEATH_SOURCE_TYPE,
            source_id=source["source_id"], payload_kind=TEXT_PAYLOAD_KIND,
            ending_state="pending", source_payload_fingerprint=attempt["source_payload_fingerprint"],
            publication_fingerprint=attempt["publication_fingerprint"], telegram_called=False,
            persistence_succeeded=True, bundle_path=str(final),
        )
    except (OSError, ValueError, ChannelPublicationError) as error:
        return ChannelPublicationResult(ok=False, source_type=RECENT_DEATH_SOURCE_TYPE,
                                        source_id=source_id, telegram_called=False,
                                        error_kind="validation_or_persistence", error_summary=str(error))


def prepare_channel_publication(
    *, source_type, report_post_root=None, state_root, source_id=None, database_path=None, **kwargs
):
    if source_type == RECENT_DEATH_SOURCE_TYPE:
        if not database_path:
            return ChannelPublicationResult(ok=False, source_type=source_type, source_id=source_id,
                                            telegram_called=False, error_kind="missing_database",
                                            error_summary="--database is required for recent_death")
        return prepare_channel_publication_from_recent_death(
            database_path=database_path, state_root=state_root, source_id=source_id, **kwargs)
    if source_type != SOURCE_TYPE:
        return ChannelPublicationResult(
            ok=False,
            source_type=source_type,
            source_id=source_id,
            telegram_called=False,
            error_kind="unsupported_source_type",
            error_summary="source type {} is not implemented in this phase".format(source_type),
        )
    return prepare_channel_publication_from_report(
        report_post_root=report_post_root,
        state_root=state_root,
        source_id=source_id,
        **kwargs,
    )


def _save_channel_transition(bundle, attempt, new_state, *, now=None, ids=None,
                             error_kind=None, error_summary=None, resolution_note=None):
    if not is_legal_transition(attempt["state"], new_state):
        raise ChannelPublicationError("transition {} -> {} is not allowed".format(attempt["state"], new_state))
    changed = dict(attempt)
    changed["state"] = new_state
    changed["state_changed_at"] = _timestamp(now)
    changed["telegram_message_ids"] = list(ids or [])
    changed["error_kind"] = error_kind
    changed["error_summary"] = error_summary
    if resolution_note is not None:
        changed["resolution_note"] = resolution_note
    validate_publication(changed)
    _atomic_json(Path(bundle) / "attempt.json", changed)
    return changed


def _dispatch_outcome(telegram, expected_count):
    outcome = getattr(telegram, "outcome", None)
    ids = list(getattr(telegram, "message_ids", None) or [])
    if not ids and expected_count == 1:
        message_id = getattr(telegram, "message_id", None)
        if isinstance(message_id, int) and not isinstance(message_id, bool):
            ids = [message_id]
    if outcome == TELEGRAM_OUTCOME_CONFIRMED_SUCCESS and len(ids) == expected_count and all(
        isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in ids
    ):
        return "sent", ids, None, None
    if outcome == TELEGRAM_OUTCOME_DEFINITE_REJECTION:
        return "failed", [], "telegram_rejected", "Telegram explicitly rejected the publication"
    reason = getattr(telegram, "error_reason", None)
    kind = "response_invalid" if reason in ("invalid_json", "invalid_response") or getattr(telegram, "ok", False) else "transport_ambiguous"
    return "unknown", [], kind, "Telegram publication delivery could not be established"


def dispatch_channel_publication(publication_id, state_root, *, confirmed=False,
                                 sender_text=None, sender_media=None, settings=None,
                                 now=None):
    """Dispatch exactly one frozen channel bundle, with no source lookup."""
    result_base = dict(publication_id=publication_id, telegram_called=False)
    if not _valid_uuid(publication_id):
        return ChannelDispatchResult(False, **result_base, error_kind="validation", error_summary="publication_id must be a canonical UUID")
    try:
        attempt, bundle = load_publication(state_root, publication_id)
    except (OSError, ChannelPublicationError) as error:
        return ChannelDispatchResult(False, **result_base, error_kind="payload_integrity", error_summary=str(error), manual_reconciliation_required=True)
    result_base.update(source_type=attempt["source_type"], source_id=attempt["source_id"], payload_kind=attempt["payload_kind"], starting_state=attempt["state"])
    if not confirmed:
        return ChannelDispatchResult(False, **result_base, error_kind="confirmation_required", error_summary="explicit pending-dispatch confirmation is required")
    if attempt["state"] != "pending":
        return ChannelDispatchResult(False, **result_base, error_kind="invalid_state", error_summary="only a pending publication can be dispatched")
    try:
        payload = validate_publication_bundle(bundle, attempt)
    except (OSError, ChannelPublicationError) as error:
        return ChannelDispatchResult(False, **result_base, error_kind="payload_integrity", error_summary=str(error), manual_reconciliation_required=True)
    if settings is None:
        text_url, media_url, chat_id = get_channel_telegram_settings()
    else:
        text_url, media_url, chat_id = settings
    if not chat_id or not (media_url if attempt["payload_kind"] == PAYLOAD_KIND else text_url):
        return ChannelDispatchResult(False, **result_base, error_kind="missing_configuration", error_summary="channel Telegram token and chat ID are required")
    try:
        if attempt["payload_kind"] == PAYLOAD_KIND:
            sender = sender_media or send_media_group_to_chat
            telegram = sender(payload, attempt["caption_html"], media_url, chat_id)
            expected = len(attempt["media"])
        else:
            sender = sender_text or send_message_to_chat
            telegram = sender(attempt["message_html"], text_url, chat_id)
            expected = 1
    except Exception:
        telegram = type("Ambiguous", (), {"outcome": "ambiguous", "message_ids": None, "error_reason": "request_exception", "ok": False})()
    state, ids, error_kind, summary = _dispatch_outcome(telegram, expected)
    result_base["telegram_called"] = True
    try:
        _save_channel_transition(bundle, attempt, state, now=now, ids=ids, error_kind=error_kind, error_summary=summary)
    except (OSError, ChannelPublicationError):
        return ChannelDispatchResult(False, **result_base, ending_state=attempt["state"], telegram_message_ids=ids,
                                     persistence_succeeded=False, manual_reconciliation_required=True,
                                     error_kind="persistence_error", error_summary="Telegram may have accepted the publication but terminal state could not be persisted")
    return ChannelDispatchResult(state == "sent", **result_base, ending_state=state, telegram_message_ids=ids,
                                 persistence_succeeded=True, manual_reconciliation_required=state == "unknown",
                                 error_kind=error_kind, error_summary=summary)


def reconcile_channel_publication(publication_id, state_root, operation, *, message_ids=None,
                                  note=None, confirm_unsafe=False, now=None, show_message=False):
    if not _valid_uuid(publication_id):
        return {"ok": False, "operation": operation, "publication_id": publication_id,
                "error_kind": "validation", "error_summary": "publication_id must be a canonical UUID"}
    try:
        attempt, bundle = load_publication(state_root, publication_id)
    except (OSError, ChannelPublicationError) as error:
        return {"ok": False, "operation": operation, "publication_id": publication_id,
                "error_kind": "payload_integrity", "error_summary": str(error)}
    if operation == "show":
        result = {"ok": True, "operation": operation, "publication_id": publication_id,
                  "source_type": attempt["source_type"], "source_id": attempt["source_id"],
                  "payload_kind": attempt["payload_kind"], "state": attempt["state"],
                  "state_changed_at": attempt["state_changed_at"], "telegram_message_ids": attempt["telegram_message_ids"],
                  "error_kind": attempt.get("error_kind"), "error_summary": attempt.get("error_summary"),
                  "source_payload_fingerprint": attempt["source_payload_fingerprint"],
                  "publication_fingerprint": attempt["publication_fingerprint"], "media": attempt["media"]}
        if show_message and attempt["payload_kind"] == TEXT_PAYLOAD_KIND:
            result["message_html"] = attempt["message_html"]
        return result
    if not isinstance(note, str) or not note.strip():
        return {"ok": False, "operation": operation, "publication_id": publication_id,
                "error_kind": "validation", "error_summary": "a non-empty note is required"}
    state = attempt["state"]
    if operation == "mark-sent":
        ids = list(message_ids or [])
        expected = 1 if attempt["payload_kind"] == TEXT_PAYLOAD_KIND else len(attempt["media"])
        if state not in ("pending", "unknown") or len(ids) != expected or any(not isinstance(i, int) or isinstance(i, bool) or i <= 0 for i in ids):
            return {"ok": False, "operation": operation, "publication_id": publication_id,
                    "error_kind": "validation", "error_summary": "mark-sent requires pending/unknown state and complete positive message IDs"}
        target, ids, error_kind, error_summary = "sent", ids, None, None
    elif operation == "cancel":
        if state not in ("pending", "failed", "unknown"):
            return {"ok": False, "operation": operation, "publication_id": publication_id,
                    "error_kind": "invalid_state", "error_summary": "only unresolved publications can be cancelled"}
        if state == "unknown" and not confirm_unsafe:
            return {"ok": False, "operation": operation, "publication_id": publication_id,
                    "error_kind": "unsafe_confirmation_required", "error_summary": "cancelling unknown delivery requires --confirm-unsafe"}
        target, ids, error_kind, error_summary = "cancelled", [], None, None
    else:
        return {"ok": False, "operation": operation, "publication_id": publication_id,
                "error_kind": "invalid_operation", "error_summary": "unsupported reconciliation operation"}
    try:
        _save_channel_transition(bundle, attempt, target, now=now, ids=ids, error_kind=error_kind,
                                 error_summary=error_summary, resolution_note=note)
    except (OSError, ChannelPublicationError) as error:
        return {"ok": False, "operation": operation, "publication_id": publication_id,
                "error_kind": "persistence_error", "error_summary": str(error)}
    return {"ok": True, "operation": operation, "publication_id": publication_id,
            "old_state": state, "new_state": target, "telegram_message_ids": ids, "resolution_note": note}
