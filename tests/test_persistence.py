import json
import wiki_philosopher_bot.cache as cache
import pytest
import threading
from wiki_philosopher_bot.runtime import persistence_lock, stats_lock
from concurrent.futures import ThreadPoolExecutor

def test_runtime_exposes_separate_persistence_and_stats_locks():
    assert isinstance(persistence_lock, type(threading.Lock()))
    assert isinstance(stats_lock, type(threading.Lock()))
    assert persistence_lock is not stats_lock

def test_persist_jsonl_cache_entry_appends_then_updates_cache(tmp_path):
    memory_cache = {}
    lock = threading.Lock()

    cache.persist_jsonl_cache_entry(
        cache=memory_cache,
        title="Ada Lovelace",
        cache_value="A mathematician.",
        filename="summaries.jsonl",
        file_entry={
            "title": "Ada Lovelace",
            "summary": "A mathematician.",
        },
        data_folder=tmp_path,
        persistence_lock=lock,
    )

    assert memory_cache == {"Ada Lovelace": "A mathematician."}

    lines = (tmp_path / "summaries.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()
    assert [json.loads(line) for line in lines] == [
        {
            "title": "Ada Lovelace",
            "summary": "A mathematician.",
        }
    ]

def test_persist_jsonl_cache_entry_keeps_cache_unchanged_on_append_failure(
    tmp_path, monkeypatch
):
    memory_cache = {}
    lock = threading.Lock()

    def raise_os_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cache, "_append_jsonl_unlocked", raise_os_error)

    with pytest.raises(OSError, match="disk full"):
        cache.persist_jsonl_cache_entry(
            cache=memory_cache,
            title="Ada Lovelace",
            cache_value="A mathematician.",
            filename="summaries.jsonl",
            file_entry={
                "title": "Ada Lovelace",
                "summary": "A mathematician.",
            },
            data_folder=tmp_path,
            persistence_lock=lock,
        )

    assert memory_cache == {}

def test_persist_jsonl_cache_entry_appends_before_updating_cache(
    tmp_path,
    monkeypatch,
):
    memory_cache = {}
    lock = threading.Lock()

    def inspect_during_append(filename, entry, data_folder):
        assert "Ada Lovelace" not in memory_cache

    monkeypatch.setattr(
        cache,
        "_append_jsonl_unlocked",
        inspect_during_append,
    )

    cache.persist_jsonl_cache_entry(
        cache=memory_cache,
        title="Ada Lovelace",
        cache_value="A mathematician.",
        filename="summaries.jsonl",
        file_entry={
            "title": "Ada Lovelace",
            "summary": "A mathematician.",
        },
        data_folder=tmp_path,
        persistence_lock=lock,
    )

    assert memory_cache == {
        "Ada Lovelace": "A mathematician."
    }

def test_persist_evaluation_entry_updates_accepted_cache_after_success(
    tmp_path,
):
    result_cache = {}
    processed_cache = {}

    stats = {
        "new_accepted": 0,
        "new_rejected": 0,
    }

    entry = {
        "title": "Ada Lovelace",
        "status": "accepted",
        "philosopher_confidence": 2,
        "human_confidence": 3,
        "content_confidence": 1,
        "reasons": ["test reason"],
        "last_processed": 123.0,
    }

    cache.persist_evaluation_entry(
        entry,
        result_cache,
        processed_cache,
        stats,
        threading.Lock(),
        threading.Lock(),
        "results.jsonl",
        "processed.jsonl",
        tmp_path,
    )

    assert result_cache == {
        "Ada Lovelace": entry
    }

    assert processed_cache == {}

    assert stats == {
        "new_accepted": 1,
        "new_rejected": 0,
    }

    lines = (
        tmp_path / "results.jsonl"
    ).read_text(encoding="utf-8").splitlines()

    assert [json.loads(line) for line in lines] == [
        entry
    ]

    assert not (
        tmp_path / "processed.jsonl"
    ).exists()

