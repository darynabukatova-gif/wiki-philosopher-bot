import json
import threading
import copy
import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest

import cache
import database_schema
from migrate_database import make_empty_database_entry


def database_path(tmp_path):
    return tmp_path / "database.jsonl"


def write_entries(path, entries):
    path.write_bytes(
        database_schema.serialize_database_entries(entries)
    )


def valid_entries():
    return [
        make_empty_database_entry("Zeno"),
        make_empty_database_entry("Ada Lovelace"),
    ]


def test_load_database_returns_valid_title_mapping(tmp_path):
    write_entries(database_path(tmp_path), valid_entries())

    database = cache.load_database(
        "database.jsonl",
        str(tmp_path),
    )

    assert list(database) == ["Ada Lovelace", "Zeno"]
    assert database["Ada Lovelace"] == make_empty_database_entry(
        "Ada Lovelace"
    )


def test_load_database_rejects_malformed_jsonl(tmp_path):
    path = database_path(tmp_path)
    path.write_bytes(b'{"title":\n')

    with pytest.raises(ValueError, match="Malformed JSON"):
        cache.load_database("database.jsonl", str(tmp_path))


def test_load_database_rejects_duplicate_titles(tmp_path):
    entry = make_empty_database_entry("Ada Lovelace")
    write_entries(database_path(tmp_path), [entry, entry])

    with pytest.raises(ValueError, match="Duplicate database title"):
        cache.load_database("database.jsonl", str(tmp_path))


def test_load_database_rejects_schema_invalid_entry(tmp_path):
    path = database_path(tmp_path)
    path.write_text(
        json.dumps({"title": "Ada Lovelace"}) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Missing required top-level field"):
        cache.load_database("database.jsonl", str(tmp_path))


def test_get_entry_returns_exact_title_copy():
    source_entry = make_empty_database_entry("Ada Lovelace")
    database = {"Ada Lovelace": source_entry}

    entry = cache.get_entry(database, "Ada Lovelace")

    assert entry == source_entry
    assert entry is not source_entry
    entry["summary"]["text"] = "changed only in returned copy"
    assert database["Ada Lovelace"]["summary"]["text"] is None


def test_get_entry_returns_none_for_missing_title():
    database = {
        "Ada Lovelace": make_empty_database_entry("Ada Lovelace")
    }

    assert cache.get_entry(database, "Zeno") is None


def test_rewrite_database_is_deterministic_and_replaces_snapshot(tmp_path):
    path = database_path(tmp_path)
    path.write_bytes(b"old canonical snapshot\n")
    database = {
        entry["title"]: entry
        for entry in valid_entries()
    }

    first_hash = cache.rewrite_database(
        database,
        "database.jsonl",
        str(tmp_path),
        persistence_lock=threading.Lock(),
    )
    first_bytes = path.read_bytes()
    second_hash = cache.rewrite_database(
        database,
        "database.jsonl",
        str(tmp_path),
        persistence_lock=threading.Lock(),
    )

    assert first_bytes == database_schema.serialize_database_entries(
        list(database.values())
    )
    assert path.read_bytes() == first_bytes
    assert first_hash == second_hash


def test_canonical_serializer_uses_explicit_evaluation_field_order():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"] = {
        "legacy_result": {"z": 1, "a": 2},
        "processed_at": 123,
        "reasons": ["reason"],
        "content_confidence": 2,
        "human_confidence": 3,
        "philosopher_confidence": 4,
        "algorithm_version": 2,
        "status": "accepted",
    }

    serialized = database_schema.serialize_database_entries([entry])
    parsed = json.loads(
        serialized.decode("utf-8"),
        object_pairs_hook=lambda pairs: pairs,
    )
    evaluation_pairs = dict(parsed)["evaluation"]

    assert [key for key, _ in evaluation_pairs] == [
        "status",
        "algorithm_version",
        "philosopher_confidence",
        "human_confidence",
        "content_confidence",
        "reasons",
        "processed_at",
        "legacy_result",
    ]
    assert serialized == database_schema.serialize_database_entries([entry])


