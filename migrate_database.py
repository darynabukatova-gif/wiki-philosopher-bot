import os
import stat
import copy
import json
import hashlib
import tempfile
import argparse
from pathlib import Path
from utils import clean_title
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple, Set
from database_schema import (
    DATABASE_SCHEMA_VERSION,
    make_empty_database_entry,
    serialize_database_entries,
    validate_database_dataset,
    validate_database_entry,
)
from config import (
    DATABASE_FILE,
    ENTITY_FILE,
    POSTED_FILE,
    PROCESSED_FILE,
    QUOTE_FAILURE_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)
from migration import (
    count_legacy_records,
    read_legacy_sources,
    report_conflicts,
    validate_legacy_files,
)

@dataclass(frozen=True)
class FileFingerprint:
    filename: str
    byte_size: int
    sha256: str

@dataclass(frozen=True)
class HistoricalEvaluation:
    status: str
    algorithm_version: Optional[int]
    human_confidence: Optional[int]
    philosopher_confidence: Optional[int]
    content_confidence: Optional[int]
    reasons: List[str]
    processed_at: Optional[float]  # final type subject to schema clarification
    legacy_result: Optional[dict]
    source: dict

def fingerprint_file(path: Path) -> FileFingerprint:
    sha256 = hashlib.sha256()

    with path.open("rb") as file:
        while True:
            chunk = file.read(1024 * 1024)

            if not chunk:
                break

            sha256.update(chunk)

    return FileFingerprint(
        filename=path.name,
        byte_size=path.stat().st_size,
        sha256=sha256.hexdigest(),
    )

def parse_backup_manifest(
    manifest_path: Path,
    expected_filenames: Iterable[str],
) -> Dict[str, FileFingerprint]:
    expected = set(expected_filenames)
    fingerprints = {}

    with manifest_path.open(
        "r",
        encoding="utf-8",
    ) as file:
        for raw_line in file:
            line = raw_line.strip()

            if not line:
                continue

            parts = [
                part.strip()
                for part in line.split("|")
            ]

            if len(parts) != 3:
                continue

            sha256_value, byte_size_text, filename = parts

            # Ignore the human-readable format/header line.
            if sha256_value == "SHA-256":
                continue

            if filename not in expected:
                continue

            try:
                byte_size = int(byte_size_text)
            except ValueError:
                raise ValueError(
                    "Invalid byte size in manifest for {!r}: {!r}".format(
                        filename,
                        byte_size_text,
                    )
                )

            if len(sha256_value) != 64:
                raise ValueError(
                    "Invalid SHA-256 in manifest for {!r}".format(
                        filename
                    )
                )

            if filename in fingerprints:
                raise ValueError(
                    "Duplicate manifest entry for {!r}".format(
                        filename
                    )
                )

            fingerprints[filename] = FileFingerprint(
                filename=filename,
                byte_size=byte_size,
                sha256=sha256_value,
            )

    found = set(fingerprints)

    missing = expected - found
    extra = found - expected

    if missing:
        raise ValueError(
            "Manifest is missing expected files: {}".format(
                sorted(missing)
            )
        )

    if extra:
        raise ValueError(
            "Manifest contains unexpected files: {}".format(
                sorted(extra)
            )
        )

    if len(fingerprints) != len(expected):
        raise ValueError(
            "Manifest does not contain exactly the expected files"
        )

    return fingerprints

def verify_source_manifest(
    source_dir: Path,
    expected: Dict[str, FileFingerprint],
) -> List[str]:
    """Return mismatch messages; empty list means exact match."""
    mismatches = []

    for filename in sorted(expected):
        expected_fingerprint = expected[filename]
        path = source_dir / filename

        if not path.is_file():
            mismatches.append(
                "Missing source file: {}".format(filename)
            )
            continue

        actual = fingerprint_file(path)

        if actual.byte_size != expected_fingerprint.byte_size:
            mismatches.append(
                "{}: byte size {} != expected {}".format(
                    filename,
                    actual.byte_size,
                    expected_fingerprint.byte_size,
                )
            )

        if actual.sha256 != expected_fingerprint.sha256:
            mismatches.append(
                "{}: SHA-256 {} != expected {}".format(
                    filename,
                    actual.sha256,
                    expected_fingerprint.sha256,
                )
            )

    return mismatches

def has_write_bits(path: Path) -> bool:
    mode = path.stat().st_mode

    write_bits = (
        stat.S_IWUSR
        | stat.S_IWGRP
        | stat.S_IWOTH
    )

    return bool(mode & write_bits)

def verify_backup_snapshot(
    backup_dir: Path,
    manifest_path: Path,
    expected_filenames: Iterable[str],
) -> List[str]:
    """Verify names, manifest, hashes, sizes, and non-writable permissions."""
    expected_filenames = tuple(expected_filenames)
    mismatches = []

    if not backup_dir.is_dir():
        return [
            "Backup directory does not exist: {}".format(
                backup_dir
            )
        ]

    if not manifest_path.is_file():
        return [
            "Backup manifest does not exist: {}".format(
                manifest_path
            )
        ]

    try:
        expected = parse_backup_manifest(
            manifest_path,
            expected_filenames,
        )
    except (OSError, ValueError) as error:
        return [
            "Invalid backup manifest: {}".format(error)
        ]

    mismatches.extend(
        verify_source_manifest(
            backup_dir,
            expected,
        )
    )

    database_path = backup_dir / "database.jsonl"

    if database_path.exists():
        mismatches.append(
            "database.jsonl must not be present in the migration-input backup"
        )

    if has_write_bits(backup_dir):
        mismatches.append(
            "Backup directory is writable"
        )

    for filename in sorted(expected_filenames):
        path = backup_dir / filename

        if path.exists() and has_write_bits(path):
            mismatches.append(
                "Backup file is writable: {}".format(
                    filename
                )
            )

    if has_write_bits(manifest_path):
        mismatches.append(
            "Backup manifest is writable"
        )

    return mismatches

def verify_live_and_backup_baseline(
    live_source_dir: Path,
    backup_dir: Path,
    manifest_path: Path,
    expected_filenames: Iterable[str],
) -> Dict[str, object]:
    expected_filenames = tuple(expected_filenames)

    expected = parse_backup_manifest(
        manifest_path,
        expected_filenames,
    )

    live_mismatches = verify_source_manifest(
        live_source_dir,
        expected,
    )

    backup_mismatches = verify_backup_snapshot(
        backup_dir,
        manifest_path,
        expected_filenames,
    )

    return {
        "ok": (
            not live_mismatches
            and not backup_mismatches
        ),
        "expected": expected,
        "live_mismatches": live_mismatches,
        "backup_mismatches": backup_mismatches,
    }