def test_persist_evaluation_entry_updates_rejected_cache_after_success(
    tmp_path,
):
    result_cache = {}
    processed_cache = {}

    stats = {
        "new_accepted": 0,
        "new_rejected": 0,
    }

    entry = {
        "title": "Book title",
        "status": "rejected",
        "philosopher_confidence": -1,
        "human_confidence": 0,
        "content_confidence": -1,
        "reasons": ["test rejection"],
        "last_processed": 123.0,
    }

    cache.persist_evaluation_entry(
        entry,
        result_cache,
        processed_cache,
        stats,
        threading.Lock(),
        threading.Lock(),
        "results.jsonl",
        "processed.jsonl",
        tmp_path,
    )

    assert result_cache == {}

    assert processed_cache == {
        "Book title": entry
    }

    assert stats == {
        "new_accepted": 0,
        "new_rejected": 1,
    }

    lines = (
        tmp_path / "processed.jsonl"
    ).read_text(encoding="utf-8").splitlines()

    assert [json.loads(line) for line in lines] == [
        entry
    ]

    assert not (
        tmp_path / "results.jsonl"
    ).exists()

def test_persist_evaluation_entry_does_not_update_cache_or_stats_on_failure(
    tmp_path,
    monkeypatch,
):
    result_cache = {}
    processed_cache = {}

    stats = {
        "new_accepted": 0,
        "new_rejected": 0,
    }

    entry = {
        "title": "Ada Lovelace",
        "status": "accepted",
        "philosopher_confidence": 2,
        "human_confidence": 3,
        "content_confidence": 1,
        "reasons": [],
        "last_processed": 123.0,
    }

    def raise_os_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(
        cache,
        "_append_jsonl_unlocked",
        raise_os_error,
    )

    with pytest.raises(OSError, match="disk full"):
        cache.persist_evaluation_entry(
            entry,
            result_cache,
            processed_cache,
            stats,
            threading.Lock(),
            threading.Lock(),
            "results.jsonl",
            "processed.jsonl",
            tmp_path,
        )

    assert result_cache == {}
    assert processed_cache == {}

    assert stats == {
        "new_accepted": 0,
        "new_rejected": 0,
    }

def test_persist_evaluation_entry_rejects_unknown_status(
    tmp_path,
):
    result_cache = {}
    processed_cache = {}

    stats = {
        "new_accepted": 0,
        "new_rejected": 0,
    }

    entry = {
        "title": "Ada Lovelace",
        "status": "maybe",
    }

    with pytest.raises(
        ValueError,
        match="Unexpected evaluation status",
    ):
        cache.persist_evaluation_entry(
            entry,
            result_cache,
            processed_cache,
            stats,
            threading.Lock(),
            threading.Lock(),
            "results.jsonl",
            "processed.jsonl",
            tmp_path,
        )

    assert result_cache == {}
    assert processed_cache == {}
    assert stats == {
        "new_accepted": 0,
        "new_rejected": 0,
    }

    assert list(tmp_path.iterdir()) == []

def test_concurrent_jsonl_cache_persistence_keeps_every_record_valid(
    tmp_path,
):
    memory_cache = {}
    persistence_lock = threading.Lock()

    def persist(index):
        title = "Person {}".format(index)

        cache.persist_jsonl_cache_entry(
            cache=memory_cache,
            title=title,
            cache_value="Summary {}".format(index),
            filename="summaries.jsonl",
            file_entry={
                "title": title,
                "summary": "Summary {}".format(index),
            },
            data_folder=tmp_path,
            persistence_lock=persistence_lock,
        )

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(persist, range(20)))

    lines = (
        tmp_path / "summaries.jsonl"
    ).read_text(encoding="utf-8").splitlines()

    records = [
        json.loads(line)
        for line in lines
    ]

    assert len(records) == 20

    assert {
        record["title"]
        for record in records
    } == {
        "Person {}".format(index)
        for index in range(20)
    }

    assert len(memory_cache) == 20

    assert {
        record["title"]: record["summary"]
        for record in records
    } == {
        "Person {}".format(index): "Summary {}".format(index)
        for index in range(20)
    }

    assert memory_cache == {
        "Person {}".format(index): "Summary {}".format(index)
        for index in range(20)
    }