def test_rewrite_database_leaves_existing_snapshot_on_validation_failure(
    tmp_path,
):
    path = database_path(tmp_path)
    original = database_schema.serialize_database_entries(
        [make_empty_database_entry("Ada Lovelace")]
    )
    path.write_bytes(original)
    invalid_database = {
        "Ada Lovelace": {"title": "Ada Lovelace"}
    }

    with pytest.raises(ValueError):
        cache.rewrite_database(
            invalid_database,
            "database.jsonl",
            str(tmp_path),
            persistence_lock=threading.Lock(),
        )

    assert path.read_bytes() == original


def test_repository_operations_leave_legacy_sources_unchanged(tmp_path):
    legacy_files = {
        "summaries.jsonl": b'{"title":"Ada","summary":"text"}\n',
        "entities.jsonl": b'{"title":"Ada","valid":true}\n',
        "quotes.jsonl": b'{"title":"Ada","quotes":[]}\n',
        "quote_failures.jsonl": b'{"title":"Ada","reason":"404"}\n',
        "results.jsonl": b'{"title":"Ada","accepted":true}\n',
        "processed.jsonl": b'{"title":"Ada","accepted":false}\n',
        "posted.json": b'["Ada"]\n',
    }

    for filename, contents in legacy_files.items():
        (tmp_path / filename).write_bytes(contents)

    before = {
        filename: (tmp_path / filename).read_bytes()
        for filename in legacy_files
    }
    database = {
        "Ada Lovelace": make_empty_database_entry("Ada Lovelace")
    }

    cache.rewrite_database(
        database,
        "database.jsonl",
        str(tmp_path),
        persistence_lock=threading.Lock(),
    )

    assert {
        filename: (tmp_path / filename).read_bytes()
        for filename in legacy_files
    } == before


def test_upsert_entry_adds_new_title_after_successful_persistence(tmp_path):
    path = database_path(tmp_path)
    first_entry = make_empty_database_entry("Ada Lovelace")
    second_entry = make_empty_database_entry("Zeno")
    write_entries(path, [first_entry])
    database = {first_entry["title"]: first_entry}

    final_hash = cache.upsert_entry(
        database,
        second_entry,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )

    assert set(cache.load_database("database.jsonl", str(tmp_path))) == {
        "Ada Lovelace",
        "Zeno",
    }
    assert set(database) == {"Ada Lovelace", "Zeno"}
    assert final_hash == hashlib.sha256(path.read_bytes()).hexdigest()


def test_upsert_entry_replaces_existing_title_after_successful_persistence(
    tmp_path,
):
    path = database_path(tmp_path)
    original_entry = make_empty_database_entry("Ada Lovelace")
    replacement_entry = copy.deepcopy(original_entry)
    replacement_entry["summary"]["text"] = "Updated summary"
    write_entries(path, [original_entry])
    database = {original_entry["title"]: original_entry}

    cache.upsert_entry(
        database,
        replacement_entry,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )

    disk_database = cache.load_database("database.jsonl", str(tmp_path))
    assert list(disk_database) == ["Ada Lovelace"]
    assert disk_database["Ada Lovelace"]["summary"]["text"] == "Updated summary"
    assert database["Ada Lovelace"]["summary"]["text"] == "Updated summary"


def test_upsert_entry_keeps_memory_and_disk_unchanged_after_write_failure(
    tmp_path,
    monkeypatch,
):
    path = database_path(tmp_path)
    original_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [original_entry])
    database = {original_entry["title"]: copy.deepcopy(original_entry)}
    before_disk = path.read_bytes()
    before_memory = copy.deepcopy(database)

    def fail_rewrite(*args, **kwargs):
        raise OSError("simulated write failure")

    monkeypatch.setattr(
        cache,
        "_rewrite_database_unlocked",
        fail_rewrite,
    )

    with pytest.raises(OSError, match="simulated write failure"):
        cache.upsert_entry(
            database,
            make_empty_database_entry("Zeno"),
            "database.jsonl",
            str(tmp_path),
            threading.Lock(),
        )

    assert path.read_bytes() == before_disk
    assert database == before_memory