SOURCE_ORDER = (
    SUMMARY_FILE,
    ENTITY_FILE,
    QUOTE_FILE,
    QUOTE_FAILURE_FILE,
    RESULT_FILE,
    PROCESSED_FILE,
    POSTED_FILE,
)

def valid_title_from_record(
    filename: str,
    record: dict,
) -> Optional[str]:
    value = record.get("value")

    if filename == POSTED_FILE:
        if isinstance(value, str) and value:
            return value
        return None

    if not isinstance(value, dict):
        return None

    title = value.get("title")

    if isinstance(title, str) and title:
        return title

    return None

def build_title_index(
    sources: Dict[str, dict],
) -> Dict[str, Dict[str, List[dict]]]:
    title_index = {}

    for filename in SOURCE_ORDER:
        source = sources.get(filename)

        if source is None:
            raise ValueError(
                "Missing migration source: {!r}".format(filename)
            )

        records = source.get("records")

        if not isinstance(records, list):
            raise ValueError(
                "Invalid records collection for {!r}".format(filename)
            )

        for raw_record in records:
            title = valid_title_from_record(filename, raw_record)

            if title is None:
                raise ValueError(
                    "Invalid title in {!r}, record_index={!r}, line_number={!r}".format(
                        filename,
                        raw_record.get("record_index"),
                        raw_record.get("line_number"),
                    )
                )

            if title not in title_index:
                title_index[title] = {}

            if filename not in title_index[title]:
                title_index[title][filename] = []

            title_index[title][filename].append(raw_record)

    return title_index

def source_value(
    filename: str,
    raw_record: dict,
    value: object,
) -> dict:
    return {
        "source": filename,
        "line_number": raw_record.get("line_number"),
        "record_index": raw_record.get("record_index"),
        "value": value,
    }

SOURCE_RANK = {
    filename: index
    for index, filename in enumerate(SOURCE_ORDER)
}

def add_legacy_source(
    entry: dict,
    filename: str,
) -> None:
    if filename not in SOURCE_RANK:
        raise ValueError(
            "Unknown migration legacy source: {!r}".format(
                filename
            )
        )

    sources = entry["migration"]["legacy_sources"]

    if filename not in sources:
        sources.append(filename)

    sources.sort(
        key=lambda item: SOURCE_RANK[item]
    )

def values_are_equal(values: List[object]) -> bool:
    if not values:
        return True

    first = values[0]

    return all(
        value == first
        for value in values[1:]
    )

def _source_value_sort_key(item):
    return (
        SOURCE_RANK.get(
            item.get("source"),
            len(SOURCE_ORDER),
        ),
        item.get("line_number")
        if item.get("line_number") is not None
        else float("inf"),
        item.get("record_index")
        if item.get("record_index") is not None
        else float("inf"),
    )

def _conflict_sort_key(conflict):
    return (
        conflict["field"],
        tuple(
            _source_value_sort_key(value)
            for value in conflict["values"]
        ),
    )

def add_conflict(
    entry: dict,
    field: str,
    values: List[dict],
    resolution: str,
) -> None:
    ordered_values = sorted(
        values,
        key=_source_value_sort_key,
    )

    conflict = {
        "field": field,
        "values": ordered_values,
        "resolution": resolution,
    }

    entry["migration"]["conflicts"].append(
        conflict
    )

    entry["migration"]["conflicts"].sort(
        key=_conflict_sort_key
    )

def choose_known_value(
    field: str,
    candidates: List[dict],
    safe_default: object,
    entry: dict,
) -> object:
    if not candidates:
        return safe_default

    known_candidates = [
        candidate
        for candidate in candidates
        if candidate["value"] is not None
    ]

    if not known_candidates:
        return safe_default

    known_values = [
        candidate["value"]
        for candidate in known_candidates
    ]

    if values_are_equal(known_values):
        return known_values[0]

    add_conflict(
        entry=entry,
        field=field,
        values=known_candidates,
        resolution="unresolved_safe_default",
    )

    return safe_default

def merge_summary_records(
    entry: dict,
    records: List[dict],
) -> None:
    if not records:
        return

    add_legacy_source(entry, SUMMARY_FILE)

    candidates = []

    for raw_record in records:
        value = raw_record.get("value")

        if not isinstance(value, dict):
            raise ValueError(
                "Invalid summary record at line {!r}".format(
                    raw_record.get("line_number")
                )
            )

        summary = value.get("summary")

        if summary is not None and not isinstance(summary, str):
            raise ValueError(
                "Invalid summary type at line {!r}".format(
                    raw_record.get("line_number")
                )
            )

        candidates.append(
            source_value(
                SUMMARY_FILE,
                raw_record,
                summary,
            )
        )

    entry["summary"]["text"] = choose_known_value(
        field="summary.text",
        candidates=candidates,
        safe_default=None,
        entry=entry,
    )

    entry["summary"]["source"] = "Wikipedia"
    entry["summary"]["fetched_at"] = None

def normalize_entity_record(
    raw_record: dict,
) -> dict:
    value = raw_record.get("value")

    if not isinstance(value, dict):
        raise ValueError(
            "Entity record must be an object"
        )

    valid = value.get("valid")

    if not isinstance(valid, bool):
        raise ValueError(
            "Entity record 'valid' must be boolean"
        )

    if valid:
        qid = value.get("qid")
        instances = value.get("instances")
        occupations = value.get("occupations")
        birth = value.get("birth")
        death = value.get("death")
        is_human = value.get("is_human")
        is_philosopher = value.get("is_philosopher")

        if qid is not None and not isinstance(qid, str):
            raise ValueError("Entity qid must be string or null")

        if (
            not isinstance(instances, list)
            or not all(isinstance(item, str) for item in instances)
        ):
            raise ValueError(
                "Entity instances must be a list of strings"
            )

        if (
            not isinstance(occupations, list)
            or not all(isinstance(item, str) for item in occupations)
        ):
            raise ValueError(
                "Entity occupations must be a list of strings"
            )

        for name, year in (
            ("birth", birth),
            ("death", death),
        ):
            if (
                year is not None
                and (
                    not isinstance(year, int)
                    or isinstance(year, bool)
                )
            ):
                raise ValueError(
                    "{} must be integer or null".format(name)
                )

        for name, flag in (
            ("is_human", is_human),
            ("is_philosopher", is_philosopher),
        ):
            if not isinstance(flag, bool):
                raise ValueError(
                    "{} must be boolean".format(name)
                )

        return {
            "status": "available",
            "reason": None,
            "qid": qid,
            "instances": list(instances),
            "occupations": list(occupations),
            "birth_year": birth,
            "death_year": death,
            "is_human": is_human,
            "is_philosopher": is_philosopher,
            "fetched_at": None,
        }

    reason = value.get("reason")
    qid = value.get("qid")

    if not isinstance(reason, str) or not reason:
        raise ValueError(
            "Invalid entity record must contain a reason"
        )

    if qid is not None and not isinstance(qid, str):
        raise ValueError(
            "Entity qid must be string or null"
        )

    return {
        "status": "unavailable",
        "reason": reason,
        "qid": qid,
        "instances": [],
        "occupations": [],
        "birth_year": None,
        "death_year": None,
        "is_human": None,
        "is_philosopher": None,
        "fetched_at": None,
    }

