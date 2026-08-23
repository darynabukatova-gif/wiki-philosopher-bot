import copy
import json
import main
import pytest
import threading
import cache
import evaluation
import wikipedia_api
from config import (
    CURRENT_EVALUATION_ALGORITHM_VERSION,
    CURRENT_QUOTE_PARSER_VERSION,
)
from database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)
from telegram_bot import TelegramResult


@pytest.fixture(autouse=True)
def isolate_run_reports(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "RUN_REPORTS_DIRECTORY", tmp_path / "reports/runs")


def make_postable_entry(title):
    entry = make_empty_database_entry(title)
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"] = {
        "status": "available",
        "items": [
            {
                "text": "A complete canonical quote.",
                "length": 27,
                "word_count": 5,
                "source": {
                    "work": None, "year": None, "date": None,
                    "details": None, "citation": None, "url": None,
                },
                "retrieved_from": "Wikiquote",
            }
        ],
        "failure": None,
        "fetched_at": None,
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    }
    return entry


def test_main_returns_zero_when_no_candidate(monkeypatch):
    import main

    state = main.RuntimeState(
        database={},
        stats=main.make_initial_stats(),
    )

    monkeypatch.setattr(main, "load_runtime_state", lambda folder: state)
    monkeypatch.setattr(main, "load_environment", lambda: None, raising=False)
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(main, "discover_pages", lambda term, limiter: [])
    monkeypatch.setattr(
        main,
        "build_entity_lookup",
        lambda pages, database, limiter: ({}, {}, {}),
    )
    monkeypatch.setattr(main, "evaluate_pages", lambda **kwargs: None)
    monkeypatch.setattr(main, "select_candidate", lambda state: None)
    monkeypatch.setattr(
        main,
        "format_philosopher_message",
        lambda *args, **kwargs: pytest.fail("must not format a message"),
    )

    assert main.main() == 0


def test_main_calls_environment_loading_before_runtime_startup(monkeypatch):
    events = []
    state = main.RuntimeState(
        database={},
        stats=main.make_initial_stats(),
    )

    monkeypatch.setattr(
        main,
        "load_environment",
        lambda: events.append("environment"),
        raising=False,
    )
    monkeypatch.setattr(
        main,
        "load_runtime_state",
        lambda data_folder: events.append("runtime") or state,
    )
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(main, "discover_pages", lambda term, limiter: [])
    monkeypatch.setattr(
        main,
        "build_entity_lookup",
        lambda pages, database, limiter: ({}, {}, {}),
    )
    monkeypatch.setattr(main, "evaluate_pages", lambda **kwargs: None)
    monkeypatch.setattr(main, "select_candidate", lambda state: None)

    assert main.main() == 0
    assert events == ["environment", "runtime"]


def test_main_writes_run_report_for_no_candidate(monkeypatch, tmp_path):
    state = main.RuntimeState(
        database={},
        stats=main.make_initial_stats(),
    )
    monkeypatch.setattr(main, "load_environment", lambda: None)
    monkeypatch.setattr(main, "load_runtime_state", lambda folder: state)
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(main, "discover_pages", lambda term, limiter: [])
    monkeypatch.setattr(
        main, "build_entity_lookup", lambda *args: ({}, {}, {})
    )
    monkeypatch.setattr(main, "evaluate_pages", lambda **kwargs: None)
    monkeypatch.setattr(main, "select_candidate", lambda state: None)
    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 100.0)})(),
    )

    assert main.main() == 0

    reports = sorted((tmp_path / "reports/runs").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["entries"] == {"before": 0, "after": 0}
    assert report["posting"] == {
        "selected_title": None,
        "telegram": None,
    }


def test_main_writes_run_report_for_telegram_failure(monkeypatch, tmp_path):
    entry = make_postable_entry("Ada")
    state = main.RuntimeState(
        database={"Ada": entry},
        stats=main.make_initial_stats(),
    )
    monkeypatch.setattr(main, "load_environment", lambda: None)
    monkeypatch.setattr(main, "load_runtime_state", lambda folder: state)
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(main, "discover_pages", lambda term, limiter: [])
    monkeypatch.setattr(
        main, "build_entity_lookup", lambda *args: ({}, {}, {})
    )
    monkeypatch.setattr(main, "evaluate_pages", lambda **kwargs: None)
    monkeypatch.setattr(main, "select_candidate", lambda state: entry)
    monkeypatch.setattr(main, "format_philosopher_message", lambda *args, **kwargs: "message")
    monkeypatch.setattr(
        main,
        "send_and_record_post",
        lambda *args, **kwargs: TelegramResult(False, None, "http_error"),
    )
    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 100.0)})(),
    )

    assert main.main() == 1

    reports = sorted((tmp_path / "reports/runs").glob("*.json"))
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["posting"] == {
        "selected_title": "Ada",
        "telegram": {"ok": False, "error_reason": "http_error"},
    }