def test_upsert_entry_rejects_invalid_entry_without_mutating_state(tmp_path):
    path = database_path(tmp_path)
    original_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [original_entry])
    database = {original_entry["title"]: copy.deepcopy(original_entry)}
    before_disk = path.read_bytes()
    before_memory = copy.deepcopy(database)

    with pytest.raises(ValueError):
        cache.upsert_entry(
            database,
            {"title": "Zeno"},
            "database.jsonl",
            str(tmp_path),
            threading.Lock(),
        )

    assert path.read_bytes() == before_disk
    assert database == before_memory


def test_upsert_entry_does_not_alias_caller_entry(tmp_path):
    path = database_path(tmp_path)
    original_entry = make_empty_database_entry("Ada Lovelace")
    incoming_entry = make_empty_database_entry("Zeno")
    write_entries(path, [original_entry])
    database = {original_entry["title"]: original_entry}

    cache.upsert_entry(
        database,
        incoming_entry,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )
    incoming_entry["summary"]["text"] = "Caller-owned mutation"

    assert database["Zeno"]["summary"]["text"] is None


def test_upsert_entry_uses_existing_persistence_lock_once(
    tmp_path,
    monkeypatch,
):
    class NonReentrantCountingLock:
        def __init__(self):
            self.acquisitions = 0
            self.locked = False

        def __enter__(self):
            if self.locked:
                raise AssertionError("persistence lock was reacquired")
            self.acquisitions += 1
            self.locked = True
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.locked = False

    entry = make_empty_database_entry("Ada Lovelace")
    database = {}
    lock = NonReentrantCountingLock()
    calls = []

    def fake_rewrite(candidate, filename, data_folder):
        calls.append((candidate, filename, data_folder))
        return "final-sha256"

    monkeypatch.setattr(cache, "_rewrite_database_unlocked", fake_rewrite)

    result = cache.upsert_entry(
        database,
        entry,
        "database.jsonl",
        str(tmp_path),
        lock,
    )

    assert result == "final-sha256"
    assert lock.acquisitions == 1
    assert len(calls) == 1


def test_concurrent_upserts_preserve_every_title(tmp_path):
    path = database_path(tmp_path)
    initial_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [initial_entry])
    database = {initial_entry["title"]: initial_entry}
    persistence_lock = threading.Lock()
    new_entries = [
        make_empty_database_entry(title)
        for title in ("Zeno", "Hypatia", "Simone de Beauvoir")
    ]
    start_barrier = threading.Barrier(len(new_entries))

    def upsert_after_start(entry):
        start_barrier.wait()
        return cache.upsert_entry(
            database,
            entry,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=len(new_entries)) as executor:
        futures = [
            executor.submit(upsert_after_start, entry)
            for entry in new_entries
        ]

        for future in futures:
            future.result()

    expected_titles = {
        "Ada Lovelace",
        "Zeno",
        "Hypatia",
        "Simone de Beauvoir",
    }
    disk_database = cache.load_database("database.jsonl", str(tmp_path))

    assert set(database) == expected_titles
    assert set(disk_database) == expected_titles
    assert database_schema.validate_database_dataset(
        list(disk_database.values())
    ) == []
    assert len(disk_database) == len(expected_titles)


