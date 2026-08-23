import os
import json
import copy
import hashlib
import tempfile
import re
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import List, Optional
from database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
    validate_database_dataset,
    validate_database_entry,
)
from utils import get_data_path
from config import DATABASE_BACKUP_FOLDER, OPERATIONAL_BACKUP_RETENTION_DAYS


BACKUP_METADATA_SCHEMA_VERSION = 1


@dataclass
class DatabaseBackupResult:
    path: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    created_at: Optional[str] = None
    label: Optional[str] = None
    kind: Optional[str] = None
    preserve: bool = False
    error_reason: Optional[str] = None
    pruned_paths: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def created(self):
        return self.error_reason is None and self.path is not None

    def as_report(self, attempted=True, retention_days=None):
        return {
            "attempted": attempted,
            "created": self.created,
            "path": self.path,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "label": self.label,
            "kind": self.kind,
            "preserve": self.preserve,
            "retention_days": retention_days,
            "pruned_count": len(self.pruned_paths),
            "pruned_paths": list(self.pruned_paths),
            "warnings": list(self.warnings),
            "error": self.error_reason,
        }


def default_database_backup_path(now=None):
    """Return the standard destination for one explicit canonical snapshot."""
    if now is None:
        now = datetime.now(timezone.utc)
    return Path(DATABASE_BACKUP_FOLDER) / "database-{}.jsonl".format(
        now.strftime("%Y-%m-%dT%H-%M-%SZ")
    )


def _utc_now(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _timestamp_text(now):
    return _utc_now(now).strftime("%Y-%m-%dT%H-%M-%SZ")


def _iso_timestamp(now):
    return _utc_now(now).strftime("%Y-%m-%dT%H:%M:%SZ")


def sanitize_backup_label(label):
    if not isinstance(label, str):
        raise ValueError("Backup label must be a string")
    if ".." in label or "/" in label or "\\" in label:
        raise ValueError("Backup label must not contain path components")
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip())
    normalized = normalized.strip(".-")
    if not normalized:
        raise ValueError("Backup label must contain a readable character")
    return normalized


def _backup_metadata_path(backup_path):
    backup_path = Path(backup_path)
    return backup_path.with_name(backup_path.name + ".meta.json")