def merge_entity_records(
    entry: dict,
    records: List[dict],
) -> None:
    if not records:
        return

    add_legacy_source(entry, ENTITY_FILE)

    normalized = [
        (
            raw_record,
            normalize_entity_record(raw_record),
        )
        for raw_record in records
    ]

    if len(normalized) == 1:
        entry["wikidata"] = normalized[0][1]
        return

    values = [
        item[1]
        for item in normalized
    ]

    if values_are_equal(values):
        entry["wikidata"] = values[0]
        return

    candidates = [
        source_value(
            ENTITY_FILE,
            raw_record,
            value,
        )
        for raw_record, value in normalized
    ]

    add_conflict(
        entry=entry,
        field="wikidata",
        values=candidates,
        resolution="unresolved_safe_default",
    )

    entry["wikidata"] = {
        "status": "unknown",
        "reason": None,
        "qid": None,
        "instances": [],
        "occupations": [],
        "birth_year": None,
        "death_year": None,
        "is_human": None,
        "is_philosopher": None,
        "fetched_at": None,
    }

def normalize_quote_item(item: dict) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Quote item must be an object")

    required = (
        "text",
        "length",
        "word_count",
        "source",
    )

    for field in required:
        if field not in item:
            raise ValueError(
                "Quote item missing required field: {}".format(
                    field
                )
            )

    text = item["text"]
    length = item["length"]
    word_count = item["word_count"]
    source = item["source"]

    if not isinstance(text, str) or not text:
        raise ValueError(
            "Quote text must be a non-empty string"
        )

    if (
        not isinstance(length, int)
        or isinstance(length, bool)
    ):
        raise ValueError(
            "Quote length must be an integer"
        )

    if (
        not isinstance(word_count, int)
        or isinstance(word_count, bool)
    ):
        raise ValueError(
            "Quote word_count must be an integer"
        )

    if not isinstance(source, str) or not source:
        raise ValueError(
            "Quote source must be a non-empty string"
        )

    return {
        "text": text,
        "length": length,
        "word_count": word_count,
        "source": source,
    }

def merge_quote_records(
    entry: dict,
    records: List[dict],
) -> None:
    if not records:
        return

    add_legacy_source(entry, QUOTE_FILE)

    normalized_lists = []
    merged_items = []

    for raw_record in records:
        value = raw_record.get("value")

        if not isinstance(value, dict):
            raise ValueError(
                "Quote record must be an object"
            )

        quotes = value.get("quotes")

        if not isinstance(quotes, list):
            raise ValueError(
                "Quote record 'quotes' must be a list"
            )

        normalized_items = [
            normalize_quote_item(item)
            for item in quotes
        ]

        normalized_lists.append(
            (raw_record, normalized_items)
        )

        for item in normalized_items:
            if item not in merged_items:
                merged_items.append(item)

    entry["quotes"]["items"] = merged_items
    entry["quotes"]["status"] = "available"

    quote_lists = [
        items
        for _, items in normalized_lists
    ]

    if not values_are_equal(quote_lists):
        add_conflict(
            entry=entry,
            field="quotes.items",
            values=[
                source_value(
                    QUOTE_FILE,
                    raw_record,
                    items,
                )
                for raw_record, items in normalized_lists
            ],
            resolution="merged_unique_quote_items",
        )

HISTORICAL_FAILURE_REASON_MAP = {
    "404": "http_404",
    "rate_limit": "http_429",
    "timeout": "request_exception",
}

def normalize_failure_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason:
        raise ValueError(
            "Quote failure reason must be a non-empty string"
        )

    return HISTORICAL_FAILURE_REASON_MAP.get(
        reason,
        reason,
    )

def select_newest_quote_failure(
    records: List[dict],
    entry: dict,
) -> Optional[dict]:
    if not records:
        return None

    add_legacy_source(
        entry,
        QUOTE_FAILURE_FILE,
    )

    candidates = []

    for raw_record in records:
        value = raw_record.get("value")

        if not isinstance(value, dict):
            raise ValueError(
                "Quote failure record must be an object"
            )

        reason = normalize_failure_reason(
            value.get("reason")
        )
        timestamp = value.get("timestamp")
        retries = value.get("retries")

        if (
            not isinstance(timestamp, (int, float))
            or isinstance(timestamp, bool)
        ):
            raise ValueError(
                "Quote failure timestamp must be numeric"
            )

        if (
            not isinstance(retries, int)
            or isinstance(retries, bool)
        ):
            raise ValueError(
                "Quote failure retries must be an integer"
            )

        normalized = {
            "reason": reason,
            "timestamp": timestamp,
            "retries": retries,
        }

        candidates.append(
            (raw_record, normalized)
        )

    newest_timestamp = max(
        normalized["timestamp"]
        for _, normalized in candidates
    )

    newest = [
        (raw_record, normalized)
        for raw_record, normalized in candidates
        if normalized["timestamp"]
        == newest_timestamp
    ]

    newest_values = [
        normalized
        for _, normalized in newest
    ]

    if values_are_equal(newest_values):
        return newest_values[0]

    add_conflict(
        entry=entry,
        field="quotes.failure",
        values=[
            source_value(
                QUOTE_FAILURE_FILE,
                raw_record,
                normalized,
            )
            for raw_record, normalized in newest
        ],
        resolution="tied_newest_failure_unresolved",
    )

    return None