def test_concurrent_upserts_to_different_existing_titles_preserve_both_updates(
    tmp_path,
):
    path = database_path(tmp_path)
    ada_entry = make_empty_database_entry("Ada Lovelace")
    zeno_entry = make_empty_database_entry("Zeno")
    write_entries(path, [ada_entry, zeno_entry])
    database = {
        ada_entry["title"]: ada_entry,
        zeno_entry["title"]: zeno_entry,
    }
    ada_update = copy.deepcopy(ada_entry)
    zeno_update = copy.deepcopy(zeno_entry)
    ada_update["summary"]["text"] = "Ada update"
    zeno_update["summary"]["text"] = "Zeno update"
    persistence_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def upsert_after_start(entry):
        start_barrier.wait()
        return cache.upsert_entry(
            database,
            entry,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(upsert_after_start, entry)
            for entry in (ada_update, zeno_update)
        ]

        for future in futures:
            future.result()

    disk_database = cache.load_database("database.jsonl", str(tmp_path))

    for stored_database in (database, disk_database):
        assert stored_database["Ada Lovelace"]["summary"]["text"] == "Ada update"
        assert stored_database["Zeno"]["summary"]["text"] == "Zeno update"


def test_failed_concurrent_upsert_does_not_erase_successful_update(
    tmp_path,
    monkeypatch,
):
    path = database_path(tmp_path)
    initial_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [initial_entry])
    database = {initial_entry["title"]: initial_entry}
    successful_entry = make_empty_database_entry("Zeno")
    failing_entry = make_empty_database_entry("Failure")
    persistence_lock = threading.Lock()
    start_barrier = threading.Barrier(2)
    original_rewrite = cache._rewrite_database_unlocked

    def fail_only_failure_candidate(candidate, filename, data_folder):
        if "Failure" in candidate:
            raise OSError("simulated concurrent write failure")
        return original_rewrite(candidate, filename, data_folder)

    monkeypatch.setattr(
        cache,
        "_rewrite_database_unlocked",
        fail_only_failure_candidate,
    )

    def upsert_after_start(entry):
        start_barrier.wait()
        return cache.upsert_entry(
            database,
            entry,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        successful_future = executor.submit(
            upsert_after_start,
            successful_entry,
        )
        failing_future = executor.submit(
            upsert_after_start,
            failing_entry,
        )

        successful_future.result()
        with pytest.raises(OSError, match="simulated concurrent write failure"):
            failing_future.result()

    disk_database = cache.load_database("database.jsonl", str(tmp_path))

    assert set(database) == {"Ada Lovelace", "Zeno"}
    assert set(disk_database) == {"Ada Lovelace", "Zeno"}
    assert "Failure" not in database
    assert "Failure" not in disk_database
    assert database_schema.validate_database_dataset(
        list(disk_database.values())
    ) == []


def test_repository_lock_serializes_upsert_transactions(tmp_path):
    class RecordingLock:
        def __init__(self):
            self._lock = threading.Lock()
            self._state_lock = threading.Lock()
            self.active_transactions = 0
            self.max_active_transactions = 0

        def __enter__(self):
            self._lock.acquire()
            with self._state_lock:
                self.active_transactions += 1
                self.max_active_transactions = max(
                    self.max_active_transactions,
                    self.active_transactions,
                )
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            with self._state_lock:
                self.active_transactions -= 1
            self._lock.release()

    path = database_path(tmp_path)
    initial_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [initial_entry])
    database = {initial_entry["title"]: initial_entry}
    persistence_lock = RecordingLock()
    start_barrier = threading.Barrier(2)

    def upsert_after_start(entry):
        start_barrier.wait()
        return cache.upsert_entry(
            database,
            entry,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                upsert_after_start,
                make_empty_database_entry(title),
            )
            for title in ("Zeno", "Hypatia")
        ]

        for future in futures:
            future.result()

    assert persistence_lock.max_active_transactions == 1
    assert set(database) == {"Ada Lovelace", "Zeno", "Hypatia"}