def _hash_file(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _write_json_atomically(destination, value):
    destination = Path(destination)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".database-backup-meta-",
            suffix=".tmp",
            dir=str(destination.parent),
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if destination.exists() or destination.is_symlink():
            raise ValueError("Refusing to overwrite backup metadata: {}".format(destination))
        os.replace(str(temporary_path), str(destination))
        temporary_path = None
        _fsync_directory(destination.parent)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def load_database(filename, data_folder):
    """Load and validate a canonical JSONL database without modifying it."""
    path = get_data_path(filename, data_folder)
    entries = []
    database = {}
    errors = []

    with open(path, "r", encoding="utf-8") as file_handle:
        for line_number, raw_line in enumerate(file_handle, start=1):
            line = raw_line.rstrip("\n")

            if not line:
                errors.append(
                    "Blank JSONL line in {} at line {}".format(
                        filename,
                        line_number,
                    )
                )
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError as error:
                errors.append(
                    "Malformed JSON in {} at line {}: {}".format(
                        filename,
                        line_number,
                        error.msg,
                    )
                )
                continue

            if not isinstance(entry, dict):
                errors.append(
                    "Database record in {} at line {} must be a JSON object".format(
                        filename,
                        line_number,
                    )
                )
                continue

            entry_errors = validate_database_entry(entry)
            errors.extend(
                "{} line {}: {}".format(
                    filename,
                    line_number,
                    error,
                )
                for error in entry_errors
            )

            title = entry.get("title")
            if isinstance(title, str):
                if title in database:
                    errors.append(
                        "Duplicate database title: {!r}".format(title)
                    )
                else:
                    database[title] = entry

            entries.append(entry)

    errors.extend(validate_database_dataset(entries))

    if errors:
        raise ValueError("\n".join(errors))

    return database


def get_entry(database, title):
    """Return a detached canonical entry for an exact title, or None."""
    entry = database.get(title)

    if entry is None:
        return None

    return copy.deepcopy(entry)


def _validate_database_mapping(database):
    if not isinstance(database, dict):
        raise ValueError("database must be a dictionary keyed by title")

    errors = []
    entries = list(database.values())

    for title, entry in database.items():
        if not isinstance(title, str) or not title:
            errors.append(
                "Database mapping key must be a non-empty title string"
            )
            continue

        if not isinstance(entry, dict) or entry.get("title") != title:
            errors.append(
                "Database mapping key {!r} does not match entry title".format(
                    title
                )
            )

    errors.extend(validate_database_dataset(entries))

    if errors:
        raise ValueError("\n".join(errors))

    return set(database)


def _validate_serialized_database_file(path, expected_titles):
    loaded_database = load_database(
        Path(path).name,
        str(Path(path).parent),
    )
    actual_titles = set(loaded_database)

    if actual_titles != expected_titles:
        missing_titles = sorted(expected_titles - actual_titles)
        unexpected_titles = sorted(actual_titles - expected_titles)
        raise ValueError(
            "Serialized database titles differ; missing={!r}, unexpected={!r}".format(
                missing_titles,
                unexpected_titles,
            )
        )


def _fsync_directory(directory):
    directory_fd = os.open(str(directory), os.O_RDONLY)

    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _rewrite_database_unlocked(database, filename, data_folder):
    """Validate and atomically replace one canonical database snapshot."""
    expected_titles = _validate_database_mapping(database)
    serialized_bytes = serialize_database_entries(
        list(database.values())
    )
    output_path = Path(get_data_path(filename, data_folder))
    output_directory = output_path.parent

    if not output_directory.is_dir():
        raise ValueError(
            "Database output directory does not exist: {}".format(
                output_directory
            )
        )

    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".database-rewrite-",
            suffix=".tmp",
            dir=str(output_directory),
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(serialized_bytes)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        _validate_serialized_database_file(
            temp_path,
            expected_titles,
        )

        os.replace(str(temp_path), str(output_path))
        temp_path = None
        _fsync_directory(output_directory)

        final_bytes = output_path.read_bytes()

        if final_bytes != serialized_bytes:
            raise RuntimeError(
                "Final database bytes differ from serialized database"
            )

        _validate_serialized_database_file(
            output_path,
            expected_titles,
        )

        return hashlib.sha256(final_bytes).hexdigest()
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def rewrite_database(
    database,
    filename,
    data_folder,
    persistence_lock,
):
    """Atomically replace a canonical database snapshot under one lock."""
    with persistence_lock:
        return _rewrite_database_unlocked(
            database,
            filename,
            data_folder,
        )


def upsert_entry(
    database,
    entry,
    filename,
    data_folder,
    persistence_lock,
):
    """Persist one canonical entry before replacing the shared snapshot."""
    entry_errors = validate_database_entry(entry)

    if entry_errors:
        raise ValueError("\n".join(entry_errors))

    with persistence_lock:
        candidate_database = copy.deepcopy(database)
        candidate_entry = copy.deepcopy(entry)
        candidate_database[candidate_entry["title"]] = candidate_entry

        final_hash = _rewrite_database_unlocked(
            candidate_database,
            filename,
            data_folder,
        )

        database[candidate_entry["title"]] = copy.deepcopy(candidate_entry)

        return final_hash


def update_database_entry(
    database,
    title,
    update_callback,
    filename,
    data_folder,
    persistence_lock,
):
    """Atomically apply one in-memory canonical-entry update and persist it."""
    if not isinstance(title, str) or not title:
        raise ValueError("title must be a non-empty string")

    if not callable(update_callback):
        raise TypeError("update_callback must be callable")

    with persistence_lock:
        latest_entry = database.get(title)

        if latest_entry is None:
            latest_entry = make_empty_database_entry(title)

        updated_entry = copy.deepcopy(latest_entry)
        update_callback(updated_entry)

        if updated_entry.get("title") != title:
            raise ValueError("update_callback must not change entry title")

        entry_errors = validate_database_entry(updated_entry)

        if entry_errors:
            raise ValueError("\n".join(entry_errors))

        candidate_database = copy.deepcopy(database)
        candidate_database[title] = updated_entry

        final_hash = _rewrite_database_unlocked(
            candidate_database,
            filename,
            data_folder,
        )

        database[title] = copy.deepcopy(updated_entry)

        return final_hash


def _backup_database_unlocked(filename, data_folder, backup_path):
    """Copy canonical bytes atomically without changing the live file."""
    backup_path = Path(backup_path)
    backup_directory = backup_path.parent
    source_path = Path(get_data_path(filename, data_folder))

    if backup_path.exists() or backup_path.is_symlink():
        raise ValueError(
            "Refusing to overwrite existing backup: {}".format(
                backup_path
            )
        )

    if not backup_directory.is_dir():
        raise ValueError(
            "Backup output directory does not exist: {}".format(
                backup_directory
            )
        )

    if not source_path.is_file():
        raise ValueError("Canonical database is not a regular file: {}".format(source_path))

    source_sha256, source_size = _hash_file(source_path)
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".database-backup-",
            suffix=".tmp",
            dir=str(backup_directory),
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            with source_path.open("rb") as source_handle:
                while True:
                    chunk = source_handle.read(1024 * 1024)
                    if not chunk:
                        break
                    temp_file.write(chunk)
            temp_file.flush()
            os.fsync(temp_file.fileno())

        if backup_path.exists() or backup_path.is_symlink():
            raise ValueError("Refusing to overwrite existing backup: {}".format(backup_path))
        os.replace(str(temp_path), str(backup_path))
        temp_path = None
        _fsync_directory(backup_directory)

        final_sha256, final_size = _hash_file(backup_path)
        source_final_sha256, source_final_size = _hash_file(source_path)
        if (
            source_sha256 != final_sha256
            or source_size != final_size
            or source_sha256 != source_final_sha256
            or source_size != source_final_size
        ):
            raise RuntimeError("Backup bytes differ from canonical database")

        return final_sha256, final_size
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def backup_database(
    filename,
    data_folder,
    backup_path,
    persistence_lock,
):
    """Create an explicit immutable-style canonical snapshot under one lock."""
    with persistence_lock:
        result = _backup_database_unlocked(
            filename,
            data_folder,
            backup_path,
        )
        return result[0] if isinstance(result, tuple) else result


def backup_database_to_default_location(
    filename,
    data_folder,
    persistence_lock,
    now=None,
):
    """Create an explicit canonical snapshot in backups/database/.

    This helper is intentionally not invoked by normal runtime flows.
    """
    return backup_database(
        filename,
        data_folder,
        default_database_backup_path(now),
        persistence_lock,
    )


def _backup_destination(backup_folder, label, now):
    directory = Path(backup_folder)
    timestamp = _timestamp_text(now)
    base = "database-{}-{}".format(sanitize_backup_label(label), timestamp)
    candidate = directory / (base + ".jsonl")
    suffix = 2
    while candidate.exists() or _backup_metadata_path(candidate).exists():
        candidate = directory / "{}-{}.jsonl".format(base, suffix)
        suffix += 1
    return candidate


def prune_operational_backups(backup_folder, retention_days, now=None, exclude_paths=()):
    """Prune only positively identified, expired new-system backups."""
    directory = Path(backup_folder)
    now = _utc_now(now)
    excluded = {str(Path(path)) for path in exclude_paths}
    pruned_paths = []
    warnings = []
    if not directory.is_dir():
        return pruned_paths, ["Backup directory does not exist: {}".format(directory)]

    for metadata_path in sorted(directory.glob("database-*.jsonl.meta.json")):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            backup_path = metadata_path.with_name(metadata_path.name[:-10])
            if str(backup_path) in excluded:
                continue
            if (
                metadata.get("schema_version") != BACKUP_METADATA_SCHEMA_VERSION
                or metadata.get("kind") != "operational"
                or metadata.get("preserve") is not False
                or not isinstance(metadata.get("created_at"), str)
                or not backup_path.is_file()
            ):
                continue
            created_at = datetime.strptime(
                metadata["created_at"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
            if (now - created_at).days < retention_days:
                continue
            backup_path.unlink()
            metadata_path.unlink()
            pruned_paths.append(str(backup_path))
        except Exception as error:
            warnings.append("Could not prune {}: {}".format(metadata_path, error))
    return pruned_paths, warnings


def create_database_backup(
    data_folder,
    backup_folder=DATABASE_BACKUP_FOLDER,
    label="manual",
    retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
    preserve=True,
    kind="manual",
    persistence_lock=None,
    filename="database.jsonl",
    now=None,
):
    """Create one verified, metadata-classified canonical database snapshot."""
    now = _utc_now(now)
    result = DatabaseBackupResult(
        created_at=_iso_timestamp(now),
        kind=kind,
        preserve=bool(preserve),
    )
    try:
        result.label = sanitize_backup_label(label)
    except ValueError as error:
        result.error_reason = str(error)
        return result
    if kind not in ("manual", "operational"):
        result.error_reason = "Unknown backup kind: {}".format(kind)
        return result
    if kind == "manual" and not preserve:
        result.error_reason = "Manual backups must be preserved"
        return result

    backup_path = None
    try:
        backup_path = _backup_destination(backup_folder, result.label, now)
        if persistence_lock is None:
            sha256, size_bytes = _backup_database_unlocked(
                filename, data_folder, backup_path
            )
        else:
            with persistence_lock:
                sha256, size_bytes = _backup_database_unlocked(
                    filename, data_folder, backup_path
                )
        metadata = {
            "schema_version": BACKUP_METADATA_SCHEMA_VERSION,
            "created_at": result.created_at,
            "label": result.label,
            "kind": kind,
            "preserve": bool(preserve),
            "sha256": sha256,
            "size_bytes": size_bytes,
        }
        _write_json_atomically(_backup_metadata_path(backup_path), metadata)
        result.path = str(backup_path)
        result.sha256 = sha256
        result.size_bytes = size_bytes
        if kind == "operational":
            result.pruned_paths, result.warnings = prune_operational_backups(
                backup_folder,
                retention_days,
                now=now,
                exclude_paths=(backup_path,),
            )
    except (OSError, ValueError, RuntimeError) as error:
        result.error_reason = str(error)
        if backup_path is not None and backup_path.exists() and result.path is None:
            try:
                backup_path.unlink()
            except OSError:
                pass
    return result

def load_json(filename, data_folder):
    path = get_data_path(filename, data_folder)

    if not os.path.exists(path):
        return []

    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return []

def save_json(filename, posted_cache, data_folder):
    path = get_data_path(filename, data_folder)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(posted_cache, f, ensure_ascii=False, indent=2)

def load_posted_titles(filename, data_folder):
    posted_titles = load_json(filename, data_folder)

    if not isinstance(posted_titles, list):
        raise ValueError(
            f"{filename} must contain a JSON array of title strings."
        )

    for index, title in enumerate(posted_titles):
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"{filename} item {index} must be a non-empty title string."
            )

    return posted_titles

def save_posted_titles(filename, posted_titles, data_folder):
    if not isinstance(posted_titles, list):
        raise ValueError("posted_titles must be a list of strings.")

    for index, title in enumerate(posted_titles):
        if not isinstance(title, str) or not title.strip():
            raise ValueError(
                f"posted_titles item {index} must be a non-empty string."
            )

    save_json(filename, posted_titles, data_folder)

def load_jsonl(filename, data_folder):
    path = get_data_path(filename, data_folder)

    if not os.path.exists(path):
        return {}

    data = {}

    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            entry = json.loads(line)
            data[entry["title"]] = entry

    return data

def load_summary_cache(filename, data_folder):
    records = load_jsonl(filename, data_folder)

    return {
        title: record["summary"]
        for title, record in records.items()
    }


def load_entity_cache(filename, data_folder):
    return load_jsonl(filename, data_folder)

def load_quote_cache(filename, data_folder):
    records = load_jsonl(filename, data_folder)

    return {
        title: record["quotes"]
        for title, record in records.items()
    }

def load_quote_failure_cache(filename, data_folder):
    return load_jsonl(filename, data_folder)

def load_result_cache(filename, data_folder):
    return load_jsonl(filename, data_folder)

def load_processed_cache(filename, data_folder):
    return load_jsonl(filename, data_folder)

def _append_jsonl_unlocked(filename, entry, data_folder):
    path = get_data_path(filename, data_folder)

    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

def persist_jsonl_cache_entry(
    cache,
    title,
    cache_value,
    filename,
    file_entry,
    data_folder,
    persistence_lock,
):
    """
    Append file_entry first, then update the matching in-memory cache.

    If the file append raises OSError, the cache is left unchanged.
    """
    with persistence_lock:
        _append_jsonl_unlocked(
            filename,
            file_entry,
            data_folder,
        )

        cache[title] = cache_value

def persist_evaluation_entry(
    entry,
    result_cache,
    processed_cache,
    stats,
    stats_lock,
    persistence_lock,
    result_filename,
    processed_filename,
    data_folder,
):
    title = entry["title"]
    status = entry["status"]

    persisted_entry = entry.copy()

    if status == "accepted":
        persist_jsonl_cache_entry(
            cache=result_cache,
            title=title,
            cache_value=persisted_entry,
            filename=result_filename,
            file_entry=persisted_entry,
            data_folder=data_folder,
            persistence_lock=persistence_lock,
        )

        with stats_lock:
            stats["new_accepted"] += 1

        return

    if status == "rejected":
        persist_jsonl_cache_entry(
            cache=processed_cache,
            title=title,
            cache_value=persisted_entry,
            filename=processed_filename,
            file_entry=persisted_entry,
            data_folder=data_folder,
            persistence_lock=persistence_lock,
        )

        with stats_lock:
            stats["new_rejected"] += 1

        return

    raise ValueError(
        "Unexpected evaluation status: {!r}".format(status)
    )