def reconcile_quotes_and_failure(
    entry: dict,
) -> None:
    items = entry["quotes"]["items"]
    failure = entry["quotes"]["failure"]

    if entry["quotes"]["status"] == "available":
        return

    if failure is None:
        entry["quotes"]["status"] = "unknown"
        return

    if failure["reason"] == "no_quotes_found":
        entry["quotes"]["status"] = "not_found"
    else:
        entry["quotes"]["status"] = "failed"

def status_from_legacy_evaluation(
    record: dict,
    source_filename: str,
) -> Optional[str]:
    if source_filename not in (
        RESULT_FILE,
        PROCESSED_FILE,
    ):
        raise ValueError(
            "Unsupported evaluation source: {!r}".format(
                source_filename
            )
        )

    explicit_status = record.get("status")

    if explicit_status is not None:
        if explicit_status not in (
            "accepted",
            "rejected",
        ):
            raise ValueError(
                "Invalid evaluation status: {!r}".format(
                    explicit_status
                )
            )

        if (
            source_filename == RESULT_FILE
            and explicit_status != "accepted"
        ):
            raise ValueError(
                "Explicit status {!r} contradicts results.jsonl".format(
                    explicit_status
                )
            )

        if (
            source_filename == PROCESSED_FILE
            and explicit_status != "rejected"
        ):
            raise ValueError(
                "Explicit status {!r} contradicts processed.jsonl".format(
                    explicit_status
                )
            )

        return explicit_status

    if "accepted" in record:
        accepted = record["accepted"]

        if not isinstance(accepted, bool):
            raise ValueError(
                "Legacy accepted field must be boolean"
            )

        return (
            "accepted"
            if accepted
            else "rejected"
        )

    if source_filename == RESULT_FILE:
        return "accepted"

    if source_filename == PROCESSED_FILE:
        return "rejected"

    return None

def _optional_confidence(
    record: dict,
    field_name: str,
) -> Optional[int]:
    if field_name not in record:
        return None

    value = record[field_name]

    if value is None:
        return None

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
    ):
        raise ValueError(
            "{} must be an integer or null".format(
                field_name
            )
        )

    return value

def _optional_processed_at(
    record: dict,
) -> Optional[float]:
    if "last_processed" not in record:
        return None

    value = record["last_processed"]

    if value is None:
        return None

    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
    ):
        raise ValueError(
            "last_processed must be a number or null"
        )

    return value

def _historical_reasons(
    record: dict,
) -> List[str]:
    if "reasons" not in record:
        return []

    reasons = record["reasons"]

    if not isinstance(reasons, list):
        raise ValueError(
            "reasons must be a list of strings"
        )

    if not all(
        isinstance(reason, str)
        for reason in reasons
    ):
        raise ValueError(
            "reasons must be a list of strings"
        )

    return list(reasons)

def normalize_historical_evaluation(
    raw_record: dict,
    source_filename: str,
) -> HistoricalEvaluation:
    value = raw_record.get("value")

    if not isinstance(value, dict):
        raise ValueError(
            "Historical evaluation record must be an object"
        )

    status = status_from_legacy_evaluation(
        value,
        source_filename,
    )

    if status is None:
        raise ValueError(
            "Could not determine historical evaluation status"
        )

    human_confidence = _optional_confidence(
        value,
        "human_confidence",
    )

    philosopher_confidence = _optional_confidence(
        value,
        "philosopher_confidence",
    )

    content_confidence = _optional_confidence(
        value,
        "content_confidence",
    )

    reasons = _historical_reasons(value)

    processed_at = _optional_processed_at(
        value
    )

    legacy_result = None

    if "result" in value:
        result = value["result"]

        if result is not None:
            if not isinstance(result, dict):
                raise ValueError(
                    "Non-null result must be an object"
                )

            if source_filename == RESULT_FILE:
                raise ValueError(
                    "Non-null results.jsonl result payload "
                    "has no canonical migration destination"
                )

            if source_filename == PROCESSED_FILE:
                legacy_result = result

    source = {
        "source": source_filename,
        "line_number": raw_record.get(
            "line_number"
        ),
        "record_index": raw_record.get(
            "record_index"
        ),
    }

    return HistoricalEvaluation(
        status=status,
        algorithm_version=None,
        human_confidence=human_confidence,
        philosopher_confidence=philosopher_confidence,
        content_confidence=content_confidence,
        reasons=reasons,
        processed_at=processed_at,
        legacy_result=legacy_result,
        source=source,
    )

def _merge_embedded_summary(
    entry: dict,
    value: dict,
) -> None:
    if "summary" not in value:
        return

    summary = value["summary"]

    if summary is None:
        return

    if not isinstance(summary, str):
        raise ValueError(
            "Embedded summary must be a string or null"
        )

    if not summary:
        return

    if entry["summary"]["text"] is None:
        entry["summary"]["text"] = summary

def _merge_embedded_display_title(
    entry: dict,
    value: dict,
) -> None:
    if "display_title" not in value:
        return

    display_title = value["display_title"]

    if display_title is None:
        return

    if (
        not isinstance(display_title, str)
        or not display_title
    ):
        raise ValueError(
            "Embedded display_title must be a non-empty string or null"
        )

    default_display_title = (
        clean_title(entry["title"])
        or entry["title"]
    )

    if (
        entry["display_title"]
        == default_display_title
    ):
        entry["display_title"] = (
            display_title
        )

def _merge_embedded_wikidata(
    entry: dict,
    value: dict,
) -> None:
    field_map = {
        "is_human": "is_human",
        "is_philosopher": "is_philosopher",
        "birth_w": "birth_year",
        "death_w": "death_year",
    }

    for legacy_field, canonical_field in (
        field_map.items()
    ):
        if legacy_field not in value:
            continue

        legacy_value = value[
            legacy_field
        ]

        if legacy_value is None:
            continue

        if legacy_field in (
            "is_human",
            "is_philosopher",
        ):
            if not isinstance(
                legacy_value,
                bool,
            ):
                raise ValueError(
                    "{} must be boolean or null".format(
                        legacy_field
                    )
                )

        else:
            if (
                not isinstance(
                    legacy_value,
                    int,
                )
                or isinstance(
                    legacy_value,
                    bool,
                )
            ):
                raise ValueError(
                    "{} must be integer or null".format(
                        legacy_field
                    )
                )

        if (
            entry["wikidata"][
                canonical_field
            ]
            is None
        ):
            entry["wikidata"][
                canonical_field
            ] = legacy_value