def test_concurrent_upserts_leave_no_temporary_files(tmp_path):
    path = database_path(tmp_path)
    initial_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [initial_entry])
    database = {initial_entry["title"]: initial_entry}
    persistence_lock = threading.Lock()
    entries = [
        make_empty_database_entry(title)
        for title in ("Zeno", "Hypatia", "Simone de Beauvoir")
    ]
    start_barrier = threading.Barrier(len(entries))

    def upsert_after_start(entry):
        start_barrier.wait()
        return cache.upsert_entry(
            database,
            entry,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=len(entries)) as executor:
        futures = [
            executor.submit(upsert_after_start, entry)
            for entry in entries
        ]

        for future in futures:
            future.result()

    assert not list(tmp_path.glob(".database-rewrite-*.tmp"))


def test_backup_database_creates_byte_identical_valid_snapshot(tmp_path):
    live_path = database_path(tmp_path)
    entries = valid_entries()
    write_entries(live_path, entries)
    live_bytes = live_path.read_bytes()
    backup_path = tmp_path / "database-backup.jsonl"

    backup_hash = cache.backup_database(
        "database.jsonl",
        str(tmp_path),
        backup_path,
        threading.Lock(),
    )

    expected_bytes = database_schema.serialize_database_entries(entries)
    assert backup_path.read_bytes() == expected_bytes
    assert cache.load_database(
        backup_path.name,
        str(backup_path.parent),
    ) == cache.load_database("database.jsonl", str(tmp_path))
    assert backup_hash == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    assert live_path.read_bytes() == live_bytes


def test_backup_database_refuses_existing_destination(tmp_path):
    live_path = database_path(tmp_path)
    write_entries(live_path, valid_entries())
    live_bytes = live_path.read_bytes()
    backup_path = tmp_path / "database-backup.jsonl"
    sentinel = b"existing backup must not be overwritten\n"
    backup_path.write_bytes(sentinel)

    with pytest.raises(ValueError, match="Refusing to overwrite existing backup"):
        cache.backup_database(
            "database.jsonl",
            str(tmp_path),
            backup_path,
            threading.Lock(),
        )

    assert backup_path.read_bytes() == sentinel
    assert live_path.read_bytes() == live_bytes


def test_backup_database_leaves_live_database_unchanged_on_failure(
    tmp_path,
    monkeypatch,
):
    live_path = database_path(tmp_path)
    write_entries(live_path, valid_entries())
    live_bytes = live_path.read_bytes()
    backup_path = tmp_path / "database-backup.jsonl"

    def fail_hash(*args, **kwargs):
        raise OSError("simulated backup hash failure")

    monkeypatch.setattr(
        cache,
        "_hash_file",
        fail_hash,
    )

    with pytest.raises(OSError, match="simulated backup hash failure"):
        cache.backup_database(
            "database.jsonl",
            str(tmp_path),
            backup_path,
            threading.Lock(),
        )

    assert live_path.read_bytes() == live_bytes
    assert not backup_path.exists()


def test_backup_database_cleans_up_temporary_file_on_failure(
    tmp_path,
    monkeypatch,
):
    live_path = database_path(tmp_path)
    write_entries(live_path, valid_entries())
    backup_path = tmp_path / "database-backup.jsonl"
    before_names = {path.name for path in tmp_path.iterdir()}

    def fail_hash(*args, **kwargs):
        raise OSError("simulated backup hash failure")

    monkeypatch.setattr(
        cache,
        "_hash_file",
        fail_hash,
    )

    with pytest.raises(OSError, match="simulated backup hash failure"):
        cache.backup_database(
            "database.jsonl",
            str(tmp_path),
            backup_path,
            threading.Lock(),
        )

    assert {path.name for path in tmp_path.iterdir()} == before_names
    assert not list(tmp_path.glob(".database-backup-*.tmp"))


