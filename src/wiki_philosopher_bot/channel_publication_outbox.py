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

SCHEMA_VERSION = 1
SOURCE_TYPE = "statistical_report"
SUPPORTED_SOURCE_TYPES = frozenset((SOURCE_TYPE,))
PAYLOAD_KIND = "media_group"
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


def source_payload_fingerprint(caption_html: str, media: list[dict]) -> str:
    """Identify the exact caption and ordered frozen media content."""
    return _json_fingerprint({
        "caption_html": caption_html,
        "media": _media_identity(media),
    })


def publication_fingerprint(attempt: dict) -> str:
    """Identify this source's prepared publication, excluding volatile fields."""
    return _json_fingerprint({
        "source_type": attempt.get("source_type"),
        "source_id": attempt.get("source_id"),
        "source_payload_fingerprint": attempt.get("source_payload_fingerprint"),
        "payload_kind": attempt.get("payload_kind"),
        "caption_html": attempt.get("caption_html"),
        "media": _media_identity(attempt.get("media", [])),
    })


def is_legal_transition(start_state: str, end_state: str) -> bool:
    return end_state in TRANSITIONS.get(start_state, frozenset())


def validate_publication(attempt: dict) -> dict:
    if not isinstance(attempt, dict) or attempt.get("schema_version") != SCHEMA_VERSION:
        raise ChannelPublicationError("unsupported channel-publication schema")
    if not _valid_uuid(attempt.get("publication_id")):
        raise ChannelPublicationError("publication_id must be a canonical UUID")
    if attempt.get("source_type") not in SUPPORTED_SOURCE_TYPES:
        raise ChannelPublicationError("unsupported source_type")
    if not _valid_uuid(attempt.get("source_id")):
        raise ChannelPublicationError("source_id must be a canonical UUID")
    if attempt.get("state") not in STATES:
        raise ChannelPublicationError("invalid publication state")
    if attempt.get("payload_kind") != PAYLOAD_KIND:
        raise ChannelPublicationError("unsupported payload_kind")
    for field_name in ("created_at", "state_changed_at"):
        if not isinstance(attempt.get(field_name), str) or not attempt[field_name]:
            raise ChannelPublicationError("{} is required".format(field_name))
    for field_name in ("source_payload_fingerprint", "publication_fingerprint"):
        if not _is_sha256(attempt.get(field_name)):
            raise ChannelPublicationError("{} must be a SHA-256 hex string".format(field_name))

    caption = attempt.get("caption_html")
    if not isinstance(caption, str) or not caption:
        raise ChannelPublicationError("caption_html must be a non-empty string")
    if attempt.get("message_html") is not None:
        raise ChannelPublicationError("media publication cannot contain message_html")
    media = attempt.get("media")
    if not isinstance(media, list) or not media or len(media) > MAX_MEDIA_ITEMS:
        raise ChannelPublicationError("media must contain 1 to {} items".format(MAX_MEDIA_ITEMS))
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
    if attempt["state"] == "sent" and len(message_ids) != len(media):
        raise ChannelPublicationError("sent publication requires one ID per media item")
    if attempt["state"] != "sent" and message_ids:
        raise ChannelPublicationError("non-sent publication cannot contain message IDs")
    for field_name in ("error_kind", "error_summary", "resolution_note"):
        if attempt.get(field_name) is not None and not isinstance(attempt[field_name], str):
            raise ChannelPublicationError("{} must be a string or null".format(field_name))

    if attempt["source_payload_fingerprint"] != source_payload_fingerprint(caption, media):
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

        root = Path(state_root)
        root.mkdir(parents=True, exist_ok=True)
        final = root / identifier
        if final.exists():
            raise ChannelPublicationError("publication bundle already exists")
        temporary = Path(tempfile.mkdtemp(prefix=".channel-publication-{}-".format(identifier), dir=str(root)))
        try:
            for item, payload in zip(media, source["payload"]):
                _write_bytes(temporary / item["filename"], payload["bytes"])
            _atomic_json(temporary / "attempt.json", attempt)
            publish(str(temporary), str(final))
            temporary = None
        finally:
            if temporary is not None and temporary.exists():
                shutil.rmtree(str(temporary))

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


def prepare_channel_publication(
    *, source_type, report_post_root, state_root, source_id=None, **kwargs
):
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