def _merge_embedded_quotes(
    entry: dict,
    value: dict,
) -> None:
    if "quotes" not in value:
        return

    quotes = value["quotes"]

    if quotes is None:
        return

    if not isinstance(quotes, list):
        raise ValueError(
            "Embedded quotes must be a list or null"
        )

    normalized = [
        normalize_quote_item(item)
        for item in quotes
    ]

    if (
        entry["quotes"]["status"]
        == "unknown"
        and not entry["quotes"]["items"]
    ):
        entry["quotes"]["items"] = (
            normalized
        )

        entry["quotes"]["status"] = (
            "available"
        )

def merge_embedded_legacy_facts(
    entry: dict,
    raw_record: dict,
    source_filename: str,
    raw_source: dict,
) -> None:
    value = raw_record.get("value")

    if not isinstance(value, dict):
        raise ValueError(
            "Embedded legacy record must be an object"
        )

    add_legacy_source(
        entry,
        source_filename,
    )

    legacy_sources = entry["migration"][
        "legacy_sources"
    ]

    _merge_embedded_display_title(
        entry,
        value,
    )

    if SUMMARY_FILE not in legacy_sources:
        _merge_embedded_summary(
            entry,
            value,
        )

    if ENTITY_FILE not in legacy_sources:
        _merge_embedded_wikidata(
            entry,
            value,
        )

    if QUOTE_FILE not in legacy_sources:
        _merge_embedded_quotes(
            entry,
            value,
        )

def evaluation_candidate_sort_key(
    candidate: HistoricalEvaluation,
) -> tuple:
    source = candidate.source

    source_name = source.get("source")
    source_rank = SOURCE_RANK.get(
        source_name,
        len(SOURCE_ORDER),
    )

    line_number = source.get("line_number")
    record_index = source.get("record_index")

    return (
        source_rank,
        (
            line_number
            if line_number is not None
            else float("inf")
        ),
        (
            record_index
            if record_index is not None
            else float("inf")
        ),
    )

def _historical_evaluation_value(
    candidate: HistoricalEvaluation,
) -> dict:
    return {
        "status": candidate.status,
        "algorithm_version": candidate.algorithm_version,
        "human_confidence": candidate.human_confidence,
        "philosopher_confidence": candidate.philosopher_confidence,
        "content_confidence": candidate.content_confidence,
        "reasons": list(candidate.reasons),
        "processed_at": candidate.processed_at,
        "legacy_result": copy.deepcopy(
            candidate.legacy_result
        ),
    }

def _candidate_source_value(
    candidate: HistoricalEvaluation,
    value: object,
) -> dict:
    return {
        "source": candidate.source.get("source"),
        "line_number": candidate.source.get(
            "line_number"
        ),
        "record_index": candidate.source.get(
            "record_index"
        ),
        "value": copy.deepcopy(value),
    }

def clear_unresolved_evaluation(
    entry: dict,
) -> None:
    entry["evaluation"]["status"] = "unprocessed"
    entry["evaluation"]["algorithm_version"] = None
    entry["evaluation"]["human_confidence"] = None
    entry["evaluation"]["philosopher_confidence"] = None
    entry["evaluation"]["content_confidence"] = None
    entry["evaluation"]["reasons"] = []
    entry["evaluation"]["legacy_result"] = None
    entry["evaluation"]["processed_at"] = None

def _set_historical_evaluation(
    entry: dict,
    candidate: HistoricalEvaluation,
) -> None:
    evaluation = entry["evaluation"]

    evaluation["status"] = candidate.status

    # Historical provenance is unknown.
    evaluation["algorithm_version"] = None

    evaluation["human_confidence"] = (
        candidate.human_confidence
    )
    evaluation["philosopher_confidence"] = (
        candidate.philosopher_confidence
    )
    evaluation["content_confidence"] = (
        candidate.content_confidence
    )

    evaluation["reasons"] = list(
        candidate.reasons
    )

    evaluation["processed_at"] = (
        candidate.processed_at
    )

    evaluation["legacy_result"] = copy.deepcopy(
        candidate.legacy_result
    )

def _resolve_legacy_result(
    entry: dict,
    candidates: List[HistoricalEvaluation],
) -> None:
    with_legacy_result = [
        candidate
        for candidate in candidates
        if candidate.legacy_result is not None
    ]

    if not with_legacy_result:
        entry["evaluation"]["legacy_result"] = None
        return

    values = [
        candidate.legacy_result
        for candidate in with_legacy_result
    ]

    if values_are_equal(values):
        entry["evaluation"]["legacy_result"] = (
            copy.deepcopy(values[0])
        )
        return

    entry["evaluation"]["legacy_result"] = None

    add_conflict(
        entry=entry,
        field="evaluation.legacy_result",
        values=[
            _candidate_source_value(
                candidate,
                candidate.legacy_result,
            )
            for candidate in with_legacy_result
        ],
        resolution=(
            "unresolved_legacy_result_set_null"
        ),
    )