def test_main_writes_run_report_for_successful_post(monkeypatch, tmp_path):
    entry = make_postable_entry("Ada")
    state = main.RuntimeState(
        database={"Ada": entry},
        stats=main.make_initial_stats(),
    )
    monkeypatch.setattr(main, "load_environment", lambda: None)
    monkeypatch.setattr(main, "load_runtime_state", lambda folder: state)
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(main, "discover_pages", lambda term, limiter: [])
    monkeypatch.setattr(
        main, "build_entity_lookup", lambda *args: ({}, {}, {})
    )
    monkeypatch.setattr(main, "evaluate_pages", lambda **kwargs: None)
    monkeypatch.setattr(main, "select_candidate", lambda state: entry)
    monkeypatch.setattr(main, "format_philosopher_message", lambda *args, **kwargs: "message")
    monkeypatch.setattr(
        main,
        "send_and_record_post",
        lambda *args, **kwargs: TelegramResult(True, {"ok": True}, None),
    )
    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 100.0)})(),
    )

    assert main.main() == 0

    report = json.loads(next((tmp_path / "reports/runs").glob("*.json")).read_text(encoding="utf-8"))
    assert report["posting"] == {
        "selected_title": "Ada",
        "telegram": {"ok": True, "error_reason": None},
    }


def test_main_reports_ordinary_runtime_failure_then_reraises(monkeypatch, tmp_path):
    state = main.RuntimeState(database={}, stats=main.make_initial_stats())
    monkeypatch.setattr(main, "load_environment", lambda: None)
    monkeypatch.setattr(main, "load_runtime_state", lambda folder: state)
    monkeypatch.setattr(main, "RateLimiter", lambda rate: object())
    monkeypatch.setattr(
        main,
        "discover_pages",
        lambda term, limiter: (_ for _ in ()).throw(RuntimeError("discovery failed")),
    )
    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 100.0)})(),
    )

    with pytest.raises(RuntimeError, match="discovery failed"):
        main.main()

    reports = sorted((tmp_path / "reports/runs").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["runtime_error"] == "RuntimeError: discovery failed"

def test_make_initial_stats_returns_independent_dictionaries():
    first = main.make_initial_stats()
    second = main.make_initial_stats()

    first["new_accepted"] = 1

    assert second["new_accepted"] == 0

def test_runtime_state_contains_only_database_and_stats():
    state = main.RuntimeState(
        database={},
        stats=main.make_initial_stats(),
    )

    assert set(vars(state)) == {"database", "stats"}

def test_load_runtime_state_loads_canonical_database(
    monkeypatch,
    tmp_path,
):
    calls = []

    canonical_database = {
        "Ada": {
            "title": "Ada",
        }
    }

    def fake_load_database(filename, data_folder):
        calls.append(("database", filename, data_folder))
        return canonical_database

    monkeypatch.setattr(main, "load_database", fake_load_database)

    assert not any(
        hasattr(main, loader_name)
        for loader_name in (
            "load_summary_cache",
            "load_entity_cache",
            "load_quote_cache",
            "load_quote_failure_cache",
        )
    )

    for loader_name in (
        "load_summary_cache",
        "load_entity_cache",
        "load_quote_cache",
        "load_quote_failure_cache",
    ):
        monkeypatch.setattr(
            main,
            loader_name,
            lambda *args, **kwargs: pytest.fail(
                "legacy content cache loader must not run"
            ),
            raising=False,
        )

    for loader_name in (
        "load_processed_cache",
        "load_result_cache",
    ):
        monkeypatch.setattr(
            main,
            loader_name,
            lambda *args, **kwargs: pytest.fail(
                "legacy evaluation loader must not run"
            ),
            raising=False,
        )

    def forbidden_posted_loader(*args, **kwargs):
        pytest.fail("legacy posted loader must not run")

    monkeypatch.setattr(
        main,
        "load_posted_titles",
        forbidden_posted_loader,
        raising=False,
    )
    monkeypatch.setattr(
        cache,
        "load_posted_titles",
        forbidden_posted_loader,
    )

    state = main.load_runtime_state(tmp_path)

    assert state.database is canonical_database

    assert not hasattr(state, "summary_cache")
    assert not hasattr(state, "entity_cache")
    assert not hasattr(state, "quote_cache")
    assert not hasattr(state, "quote_failure_cache")

    assert not hasattr(state, "processed_cache")
    assert not hasattr(state, "result_cache")

    assert len(calls) == 1

    assert all(
        data_folder == tmp_path
        for _, _, data_folder in calls
    )

    assert state.stats == main.make_initial_stats()

    assert {
        name: filename
        for name, filename, _ in calls
    } == {
        "database": main.DATABASE_FILE,
    }


def test_load_runtime_state_loads_canonical_database_once(monkeypatch, tmp_path):
    calls = []
    canonical_database = {"Ada Lovelace": {"title": "Ada Lovelace"}}

    def fake_load_database(filename, data_folder):
        calls.append((filename, data_folder))
        return canonical_database

    monkeypatch.setattr(main, "load_database", fake_load_database)

    monkeypatch.setattr(
        cache,
        "load_posted_titles",
        lambda *args: pytest.fail(
            "legacy posted loader must not run"
        ),
    )

    for loader_name in (
        "load_processed_cache",
        "load_result_cache",
    ):
        monkeypatch.setattr(
            main,
            loader_name,
            lambda *args, **kwargs: pytest.fail(
                "legacy evaluation loader must not run"
            ),
            raising=False,
        )

    state = main.load_runtime_state(tmp_path)

    assert state.database is canonical_database
    assert calls == [(main.DATABASE_FILE, tmp_path)]


def test_load_runtime_state_does_not_call_load_posted_titles(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([])
    )

    monkeypatch.setattr(
        cache,
        "load_posted_titles",
        lambda *args, **kwargs: pytest.fail(
            "legacy posted loader must not run"
        ),
    )

    assert main.load_runtime_state(tmp_path).database == {}


def test_load_runtime_state_propagates_missing_canonical_database(
    monkeypatch,
    tmp_path,
):
    def missing_database(filename, data_folder):
        raise FileNotFoundError(filename)

    monkeypatch.setattr(main, "load_database", missing_database)

    with pytest.raises(FileNotFoundError):
        main.load_runtime_state(tmp_path)


def test_load_runtime_state_propagates_invalid_canonical_database(
    monkeypatch,
    tmp_path,
):
    def invalid_database(filename, data_folder):
        raise ValueError("invalid canonical database")

    monkeypatch.setattr(main, "load_database", invalid_database)

    with pytest.raises(ValueError, match="invalid canonical database"):
        main.load_runtime_state(tmp_path)


def test_load_runtime_state_does_not_require_posted_json(tmp_path):
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([])
    )

    state = main.load_runtime_state(tmp_path)

    assert state.database == {}
    assert state.stats == main.make_initial_stats()
    assert not (tmp_path / "posted.json").exists()