def test_backup_database_uses_existing_persistence_lock_once(
    tmp_path,
    monkeypatch,
):
    class NonReentrantCountingLock:
        def __init__(self):
            self.acquisitions = 0
            self.locked = False

        def __enter__(self):
            if self.locked:
                raise AssertionError("persistence lock was reacquired")
            self.acquisitions += 1
            self.locked = True
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.locked = False

    calls = []

    def fake_backup(filename, data_folder, backup_path):
        calls.append((filename, data_folder, backup_path))
        return "backup-sha256"

    monkeypatch.setattr(cache, "_backup_database_unlocked", fake_backup)
    lock = NonReentrantCountingLock()
    backup_path = tmp_path / "database-backup.jsonl"

    result = cache.backup_database(
        "database.jsonl",
        str(tmp_path),
        backup_path,
        lock,
    )

    assert result == "backup-sha256"
    assert lock.acquisitions == 1
    assert calls == [
        ("database.jsonl", str(tmp_path), backup_path),
    ]


def test_update_database_entry_creates_missing_title(tmp_path):
    path = database_path(tmp_path)
    existing_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [existing_entry])
    database = {existing_entry["title"]: existing_entry}

    def update_summary(entry):
        entry["summary"]["text"] = "Zeno was a Greek philosopher."
        entry["summary"]["fetched_at"] = 123

    final_hash = cache.update_database_entry(
        database,
        "Zeno",
        update_summary,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )

    disk_database = cache.load_database("database.jsonl", str(tmp_path))
    assert database["Zeno"]["summary"]["text"] == (
        "Zeno was a Greek philosopher."
    )
    assert disk_database["Zeno"] == database["Zeno"]
    assert database_schema.validate_database_dataset(
        list(disk_database.values())
    ) == []
    assert final_hash == hashlib.sha256(path.read_bytes()).hexdigest()


def test_update_database_entry_updates_existing_title_without_changing_other_sections(
    tmp_path,
):
    path = database_path(tmp_path)
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "Existing summary"
    entry["summary"]["fetched_at"] = 10
    entry["wikidata"]["status"] = "available"
    entry["wikidata"]["qid"] = "Q7259"
    entry["wikidata"]["instances"] = ["Q5"]
    entry["wikidata"]["occupations"] = ["Q4964182"]
    entry["wikidata"]["is_human"] = True
    entry["wikidata"]["is_philosopher"] = True
    entry["wikidata"]["fetched_at"] = 11
    before = copy.deepcopy(entry)
    write_entries(path, [entry])
    database = {entry["title"]: entry}

    def update_quotes(candidate):
        candidate["quotes"]["status"] = "available"
        candidate["quotes"]["items"] = [
            {
                "text": "A sufficiently long synthetic quotation.",
                "length": 39,
                "word_count": 6,
                "source": "Wikiquote",
            }
        ]
        candidate["quotes"]["fetched_at"] = 12

    cache.update_database_entry(
        database,
        "Ada Lovelace",
        update_quotes,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )

    updated = database["Ada Lovelace"]
    assert updated["quotes"]["status"] == "available"
    assert updated["summary"] == before["summary"]
    assert updated["wikidata"] == before["wikidata"]
    assert updated["evaluation"] == before["evaluation"]
    assert updated["posting"] == before["posting"]
    assert updated["migration"] == before["migration"]


def test_section_updates_for_same_title_preserve_both_sections(tmp_path):
    path = database_path(tmp_path)
    entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [entry])
    database = {entry["title"]: entry}
    persistence_lock = threading.Lock()
    start_barrier = threading.Barrier(2)

    def update_summary(candidate):
        candidate["summary"]["text"] = "Concurrent summary"
        candidate["summary"]["fetched_at"] = 100

    def update_quotes(candidate):
        candidate["quotes"]["status"] = "available"
        candidate["quotes"]["items"] = [
            {
                "text": "Concurrent quote with enough words and punctuation.",
                "length": 51,
                "word_count": 8,
                "source": "Wikiquote",
            }
        ]
        candidate["quotes"]["fetched_at"] = 101

    def update_after_start(callback):
        start_barrier.wait()
        return cache.update_database_entry(
            database,
            "Ada Lovelace",
            callback,
            "database.jsonl",
            str(tmp_path),
            persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(update_after_start, callback)
            for callback in (update_summary, update_quotes)
        ]

        for future in futures:
            future.result()

    disk_database = cache.load_database("database.jsonl", str(tmp_path))

    for stored_database in (database, disk_database):
        updated = stored_database["Ada Lovelace"]
        assert updated["summary"]["text"] == "Concurrent summary"
        assert updated["quotes"]["items"][0]["text"] == (
            "Concurrent quote with enough words and punctuation."
        )

    assert database_schema.validate_database_dataset(
        list(disk_database.values())
    ) == []