def apply_historical_evaluation(
    entry: dict,
    candidates: List[HistoricalEvaluation],
) -> None:
    if not candidates:
        return

    ordered = sorted(
        candidates,
        key=evaluation_candidate_sort_key,
    )

    # Every evaluation source represented by the candidates
    # contributed historical information.
    for candidate in ordered:
        source_name = candidate.source.get(
            "source"
        )

        if source_name is not None:
            add_legacy_source(
                entry,
                source_name,
            )

    candidate_values = [
        _historical_evaluation_value(
            candidate
        )
        for candidate in ordered
    ]

    # Exact logical duplicates: retain one.
    if values_are_equal(candidate_values):
        _set_historical_evaluation(
            entry,
            ordered[0],
        )

        _resolve_legacy_result(
            entry,
            ordered,
        )

        return

    # One candidate only.
    if len(ordered) == 1:
        _set_historical_evaluation(
            entry,
            ordered[0],
        )

        _resolve_legacy_result(
            entry,
            ordered,
        )

        return

    timestamped = [
        candidate
        for candidate in ordered
        if candidate.processed_at is not None
    ]

    selected = None

    if timestamped:
        newest_timestamp = max(
            candidate.processed_at
            for candidate in timestamped
        )

        newest_candidates = [
            candidate
            for candidate in timestamped
            if candidate.processed_at
            == newest_timestamp
        ]

        # Only a genuinely unique newest timestamp
        # may resolve the historical evaluation.
        if len(newest_candidates) == 1:
            selected = newest_candidates[0]

    statuses = [
        candidate.status
        for candidate in ordered
    ]

    statuses_disagree = not values_are_equal(
        statuses
    )

    if selected is not None:
        _set_historical_evaluation(
            entry,
            selected,
        )

        if statuses_disagree:
            add_conflict(
                entry=entry,
                field="evaluation.status",
                values=[
                    _candidate_source_value(
                        candidate,
                        candidate.status,
                    )
                    for candidate in ordered
                ],
                resolution=(
                    "selected_unique_latest_timestamp"
                ),
            )
        else:
            # Same decision, but other parts of the
            # historical evaluation differed.
            add_conflict(
                entry=entry,
                field="evaluation",
                values=[
                    _candidate_source_value(
                        candidate,
                        _historical_evaluation_value(
                            candidate
                        ),
                    )
                    for candidate in ordered
                ],
                resolution=(
                    "selected_unique_latest_timestamp"
                ),
            )

        _resolve_legacy_result(
            entry,
            ordered,
        )

        return

    # No reliable unique newest evaluation.
    clear_unresolved_evaluation(entry)

    if statuses_disagree:
        add_conflict(
            entry=entry,
            field="evaluation.status",
            values=[
                _candidate_source_value(
                    candidate,
                    candidate.status,
                )
                for candidate in ordered
            ],
            resolution=(
                "unresolved_set_unprocessed"
            ),
        )
    else:
        # Same accepted/rejected decision, but multiple
        # incompatible historical bundles and no reliable
        # temporal winner.
        add_conflict(
            entry=entry,
            field="evaluation",
            values=[
                _candidate_source_value(
                    candidate,
                    _historical_evaluation_value(
                        candidate
                    ),
                )
                for candidate in ordered
            ],
            resolution=(
                "unresolved_set_unprocessed"
            ),
        )

    # legacy_result has its own independent preservation rule.
    _resolve_legacy_result(
        entry,
        ordered,
    )

def apply_posted_title(
    entry: dict,
    raw_record: dict,
) -> None:
    value = raw_record.get("value")

    if not isinstance(value, str) or not value:
        raise ValueError(
            "posted.json item must be a non-empty string"
        )

    if value != entry.get("title"):
        raise ValueError(
            "Posted title {!r} does not match entry title {!r}".format(
                value,
                entry.get("title"),
            )
        )

    entry["posting"]["has_been_posted"] = True
    entry["posting"]["posted_at"] = []
    entry["posting"]["legacy_posted_without_timestamp"] = True

    add_legacy_source(
        entry,
        POSTED_FILE,
    )

def finalize_entry(entry: dict) -> dict:
    """Sort provenance/conflicts and return a JSON-safe canonical entry."""
    finalized = copy.deepcopy(entry)

    legacy_sources = finalized["migration"][
        "legacy_sources"
    ]

    unknown_sources = [
        filename
        for filename in legacy_sources
        if filename not in SOURCE_RANK
    ]

    if unknown_sources:
        raise ValueError(
            "Unknown migration legacy source(s): {}".format(
                sorted(set(unknown_sources))
            )
        )

    finalized["migration"]["legacy_sources"] = sorted(
        set(legacy_sources),
        key=lambda filename: SOURCE_RANK[filename],
    )

    conflicts = finalized["migration"][
        "conflicts"
    ]

    for conflict in conflicts:
        conflict["values"] = sorted(
            conflict["values"],
            key=_source_value_sort_key,
        )

    finalized["migration"]["conflicts"] = sorted(
        conflicts,
        key=_conflict_sort_key,
    )

    return finalized

def build_database_entries(
    sources: Dict[str, dict],
) -> List[dict]:
    title_index = build_title_index(sources)
    entries = []

    for title in sorted(title_index):
        entry = make_empty_database_entry(title)
        source_records = title_index[title]

        summary_records = source_records.get(
            SUMMARY_FILE,
            [],
        )
        entity_records = source_records.get(
            ENTITY_FILE,
            [],
        )
        quote_records = source_records.get(
            QUOTE_FILE,
            [],
        )
        failure_records = source_records.get(
            QUOTE_FAILURE_FILE,
            [],
        )
        result_records = source_records.get(
            RESULT_FILE,
            [],
        )
        processed_records = source_records.get(
            PROCESSED_FILE,
            [],
        )
        posted_records = source_records.get(
            POSTED_FILE,
            [],
        )

        merge_summary_records(
            entry,
            summary_records,
        )

        merge_entity_records(
            entry,
            entity_records,
        )

        merge_quote_records(
            entry,
            quote_records,
        )

        failure = select_newest_quote_failure(
            failure_records,
            entry,
        )
        entry["quotes"]["failure"] = failure

        reconcile_quotes_and_failure(entry)

        evaluation_candidates = []

        for raw_record in result_records:
            candidate = normalize_historical_evaluation(
                raw_record,
                RESULT_FILE,
            )
            evaluation_candidates.append(candidate)

            merge_embedded_legacy_facts(
                entry,
                raw_record,
                RESULT_FILE,
                sources[RESULT_FILE],
            )

        for raw_record in processed_records:
            candidate = normalize_historical_evaluation(
                raw_record,
                PROCESSED_FILE,
            )
            evaluation_candidates.append(candidate)

            merge_embedded_legacy_facts(
                entry,
                raw_record,
                PROCESSED_FILE,
                sources[PROCESSED_FILE],
            )

        apply_historical_evaluation(
            entry,
            evaluation_candidates,
        )

        for raw_record in posted_records:
            apply_posted_title(
                entry,
                raw_record,
            )

        finalized = finalize_entry(entry)

        entry_errors = validate_database_entry(
            finalized
        )

        if entry_errors:
            raise ValueError(
                "Invalid canonical entry for {!r}: {}".format(
                    title,
                    "; ".join(entry_errors),
                )
            )

        entries.append(finalized)

    return entries