def test_canonical_posting_runtime_work_leaves_posted_json_unchanged(
    monkeypatch,
    tmp_path,
):
    entry = make_postable_entry("Ada Lovelace")
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    posted_path = tmp_path / "posted.json"
    posted_path.write_bytes(b"not runtime JSON\n")
    posted_before = posted_path.read_bytes()
    state = main.load_runtime_state(tmp_path)

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
    )

    candidate = main.select_candidate(state)
    result = main.send_and_record_post(
        candidate["title"],
        "hello",
        state.database,
        threading.Lock(),
        str(tmp_path),
        send=lambda message: TelegramResult(
            ok=True,
            response_data={"ok": True},
            error_reason=None,
        ),
    )

    assert result.ok is True
    assert state.database["Ada Lovelace"]["posting"] == {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": False,
    }
    assert posted_path.read_bytes() == posted_before


def test_runtime_posting_does_not_call_legacy_posting_helpers(
    monkeypatch,
    tmp_path,
):
    entry = make_postable_entry("Ada Lovelace")
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    def forbidden(*args, **kwargs):
        pytest.fail("legacy posting helper must not run")

    monkeypatch.setattr(cache, "load_posted_titles", forbidden)
    monkeypatch.setattr(cache, "save_posted_titles", forbidden)

    state = main.load_runtime_state(tmp_path)
    candidate = main.select_candidate(state)

    result = main.send_and_record_post(
        candidate["title"],
        "hello",
        state.database,
        threading.Lock(),
        str(tmp_path),
        send=lambda message: TelegramResult(
            ok=True,
            response_data={"ok": True},
            error_reason=None,
        ),
    )

    assert result.ok is True
    assert state.database["Ada Lovelace"]["posting"]["has_been_posted"] is True


def test_runtime_posting_state_works_without_posted_json(
    monkeypatch,
    tmp_path,
):
    unposted = make_postable_entry("Unposted")
    posted = make_postable_entry("Posted")
    posted["posting"] = {
        "has_been_posted": True,
        "posted_at": [100],
        "legacy_posted_without_timestamp": False,
    }
    rejected = make_postable_entry("Rejected")
    rejected["evaluation"]["status"] = "rejected"
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([unposted, posted, rejected])
    )

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
    )

    state = main.load_runtime_state(tmp_path)
    candidate = main.select_candidate(state)

    assert candidate["title"] == "Unposted"

    result = main.send_and_record_post(
        candidate["title"],
        "hello",
        state.database,
        threading.Lock(),
        str(tmp_path),
        send=lambda message: TelegramResult(
            ok=True,
            response_data={"ok": True},
            error_reason=None,
        ),
    )

    assert result.ok is True
    assert state.database["Unposted"]["posting"]["posted_at"] == [
        1234567890
    ]
    assert not (tmp_path / "posted.json").exists()


def test_failed_telegram_send_without_posted_json_changes_nothing(tmp_path):
    entry = make_postable_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    before_database = serialize_database_entries(list(database.values()))
    before_disk = database_path.read_bytes()

    result = main.send_and_record_post(
        "Ada Lovelace",
        "hello",
        database,
        threading.Lock(),
        str(tmp_path),
        send=lambda message: TelegramResult(
            ok=False,
            response_data=None,
            error_reason="http_error",
        ),
    )

    assert result.ok is False
    assert serialize_database_entries(list(database.values())) == before_database
    assert database_path.read_bytes() == before_disk
    assert not (tmp_path / "posted.json").exists()