def test_update_database_entry_leaves_memory_and_disk_unchanged_on_write_failure(
    tmp_path,
    monkeypatch,
):
    path = database_path(tmp_path)
    entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [entry])
    database = {entry["title"]: entry}
    before_disk = path.read_bytes()
    before_memory = copy.deepcopy(database)

    def fail_rewrite(*args, **kwargs):
        raise OSError("simulated section write failure")

    monkeypatch.setattr(cache, "_rewrite_database_unlocked", fail_rewrite)

    def update_summary(candidate):
        candidate["summary"]["text"] = "Must not leak"

    with pytest.raises(OSError, match="simulated section write failure"):
        cache.update_database_entry(
            database,
            "Ada Lovelace",
            update_summary,
            "database.jsonl",
            str(tmp_path),
            threading.Lock(),
        )

    assert path.read_bytes() == before_disk
    assert database == before_memory


def test_update_database_entry_does_not_alias_callback_objects(tmp_path):
    path = database_path(tmp_path)
    entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [entry])
    database = {entry["title"]: entry}
    callback_quotes = [
        {
            "text": "Callback-owned quote with enough words and punctuation.",
            "length": 55,
            "word_count": 8,
            "source": "Wikiquote",
        }
    ]

    def update_quotes(candidate):
        candidate["quotes"]["status"] = "available"
        candidate["quotes"]["items"] = callback_quotes

    cache.update_database_entry(
        database,
        "Ada Lovelace",
        update_quotes,
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )
    callback_quotes[0]["text"] = "Caller mutation"

    assert database["Ada Lovelace"]["quotes"]["items"][0]["text"] != (
        "Caller mutation"
    )


def test_update_database_entry_uses_persistence_lock_once(
    tmp_path,
    monkeypatch,
):
    class NonReentrantCountingLock:
        def __init__(self):
            self.acquisitions = 0
            self.locked = False

        def __enter__(self):
            if self.locked:
                raise AssertionError("persistence lock was reacquired")
            self.acquisitions += 1
            self.locked = True
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.locked = False

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    lock = NonReentrantCountingLock()
    calls = []

    def fake_rewrite(candidate, filename, data_folder):
        calls.append((candidate, filename, data_folder))
        return "section-sha256"

    monkeypatch.setattr(cache, "_rewrite_database_unlocked", fake_rewrite)

    result = cache.update_database_entry(
        database,
        "Ada Lovelace",
        lambda candidate: candidate["summary"].update(
            {"text": "Updated"}
        ),
        "database.jsonl",
        str(tmp_path),
        lock,
    )

    assert result == "section-sha256"
    assert lock.acquisitions == 1
    assert len(calls) == 1


def test_upsert_commit_does_not_clear_shared_mapping(tmp_path):
    class NoClearMapping(dict):
        def clear(self):
            raise AssertionError("upsert must not clear shared mapping")

    path = database_path(tmp_path)
    existing_entry = make_empty_database_entry("Ada Lovelace")
    write_entries(path, [existing_entry])
    database = NoClearMapping({existing_entry["title"]: existing_entry})

    cache.upsert_entry(
        database,
        make_empty_database_entry("Zeno"),
        "database.jsonl",
        str(tmp_path),
        threading.Lock(),
    )

    assert set(database) == {"Ada Lovelace", "Zeno"}