def validate_migration_dataset(
    entries: List[dict],
    sources: Dict[str, dict],
    audit_counts: dict,
    audit_conflicts: dict,
) -> List[str]:
    errors = []

    errors.extend(
        validate_database_dataset(entries)
    )

    entry_titles = [
        entry.get("title")
        for entry in entries
        if isinstance(entry, dict)
    ]

    entry_title_set = set(entry_titles)

    title_index = build_title_index(sources)
    expected_titles = set(title_index)

    missing_titles = sorted(
        expected_titles - entry_title_set
    )

    extra_titles = sorted(
        entry_title_set - expected_titles
    )

    if missing_titles:
        errors.append(
            "Canonical dataset is missing source titles: {}".format(
                missing_titles
            )
        )

    if extra_titles:
        errors.append(
            "Canonical dataset contains unexpected titles: {}".format(
                extra_titles
            )
        )

    posted_titles = {
        title
        for title, source_map in title_index.items()
        if POSTED_FILE in source_map
    }

    for title in sorted(posted_titles):
        if title not in entry_title_set:
            errors.append(
                "Posted title missing from canonical dataset: {!r}".format(
                    title
                )
            )
            continue

        matching_entry = next(
            entry
            for entry in entries
            if entry["title"] == title
        )

        if (
            matching_entry["posting"][
                "has_been_posted"
            ]
            is not True
        ):
            errors.append(
                "Posted title not marked posted: {!r}".format(
                    title
                )
            )

    entries_by_title = {
        entry["title"]: entry
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
    }

    evaluation_conflicts = audit_conflicts.get(
        "accepted_rejected_conflicts",
        [],
    )

    for conflict in evaluation_conflicts:
        title = conflict.get("title")

        entry = entries_by_title.get(title)

        if entry is None:
            errors.append(
                "Evaluation-conflict title missing from canonical dataset: {!r}".format(
                    title
                )
            )
            continue

        if (
            entry["evaluation"]["status"]
            != "unprocessed"
        ):
            errors.append(
                "Evaluation-conflict title must be unprocessed: {!r}".format(
                    title
                )
            )

        has_status_conflict = any(
            migration_conflict.get("field")
            == "evaluation.status"
            for migration_conflict
            in entry["migration"]["conflicts"]
        )

        if not has_status_conflict:
            errors.append(
                "Evaluation-conflict title is missing evaluation.status conflict: {!r}".format(
                    title
                )
            )

    return errors

def build_migration_report(
    *,
    source_fingerprints: Dict[str, FileFingerprint],
    audit_counts: dict,
    audit_validation: dict,
    audit_conflicts: dict,
    entries: List[dict],
    validation_errors: List[str],
) -> dict:
    return {
        "source_files": {
            filename: {
                "byte_size": fingerprint.byte_size,
                "sha256": fingerprint.sha256,
            }
            for filename, fingerprint in sorted(
                source_fingerprints.items()
            )
        },
        "audit": {
            "counts": audit_counts,
            "validation": audit_validation,
            "conflicts": audit_conflicts,
        },
        "canonical": {
            "entry_count": len(entries),
            "validation_errors": list(
                validation_errors
            ),
        },
    }

def run_dry_migration(
    backup_dir: Path,
    live_source_dir: Path,
    manifest_path: Path,
) -> Tuple[List[dict], dict]:
    baseline = verify_live_and_backup_baseline(
        live_source_dir=live_source_dir,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        expected_filenames=SOURCE_ORDER,
    )

    if not baseline["ok"]:
        raise ValueError(
            "Migration baseline verification failed: {}".format(
                baseline
            )
        )

    sources = read_legacy_sources(backup_dir)

    audit_counts = count_legacy_records(sources)
    audit_validation = validate_legacy_files(sources)
    audit_conflicts = report_conflicts(sources)

    if audit_counts["total_malformed_json"] != 0:
        raise ValueError(
            "Legacy source contains malformed JSON"
        )

    entries = build_database_entries(sources)

    validation_errors = validate_migration_dataset(
        entries=entries,
        sources=sources,
        audit_counts=audit_counts,
        audit_conflicts=audit_conflicts,
    )

    report = build_migration_report(
        source_fingerprints=baseline["expected"],
        audit_counts=audit_counts,
        audit_validation=audit_validation,
        audit_conflicts=audit_conflicts,
        entries=entries,
        validation_errors=validation_errors,
    )

    if validation_errors:
        raise ValueError(
            "Migration dataset validation failed: {}".format(
                validation_errors
            )
        )

    return entries, report

def print_migration_report(report: dict) -> None:
    print(
        json.dumps(
            report,
            indent=2,
            sort_keys=True,
        )
    )

def build_argument_parser():
    parser = argparse.ArgumentParser(
        description=(
            "Migrate legacy scraper data to the canonical database format."
        )
    )

    parser.add_argument(
        "--source-dir",
        required=True,
        type=Path,
        help="Verified read-only legacy backup directory.",
    )

    parser.add_argument(
        "--live-source-dir",
        required=True,
        type=Path,
        help="Current live legacy source directory used for baseline verification.",
    )

    parser.add_argument(
        "--manifest",
        required=True,
        type=Path,
        help="Approved SHA-256 backup manifest.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        help=(
            "Explicit database.jsonl output path. "
            "Required with --write."
        ),
    )

    mode = parser.add_mutually_exclusive_group(
        required=True
    )

    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Read, merge, validate, and report without writing database.jsonl.",
    )

    mode.add_argument(
        "--write",
        action="store_true",
        help="Write the canonical database after all safety gates pass.",
    )

    return parser

def ensure_output_path_is_safe(
    output_path: Path,
) -> None:
    """
    Allow absent or zero-byte output.
    Refuse non-empty existing database.jsonl.
    """
    output_path = Path(output_path)

    if output_path.name != DATABASE_FILE:
        raise ValueError(
            "Output filename must be {!r}".format(
                DATABASE_FILE
            )
        )

    parent = output_path.parent

    if not parent.exists():
        raise ValueError(
            "Output directory does not exist: {}".format(
                parent
            )
        )

    if not parent.is_dir():
        raise ValueError(
            "Output parent is not a directory: {}".format(
                parent
            )
        )

    if output_path.is_symlink():
        raise ValueError(
            "Refusing symbolic-link output path: {}".format(
                output_path
            )
        )

    if not output_path.exists():
        return

    if not output_path.is_file():
        raise ValueError(
            "Output path is not a regular file: {}".format(
                output_path
            )
        )

    if output_path.stat().st_size != 0:
        raise ValueError(
            "Refusing to overwrite non-empty database: {}".format(
                output_path
            )
        )