def test_canonical_evaluation_runtime_work_leaves_results_and_processed_jsonl_unchanged(
    monkeypatch,
    tmp_path,
):
    entries = [
        make_empty_database_entry("Accepted title"),
        make_empty_database_entry("Rejected title"),
    ]
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries(entries)
    )
    (tmp_path / "posted.json").write_bytes(b"[]")
    results_path = tmp_path / "results.jsonl"
    processed_path = tmp_path / "processed.jsonl"
    results_path.write_bytes(b"results sentinel\n")
    processed_path.write_bytes(b"processed sentinel\n")
    results_before = results_path.read_bytes()
    processed_before = processed_path.read_bytes()
    state = main.load_runtime_state(tmp_path)

    def fake_process_title(page, *args, **kwargs):
        status = (
            "accepted"
            if page["title"] == "Accepted title"
            else "rejected"
        )
        return {
            "title": page["title"],
            "status": status,
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": 1,
            "reasons": [status],
            "last_processed": 123.0,
        }

    monkeypatch.setattr(main, "process_title", fake_process_title)

    main.evaluate_pages(
        pages=[
            {"title": "Accepted title"},
            {"title": "Rejected title"},
        ],
        state=state,
        all_qids={},
        all_entities={},
        limiter=None,
        max_workers=1,
        data_folder=str(tmp_path),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    persisted = main.load_database("database.jsonl", str(tmp_path))
    assert persisted["Accepted title"]["evaluation"]["status"] == "accepted"
    assert persisted["Rejected title"]["evaluation"]["status"] == "rejected"
    assert results_path.read_bytes() == results_before
    assert processed_path.read_bytes() == processed_before


def test_runtime_evaluation_does_not_call_legacy_persistence_helpers(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )
    (tmp_path / "posted.json").write_bytes(b"[]")
    state = main.load_runtime_state(tmp_path)

    monkeypatch.setattr(
        cache,
        "persist_evaluation_entry",
        lambda *args, **kwargs: pytest.fail(
            "runtime must not persist legacy evaluation records"
        ),
    )
    monkeypatch.setattr(
        cache,
        "persist_jsonl_cache_entry",
        lambda *args, **kwargs: pytest.fail(
            "runtime must not append legacy evaluation JSONL"
        ),
    )
    monkeypatch.setattr(
        main,
        "process_title",
        lambda page, *args, **kwargs: {
            "title": page["title"],
            "status": "accepted",
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": 1,
            "reasons": ["accepted"],
            "last_processed": 123.0,
        },
    )

    main.evaluate_pages(
        pages=[{"title": "Ada Lovelace"}],
        state=state,
        all_qids={},
        all_entities={},
        limiter=None,
        max_workers=1,
        data_folder=str(tmp_path),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert state.database["Ada Lovelace"]["evaluation"]["status"] == (
        "accepted"
    )


def test_runtime_evaluation_state_works_without_legacy_result_files(
    monkeypatch,
    tmp_path,
):
    accepted = make_empty_database_entry("Accepted")
    accepted["evaluation"].update({
        "status": "accepted",
        "algorithm_version": CURRENT_EVALUATION_ALGORITHM_VERSION,
    })
    accepted["quotes"].update({
        "status": "available",
        "items": [{
            "text": "Quote",
            "length": 5,
            "word_count": 1,
            "source": {
                "work": None, "year": None, "date": None,
                "details": None, "citation": None, "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    })
    rejected = make_empty_database_entry("Rejected")
    rejected["evaluation"].update({
        "status": "rejected",
        "algorithm_version": CURRENT_EVALUATION_ALGORITHM_VERSION,
    })
    unprocessed = make_empty_database_entry("Unprocessed")
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([accepted, rejected, unprocessed])
    )
    (tmp_path / "posted.json").write_bytes(b"[]")
    state = main.load_runtime_state(tmp_path)

    def filters_must_not_run(*args, **kwargs):
        raise AssertionError("current evaluations must be skipped")

    monkeypatch.setattr(evaluation, "title_filter", filters_must_not_run)

    assert evaluation.process_title(
        {"title": "Accepted"}, state.stats, state.database, {}, {},
        stats_lock=threading.Lock(), persistence_lock=threading.Lock(),
    ) is None
    assert evaluation.process_title(
        {"title": "Rejected"}, state.stats, state.database, {}, {},
        stats_lock=threading.Lock(), persistence_lock=threading.Lock(),
    ) is None
    assert main.select_candidate(state) is state.database["Accepted"]

    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: evaluation.FilterResult(
            human_bonus=1,
            philosopher_bonus=1,
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )

    assert evaluation.process_title(
        {"title": "Unprocessed"}, state.stats, state.database, {}, {},
        stats_lock=threading.Lock(), persistence_lock=threading.Lock(),
    ) is not None


def test_canonical_content_operations_leave_legacy_content_files_unchanged(
    monkeypatch,
    tmp_path,
):
    sentinel_files = {
        "summaries.jsonl": b"summary sentinel\n",
        "entities.jsonl": b"entity sentinel\n",
        "quotes.jsonl": b"quote sentinel\n",
        "quote_failures.jsonl": b"failure sentinel\n",
    }
    for filename, content in sentinel_files.items():
        (tmp_path / filename).write_bytes(content)

    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([])
    )
    database = {}
    stats = main.make_initial_stats()

    class SummaryResponse:
        def json(self):
            return {"extract": "A canonical summary."}

    class QuoteResponse:
        text = (
            '<div class="mw-parser-output"><ul><li>'
            "A sufficiently long canonical quote for integration testing."
            "</li></ul></div>"
        )

    responses = [
        wikipedia_api.RequestResult(SummaryResponse(), None, 1),
        wikipedia_api.RequestResult(QuoteResponse(), None, 1),
    ]
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: responses.pop(0),
    )
    monkeypatch.setattr(
        evaluation,
        "prepare_entity",
        lambda title, all_qids, all_entities: {
            "title": title,
            "valid": False,
            "reason": "no_qid",
            "qid": None,
        },
    )

    stats_lock = threading.Lock()
    persistence_lock = threading.Lock()
    assert wikipedia_api.get_summary(
        "Summary Title",
        database,
        stats,
        stats_lock,
        persistence_lock,
        str(tmp_path),
    ) == "A canonical summary."
    evaluation.prepare_entity_cached(
        "Entity Title",
        database,
        {},
        {},
        stats,
        stats_lock,
        persistence_lock,
        data_folder=str(tmp_path),
    )
    assert wikipedia_api.get_quotes(
        "Quote Title",
        database,
        stats,
        stats_lock,
        persistence_lock,
        str(tmp_path),
    )

    assert {
        filename: (tmp_path / filename).read_bytes()
        for filename in sentinel_files
    } == sentinel_files

def test_discover_pages_deduplicates_by_title(
    monkeypatch,
):
    limiter = object()

    monkeypatch.setattr(
        main,
        "get_all_pages",
        lambda search_term, limiter=None: [
            {
                "title": "Ada Lovelace",
                "pageid": 1,
            },
            {
                "title": "Plato",
                "pageid": 2,
            },
            {
                "title": "Ada Lovelace",
                "pageid": 3,
            },
        ],
    )

    pages = main.discover_pages(
        "philosopher",
        limiter=limiter,
    )

    assert pages == [
        {
            "title": "Ada Lovelace",
            "pageid": 3,
        },
        {
            "title": "Plato",
            "pageid": 2,
        },
    ]

def test_build_entity_lookup_passes_page_titles(
    monkeypatch,
):
    captured = {}
    limiter = object()

    def fake_build_entity_cache(
        titles,
        limiter=None,
        page_properties=None,
        pageprops_errors=None,
    ):
        captured["titles"] = titles
        captured["limiter"] = limiter

        return (
            {
                "Ada": "Q7259",
                "Plato": "Q859",
            },
            {
                "Q7259": {
                    "id": "Q7259"
                },
                "Q859": {
                    "id": "Q859"
                },
            },
            {},
        )

    monkeypatch.setattr(
        main,
        "build_entity_cache",
        fake_build_entity_cache,
    )
    monkeypatch.setattr(
        main,
        "build_page_properties_cache",
        lambda titles, limiter=None: ({}, {}),
    )

    qids, entities, errors = main.build_entity_lookup(
        [
            {"title": "Ada"},
            {"title": "Plato"},
        ],
        {},
        limiter=limiter,
    )

    assert captured["titles"] == [
        "Ada",
        "Plato",
    ]

    assert captured["limiter"] is limiter

    assert qids == {
        "Ada": "Q7259",
        "Plato": "Q859",
    }

    assert entities == {
        "Q7259": {"id": "Q7259"},
        "Q859": {"id": "Q859"},
    }
    assert errors == {}


def test_build_entity_lookup_attaches_pageprops_disambiguation_metadata(
    monkeypatch,
):
    pages = [
        {"title": "Alan White"},
        {"title": "Alan White (American philosopher)"},
    ]
    page_properties = {
        "Alan White": wikipedia_api.PageProperties(None, True),
        "Alan White (American philosopher)": wikipedia_api.PageProperties(
            "Q123", False,
        ),
    }

    monkeypatch.setattr(
        main,
        "build_page_properties_cache",
        lambda titles, limiter=None: (page_properties, {}),
    )
    monkeypatch.setattr(
        main,
        "build_entity_cache",
        lambda titles, limiter=None, page_properties=None, pageprops_errors=None: (
            {}, {}, {},
        ),
    )

    assert main.build_entity_lookup(pages, {}, limiter=object()) == ({}, {}, {})
    assert pages == [
        {"title": "Alan White", "is_disambiguation": True},
        {
            "title": "Alan White (American philosopher)",
            "is_disambiguation": False,
        },
    ]


def test_build_entity_lookup_keeps_page_type_unknown_when_pageprops_fail(
    monkeypatch,
):
    pages = [{"title": "Alan White"}]
    captured = {}

    monkeypatch.setattr(
        main,
        "build_page_properties_cache",
        lambda titles, limiter=None: ({}, {"Alan White": "request_exception"}),
    )

    def fake_build_entity_cache(
        titles,
        limiter=None,
        page_properties=None,
        pageprops_errors=None,
    ):
        captured["titles"] = titles
        captured["pageprops_errors"] = pageprops_errors
        return {}, {}, {"Alan White": "request_exception"}

    monkeypatch.setattr(main, "build_entity_cache", fake_build_entity_cache)

    assert main.build_entity_lookup(pages, {}, limiter=object()) == (
        {}, {}, {"Alan White": "request_exception"},
    )
    assert pages == [{"title": "Alan White", "is_disambiguation": None}]
    assert captured == {
        "titles": ["Alan White"],
        "pageprops_errors": {"Alan White": "request_exception"},
    }


def test_build_entity_lookup_skips_titles_with_known_canonical_wikidata(
    monkeypatch,
):
    captured = {}

    def fake_build_entity_cache(
        titles,
        limiter=None,
        page_properties=None,
        pageprops_errors=None,
    ):
        captured["titles"] = titles
        captured["limiter"] = limiter
        return {}, {}, {}

    monkeypatch.setattr(main, "build_entity_cache", fake_build_entity_cache)
    monkeypatch.setattr(
        main,
        "build_page_properties_cache",
        lambda titles, limiter=None: ({}, {}),
    )

    database = {
        "Available": {"wikidata": {"status": "available"}},
        "Unavailable": {"wikidata": {"status": "unavailable"}},
        "Unknown": {"wikidata": {"status": "unknown"}},
    }
    limiter = object()

    assert main.build_entity_lookup(
        [
            {"title": "Available"},
            {"title": "Unavailable"},
            {"title": "Unknown"},
            {"title": "Absent"},
        ],
        database,
        limiter=limiter,
    ) == ({}, {}, {})

    assert captured == {
        "titles": ["Unknown", "Absent"],
        "limiter": limiter,
    }

def test_evaluate_pages_persists_worker_results_to_canonical_database(
    monkeypatch,
):
    state = main.RuntimeState(
        database={},
        stats=main.make_initial_stats(),
    )

    processed_titles = []
    received_databases = []
    received_data_folders = []
    canonical_persistence_calls = []
    runner_calls = []
    provided_stats_lock = threading.Lock()
    provided_persistence_lock = threading.Lock()

    def fake_process_title(
        page,
        *args,
        **kwargs,
    ):
        processed_titles.append(
            page["title"]
        )
        received_databases.append(args[1])
        received_data_folders.append(kwargs["data_folder"])

        return {
            "title": page["title"],
            "status": "accepted",
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": 1,
            "reasons": ["test reason"],
            "last_processed": 123.0,
        }

    def fake_persist_canonical_evaluation(
        result,
        database,
        stats,
        received_stats_lock,
        received_persistence_lock,
        data_folder,
    ):
        canonical_persistence_calls.append(
            {
                "result": result,
                "database": database,
                "stats": stats,
                "stats_lock": received_stats_lock,
                "persistence_lock": received_persistence_lock,
                "data_folder": data_folder,
            }
        )

    original_runner = (
        main.process_completed_futures
    )

    def recording_runner(
        *args,
        **kwargs,
    ):
        runner_calls.append(True)

        return original_runner(
            *args,
            **kwargs,
        )

    monkeypatch.setattr(
        main,
        "process_title",
        fake_process_title,
    )

    monkeypatch.setattr(
        main,
        "persist_canonical_evaluation",
        fake_persist_canonical_evaluation,
    )
    monkeypatch.setattr(
        main,
        "persist_evaluation_entry",
        lambda *args, **kwargs: pytest.fail(
            "evaluate_pages must not use legacy evaluation persistence"
        ),
        raising=False,
    )

    monkeypatch.setattr(
        main,
        "process_completed_futures",
        recording_runner,
    )

    main.evaluate_pages(
        pages=[
            {"title": "Ada Lovelace"},
            {"title": "Plato"},
        ],
        state=state,
        all_qids={},
        all_entities={},
        limiter=None,
        max_workers=1,
        data_folder="unused-in-test",
        stats_lock=provided_stats_lock,
        persistence_lock=provided_persistence_lock,
    )

    assert processed_titles == [
        "Ada Lovelace",
        "Plato",
    ]

    assert received_databases == [state.database, state.database]
    assert all(
        database is state.database
        for database in received_databases
    )
    assert received_data_folders == [
        "unused-in-test",
        "unused-in-test",
    ]

    assert [
        call["result"]["title"]
        for call in canonical_persistence_calls
    ] == ["Ada Lovelace", "Plato"]
    assert all(
        call["database"] is state.database
        and call["stats"] is state.stats
        and call["stats_lock"] is provided_stats_lock
        and call["persistence_lock"] is provided_persistence_lock
        and call["data_folder"] == "unused-in-test"
        for call in canonical_persistence_calls
    )

    assert runner_calls == [True]


def test_canonical_evaluation_persistence_does_not_write_legacy_result_files(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    results_path = tmp_path / "results.jsonl"
    processed_path = tmp_path / "processed.jsonl"
    results_path.write_bytes(b"legacy results sentinel\n")
    processed_path.write_bytes(b"legacy processed sentinel\n")
    results_before = results_path.read_bytes()
    processed_before = processed_path.read_bytes()
    state = main.RuntimeState(
        database={entry["title"]: entry},
        stats=main.make_initial_stats(),
    )

    monkeypatch.setattr(
        main,
        "process_title",
        lambda page, *args, **kwargs: {
            "title": page["title"],
            "status": "accepted",
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": 1,
            "reasons": ["accepted"],
            "last_processed": 123.0,
        },
    )
    monkeypatch.setattr(
        main,
        "persist_evaluation_entry",
        lambda *args, **kwargs: pytest.fail(
            "canonical completion persistence must not write legacy files"
        ),
        raising=False,
    )

    main.evaluate_pages(
        pages=[{"title": "Ada Lovelace"}],
        state=state,
        all_qids={},
        all_entities={},
        limiter=None,
        max_workers=1,
        data_folder=str(tmp_path),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert state.database["Ada Lovelace"]["evaluation"]["status"] == (
        "accepted"
    )
    assert results_path.read_bytes() == results_before
    assert processed_path.read_bytes() == processed_before


def test_evaluate_pages_continues_after_canonical_persistence_error(
    monkeypatch,
    tmp_path,
):
    entries = [
        make_empty_database_entry("Unsaved"),
        make_empty_database_entry("Saved"),
    ]
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries(entries)
    )
    state = main.RuntimeState(
        database={entry["title"]: entry for entry in entries},
        stats=main.make_initial_stats(),
    )
    errors = []
    original_persist = evaluation.persist_canonical_evaluation

    monkeypatch.setattr(
        main,
        "process_title",
        lambda page, *args, **kwargs: {
            "title": page["title"],
            "status": "accepted",
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": 1,
            "reasons": [page["title"]],
            "last_processed": 123.0,
        },
    )

    def fail_one_canonical_persistence(*args, **kwargs):
        result = args[0] if args else kwargs["result"]
        if result["title"] == "Unsaved":
            raise OSError("disk full")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(
        main,
        "persist_canonical_evaluation",
        fail_one_canonical_persistence,
    )
    monkeypatch.setattr(
        main,
        "report_processing_error",
        lambda title, error: errors.append((title, type(error), str(error))),
    )

    main.evaluate_pages(
        pages=[{"title": "Unsaved"}, {"title": "Saved"}],
        state=state,
        all_qids={},
        all_entities={},
        limiter=None,
        max_workers=1,
        data_folder=str(tmp_path),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert state.database["Unsaved"]["evaluation"]["status"] == (
        "unprocessed"
    )
    assert state.database["Saved"]["evaluation"]["status"] == "accepted"
    assert errors == [("Unsaved", OSError, "disk full")]

def test_select_candidate_passes_runtime_database(
    monkeypatch,
):
    state = main.RuntimeState(
        database={
            "Ada Lovelace": {
                "quotes": {"items": [{"text": "quote"}]},
            },
        },
        stats=main.make_initial_stats(),
    )

    captured = {}

    expected = {
        "title": "Ada Lovelace",
        "status": "accepted",
    }

    def fake_get_random_philosopher(database):
        captured["database"] = database

        return expected

    monkeypatch.setattr(
        main,
        "get_random_philosopher",
        fake_get_random_philosopher,
    )

    result = main.select_candidate(state)

    assert result is expected

    assert (
        captured["database"]
        is state.database
    )

def test_send_and_record_post_updates_canonical_posting_only_after_telegram_success(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "recognizable summary"
    before_summary = copy.deepcopy(entry["summary"])
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    sent_messages = []

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
    )

    result = main.send_and_record_post(
        title="Ada Lovelace",
        message="hello",
        database=database,
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
        send=lambda message: (
            sent_messages.append(message)
            or TelegramResult(
                ok=True,
                response_data={"ok": True},
                error_reason=None,
            )
        ),
    )

    assert result.ok is True
    assert sent_messages == ["hello"]
    assert database["Ada Lovelace"]["posting"] == {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": False,
    }
    assert database["Ada Lovelace"]["summary"] == before_summary
    assert main.load_database("database.jsonl", str(tmp_path))["Ada Lovelace"][
        "posting"
    ] == database["Ada Lovelace"]["posting"]


def test_send_and_record_post_does_not_update_canonical_posting_after_telegram_failure(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    before_database = serialize_database_entries(list(database.values()))
    before_disk = database_path.read_bytes()
    sent_messages = []

    monkeypatch.setattr(
        main,
        "persist_canonical_posting",
        lambda *args, **kwargs: pytest.fail(
            "canonical posting must not persist after Telegram failure"
        ),
    )

    result = main.send_and_record_post(
        title="Ada Lovelace",
        message="hello",
        database=database,
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
        send=lambda message: (
            sent_messages.append(message)
            or TelegramResult(
                ok=False,
                response_data=None,
                error_reason="http_error",
            )
        ),
    )

    assert result.ok is False
    assert sent_messages == ["hello"]
    assert serialize_database_entries(list(database.values())) == before_database
    assert database_path.read_bytes() == before_disk
    assert not (tmp_path / "posted.json").exists()


def test_send_and_record_post_preserves_historical_posting_marker(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
    )

    main.send_and_record_post(
        title="Ada Lovelace",
        message="hello",
        database=database,
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
        send=lambda message: TelegramResult(
            ok=True,
            response_data={"ok": True},
            error_reason=None,
        ),
    )

    assert database["Ada Lovelace"]["posting"] == {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": True,
    }


def test_send_and_record_post_surfaces_canonical_persistence_failure_after_telegram_success(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    before_database = serialize_database_entries(list(database.values()))
    before_disk = database_path.read_bytes()
    sent_messages = []

    monkeypatch.setattr(
        main,
        "persist_canonical_posting",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        main.send_and_record_post(
            title="Ada Lovelace",
            message="hello",
            database=database,
            persistence_lock=threading.Lock(),
            data_folder=str(tmp_path),
            send=lambda message: (
                sent_messages.append(message)
                or TelegramResult(
                    ok=True,
                    response_data={"ok": True},
                    error_reason=None,
                )
            ),
        )

    assert sent_messages == ["hello"]
    assert serialize_database_entries(list(database.values())) == before_database
    assert database_path.read_bytes() == before_disk
    assert not (tmp_path / "posted.json").exists()


def test_send_and_record_post_does_not_call_save_posted_titles(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "save_posted_titles",
        lambda *args, **kwargs: pytest.fail(
            "canonical post recording must not write posted.json"
        ),
        raising=False,
    )

    main.send_and_record_post(
        title="Ada Lovelace",
        message="hello",
        database=database,
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
        send=lambda message: TelegramResult(
            ok=True,
            response_data={"ok": True},
            error_reason=None,
        ),
    )

    assert database["Ada Lovelace"]["posting"]["has_been_posted"] is True


def test_persist_canonical_posting_sets_posted_state_after_success(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    assert database["Ada Lovelace"]["posting"] == {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": False,
    }
    assert main.load_database("database.jsonl", str(tmp_path))["Ada Lovelace"][
        "posting"
    ] == database["Ada Lovelace"]["posting"]


def test_persist_canonical_posting_appends_integer_timestamp(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "time",
        type(
            "FractionalTime",
            (),
            {"time": staticmethod(lambda: 1234567890.75)},
        )(),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    timestamp = database["Ada Lovelace"]["posting"]["posted_at"][0]

    assert timestamp == 1234567890
    assert isinstance(timestamp, int)
    assert not isinstance(timestamp, bool)


def test_persist_canonical_posting_preserves_historical_marker(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    assert database["Ada Lovelace"]["posting"] == {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": True,
    }


def test_persist_canonical_posting_preserves_existing_timestamps(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [100, 200],
        "legacy_posted_without_timestamp": False,
    }
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    assert database["Ada Lovelace"]["posting"]["posted_at"] == [
        100,
        200,
        1234567890,
    ]


def test_persist_canonical_posting_updates_only_posting_section(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "recognizable summary"
    entry["wikidata"]["status"] = "unavailable"
    entry["wikidata"]["reason"] = "no_qid"
    entry["quotes"]["status"] = "available"
    entry["quotes"]["items"] = []
    entry["evaluation"]["status"] = "accepted"
    entry["migration"]["legacy_sources"] = ["posted.json"]
    before = copy.deepcopy({
        section: entry[section]
        for section in (
            "summary",
            "wikidata",
            "quotes",
            "evaluation",
            "migration",
        )
    })
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "time",
        type("FixedTime", (), {"time": staticmethod(lambda: 1234567890)})(),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    assert {
        section: database["Ada Lovelace"][section]
        for section in before
    } == before


def test_persist_canonical_posting_rejects_missing_canonical_title(tmp_path):
    database = {}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([]))
    before = database_path.read_bytes()

    with pytest.raises(ValueError, match="canonical database"):
        main.persist_canonical_posting(
            "Absent",
            database,
            threading.Lock(),
            str(tmp_path),
        )

    assert database == {}
    assert database_path.read_bytes() == before


def test_persist_canonical_posting_failure_keeps_memory_and_disk_unchanged(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    database_path = tmp_path / "database.jsonl"
    database_path.write_bytes(serialize_database_entries([entry]))
    before_database = serialize_database_entries(list(database.values()))
    before_disk = database_path.read_bytes()

    monkeypatch.setattr(
        main,
        "update_database_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        raising=False,
    )

    with pytest.raises(OSError, match="disk full"):
        main.persist_canonical_posting(
            "Ada Lovelace",
            database,
            threading.Lock(),
            str(tmp_path),
        )

    assert serialize_database_entries(list(database.values())) == before_database
    assert database_path.read_bytes() == before_disk


def test_persist_canonical_posting_does_not_write_posted_json(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    (tmp_path / "database.jsonl").write_bytes(
        serialize_database_entries([entry])
    )

    monkeypatch.setattr(
        main,
        "save_posted_titles",
        lambda *args, **kwargs: pytest.fail(
            "canonical posting helper must not write posted.json"
        ),
        raising=False,
    )

    main.persist_canonical_posting(
        "Ada Lovelace",
        database,
        threading.Lock(),
        str(tmp_path),
    )

    assert database["Ada Lovelace"]["posting"]["has_been_posted"] is True