def validate_serialized_database(
    path: Path,
    expected_titles: Set[str],
) -> List[str]:
    errors = []
    entries = []

    try:
        with Path(path).open(
            "r",
            encoding="utf-8",
        ) as file_handle:
            for line_number, raw_line in enumerate(
                file_handle,
                start=1,
            ):
                line = raw_line.rstrip("\n")

                if not line:
                    errors.append(
                        "Blank JSONL line at line {}".format(
                            line_number
                        )
                    )
                    continue

                try:
                    value = json.loads(line)
                except json.JSONDecodeError as error:
                    errors.append(
                        "Malformed JSON at line {}: {}".format(
                            line_number,
                            error.msg,
                        )
                    )
                    continue

                if not isinstance(value, dict):
                    errors.append(
                        "Database entry at line {} must be an object".format(
                            line_number
                        )
                    )
                    continue

                entry_errors = validate_database_entry(
                    value
                )

                for error in entry_errors:
                    errors.append(
                        "line {}: {}".format(
                            line_number,
                            error,
                        )
                    )

                entries.append(value)

    except OSError as error:
        return [
            "Could not read serialized database: {}".format(
                error
            )
        ]

    # This already checks canonical-entry validity and duplicate titles.
    dataset_errors = validate_database_dataset(
        entries
    )

    for error in dataset_errors:
        errors.append(
            "dataset: {}".format(error)
        )

    actual_titles = {
        entry["title"]
        for entry in entries
        if isinstance(entry.get("title"), str)
    }

    missing_titles = sorted(
        expected_titles - actual_titles
    )

    unexpected_titles = sorted(
        actual_titles - expected_titles
    )

    if missing_titles:
        errors.append(
            "Serialized database is missing expected titles: {}".format(
                missing_titles
            )
        )

    if unexpected_titles:
        errors.append(
            "Serialized database contains unexpected titles: {}".format(
                unexpected_titles
            )
        )

    if len(entries) != len(expected_titles):
        errors.append(
            "Serialized database contains {} entries; expected {}".format(
                len(entries),
                len(expected_titles),
            )
        )

    return errors

def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def _sha256_file(path: Path) -> str:
    sha256 = hashlib.sha256()

    with Path(path).open("rb") as file_handle:
        while True:
            chunk = file_handle.read(
                1024 * 1024
            )

            if not chunk:
                break

            sha256.update(chunk)

    return sha256.hexdigest()

def _fsync_directory(path: Path) -> None:
    directory_fd = os.open(
        str(path),
        os.O_RDONLY,
    )

    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)

def write_database_atomically(
    output_path: Path,
    serialized_bytes: bytes,
    expected_titles: Set[str],
) -> str:
    """Return output SHA-256 after re-read validation."""
    output_path = Path(output_path)

    ensure_output_path_is_safe(
        output_path
    )

    output_dir = output_path.parent

    temp_path = None
    replaced = False

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".database-migration-",
            suffix=".tmp",
            dir=str(output_dir),
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)

            temp_file.write(
                serialized_bytes
            )

            temp_file.flush()
            os.fsync(
                temp_file.fileno()
            )

        validation_errors = (
            validate_serialized_database(
                temp_path,
                expected_titles,
            )
        )

        if validation_errors:
            raise ValueError(
                "Temporary database validation failed: {}".format(
                    "; ".join(validation_errors)
                )
            )

        # Re-check the final path immediately before committing.
        # This is useful if something changed after the initial check.
        ensure_output_path_is_safe(
            output_path
        )

        os.replace(
            str(temp_path),
            str(output_path),
        )

        replaced = True
        temp_path = None

        _fsync_directory(
            output_dir
        )

        final_bytes = output_path.read_bytes()

        if final_bytes != serialized_bytes:
            raise RuntimeError(
                "Final database bytes differ from serialized migration output"
            )

        post_write_errors = (
            validate_serialized_database(
                output_path,
                expected_titles,
            )
        )

        if post_write_errors:
            raise RuntimeError(
                "Post-write database validation failed: {}".format(
                    "; ".join(post_write_errors)
                )
            )

        return _sha256_bytes(
            final_bytes
        )

    finally:
        if (
            not replaced
            and temp_path is not None
            and temp_path.exists()
        ):
            try:
                temp_path.unlink()
            except OSError:
                pass

def ensure_output_not_inside_backup(
    output_path: Path,
    backup_dir: Path,
) -> None:
    output_resolved = output_path.resolve()
    backup_resolved = backup_dir.resolve()

    try:
        output_resolved.relative_to(
            backup_resolved
        )
    except ValueError:
        return

    raise ValueError(
        "Output path inside the selected backup directory is forbidden"
    )

import copy


def run_write_migration(
    backup_dir: Path,
    live_source_dir: Path,
    manifest_path: Path,
    output_path: Path,
) -> dict:
    ensure_output_not_inside_backup(
        output_path,
        backup_dir,
    )

    entries, dry_report = run_dry_migration(
        backup_dir=backup_dir,
        live_source_dir=live_source_dir,
        manifest_path=manifest_path,
    )

    serialized_bytes = serialize_database_entries(
        entries
    )

    expected_titles = {
        entry["title"]
        for entry in entries
    }

    output_sha256 = write_database_atomically(
        output_path=output_path,
        serialized_bytes=serialized_bytes,
        expected_titles=expected_titles,
    )

    postflight = verify_live_and_backup_baseline(
        live_source_dir=live_source_dir,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        expected_filenames=SOURCE_ORDER,
    )

    if not postflight["ok"]:
        raise RuntimeError(
            "Critical post-write baseline verification failure: {}".format(
                postflight
            )
        )

    report = copy.deepcopy(dry_report)

    report["write"] = {
        "output_path": str(output_path),
        "sha256": output_sha256,
        "validation_errors": [],
        "post_write_baseline": {
            "ok": True,
            "live_mismatches": list(
                postflight["live_mismatches"]
            ),
            "backup_mismatches": list(
                postflight["backup_mismatches"]
            ),
        },
    }

    return report

def main(argv=None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)

    if args.write:
        if args.output is None:
            parser.error(
                "--write requires --output"
            )

        if args.dry_run and args.output is not None:
            parser.error(
                "--output may only be used with --write"
            )

        try:
            report = run_write_migration(
                backup_dir=args.source_dir,
                live_source_dir=args.live_source_dir,
                manifest_path=args.manifest,
                output_path=args.output,
            )
        except (
            OSError,
            ValueError,
            RuntimeError,
        ) as error:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "error": str(error),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 1

        print_migration_report(report)
        return 0

    try:
        _, report = run_dry_migration(
            backup_dir=args.source_dir,
            live_source_dir=args.live_source_dir,
            manifest_path=args.manifest,
        )
    except (OSError, ValueError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(error),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return 1

    print_migration_report(report)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
