import copy
import math

import pytest
import requests
import threading
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)


def write_canonical_database(tmp_path, entries):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", json_error=None, url=None):
        self.status_code = status_code
        self.payload = payload if payload is not None else {}
        self.text = text
        self.json_error = json_error
        self.url = url

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "params": params,
                "timeout": timeout,
            }
        )

        outcome = self.outcomes.pop(0)

        if isinstance(outcome, BaseException):
            raise outcome

        return outcome


class FakeLimiter:
    def __init__(self):
        self.calls = 0

    def wait(self):
        self.calls += 1


def test_quote_selection_weight_prefers_shorter_quotes_with_an_eight_word_floor():
    two_words = {"word_count": 2}
    eight_words = {"word_count": 8}
    twenty_words = {"word_count": 20}
    forty_words = {"word_count": 40}

    assert wikipedia_api.quote_selection_weight(two_words) == pytest.approx(
        wikipedia_api.quote_selection_weight(eight_words)
    )
    assert wikipedia_api.quote_selection_weight(eight_words) > (
        wikipedia_api.quote_selection_weight(twenty_words)
    )
    assert wikipedia_api.quote_selection_weight(twenty_words) > (
        wikipedia_api.quote_selection_weight(forty_words)
    )
    assert wikipedia_api.quote_selection_weight(forty_words) > 0
    assert wikipedia_api.quote_selection_weight({"word_count": 20}) == pytest.approx(
        1 / math.sqrt(20)
    )


@pytest.mark.parametrize("word_count", (None, "20", 20.0, True, False))
def test_quote_selection_weight_rejects_malformed_word_count(word_count):
    with pytest.raises(TypeError, match="quote word_count must be an integer"):
        wikipedia_api.quote_selection_weight({"word_count": word_count})


def test_get_random_quote_uses_weights_inside_existing_preferred_pool(monkeypatch):
    quotes = [
        {"text": "short", "word_count": 2, "source": {"work": "A"}, "retrieved_from": "Wikiquote"},
        {"text": "eight", "word_count": 8, "source": {"work": "B"}, "retrieved_from": "Wikiquote"},
        {"text": "medium", "word_count": 20, "source": {"work": "C"}, "retrieved_from": "Wikiquote"},
        {"text": "long", "word_count": 51, "source": {"work": "D"}, "retrieved_from": "Wikiquote"},
    ]
    before = copy.deepcopy(quotes)
    captured = {}

    monkeypatch.setattr(wikipedia_api, "get_quotes", lambda *args, **kwargs: quotes)

    def chooser(pool, weights, k):
        captured["pool"] = pool
        captured["weights"] = weights
        captured["k"] = k
        return [pool[-1]]

    selected = wikipedia_api.get_random_quote(
        "Ada", {}, {}, None, None, "unused", chooser=chooser,
    )

    assert captured["pool"] == quotes[:3]
    assert captured["weights"] == [
        wikipedia_api.quote_selection_weight(quote)
        for quote in quotes[:3]
    ]
    assert captured["k"] == 1
    assert selected is quotes[2]
    assert selected["source"] == {"work": "C"}
    assert selected["retrieved_from"] == "Wikiquote"
    assert quotes == before


def test_get_random_quote_uses_existing_all_quote_fallback_with_weights(monkeypatch):
    quotes = [
        {"text": "long", "word_count": 51},
        {"text": "longer", "word_count": 80},
    ]
    captured = {}
    monkeypatch.setattr(wikipedia_api, "get_quotes", lambda *args, **kwargs: quotes)

    def chooser(pool, weights, k):
        captured.update(pool=pool, weights=weights, k=k)
        return [pool[0]]

    assert wikipedia_api.get_random_quote(
        "Ada", {}, {}, None, None, "unused", chooser=chooser,
    ) is quotes[0]
    assert captured["pool"] == quotes
    assert captured["weights"] == [
        wikipedia_api.quote_selection_weight(quote) for quote in quotes
    ]

def test_safe_request_returns_successful_result():
    response = FakeResponse(status_code=200, payload={"ok": True})
    session = FakeSession([response])
    limiter = FakeLimiter()

    result = wikipedia_api.safe_request(
        "https://example.invalid",
        limiter=limiter,
        session=session,
        max_retries=3,
        sleep=lambda seconds: None,
        jitter=lambda start, end: 0,
    )

    assert result.ok is True
    assert result.response is response
    assert result.error_reason is None
    assert result.attempts == 1
    assert limiter.calls == 1
    assert len(session.calls) == 1

def test_safe_request_retries_then_returns_success(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "calculate_backoff",
        lambda attempt: 7,
    )

    session = FakeSession(
        [
            FakeResponse(status_code=503),
            FakeResponse(status_code=200, payload={"ok": True}),
        ]
    )
    sleep_calls = []

    result = wikipedia_api.safe_request(
        "https://example.invalid",
        session=session,
        max_retries=3,
        sleep=sleep_calls.append,
        jitter=lambda start, end: 0,
    )

    assert result.ok is True
    assert result.attempts == 2
    assert len(session.calls) == 2
    assert sleep_calls == [7]

def test_safe_request_returns_error_after_exhausted_retries(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "calculate_backoff",
        lambda attempt: 0,
    )

    final_response = FakeResponse(status_code=503)
    session = FakeSession(
        [
            FakeResponse(status_code=503),
            final_response,
        ]
    )

    result = wikipedia_api.safe_request(
        "https://example.invalid",
        session=session,
        max_retries=2,
        sleep=lambda seconds: None,
        jitter=lambda start, end: 0,
    )

    assert result.ok is False
    assert result.response is final_response
    assert result.error_reason == "http_503"
    assert result.attempts == 2

def test_safe_request_returns_error_after_request_exception(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "calculate_backoff",
        lambda attempt: 0,
    )

    session = FakeSession(
        [
            requests.Timeout("first timeout"),
            requests.Timeout("second timeout"),
        ]
    )

    result = wikipedia_api.safe_request(
        "https://example.invalid",
        session=session,
        max_retries=2,
        sleep=lambda seconds: None,
        jitter=lambda start, end: 0,
    )

    assert result.ok is False
    assert result.response is None
    assert result.error_reason == "request_exception"
    assert result.attempts == 2

def test_safe_request_returns_http_404_without_retry():
    response = FakeResponse(status_code=404)
    session = FakeSession([response])

    result = wikipedia_api.safe_request(
        "https://example.invalid",
        session=session,
        max_retries=3,
        sleep=lambda seconds: None,
        jitter=lambda start, end: 0,
    )

    assert result.ok is False
    assert result.response is response
    assert result.error_reason == "http_404"
    assert result.attempts == 1
    assert len(session.calls) == 1

def success_result(response, attempts=1):
    return wikipedia_api.RequestResult(
        response=response,
        error_reason=None,
        attempts=attempts,
    )

def test_get_all_pages_returns_empty_list_on_request_failure(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=None,
            error_reason="request_exception",
            attempts=3,
        ),
    )

    assert wikipedia_api.get_all_pages("philosopher") == []

def test_get_all_pages_returns_collected_pages_when_later_page_fails(
    monkeypatch,
):
    first_response = FakeResponse(
        payload={
            "query": {
                "search": [{"title": "Ada Lovelace"}],
            },
            "continue": {"continue": "next"},
        }
    )

    results = [
        success_result(first_response),
        wikipedia_api.RequestResult(
            response=None,
            error_reason="request_exception",
            attempts=3,
        ),
    ]

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: results.pop(0),
    )

    assert wikipedia_api.get_all_pages("philosopher") == [
        {"title": "Ada Lovelace"}
    ]

def test_get_all_pages_returns_empty_list_for_invalid_json(monkeypatch):
    response = FakeResponse(
        json_error=ValueError("invalid JSON"),
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == []

def test_get_all_pages_returns_empty_list_when_query_is_missing(monkeypatch):
    response = FakeResponse(payload={})

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == []

def test_get_all_pages_returns_empty_list_when_search_is_missing(
    monkeypatch,
):
    response = FakeResponse(
        payload={
            "query": {},
            "continue": {"continue": "next"},
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == []

def test_get_all_pages_returns_search_results(monkeypatch):
    response = FakeResponse(
        payload={
            "query": {
                "search": [
                    {"title": "Ada Lovelace"},
                    {"title": "Plato"},
                ]
            }
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == [
        {"title": "Ada Lovelace"},
        {"title": "Plato"},
    ]


def test_get_all_pages_ignores_malformed_search_members(monkeypatch):
    response = FakeResponse(
        payload={
            "query": {
                "search": [
                    {"title": "Valid One"},
                    None,
                    "not an object",
                    123,
                    {},
                    {"title": None},
                    {"title": ""},
                    {"title": "   "},
                    {"title": "Valid Two "},
                ]
            }
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == [
        {"title": "Valid One"},
        {"title": "Valid Two "},
    ]


def test_get_all_pages_returns_empty_when_all_search_members_are_malformed(
    monkeypatch,
):
    response = FakeResponse(
        payload={
            "query": {
                "search": [None, "not an object", {}, {"title": "   "}],
            }
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_all_pages("philosopher") == []


def test_get_all_pages_keeps_valid_members_across_malformed_paginated_pages(
    monkeypatch,
):
    first_response = FakeResponse(
        payload={
            "query": {"search": [{"title": "Valid One"}, None]},
            "continue": {"continue": "next"},
        }
    )
    second_response = FakeResponse(
        payload={
            "query": {"search": ["not an object", {"title": "Valid Two"}]},
        }
    )
    results = [success_result(first_response), success_result(second_response)]

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: results.pop(0),
    )

    assert wikipedia_api.get_all_pages("philosopher") == [
        {"title": "Valid One"},
        {"title": "Valid Two"},
    ]

def test_get_summary_uses_canonical_summary_cache_hit_without_http(monkeypatch):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "Ada Lovelace was a mathematician."
    database = {entry["title"]: entry}
    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail("cache hit must not fetch"),
    )

    result = wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        "unused-data-folder",
    )

    assert result == "Ada Lovelace was a mathematician."
    assert stats["cached_summaries"] == 1

def test_get_summary_failure_keeps_canonical_summary_null(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=None,
            error_reason="request_exception",
            attempts=3,
        ),
    )

    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}

    result = wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert result is None
    assert database["Ada Lovelace"]["summary"] == {
        "text": None,
        "source": "Wikipedia",
        "fetched_at": None,
    }
    assert stats["downloaded_summaries"] == 0

def test_get_summary_returns_none_for_invalid_json(monkeypatch, tmp_path):
    response = FakeResponse(json_error=ValueError("invalid JSON"))

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }

    database = {"Ada Lovelace": make_empty_database_entry("Ada Lovelace")}

    assert wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    ) is None
    assert database["Ada Lovelace"]["summary"]["fetched_at"] is None
    assert stats["downloaded_summaries"] == 0


def test_get_summary_returns_none_for_non_object_json(monkeypatch, tmp_path):
    response = FakeResponse(payload=["not", "an", "object"])
    database = {"Ada Lovelace": make_empty_database_entry("Ada Lovelace")}
    stats = {"cached_summaries": 0, "downloaded_summaries": 0}

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    assert wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    ) is None
    assert database["Ada Lovelace"]["summary"] == {
        "text": None,
        "source": "Wikipedia",
        "fetched_at": None,
    }
    assert stats["downloaded_summaries"] == 0

def test_get_summary_returns_none_when_extract_is_missing(monkeypatch, tmp_path):
    response = FakeResponse(payload={})

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }

    database = {"Ada Lovelace": make_empty_database_entry("Ada Lovelace")}

    assert wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    ) is None
    assert database["Ada Lovelace"]["summary"]["text"] is None
    assert stats["downloaded_summaries"] == 0

def test_get_summary_returns_none_when_extract_is_empty(monkeypatch, tmp_path):
    response = FakeResponse(
        payload={"extract": ""}
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }

    database = {"Ada Lovelace": make_empty_database_entry("Ada Lovelace")}

    result = wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert result is None
    assert database["Ada Lovelace"]["summary"]["text"] is None
    assert stats["downloaded_summaries"] == 0

def test_wikidata_qid_lookup_request_failure_is_distinguishable_from_empty_success(
    monkeypatch,
):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=None,
            error_reason="request_exception",
            attempts=3,
        ),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["Ada Lovelace"])

    assert result.data == {}
    assert result.error_reason == "request_exception"

def test_wikidata_qid_lookup_invalid_json_is_distinguishable_from_empty_success(
    monkeypatch,
):
    response = FakeResponse(json_error=ValueError("invalid JSON"))

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["Ada Lovelace"])

    assert result.data == {}
    assert result.error_reason == "invalid_json"


def test_wikidata_qid_lookup_success_without_qid_is_not_request_failure(
    monkeypatch,
):
    response = FakeResponse(
        payload={
            "query": {
                "pages": {
                    "1": {"title": "No QID"},
                }
            }
        }
    )
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["No QID"])

    assert result.data == {}
    assert result.error_reason is None

def test_get_wikidata_entities_batch_returns_empty_dict_when_entities_missing(
    monkeypatch,
):
    response = FakeResponse(payload={})

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_entities_batch(["Q7259"])

    assert result.data == {}
    assert result.error_reason == "malformed_response"


def test_get_wikidata_entities_batch_requests_claims_and_sitelinks(monkeypatch):
    captured = {}
    response = FakeResponse(payload={"entities": {"Q7259": {"claims": {}}}})

    def capture_request(url, params, limiter=None):
        captured["params"] = params
        return success_result(response)

    monkeypatch.setattr(wikipedia_api, "safe_request", capture_request)

    result = wikipedia_api.get_wikidata_entities_batch(["Q7259"])

    assert result.error_reason is None
    assert captured["params"]["props"] == "claims|sitelinks"

def test_build_entity_cache_keeps_successful_batches_on_partial_failure(
    monkeypatch,
):

    monkeypatch.setattr(
        wikipedia_api,
        "chunk_list",
        lambda items, size: [[item] for item in items],
    )

    id_batches = [
        wikipedia_api.BatchLookupResult(
            {"Ada Lovelace": "Q7259"}, None,
        ),
        wikipedia_api.BatchLookupResult({}, "request_exception"),
    ]
    entity_batches = [
        wikipedia_api.BatchLookupResult(
            {"Q7259": {"id": "Q7259"}}, None,
        ),
    ]

    monkeypatch.setattr(
        wikipedia_api,
        "get_wikidata_ids_batch",
        lambda *args, **kwargs: id_batches.pop(0),
    )
    monkeypatch.setattr(
        wikipedia_api,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: entity_batches.pop(0),
    )

    qids, entities, errors = wikipedia_api.build_entity_cache(
        ["Ada Lovelace", "Failure title"],
    )

    assert qids == {"Ada Lovelace": "Q7259"}
    assert entities == {"Q7259": {"id": "Q7259"}}
    assert errors == {"Failure title": "request_exception"}


def test_build_entity_cache_marks_titles_when_entity_lookup_fails(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "get_wikidata_ids_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {"Ada Lovelace": "Q7259"}, None,
        ),
    )
    monkeypatch.setattr(
        wikipedia_api,
        "get_wikidata_entities_batch",
        lambda *args, **kwargs: wikipedia_api.BatchLookupResult(
            {}, "request_exception",
        ),
    )

    qids, entities, errors = wikipedia_api.build_entity_cache(
        ["Ada Lovelace"],
    )

    assert qids == {"Ada Lovelace": "Q7259"}
    assert entities == {}
    assert errors == {"Ada Lovelace": "request_exception"}

def test_get_wikidata_ids_batch_returns_empty_dict_when_pages_missing(
    monkeypatch,
):
    response = FakeResponse(
        payload={"query": {}}
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["Ada Lovelace"])

    assert result.data == {}
    assert result.error_reason == "malformed_response"


def test_get_wikidata_ids_batch_ignores_non_object_pageprops(monkeypatch):
    response = FakeResponse(
        payload={
            "query": {
                "pages": {
                    "1": {
                        "title": "Valid Title",
                        "pageprops": {"wikibase_item": "Q123"},
                    },
                    "2": {"title": "Malformed None", "pageprops": None},
                    "3": {"title": "Malformed List", "pageprops": []},
                    "4": {"title": "Malformed String", "pageprops": "oops"},
                    "5": {"title": "No Pageprops"},
                }
            }
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["Valid Title"])

    assert result.data == {"Valid Title": "Q123"}
    assert result.error_reason is None


@pytest.mark.parametrize(
    ("pageprops", "expected_qid", "expected_disambiguation"),
    [
        ({"wikibase_item": "Q123"}, "Q123", False),
        ({"disambiguation": ""}, None, True),
        ({"wikibase_item": "Q123", "disambiguation": ""}, "Q123", True),
        ({}, None, False),
    ],
)
def test_get_page_properties_batch_distinguishes_qid_and_disambiguation(
    monkeypatch,
    pageprops,
    expected_qid,
    expected_disambiguation,
):
    response = FakeResponse(payload={
        "query": {"pages": {"1": {"title": "Alan White", "pageprops": pageprops}}}
    })
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_page_properties_batch(["Alan White"])

    assert result.error_reason is None
    properties = result.data["Alan White"]
    assert properties.qid == expected_qid
    assert properties.is_disambiguation is expected_disambiguation


@pytest.mark.parametrize(
    "request_result, expected_error",
    [
        (wikipedia_api.RequestResult(None, "request_exception", 1), "request_exception"),
        (success_result(FakeResponse(json_error=ValueError("bad json"))), "invalid_json"),
        (success_result(FakeResponse(payload={"query": {}})), "malformed_response"),
    ],
)
def test_get_page_properties_batch_keeps_page_type_unknown_on_errors(
    monkeypatch,
    request_result,
    expected_error,
):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: request_result,
    )

    result = wikipedia_api.get_page_properties_batch(["Alan White"])

    assert result.data == {}
    assert result.error_reason == expected_error


def test_get_page_properties_batch_treats_missing_pageprops_as_successful_absence(
    monkeypatch,
):
    response = FakeResponse(payload={
        "query": {"pages": {"1": {"title": "No Properties"}}}
    })
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_page_properties_batch(["No Properties"])

    assert result.error_reason is None
    assert result.data["No Properties"] == wikipedia_api.PageProperties(
        None, False,
    )


def test_get_years_from_wikidata_preserves_signed_leading_years():
    def time_claim(value):
        return {
            "mainsnak": {
                "datavalue": {
                    "value": {
                        "time": value,
                        "precision": 9,
                        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                    }
                }
            }
        }

    assert wikipedia_api.get_years_from_wikidata({
        "claims": {
            "P569": [time_claim("+1951-01-01T00:00:00Z")],
            "P570": [time_claim("-0548-00-00T00:00:00Z")],
        }
    }) == (1951, -548)


@pytest.mark.parametrize(
    ("time_value", "expected"),
    [
        ("-0650-00-00T00:00:00Z", -650),
        ("-0044-00-00T00:00:00Z", -44),
        ("+0005-00-00T00:00:00Z", 5),
        ("+0500-00-00T00:00:00Z", 500),
    ],
)
def test_get_years_from_wikidata_parses_only_signed_leading_year(
    time_value,
    expected,
):
    entity = {
        "claims": {
            "P569": [{"mainsnak": {"datavalue": {"value": {
                "time": time_value,
            }}}}],
        }
    }

    assert wikipedia_api.get_years_from_wikidata(entity) == (expected, None)


def test_get_years_from_wikidata_does_not_search_for_years_inside_other_text():
    entity = {
        "claims": {
            "P569": [{"mainsnak": {"datavalue": {"value": {
                "time": "about -0650-00-00T00:00:00Z",
            }}}}],
        }
    }

    assert wikipedia_api.get_years_from_wikidata(entity) == (None, None)


def wikidata_time_claim(time_value, rank="normal", snaktype="value"):
    claim = {"rank": rank, "mainsnak": {"snaktype": snaktype}}
    if snaktype == "value":
        claim["mainsnak"]["datavalue"] = {"value": {"time": time_value}}
    return claim


def test_select_wikidata_time_claim_selects_one_normal_statement():
    claim = wikidata_time_claim("+1951-00-00T00:00:00Z")

    assert wikipedia_api.select_wikidata_time_claim([claim]) is claim


def test_select_wikidata_time_claim_skips_deprecated_statement_before_normal():
    deprecated = wikidata_time_claim("+1932-05-12T00:00:00Z", "deprecated")
    normal = wikidata_time_claim("+1932-06-12T00:00:00Z", "normal")

    assert wikipedia_api.select_wikidata_time_claim([deprecated, normal]) is normal


def test_select_wikidata_time_claim_skips_deprecated_statement_after_normal():
    normal = wikidata_time_claim("+1932-06-12T00:00:00Z", "normal")
    deprecated = wikidata_time_claim("+1932-05-12T00:00:00Z", "deprecated")

    assert wikipedia_api.select_wikidata_time_claim([normal, deprecated]) is normal


def test_select_wikidata_time_claim_prefers_preferred_over_normal():
    normal = wikidata_time_claim("+1900-00-00T00:00:00Z", "normal")
    preferred = wikidata_time_claim("+1901-00-00T00:00:00Z", "preferred")

    assert wikipedia_api.select_wikidata_time_claim([normal, preferred]) is preferred


def test_select_wikidata_time_claim_returns_none_when_only_deprecated_exists():
    deprecated = wikidata_time_claim("+1900-00-00T00:00:00Z", "deprecated")

    assert wikipedia_api.select_wikidata_time_claim([deprecated]) is None


def test_select_wikidata_time_claim_skips_malformed_preferred_for_valid_normal():
    malformed_preferred = wikidata_time_claim("not-a-wikidata-time", "preferred")
    normal = wikidata_time_claim("-0044-00-00T00:00:00Z", "normal")

    assert wikipedia_api.select_wikidata_time_claim(
        [malformed_preferred, normal]
    ) is normal
    assert wikipedia_api.get_years_from_wikidata({
        "claims": {"P569": [malformed_preferred, normal]},
    }) == (-44, None)


def test_select_wikidata_time_claim_ignores_missing_and_non_value_snaks():
    missing_mainsnak = {"rank": "preferred"}
    some_value = {"rank": "preferred", "mainsnak": {"snaktype": "somevalue"}}
    normal = wikidata_time_claim("+1901-00-00T00:00:00Z", "normal")

    assert wikipedia_api.select_wikidata_time_claim(
        [missing_mainsnak, some_value, normal]
    ) is normal


def test_select_wikidata_time_claim_same_rank_uses_first_usable_response_order():
    first = wikidata_time_claim("+1900-00-00T00:00:00Z")
    second = wikidata_time_claim("+1901-00-00T00:00:00Z")

    assert wikipedia_api.select_wikidata_time_claim([first, second]) is first


def test_get_years_from_wikidata_ervin_laszlo_prefers_normal_over_deprecated():
    deprecated = wikidata_time_claim("+1932-05-12T00:00:00Z", "deprecated")
    normal = wikidata_time_claim("+1932-06-12T00:00:00Z", "normal")

    assert wikipedia_api.get_years_from_wikidata({
        "claims": {"P569": [deprecated, normal]},
    }) == (1932, None)


@pytest.mark.parametrize(
    ("time_value", "precision", "expected"),
    [
        ("+2026-06-29T00:00:00Z", 11, "2026-06-29"),
        ("+2026-00-00T00:00:00Z", 9, None),
        ("+2026-06-00T00:00:00Z", 10, None),
        ("-0044-03-15T00:00:00Z", 11, None),
    ],
)
def test_parse_wikidata_time_claim_exact_date_requires_supported_day_precision(
    time_value, precision, expected,
):
    claim = wikidata_time_claim(time_value)
    claim["mainsnak"]["datavalue"]["value"].update({
        "precision": precision,
        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
    })

    assert wikipedia_api.parse_wikidata_time_claim_exact_date(claim) == expected


def test_get_life_dates_from_wikidata_uses_same_rank_aware_death_claim():
    deprecated = wikidata_time_claim("+2026-05-12T00:00:00Z", "deprecated")
    normal = wikidata_time_claim("+2026-06-29T00:00:00Z", "normal")
    normal["mainsnak"]["datavalue"]["value"].update({
        "precision": 11,
        "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
    })

    assert wikipedia_api.get_life_dates_from_wikidata({
        "claims": {"P570": [deprecated, normal]},
    }) == (None, 2026, "2026-06-29")


def test_get_wikidata_ids_batch_ignores_non_object_page_entries(monkeypatch):
    response = FakeResponse(
        payload={
            "query": {
                "pages": {
                    "1": "not an object",
                    "2": {
                        "title": "Valid Title",
                        "pageprops": {"wikibase_item": "Q123"},
                    },
                }
            }
        }
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_ids_batch(["Valid Title"])

    assert result.data == {"Valid Title": "Q123"}
    assert result.error_reason is None

def test_get_wikidata_entities_batch_returns_empty_dict_for_invalid_json(
    monkeypatch,
):
    response = FakeResponse(
        json_error=ValueError("invalid JSON")
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    result = wikipedia_api.get_wikidata_entities_batch(["Q7259"])

    assert result.data == {}
    assert result.error_reason == "invalid_json"


def test_english_wikisource_url_requires_the_exact_wikidata_sitelink():
    entity = {
        "sitelinks": {
            "enwikisource": {"site": "enwikisource", "title": "Author:Ada Lovelace"},
        },
    }

    assert wikipedia_api.get_english_wikisource_sitelink(entity) == (
        "https://en.wikisource.org/wiki/Author:Ada_Lovelace"
    )
    assert wikipedia_api.get_english_wikisource_sitelink({"sitelinks": {}}) is None
    assert wikipedia_api.get_english_wikisource_sitelink({}) is None


def test_canonical_wikiquote_url_uses_only_the_final_fetched_page_identity():
    response = FakeResponse(url="https://en.wikiquote.org/wiki/Ada_Lovelace?oldformat=true#Quotes")

    assert wikipedia_api.canonical_wikiquote_page_url(response) == (
        "https://en.wikiquote.org/wiki/Ada_Lovelace"
    )
    assert wikipedia_api.canonical_wikiquote_page_url(FakeResponse()) is None


def test_read_only_wikiquote_external_link_lookup_requires_parse_and_uses_final_redirect(monkeypatch):
    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline">Quotes</span></h2>
      <ul><li>{}</li></ul>
    </div>
    """.format(SECTION_QUOTE)
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(
            text=html,
            url="https://en.wikiquote.org/wiki/Ada_(philosopher)?oldformat=true",
        )),
    )

    url, reason = wikipedia_api.lookup_wikiquote_external_link("Ada")

    assert (url, reason) == (
        "https://en.wikiquote.org/wiki/Ada_(philosopher)", None,
    )


def test_read_only_wikiquote_external_link_lookup_rejects_successful_but_empty_parse(monkeypatch):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(
            text='<div class="mw-parser-output"><p>No quotations.</p></div>',
            url="https://en.wikiquote.org/wiki/Ada",
        )),
    )

    assert wikipedia_api.lookup_wikiquote_external_link("Ada") == (
        None, wikipedia_api.QUOTE_FAILURE_NO_QUOTES_FOUND,
    )

@pytest.mark.parametrize(
    "reason, expected_days",
    [
        ("404", 30),
        (wikipedia_api.QUOTE_FAILURE_HTTP_404, 30),
        ("rate_limit", 1),
        (wikipedia_api.QUOTE_FAILURE_HTTP_429, 1),
        ("timeout", 7),
        (wikipedia_api.QUOTE_FAILURE_REQUEST_EXCEPTION, 7),
        (wikipedia_api.QUOTE_FAILURE_PARSING_ERROR, 14),
        (wikipedia_api.QUOTE_FAILURE_NO_QUOTES_FOUND, 60),
        ("unknown_old_reason", 30),
    ],
)
def test_quote_retry_base_days_supports_old_and_new_reasons(
    reason,
    expected_days,
):
    assert wikipedia_api.quote_retry_base_days(reason) == expected_days

def quote_stats():
    return {
        "cached_quotes": 0,
        "downloaded_quotes": 0,
        "failed_quotes": 0,
    }


def canonical_quote(title, text):
    return {
        "text": text,
        "length": len(text),
        "word_count": len(text.split()),
        "source": "Wikiquote",
    }


def current_parser_quote(text):
    return {
        "text": text,
        "length": len(text),
        "word_count": len(text.split()),
        "source": {
            "work": None, "year": None, "date": None, "details": None,
            "citation": None, "url": None,
        },
        "retrieved_from": "Wikiquote",
    }


def canonical_quote_database(title="Ada Lovelace", status="unknown", items=None):
    entry = make_empty_database_entry(title)
    entry["quotes"]["status"] = status
    entry["quotes"]["items"] = [] if items is None else items
    return {title: entry}


def test_get_quotes_uses_canonical_available_items_without_http(monkeypatch):
    quote_item = canonical_quote(
        "Ada Lovelace",
        "A sufficiently long synthetic quotation for canonical cache testing.",
    )
    database = canonical_quote_database(
        status="available",
        items=[quote_item],
    )
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail(
            "canonical available quotes must not make an HTTP request"
        ),
    )

    result = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        data_folder="unused",
    )

    assert result == [quote_item]
    assert stats["cached_quotes"] == 1


def test_get_quotes_preserves_historical_available_empty_items_cache_hit(
    monkeypatch,
):
    database = canonical_quote_database(status="available", items=[])
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail(
            "historical available empty quotes must remain a cache hit"
        ),
    )

    result = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        data_folder="unused",
    )

    assert result == []
    assert database["Ada Lovelace"]["quotes"]["status"] == "available"
    assert stats["cached_quotes"] == 1


def test_get_quotes_success_persists_canonical_available_section(
    monkeypatch,
    tmp_path,
):
    quote_text = (
        "A sufficiently long synthetic quotation with enough words for "
        "canonical quote persistence testing."
    )
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "Existing summary"
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(
            FakeResponse(
                text=(
                    '<div class="mw-parser-output"><ul><li>{}</li></ul></div>'
                ).format(quote_text)
            )
        ),
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)
    result = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert result == [current_parser_quote(quote_text)]
    assert database["Ada Lovelace"]["summary"]["text"] == "Existing summary"
    assert database["Ada Lovelace"]["quotes"] == {
        "status": "available",
        "items": [current_parser_quote(quote_text)],
        "failure": None,
        "fetched_at": 123,
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    }
    assert stats["downloaded_quotes"] == 1


@pytest.mark.parametrize(
    "reason",
    (
        "http_404",
        "http_429",
        "request_exception",
        "parsing_error",
    ),
)
def test_quote_failure_persists_canonical_failed_section(
    monkeypatch,
    tmp_path,
    reason,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = quote_stats()

    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 456)
    wikipedia_api.record_quote_failure(
        "Ada Lovelace",
        reason,
        retries=0,
        database=database,
        stats=stats,
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
    )

    assert database["Ada Lovelace"]["quotes"] == {
        "status": "failed",
        "items": [],
        "failure": {
            "reason": reason,
            "timestamp": 456,
            "retries": 1,
        },
        "fetched_at": None,
        "parser_version": None,
    }
    assert stats["failed_quotes"] == 1


def test_quote_failure_no_quotes_found_persists_not_found(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 456)
    wikipedia_api.record_quote_failure(
        "Ada Lovelace",
        "no_quotes_found",
        retries=0,
        database=database,
        stats=quote_stats(),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
    )

    assert database["Ada Lovelace"]["quotes"]["status"] == "not_found"
    assert database["Ada Lovelace"]["quotes"]["items"] == []


def test_quote_failure_preserves_known_available_items(monkeypatch, tmp_path):
    quote_item = canonical_quote(
        "Ada Lovelace",
        "A known quote must remain when a later request failure is recorded.",
    )
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"] = {
        "status": "available",
        "items": [quote_item],
        "failure": None,
        "fetched_at": 12,
        "parser_version": None,
    }
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 456)
    wikipedia_api.record_quote_failure(
        "Ada Lovelace",
        "http_429",
        retries=2,
        database=database,
        stats=quote_stats(),
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
    )

    assert database["Ada Lovelace"]["quotes"] == {
        "status": "available",
        "items": [quote_item],
        "failure": {
            "reason": "http_429",
            "timestamp": 456,
            "retries": 3,
        },
        "fetched_at": 12,
        "parser_version": None,
    }


def test_get_quotes_uses_canonical_failure_for_retry_suppression(
    monkeypatch,
):
    database = canonical_quote_database(status="failed")
    database["Ada Lovelace"]["quotes"]["failure"] = {
        "reason": "http_404",
        "timestamp": 100,
        "retries": 1,
    }
    stats = quote_stats()
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 101)
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail(
            "a non-retry-eligible canonical failure must suppress HTTP"
        ),
    )

    assert wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        "unused",
    ) == []
    assert database["Ada Lovelace"]["quotes"]["failure"] == {
        "reason": "http_404",
        "timestamp": 100,
        "retries": 1,
    }


def test_quote_persistence_failure_keeps_canonical_memory_unchanged(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    before = serialize_database_entries([entry])
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(
            FakeResponse(
                text=(
                    '<div class="mw-parser-output"><ul><li>'
                    "A sufficiently long quote for write failure testing."
                    "</li></ul></div>"
                )
            )
        ),
    )
    monkeypatch.setattr(
        wikipedia_api,
        "update_database_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("write failed")),
    )

    with pytest.raises(OSError, match="write failed"):
        wikipedia_api.get_quotes(
            "Ada Lovelace",
            database,
            stats,
            threading.Lock(),
            threading.Lock(),
            str(tmp_path),
        )

    assert database["Ada Lovelace"] == entry
    assert (tmp_path / "database.jsonl").read_bytes() == before
    assert stats["downloaded_quotes"] == 0

def test_get_quotes_records_request_failure_without_crashing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=None,
            error_reason="request_exception",
            attempts=3,
        ),
    )
    monkeypatch.setattr(
        wikipedia_api.time,
        "time",
        lambda: 100,
    )

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert quotes == []
    assert database["Ada Lovelace"]["quotes"]["failure"]["reason"] == "request_exception"

def test_get_quotes_records_http_404(monkeypatch, tmp_path):
    response = FakeResponse(status_code=404)

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=response,
            error_reason="http_404",
            attempts=1,
        ),
    )
    monkeypatch.setattr(
        wikipedia_api.time,
        "time",
        lambda: 100,
    )

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert quotes == []
    assert database["Ada Lovelace"]["quotes"]["failure"] == {
        "reason": "http_404",
        "timestamp": 100,
        "retries": 1,
    }

def test_get_quotes_records_parsing_error_when_main_content_missing(
    monkeypatch,
    tmp_path,
):
    response = FakeResponse(
        status_code=200,
        text="<html><body>No parser output here</body></html>",
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert quotes == []
    assert database["Ada Lovelace"]["quotes"]["failure"]["reason"] == "parsing_error"

def test_get_quotes_records_no_quotes_found(monkeypatch, tmp_path):
    response = FakeResponse(
        status_code=200,
        text=(
            '<div class="mw-parser-output">'
            "<ul><li>Too short</li></ul>"
            "</div>"
        ),
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert quotes == []
    assert database["Ada Lovelace"]["quotes"]["failure"]["reason"] == "no_quotes_found"

def test_get_quotes_prefers_cached_quotes_over_old_failure():
    cached_quotes = [
        {
            "text": "A sufficiently long synthetic quotation for testing.",
            "length": 52,
            "word_count": 8,
            "source": "Wikiquote",
        }
    ]

    database = canonical_quote_database(
        status="available",
        items=cached_quotes,
    )
    database["Ada Lovelace"]["quotes"]["failure"] = {
            "reason": "http_404",
            "timestamp": 100,
            "retries": 1,
    }

    stats = quote_stats()

    result = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        "unused",
    )

    assert result == cached_quotes
    assert stats["cached_quotes"] == 1

def test_get_quotes_preserves_all_valid_quotes_when_max_quotes_is_one(
    monkeypatch,
    tmp_path,
):
    quote_one = (
        "This is a sufficiently long quotation with clear punctuation, "
        "enough words, and content suitable for the current quote filter."
    )
    quote_two = (
        "Another sufficiently long quotation with clear punctuation, "
        "enough words, and content suitable for the current quote filter."
    )

    response = FakeResponse(
        status_code=200,
        text=(
            '<div class="mw-parser-output"><ul>'
            "<li>{}</li>"
            "<li>{}</li>"
            "</ul></div>"
        ).format(quote_one, quote_two),
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
        max_quotes=1,
    )

    assert len(quotes) == 2
    assert len(database["Ada Lovelace"]["quotes"]["items"]) == 2

def test_get_summary_canonical_persistence_failure_keeps_memory_unchanged(
    monkeypatch,
    tmp_path,
):

    response = FakeResponse(
        status_code=200,
        payload={
            "extract": "Ada Lovelace was a mathematician."
        },
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=response,
            error_reason=None,
            attempts=1,
        ),
    )

    def raise_os_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(wikipedia_api, "update_database_entry", raise_os_error)

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}

    stats = {
        "downloaded_summaries": 0,
        "cached_summaries": 0,
    }

    with pytest.raises(OSError, match="disk full"):
        wikipedia_api.get_summary(
            "Ada Lovelace",
            database,
            stats,
            threading.Lock(),
            threading.Lock(),
            str(tmp_path),
            limiter=None,
        )

    assert database["Ada Lovelace"]["summary"]["text"] is None
    assert stats["downloaded_summaries"] == 0

def test_get_summary_persists_success_to_canonical_summary_section(
    monkeypatch,
    tmp_path,
):
    import threading

    response = FakeResponse(
        status_code=200,
        payload={
            "extract": "Ada Lovelace was a mathematician."
        },
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123456)

    entry = make_empty_database_entry("Ada Lovelace")
    entry["wikidata"]["status"] = "available"
    entry["wikidata"]["qid"] = "Q7259"
    entry["wikidata"]["instances"] = ["Q5"]
    entry["wikidata"]["occupations"] = ["Q4964182"]
    entry["wikidata"]["is_human"] = True
    entry["wikidata"]["is_philosopher"] = True
    entry["wikidata"]["fetched_at"] = 4
    database = {entry["title"]: entry}
    path = write_canonical_database(tmp_path, [entry])
    unrelated_sections = {
        name: entry[name]
        for name in ("wikidata", "quotes", "evaluation", "posting", "migration")
    }
    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
    }
    stats_lock = threading.Lock()
    persistence_lock = threading.Lock()

    result = wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        stats_lock,
        persistence_lock,
        str(tmp_path),
    )

    assert result == "Ada Lovelace was a mathematician."
    assert database["Ada Lovelace"]["summary"] == {
        "text": "Ada Lovelace was a mathematician.",
        "source": "Wikipedia",
        "fetched_at": 123456,
    }
    assert all(
        database["Ada Lovelace"][name] == value
        for name, value in unrelated_sections.items()
    )
    assert stats["downloaded_summaries"] == 1
    assert path.read_bytes() == serialize_database_entries(
        list(database.values())
    )


def test_get_summary_does_not_append_legacy_summary_jsonl(monkeypatch, tmp_path):
    response = FakeResponse(
        status_code=200,
        payload={"extract": "Ada Lovelace was a mathematician."},
    )
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = {"cached_summaries": 0, "downloaded_summaries": 0}

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )
    assert wikipedia_api.get_summary(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    ) == "Ada Lovelace was a mathematician."
    assert database["Ada Lovelace"]["summary"]["text"] is not None

def test_get_quotes_does_not_append_legacy_quotes_jsonl(monkeypatch, tmp_path):
    import threading

    quote_text = (
        "This is a sufficiently long synthetic quotation "
        "with enough words and punctuation for testing."
    )

    response = FakeResponse(
        status_code=200,
        text=(
            '<div class="mw-parser-output">'
            "<ul>"
            "<li>{}</li>"
            "</ul>"
            "</div>"
        ).format(quote_text),
    )

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(response),
    )

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "persist_jsonl_cache_entry",
        lambda *args, **kwargs: pytest.fail("legacy quotes JSONL append"),
        raising=False,
    )

    quotes = wikipedia_api.get_quotes(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    assert len(quotes) == 1
    quote = database["Ada Lovelace"]["quotes"]["items"][0]

    assert quote["text"] == quote_text
    assert quote["source"] == current_parser_quote(quote_text)["source"]
    assert quote["retrieved_from"] == "Wikiquote"
    assert isinstance(quote["word_count"], int)
    assert isinstance(quote["length"], int)

    assert database["Ada Lovelace"]["quotes"]["items"] == quotes


def test_quote_failure_does_not_append_legacy_quote_failures_jsonl(
    monkeypatch,
    tmp_path,
):
    import threading

    monkeypatch.setattr(
        wikipedia_api.time,
        "time",
        lambda: 123,
    )

    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = quote_stats()

    monkeypatch.setattr(
        wikipedia_api,
        "persist_jsonl_cache_entry",
        lambda *args, **kwargs: pytest.fail("legacy quote failure JSONL append"),
        raising=False,
    )

    result = wikipedia_api.record_quote_failure(
        "Ada Lovelace",
        "http_404",
        retries=2,
        database=database,
        stats=stats,
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
        data_folder=str(tmp_path),
    )

    expected_failure = {
        "reason": "http_404",
        "timestamp": 123,
        "retries": 3,
    }

    assert result == []

    assert database["Ada Lovelace"]["quotes"]["failure"] == expected_failure
    assert stats["failed_quotes"] == 1


SECTION_QUOTE = (
    "A sufficiently long subject quotation with clear punctuation and enough "
    "words to pass the existing quote quality filter."
)


def get_quotes_from_section_html(monkeypatch, tmp_path, html, title="Ernst Mach"):
    entry = make_empty_database_entry(title)
    database = {title: entry}
    write_canonical_database(tmp_path, [entry])

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(text=html)),
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    return wikipedia_api.get_quotes(
        title,
        database,
        quote_stats(),
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )


def test_get_quotes_excludes_quotes_about_subject_but_keeps_subject_quotes(
    monkeypatch,
    tmp_path,
):
    commentary = (
        "Some Machians were sufficiently impressed by Einstein's interpretations "
        "of Brownian movement to accept atomism. Mach himself brushed such "
        "objections aside, and also emphatically rejected Einstein's relativity "
        "theory."
    )
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{subject_quote}</li></ul>
      <h2><span class="mw-headline" id="Quotes_about_Ernst_Mach">
        Quotes about Ernst Mach
      </span></h2>
      <ul><li>{commentary}</li></ul>
    </div>
    """.format(subject_quote=SECTION_QUOTE, commentary=commentary)

    quotes = get_quotes_from_section_html(monkeypatch, tmp_path, html)

    assert [item["text"] for item in quotes] == [SECTION_QUOTE]


@pytest.mark.parametrize(
    "heading_id, heading_text",
    [
        ("Quotes_about_Ernst_Mach", "Quotes about Ernst Mach"),
        ("About_Ernst_Mach", "About Ernst Mach"),
        ("Misattributed", "Misattributed"),
        ("See_also", "See also"),
        ("References", "References"),
        ("External_links", "External links"),
        ("Further_reading", "Further reading"),
        ("Bibliography", "Bibliography"),
        ("Notes", "Notes"),
        ("Sources", "Sources"),
    ],
)
def test_get_quotes_excludes_structural_non_quote_sections(
    monkeypatch,
    tmp_path,
    heading_id,
    heading_text,
):
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="{heading_id}">{heading_text}</span></h2>
      <ul><li>{quote}</li></ul>
    </div>
    """.format(
        heading_id=heading_id,
        heading_text=heading_text,
        quote=SECTION_QUOTE,
    )

    assert get_quotes_from_section_html(monkeypatch, tmp_path, html) == []


def test_get_quotes_keeps_allowed_and_unknown_nested_sections(monkeypatch, tmp_path):
    work_quote = SECTION_QUOTE.replace("subject", "work")
    unknown_quote = SECTION_QUOTE.replace("subject", "unknown-heading")
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="The_Analysis_of_Sensations">
        The Analysis of Sensations
      </span></h3>
      <ul><li>{work_quote}</li></ul>
      <h2><span class="mw-headline" id="Knowledge_and_Error">Knowledge and Error</span></h2>
      <ul><li>{unknown_quote}</li></ul>
    </div>
    """.format(work_quote=work_quote, unknown_quote=unknown_quote)

    quotes = get_quotes_from_section_html(monkeypatch, tmp_path, html)

    assert [item["text"] for item in quotes] == [work_quote, unknown_quote]


def test_get_quotes_excludes_descendants_of_excluded_section(monkeypatch, tmp_path):
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes_about_Ernst_Mach">
        Quotes about Ernst Mach
      </span></h2>
      <h3><span class="mw-headline" id="By_Albert_Einstein">By Albert Einstein</span></h3>
      <ul><li>{quote}</li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)

    assert get_quotes_from_section_html(monkeypatch, tmp_path, html) == []


def test_get_quotes_omits_nested_source_list_without_mutating_second_candidate(
    monkeypatch,
    tmp_path,
):
    quote_body = SECTION_QUOTE
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul>
        <li>{quote_body}<ul><li>Source: The Analysis of Sensations, p. 42.</li></ul></li>
      </ul>
    </div>
    """.format(quote_body=quote_body)

    quotes = get_quotes_from_section_html(monkeypatch, tmp_path, html)

    assert [item["text"] for item in quotes] == [quote_body]


def test_get_quotes_omits_sup_reference_from_quote_body(monkeypatch, tmp_path):
    quote_body = SECTION_QUOTE
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote_body}<sup>[1]</sup></li></ul>
    </div>
    """.format(quote_body=quote_body)

    quotes = get_quotes_from_section_html(monkeypatch, tmp_path, html)

    assert [item["text"] for item in quotes] == [quote_body]


def test_wikiquote_section_label_prefers_heading_id_and_normalizes_whitespace():
    from bs4 import BeautifulSoup

    heading = BeautifulSoup(
        '<h2 id="  Visible_Id  "><span class="mw-headline" '
        'id="Nested_Id">Visible Heading</span></h2>',
        "html.parser",
    ).h2

    assert wikipedia_api.get_wikiquote_section_label(heading) == "visible id"
    assert wikipedia_api.normalize_wikiquote_section_label(
        "  Quotes_about   Ernst Mach  "
    ) == "quotes about ernst mach"


def test_wikiquote_section_label_falls_back_to_headline_id_then_visible_text():
    from bs4 import BeautifulSoup

    headline_heading = BeautifulSoup(
        '<h2><span class="mw-headline" id="Further_reading">Read this</span></h2>',
        "html.parser",
    ).h2
    text_heading = BeautifulSoup("<h2>  Quotes   about Ada  </h2>", "html.parser").h2

    assert wikipedia_api.get_wikiquote_section_label(headline_heading) == "further reading"
    assert wikipedia_api.get_wikiquote_section_label(text_heading) == "quotes about ada"


def test_extract_wikiquote_candidate_text_does_not_mutate_source_tag():
    from bs4 import BeautifulSoup

    candidate = BeautifulSoup(
        "<li>{}<sup>[1]</sup><ul><li>Source: retained in source tag.</li></ul></li>".format(
            SECTION_QUOTE
        ),
        "html.parser",
    ).li
    original_html = str(candidate)

    assert wikipedia_api.extract_wikiquote_candidate_text(candidate) == SECTION_QUOTE
    assert str(candidate) == original_html


def test_current_parser_extracts_date_details_and_meaningful_source_link():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li><cite><a href="/wiki/Example_work">Example Work</a></cite>, 12 March 1932, Ch. 2.</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    content = BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(content))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source == {
        "work": "Example Work",
        "year": 1932,
        "date": "12 March 1932",
        "details": "Ch. 2",
        "citation": "Example Work, 12 March 1932, Ch. 2.",
        "url": "https://en.wikiquote.org/wiki/Example_work",
    }


@pytest.mark.parametrize(
    ("citation", "expected"),
    (
        (
            "Warranted Christian Belief. 2000. p. 145. ISBN 9780195131925.",
            {"work": "Warranted Christian Belief", "year": 2000, "details": "p. 145"},
        ),
        (
            "155d, The Dialogues of Plato, Volume 3, 1871, p. 377",
            {"work": "The Dialogues of Plato", "year": 1871, "details": "Volume 3, p. 377"},
        ),
        (
            "R. G. Collingwood (1937), as cited in: Patrick Suppes (1973), Logic and methodology.",
            {"work": None, "year": 1937, "details": None},
        ),
        (
            'R. G. Collingwood (1925). "Plato’s philosophy of art." In: Mind.',
            {"work": "Plato’s philosophy of art", "year": 1925, "details": None},
        ),
        (
            "Said at the Dominican Monastery of Latour-Maubourg (1948), p. 73.",
            {"work": None, "year": 1948, "details": "p. 73"},
        ),
        (
            "178c, M. Joyce, trans, Collected Dialogues of Plato (1961), p. 533",
            {
                "work": "Collected Dialogues of Plato",
                "year": 1961,
                "details": "178c, p. 533",
            },
        ),
        (
            "555c, G. Grube and C. Reeve, trans., Plato: Complete Works (1997), p. 1166",
            {
                "work": "Plato: Complete Works",
                "year": 1997,
                "details": "555c, p. 1166",
            },
        ),
        (
            "Robert M. Pirsig, Zen and the Art of Motorcycle Maintenance (1974)",
            {"work": None, "year": 1974, "details": None},
        ),
        (
            "Part 2: Metaphysical Rebellion; also quoted in Albert Camus: "
            "The Invincible Summer (1958) by Albert Maquet, p. 86; a remark "
            "made about the Marquis de Sade",
            {"work": None, "year": 1958, "details": "p. 86"},
        ),
    ),
)
def test_current_parser_uses_only_bounded_work_patterns(citation, expected):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["citation"] == citation
    assert source["url"] is None
    for field, value in expected.items():
        assert source[field] == value


@pytest.mark.parametrize(
    ("citation", "expected"),
    (
        (
            'Quoted by Baron John Campbell (1818), J. Murray in "The Lives '
            'of the Lord Chancellors and Keepers of the Great Seal of England"',
            {"work": None, "year": 1818, "date": None, "details": None},
        ),
        (
            "Lee Atwater as quoted anonymously by Alexander Lamis in The "
            "Two-Party South, (1984), as quoted in Secondary Source (2012)",
            {"work": None, "year": 1984, "date": None, "details": None},
        ),
        (
            "This is attributed to Pirsig by Richard Dawkins in the Preface "
            "to The God Delusion (2006), p. 28",
            {"work": None, "year": 2006, "date": None, "details": "p. 28"},
        ),
        (
            "Introduction to You Are Not The Target by Laura Archera Huxley (1963)",
            {"work": None, "year": 1963, "date": None, "details": None},
        ),
        (
            "Review: Sacred Causes by Michael Burleigh (2006-10-28)",
            {"work": None, "year": 2006, "date": "2006-10-28", "details": None},
        ),
        (
            "Controversial Kierkegaard by Gregor Malantschuk 1976, 1980 P. 25",
            {"work": None, "year": 1976, "date": None, "details": "P. 25"},
        ),
        (
            "As quoted without citation in Discovering Evolutionary Ecology: "
            "Bringing Together Ecology And Evolution (2006) by Peter J. Mayhew, p. 24",
            {"work": None, "year": 2006, "date": None, "details": "p. 24"},
        ),
    ),
)
def test_simple_title_with_year_fails_closed_for_provenance_and_by_author(citation, expected):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["citation"] == citation
    assert source["url"] is None
    for field, value in expected.items():
        assert source[field] == value


@pytest.mark.parametrize(
    ("citation", "expected"),
    (
        (
            "Written in a letter to Francois Lafargue in Bordeaux, "
            "12 November 1866 as published in MECW Volume 42, p. 334.",
            {"work": None, "year": 1866, "date": "12 November 1866", "details": "letter, Volume 42, p. 334"},
        ),
        (
            "Angelica Balabanoff My Life As a Rebel (1938)",
            {"work": None, "year": 1938, "date": None, "details": None},
        ),
        (
            "Ian Kochinski, 2023 stream.",
            {"work": None, "year": 2023, "date": None, "details": None},
        ),
        (
            "George Jackson (1994). Soledad Brother: The Prison Letters of George Jackson, p. 16.",
            {"work": None, "year": 1994, "date": None, "details": "Letter, p. 16"},
        ),
        (
            "José Guilherme Merquior (1985). Foucault, p. 57.",
            {"work": None, "year": 1985, "date": None, "details": "p. 57"},
        ),
        (
            "Nassim Nicholas Taleb (2010). The Bed of Procrustes: Philosophical and Practical Aphorisms, p. 25.",
            {"work": None, "year": 2010, "date": None, "details": "p. 25"},
        ),
        (
            "Ludwig von Mises (1957), Theory and History: An Interpretation of Social and Economic Evolution.",
            {"work": None, "year": 1957, "date": None, "details": None},
        ),
        (
            'Paul Samuelson (1962). "Economists and History of Ideas," The American Economic Review, March 1962.',
            {"work": None, "year": 1962, "date": None, "details": None},
        ),
        (
            "Werner Sombart (1896), Socialism and the Social System NY: Dutton and Sons, translated by M. Epstein, p. 87.",
            {"work": None, "year": 1896, "date": None, "details": "p. 87"},
        ),
        (
            'Thomas Sowell (1963) "Karl Marx and the Freedom of the Individual," Ethics 73:2, p 120.',
            {"work": None, "year": 1963, "date": None, "details": None},
        ),
        (
            "Simone Weil in Raymond Aron (1955, 2011). The Opium of the Intellectuals.",
            {"work": None, "year": 1955, "date": None, "details": None},
        ),
    ),
)
def test_simple_title_with_year_rejects_event_and_author_date_leads(citation, expected):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["citation"] == citation
    assert source["url"] is None
    for field, value in expected.items():
        assert source[field] == value


@pytest.mark.parametrize(
    ("citation", "expected"),
    (
        (
            'Adam Schaff (1947), cited in: Susan Petrilli and Augusto Ponzio (2007) "Adam Schaff: from Semantics to Political Semiotics."',
            {"work": None, "year": 1947, "details": None},
        ),
        (
            'This may have arisen as a paraphrase of statements found in The Myth of Sisyphus (1942), "An Absurd Reasoning", or one found in Another Work (1962) edited by John Cruikshank, p. 218',
            {"work": None, "year": 1942, "details": "p. 218"},
        ),
        (
            'Quoted from Ram Swarup (2000). On Hinduism: Reviews and reflections, p. 17',
            {"work": None, "year": 2000, "details": "p. 17"},
        ),
        (
            'No known citation to Thoreau\'s works. First found, uncredited, in the variant "Success usually comes to those who are too busy to look for it", p. 711, Locomotive Engineers Journal, Volume 76, 1942.',
            {"work": None, "year": 1942, "details": "p. 711, Volume 76"},
        ),
        (
            'Jeremy Bentham, (1882) H. N. Pym (ed.) Memories of Old Friends, being Extracts from the Journals and Letters of Caroline Fox',
            {"work": None, "year": 1882, "details": "Letter"},
        ),
        (
            'Muriel Rukeyser The Life of Poetry (1949)',
            {"work": None, "year": 1949, "details": None},
        ),
        (
            'Sharon Salzberg, describing how he "stood out", in his youth; as quoted in "Just say no to Jesus", Washington Post, 12 November 2006.',
            {"work": None, "year": 2006, "details": None},
        ),
        (
            'On her work All Men are Mortal in Force of Circumstances (1963), p. 73',
            {"work": None, "year": 1963, "details": "p. 73"},
        ),
        ('Journal entry (30 October 1958)', {"work": None, "year": 1958, "details": None}),
        ('His will (1626)', {"work": None, "year": 1626, "details": None}),
        ('"John Rivers" in The Genius and the Goddess (1955)', {"work": None, "year": 1955, "details": None}),
        ('John Rivers in The Genius and the Goddess (1955)', {"work": None, "year": 1955, "details": None}),
    ),
)
def test_parser_v8_rejects_residual_provenance_labels_and_fragments(citation, expected):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["citation"] == citation
    assert source["url"] is None
    for field, value in expected.items():
        assert source[field] == value


def test_parent_work_heading_strips_only_bounded_trailing_year():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="Why_Not_Socialism">Why Not Socialism? (2009)</span></h3>
      <h4><span class="mw-headline" id="Part_IV">IV. Is the Ideal Feasible?</span></h4>
      <ul><li>{quote}<ul><li>IV. Is the Ideal Feasible?</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    assert wikipedia_api.extract_wikiquote_quote_source(candidate) == {
        "work": "Why Not Socialism?", "year": 2009, "date": None,
        "details": None, "citation": "IV. Is the Ideal Feasible?", "url": None,
    }


@pytest.mark.parametrize(
    "subsection",
    ("Las Meninas", "Preface", "Foreword to the English edition"),
)
def test_current_parser_recovers_dated_parent_work_without_quotes_container(subsection):
    """A named child of a dated work is detail, even without a Quotes heading."""
    from bs4 import BeautifulSoup

    parent = "The Order of Things: An Archaeology of the Human Sciences (1970)"
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Order"><a href="/wiki/The_Order_of_Things">{parent}</a></span></h2>
      <h3><span class="mw-headline" id="Subsection">{subsection}</span></h3>
      <ul><li>{quote}<ul><li>{subsection}</li></ul></li></ul>
    </div>
    """.format(parent=parent, subsection=subsection, quote=SECTION_QUOTE)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    assert wikipedia_api.extract_wikiquote_quote_source(candidate) == {
        "work": "The Order of Things: An Archaeology of the Human Sciences",
        "year": 1970,
        "date": None,
        "details": subsection,
        "citation": subsection,
        "url": "https://en.wikiquote.org/wiki/The_Order_of_Things",
    }


def test_current_parser_keeps_mixed_named_and_structural_work_hierarchy_concise():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Work">Example Work (1970)</span></h2>
      <h3><span class="mw-headline" id="Part_I">Part I: Long explanatory part heading</span></h3>
      <h4><span class="mw-headline" id="Named">Named subsection</span></h4>
      <h5><span class="mw-headline" id="Section_3">§ 3: Long section heading</span></h5>
      <ul><li>{quote}<ul><li>§ 3</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)
    assert source["work"] == "Example Work"
    assert source["year"] == 1970
    assert source["details"] == "Part I, Named subsection, § 3"
    assert source["citation"] == "§ 3"
    assert source["url"] is None


def test_current_parser_never_uses_named_subsection_link_as_parent_work_url():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Work">Example Work (1970)</span></h2>
      <h3><span class="mw-headline" id="Preface"><a href="/wiki/Example_Work#Preface">Preface</a></span></h3>
      <ul><li>{quote}<ul><li>Preface</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)
    assert source["work"] == "Example Work"
    assert source["details"] == "Preface"
    assert source["url"] is None


def _live_shaped_work_quote_candidate(
    parent, subsection, *, href=None, include_quotes=True, heading_id="Work",
):
    """Build the modern Wikiquote list-based attribution shape."""
    from bs4 import BeautifulSoup

    if href:
        link_text = parent.rsplit(" (", 1)[0]
        suffix = parent[len(link_text):]
        heading = '<a href="{}">{}</a>{}'.format(href, link_text, suffix)
    else:
        heading = parent
    quotes_heading = '<h2 id="Quotes">Quotes</h2>' if include_quotes else ""
    # Intentionally leave meta unclosed: html.parser treats later content as
    # its children, matching the live mw:PageProp/toc traversal artifact.
    html = """
    <div class="mw-parser-output">
      {quotes_heading}
      <meta property="mw:PageProp/toc">
      <h3 id="{heading_id}">{heading}</h3>
      <ul><li>{quote}<ul><li>{subsection}</li></ul></li></ul>
    </div>
    """.format(heading=heading, heading_id=heading_id, quotes_heading=quotes_heading, quote=SECTION_QUOTE, subsection=subsection)
    content = BeautifulSoup(html, "html.parser").find(
        "div", class_="mw-parser-output",
    )
    return next(wikipedia_api.iter_wikiquote_quote_candidates(content))


def test_live_shaped_foucault_metadata_wrapper_recovers_parent_work_and_url():
    candidate = _live_shaped_work_quote_candidate(
        "The Order of Things: An Archaeology of the Human Sciences (1970)",
        "Las Meninas",
        href="/wiki/The_Order_of_Things",
    )

    assert [(item[1], item[2]) for item in candidate.active_headings] == [
        ("quotes", "Quotes"),
        ("work", "The Order of Things: An Archaeology of the Human Sciences (1970)"),
    ]
    assert wikipedia_api.extract_wikiquote_quote_source(candidate) == {
        "work": "The Order of Things: An Archaeology of the Human Sciences",
        "year": 1970,
        "date": None,
        "details": "Las Meninas",
        "citation": "Las Meninas",
        "url": "https://en.wikiquote.org/wiki/The_Order_of_Things",
    }


@pytest.mark.parametrize(
    ("parent", "subsection"),
    (
        ("The Rebel (1951)", "Introduction"),
        ("The Outsider (1956)", "Chapter one, The Country of the Blind"),
        ("Democracy's Discontent (1996)", "Preface"),
        ("The Second Sex (1949)", "Introduction: Woman as Other"),
        ("Eichmann in Jerusalem : A Report on the Banality of Evil (1963)", "Epilogue"),
        ("Superintelligence: Paths, Dangers, Strategies (2014)", "Preface"),
        ("Philosophy and the Mirror of Nature (1979)", "Preface"),
    ),
)
def test_live_shaped_nested_attribution_recovers_parent_work_details(parent, subsection):
    candidate = _live_shaped_work_quote_candidate(parent, subsection)
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    expected_work, expected_year = parent.rsplit(" (", 1)
    assert source["work"] == expected_work
    assert source["year"] == int(expected_year[:-1])
    assert source["details"] == subsection
    assert source["citation"] == subsection
    assert source["url"] is None


@pytest.mark.parametrize(
    ("parent", "citation"),
    (
        ("Themes", "Preface"),
        ("Michel Foucault", "Las Meninas"),
        ("Example Work (1970)", "This is a long commentary explaining why the quote matters in a historical context."),
        ("Example Work (1970)", "Quoted from a newspaper"),
        ("Example Work (1970)", "as cited in a later source"),
        ("Example Work (1970)", "translated by Example Contributor"),
    ),
)
def test_live_shaped_nested_attribution_fails_closed_without_safe_context(parent, citation):
    candidate = _live_shaped_work_quote_candidate(
        parent, citation, include_quotes=False,
    )
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] is None
    assert source["details"] is None


def test_live_shaped_general_sources_heading_never_becomes_work():
    citation = (
        "On her work All Men are Mortal in Force of Circumstances (1963), "
        "p. 73"
    )
    candidate = _live_shaped_work_quote_candidate(
        "General sources", citation, heading_id="General_sources",
    )

    assert wikipedia_api.extract_wikiquote_quote_source(candidate) == {
        "work": None,
        "year": 1963,
        "date": None,
        "details": "p. 73",
        "citation": citation,
        "url": None,
    }


def test_live_shaped_journal_entry_does_not_inherit_collection_parent_work():
    citation = "Journal entry, August 1, 1835"
    candidate = _live_shaped_work_quote_candidate(
        "The Journals of Søren Kierkegaard, 1830s", citation,
    )

    assert wikipedia_api.extract_wikiquote_quote_source(candidate) == {
        "work": None,
        "year": 1835,
        "date": "August 1, 1835",
        "details": None,
        "citation": citation,
        "url": None,
    }


def test_live_shaped_named_chapter_detail_deduplicates_compact_locator():
    citation = 'Chapter 1: "Propaganda in the History of Political Thought"'
    candidate = _live_shaped_work_quote_candidate(
        "How Propaganda Works (2015)", citation,
    )
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == "How Propaganda Works"
    assert source["year"] == 2015
    assert source["details"] == citation


@pytest.mark.parametrize(
    ("named", "compact", "expected"),
    (
        ('Chapter 3: "A Named Section"', "Ch. 3", 'Chapter 3: "A Named Section"'),
        ("Part I: Foundations", "Part I", "Part I: Foundations"),
        ("Book 2: Arguments", "Book 2", "Book 2: Arguments"),
        ("§ 5: Scope", "§ 5", "§ 5: Scope"),
    ),
)
def test_detail_merge_semantically_deduplicates_structural_locators(
    named, compact, expected,
):
    assert wikipedia_api._merge_quote_source_details(named, compact) == expected


@pytest.mark.parametrize(
    ("values", "expected"),
    (
        (
            (
                "1840s",
                "Søren Kierkegaard, Writing Sampler, Nichol P. 73",
                "P. 73",
            ),
            "1840s, Søren Kierkegaard, Writing Sampler, Nichol P. 73",
        ),
        (
            ("1840s", "P. 73-74 Hong", "P. 73-74"),
            "1840s, P. 73-74 Hong",
        ),
        (
            ("p. 73", "p. 80"),
            "p. 73, p. 80",
        ),
        (
            ("Chapter 1", "p. 73"),
            "Chapter 1, p. 73",
        ),
    ),
)
def test_detail_merge_deduplicates_only_equivalent_embedded_page_locators(
    values, expected,
):
    assert wikipedia_api._merge_quote_source_details(*values) == expected


def test_live_shaped_parent_year_beats_background_citation_year():
    citation = (
        'Chapter 6: "Classical Education". The term "civilization savagism '
        'paradigm" comes from David Wallace Adams\' book Education for '
        'Extinction: American Indians and the Boarding School Experience, '
        '1875–1928 (Kansas University Press, 1995).'
    )
    candidate = _live_shaped_work_quote_candidate("Erasing History (2024)", citation)
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == "Erasing History"
    assert source["year"] == 2024
    assert source["details"] == "Chapter 6"
    assert source["citation"] == citation


def test_live_shaped_parent_without_bibliographic_evidence_fails_closed():
    citation = "Chapter 6: cited background material from 1875."
    candidate = _live_shaped_work_quote_candidate("Erasing History", citation)
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] is None
    assert source["year"] is None


@pytest.mark.parametrize(
    ("parent", "citation", "expected_work", "expected_year"),
    (
        (
            "Others",
            "Schopenhauer and the Wild Years of Philosophy by Rüdiger Safranski (trans. Ewald Osers)",
            "Schopenhauer and the Wild Years of Philosophy",
            None,
        ),
        ("Others", "As quoted by others", None, None),
        (
            "Das Kapital (Buch III) (1894)",
            "Written in a letter to Francois Lafargue in Bordeaux, 12 November 1866",
            None,
            1866,
        ),
        ("Das Kapital (Buch III) (1894)", "As quoted by Plato in Cratylus", None, None),
        ("General sources", "Preface", None, None),
    ),
)
def test_parent_work_requires_compatible_citation_context(
    parent, citation, expected_work, expected_year,
):
    candidate = _live_shaped_work_quote_candidate(parent, citation)
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)
    assert source["work"] == expected_work
    assert source["year"] == expected_year


@pytest.mark.parametrize(
    "heading",
    (
        "Multi-Secularism: A New Agenda,",
        "Cool Memories (1987, trans. 1990)",
        "Bartlett's Familiar Quotations, 10th ed.",
        "A Work by An Author",
    ),
)
def test_heading_quality_guards_reject_unresolved_bibliographic_prose(heading):
    candidate = _live_shaped_work_quote_candidate(heading, "Preface")
    assert wikipedia_api.extract_wikiquote_quote_source(candidate)["work"] is None


@pytest.mark.parametrize(
    "citation",
    (
        "Book I, 1094b.24",
        "Book I, 1096a.5",
        "Book I, 1099b.22",
        "Book I, 1101a",
        "Book II, 1103a.33",
        "Book II, 1103b.4",
        "Book II, 1106b.28–1107a.3",
        "Book II, 1107a.4",
        "Book II, 1107a.15",
        "Book VIII, 1155a.26",
        "Book IX, 1168b.1",
        "Book X, 1177b.4",
        "Book IV, 1005",
        "1940s",
        "early 1920s",
        "1929p. 178",
    ),
)
def test_year_parser_rejects_locator_decade_and_attached_number_forms(citation):
    assert wikipedia_api._year_from_attribution(citation) is None


def test_year_parser_keeps_publication_year_after_non_locator_book_title():
    assert wikipedia_api._year_from_attribution(
        "Book Title, 2005 edition",
    ) == 2005


@pytest.mark.parametrize(
    ("parent", "citation", "expected_details"),
    (
        (
            "The World as Will and Representation",
            "Vol. I, Ch. III, The World As Representation: Second Aspect",
            "Vol. I",
        ),
        (
            "Parerga and Paralipomena",
            'Vol. II "On the Vanity and Suffering of Life"',
            "Vol. II",
        ),
    ),
)
def test_compact_structural_citation_retains_defensible_parent_work(
    parent, citation, expected_details,
):
    candidate = _live_shaped_work_quote_candidate(parent, citation)
    source = wikipedia_api.extract_wikiquote_quote_source(candidate)
    assert source["work"] == parent
    assert source["details"] == expected_details


@pytest.mark.parametrize(
    ("parent", "citation"),
    (
        ("Others", "As quoted by others"),
        ("Das Kapital (Buch III) (1894)", "Written in a letter to Francois Lafargue in Bordeaux, 12 November 1866"),
        ("Cratylus (385 BC)", "As quoted by Plato in Cratylus"),
    ),
)
def test_parent_work_vetoes_still_beat_compact_parent_admission(parent, citation):
    candidate = _live_shaped_work_quote_candidate(parent, citation)
    assert wikipedia_api.extract_wikiquote_quote_source(candidate)["work"] is None


@pytest.mark.parametrize(
    ("values", "expected"),
    (
        (("Section 1.1, \"Labor\"", "Section 1"), "Section 1.1, \"Labor\""),
        (("Book II, ch. III, section 13. Google Books", "section 13"), "Book II, ch. III, section 13. Google Books"),
        (("Section 2: Pride", "Section 2"), "Section 2: Pride"),
        (("Part One Chapter 6", "Chapter 6"), "Part One Chapter 6"),
        (("Book 5 Section 11", "Section 11"), "Book 5 Section 11"),
        (("Section 1", "Section 2"), "Section 1, Section 2"),
        (("Book 2", "Chapter 3"), "Book 2, Chapter 3"),
    ),
)
def test_detail_merge_deduplicates_contained_structural_locators(values, expected):
    assert wikipedia_api._merge_quote_source_details(*values) == expected


@pytest.mark.parametrize(
    ("heading", "citation"),
    (
        ("Michel Foucault", "Las Meninas"),
        ("Themes", "Preface"),
        ("Preface", "Preface"),
        ("Chapter 3", "Chapter 3"),
        ("Quoted from a newspaper", "Quoted from a newspaper"),
        ("Las Meninas", "Las Meninas"),
    ),
)
def test_current_parser_requires_bibliographic_parent_without_quotes_container(
    heading,
    citation,
):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Heading">{heading}</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(heading=heading, quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)
    assert source["work"] is None
    assert source["url"] is None
    assert source["citation"] == citation


@pytest.mark.parametrize(
    ("citation", "work"),
    (
        ('“The Pale Maiden” (1837) ballad', "The Pale Maiden"),
        ('Addenda, "Relative and Absolute Surplus Value" in Economic Manuscripts (1861-63)', "Relative and Absolute Surplus Value"),
        ('as quoted in "Nationalism", Author (2009). Original: Marx, "Zur Judenfrange" in "Werke", I, (1843).', "Zur Judenfrange"),
        ('Eugène Ionesco, as quoted in Jewish Literature (2000), "Jewish Humor", p. 318.', "Jewish Humor"),
        ('Deirdre McCloskey, "Economic Liberty as Anti-Flourishing: Marx and Especially His Followers" (2016)', "Economic Liberty as Anti-Flourishing: Marx and Especially His Followers"),
    ),
)
def test_marx_explicit_quoted_work_paths_remain_available(citation, work):
    assert wikipedia_api._work_from_citation(citation) == work


@pytest.mark.parametrize(
    ("citation", "work"),
    (
        ("Karl Marx: Letter to Engels, July 13, 1851, Marx and Engels: Collected Works", "Karl Marx: Letter to Engels"),
        ("The Abolition of Landed Property Letter to Robert Applegarth (3 December 1869)", "The Abolition of Landed Property Letter to Robert Applegarth"),
        ('Often attributed to Marx. According to the book, "They Never Said It", p. 64.', "They Never Said It"),
    ),
)
def test_marx_ambiguous_cases_keep_current_behavior(citation, work):
    assert wikipedia_api._work_from_citation(citation) == work


@pytest.mark.parametrize(
    ("citation", "work", "details"),
    (
        (
            "Vol. I, Ch. 4, The World As Will: Second Aspect, § 53, "
            "as translated by Eric F. J. Payne (1958)",
            None,
            "Vol. I, Ch. 4, § 53",
        ),
        (
            "Chapter 1: The Misanthropic Argument for Anti-natalism, "
            "2015, p. 55",
            None,
            "Chapter 1, p. 55",
        ),
        ("Book 1, sec. 55 (1887)", None, "Book 1, sec. 55"),
        (
            "Chapter 6: Classical Education, 1995, p. 12",
            None,
            "Chapter 6, p. 12",
        ),
        ("Ch. 59 as interpreted by Stephen Mitchell (1992)", None, "Ch. 59"),
        (
            "Vol. I, Book 2, Ch. 22, as translated by John Scott (1999)",
            None,
            "Vol. I, Book 2, Ch. 22",
        ),
        ("122, in Moral Exhortation, p. 33 (1986)", "Moral Exhortation", "122, p. 33"),
        ("A Plain Work, trans. J. Doe (1999)", "A Plain Work", None),
        ("J. Doe, trans., A Plain Work (1999)", "A Plain Work", None),
        ("A Plain Work, edited by J. Doe (1999)", "A Plain Work", None),
        (
            "Generation of Animals as translated by Arthur Leslie Peck "
            "(1943), p. 175",
            "Generation of Animals",
            "p. 175",
        ),
        (
            "Introduction in Justice (1993) edited by Alan Ryan",
            "Introduction in Justice",
            None,
        ),
        (
            '"The Letter of Aristotle to Alexander on the Policy toward the '
            'Cities", translated from Lettre d’Aristote, an Arabic text '
            "translated and edited by Józef Bielawski and Marian Plezia "
            "(1970), p. 72",
            "The Letter of Aristotle to Alexander on the Policy toward the Cities",
            "Letter, p. 72",
        ),
        ("translated by Lin Yutang (1948)", None, None),
        ("interpreted by Stephen Mitchell (1992)", None, None),
        ("Ch. 59 as interpreted by Stephen Mitchell (1992)", None, "Ch. 59"),
        (
            "Schopenhauer and the Wild Years of Philosophy by Rüdiger "
            "Safranski (trans. Ewald Osers)",
            "Schopenhauer and the Wild Years of Philosophy",
            None,
        ),
        ("A Plain Work by A. Writer (ed. J. Editor)", "A Plain Work", None),
        ("A Plain Work (trans. J. Doe)", "A Plain Work", None),
        ("A Plain Work (ed. J. Editor)", "A Plain Work", None),
        (
            "As attributed in Dictionary of Quotations from Ancient and "
            "Modern English and Foreign Sources (1899) by James Wood, p. 624",
            None,
            "p. 624",
        ),
        (
            "Unverified attribution noted in Respectfully Quoted: A Dictionary "
            "of Quotations (1993), ed. Suzy Platt, Library of Congress, p. 227",
            None,
            "p. 227",
        ),
        (
            "A long attribution sentence that explains where someone once "
            "heard the quotation and why it was repeated, 1999",
            None,
            None,
        ),
    ),
)
def test_current_parser_keeps_only_bounded_work_titles(citation, work, details):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == work
    assert source["details"] == details
    assert source["citation"] == citation


def test_current_parser_rejects_undelimited_author_plus_work_and_attributed_name():
    from bs4 import BeautifulSoup

    citations = (
        "Elizabeth Kolbert The Sixth Extinction: An Unnatural History (2015)",
        'Attributed to "Jimmy R." in Days of Healing, Days of Joy (1987)',
    )
    for citation in citations:
        html = """
        <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
          <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
        </div>
        """.format(quote=SECTION_QUOTE, citation=citation)
        candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
            BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
        ))
        source = wikipedia_api.extract_wikiquote_quote_source(candidate)
        assert source["work"] is None
        assert source["citation"] == citation


@pytest.mark.parametrize("candidate", ("Incomplete (", "Incomplete [", "Incomplete {"))
def test_clean_work_candidate_rejects_dangling_bibliographic_punctuation(candidate):
    assert wikipedia_api._clean_work_candidate(candidate) is None


@pytest.mark.parametrize(
    "citation",
    (
        "Primary Work (1999), quoted in Secondary Work (2001)",
        "Primary Work (1999), reprinted in Secondary Work (2001)",
        "Primary Work (1999), as cited in Secondary Work (2001)",
        "Primary Work (1999), also quoted in Secondary Work (2001)",
    ),
)
def test_current_parser_retains_only_bounded_primary_work_before_source_layer(citation):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == "Primary Work"
    assert source["citation"] == citation


def test_current_parser_keeps_explicit_long_quoted_title():
    from bs4 import BeautifulSoup

    title = (
        "The Kama Sutra of Vatsyayana: Translated from the Sanskrit. In seven "
        "parts, with preface, introduction, and concluding remarks"
    )
    citation = 'In: "{}", p. 18'.format(title)
    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert len(title) > 120
    assert source["work"] == title


def test_current_parser_does_not_promote_volume_part_section_hierarchy_to_work():
    from bs4 import BeautifulSoup

    citation = (
        "Vol. I: Part I: The Being and Attributes of God, § 1: Of the "
        "existence of God, and those attributes which art deduced from his "
        "being considered as uncaused himself, and the cause of every thing "
        "else (1772)"
    )
    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source == {
        "work": None,
        "year": 1772,
        "date": None,
        "details": "Vol. I, Part I, § 1",
        "citation": citation,
        "url": None,
    }


def test_current_parser_selects_parent_work_from_hierarchical_heading_stack():
    from bs4 import BeautifulSoup

    citation = (
        "Vol. I: Part I: The Being and Attributes of God, § 1: Of the "
        "existence of God, and those attributes which art deduced from his "
        "being considered as uncaused himself, and the cause of every thing "
        "else (1772)"
    )
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="Institutes"><a href="/wiki/Institutes_of_Natural_and_Revealed_Religion">Institutes of Natural and Revealed Religion</a></span></h3>
      <h4><span class="mw-headline" id="Vol_I">Vol. I</span></h4>
      <h5><span class="mw-headline" id="Part_I">Part I: The Being and Attributes of God</span></h5>
      <h6><span class="mw-headline" id="Section_1">§ 1: Of the existence of God</span></h6>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source == {
        "work": "Institutes of Natural and Revealed Religion",
        "year": 1772,
        "date": None,
        "details": "Vol. I, Part I, § 1",
        "citation": citation,
        "url": "https://en.wikiquote.org/wiki/Institutes_of_Natural_and_Revealed_Religion",
    }


def test_current_parser_keeps_structural_parent_work_without_a_heading_link():
    """A direct link is optional provenance, not evidence required for work."""
    from bs4 import BeautifulSoup

    citation = (
        "Vol. I: Part I: The Being and Attributes of God, § 1: Of the "
        "existence of God, and those attributes which art deduced from his "
        "being considered as uncaused himself, and the cause of every thing "
        "else (1772)"
    )
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="Institutes">Institutes of Natural and Revealed Religion</span></h3>
      <h4><span class="mw-headline" id="Vol_I">Vol. I</span></h4>
      <h5><span class="mw-headline" id="Part_I">Part I: The Being and Attributes of God</span></h5>
      <h6><span class="mw-headline" id="Section_1">§ 1: Of the existence of God</span></h6>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == "Institutes of Natural and Revealed Religion"
    assert source["details"] == "Vol. I, Part I, § 1"
    assert source["citation"] == citation
    assert source["url"] is None


@pytest.mark.parametrize(
    ("citation", "expected_work", "expected_year"),
    (
        ('"Helen\'s Exile" (1948)', "Helen's Exile", 1948),
        ('"Writer’s Work" (1950)', "Writer’s Work", 1950),
        ('258d; paraphrased by Robert M. Pirsig: "And what is good, Phaedrus …"', None, None),
        ('Book I; Sometimes paraphrased as "The first and best victory is to conquer self".', None, None),
    ),
)
def test_current_parser_requires_bibliographic_context_for_quoted_work_titles(
    citation,
    expected_work,
    expected_year,
):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == expected_work
    assert source["year"] == expected_year
    assert source["citation"] == citation
    assert source["url"] is None


@pytest.mark.parametrize(
    ("citation", "work", "year"),
    (
        ('"Entre oui et non" in L\'Envers et l\'endroit (1937)', "Entre oui et non", 1937),
        (
            '"Entre oui et non" in L\'Envers et l\'endroit (1937), '
            'also quoted in The Artist and Political Vision (1982)',
            "Entre oui et non",
            1937,
        ),
        ("Return to Tipasa (1954)", "Return to Tipasa", 1954),
    ),
)
def test_current_parser_preserves_known_good_work_citation_patterns(citation, work, year):
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>{citation}</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE, citation=citation)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == work
    assert source["year"] == year


def test_current_parser_keeps_specific_work_heading_fallback():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="The_Masters_of_Suspicion">The Masters of Suspicion</span></h3>
      <ul><li>{quote}<ul><li>p. 84</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    source = wikipedia_api.extract_wikiquote_quote_source(candidate)

    assert source["work"] == "The Masters of Suspicion"
    assert source["details"] == "p. 84"


def test_current_parser_does_not_promote_generic_heading_or_cross_reference_link():
    from bs4 import BeautifulSoup

    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="Interviews">Interviews</span></h3>
      <ul><li>{quote}</li></ul>
      <h3><span class="mw-headline" id="Works">Works</span></h3>
      <ul><li>{quote}<ul><li>258d; paraphrased by <a href="/wiki/Robert_M._Pirsig">Robert M. Pirsig</a>.</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    candidates = list(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(html, "html.parser").find("div", class_="mw-parser-output")
    ))

    generic_source = wikipedia_api.extract_wikiquote_quote_source(candidates[0])
    cross_reference_source = wikipedia_api.extract_wikiquote_quote_source(candidates[1])

    assert generic_source["work"] is None
    assert cross_reference_source["work"] is None
    assert cross_reference_source["url"] is None
    assert cross_reference_source["citation"] == "258d; paraphrased by Robert M. Pirsig."


def test_current_parser_keeps_unstructured_citation_and_sourceless_quote_valid():
    from bs4 import BeautifulSoup

    citation_html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li>Recorded in an uncertain archive fragment.</li></ul></li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)
    sourceless_html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}</li></ul>
    </div>
    """.format(quote=SECTION_QUOTE)

    citation_candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(citation_html, "html.parser").find("div", class_="mw-parser-output")
    ))
    sourceless_candidate = next(wikipedia_api.iter_wikiquote_quote_candidates(
        BeautifulSoup(sourceless_html, "html.parser").find("div", class_="mw-parser-output")
    ))

    assert wikipedia_api.extract_wikiquote_quote_source(citation_candidate) == {
        "work": None, "year": None, "date": None, "details": None,
        "citation": "Recorded in an uncertain archive fragment.", "url": None,
    }
    assert wikipedia_api.extract_wikiquote_quote_source(sourceless_candidate) == {
        "work": None, "year": None, "date": None, "details": None,
        "citation": None, "url": None,
    }


def test_get_quotes_current_parser_available_cache_is_authoritative(monkeypatch):
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    quote_item = current_parser_quote(SECTION_QUOTE)
    database = canonical_quote_database("Ada", status="available", items=[quote_item])
    database["Ada"]["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
    stats = quote_stats()
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail("current quote cache must not fetch"),
    )

    assert wikipedia_api.get_quotes(
        "Ada", database, stats, threading.Lock(), threading.Lock(), "unused"
    ) == [quote_item]
    assert stats["cached_quotes"] == 1


def test_purged_quotes_trigger_fresh_current_parser_fetch(monkeypatch, tmp_path):
    entry = make_empty_database_entry("Ada")
    entry["evaluation"]["status"] = "rejected"
    entry["quotes"].update({
        "status": "purged",
        "items": [],
        "failure": None,
        "fetched_at": None,
        "parser_version": None,
    })
    database = {"Ada": entry}
    write_canonical_database(tmp_path, [entry])
    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{}</li></ul>
    </div>
    """.format(SECTION_QUOTE)
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(text=html)),
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    quotes = wikipedia_api.get_quotes(
        "Ada", database, quote_stats(), threading.Lock(), threading.Lock(), str(tmp_path)
    )

    assert quotes
    assert database["Ada"]["quotes"]["status"] == "available"
    assert database["Ada"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION


def test_successful_quote_fetch_stores_only_the_final_identified_wikiquote_url(monkeypatch, tmp_path):
    entry = make_empty_database_entry("Ada")
    database = {"Ada": entry}
    write_canonical_database(tmp_path, [entry])
    html = """
    <div class="mw-parser-output"><h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{}</li></ul>
    </div>
    """.format(SECTION_QUOTE)
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(
            text=html,
            url="https://en.wikiquote.org/wiki/Ada_(philosopher)?oldformat=true",
        )),
    )

    wikipedia_api.get_quotes(
        "Ada", database, quote_stats(), threading.Lock(), threading.Lock(), str(tmp_path),
    )

    assert database["Ada"]["external_links"] == {
        "wikiquote": "https://en.wikiquote.org/wiki/Ada_(philosopher)",
        "wikisource": None,
        "project_gutenberg": None,
    }


def test_stale_ernst_mach_refresh_replaces_false_commentary_and_sets_parser_version(
    monkeypatch,
    tmp_path,
):
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    false_text = (
        "Some Machians were sufficiently impressed by Einstein's interpretations "
        "of Brownian movement to accept atomism. Mach himself brushed such "
        "objections aside, and also emphatically rejected Einstein's relativity theory."
    )
    valid_text = SECTION_QUOTE
    entry = make_empty_database_entry("Ernst Mach")
    entry["summary"]["text"] = "Preserved summary"
    entry["quotes"].update({
        "status": "available",
        "items": [canonical_quote("Ernst Mach", false_text)],
        "parser_version": None,
    })
    before_sections = {
        key: entry[key]
        for key in ("summary", "wikidata", "evaluation", "posting", "migration")
    }
    database = {"Ernst Mach": entry}
    write_canonical_database(tmp_path, [entry])
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{valid}</li></ul>
      <h2><span class="mw-headline" id="Quotes_about_Ernst_Mach">Quotes about Ernst Mach</span></h2>
      <ul><li>{false}</li></ul>
    </div>
    """.format(valid=valid_text, false=false_text)
    monkeypatch.setattr(
        wikipedia_api, "safe_request", lambda *args, **kwargs: success_result(FakeResponse(text=html))
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    quotes = wikipedia_api.get_quotes(
        "Ernst Mach", database, quote_stats(), threading.Lock(), threading.Lock(),
        str(tmp_path), refresh_stale=True,
    )

    assert [item["text"] for item in quotes] == [valid_text]
    assert database["Ernst Mach"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION
    assert false_text not in [item["text"] for item in database["Ernst Mach"]["quotes"]["items"]]
    for key, value in before_sections.items():
        assert database["Ernst Mach"][key] == value


@pytest.mark.parametrize(
    "result",
    [
        wikipedia_api.RequestResult(None, "request_exception", 1),
        wikipedia_api.RequestResult(FakeResponse(status_code=500), "http_500", 1),
        wikipedia_api.RequestResult(FakeResponse(text="<html>bad</html>"), None, 1),
    ],
)
def test_stale_quote_refresh_failure_preserves_known_items_and_version(
    monkeypatch,
    tmp_path,
    result,
):
    old_item = canonical_quote("Ada", SECTION_QUOTE)
    entry = make_empty_database_entry("Ada")
    entry["quotes"].update({"status": "available", "items": [old_item], "parser_version": None})
    database = {"Ada": entry}
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(wikipedia_api, "safe_request", lambda *args, **kwargs: result)
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    assert wikipedia_api.get_quotes(
        "Ada", database, quote_stats(), threading.Lock(), threading.Lock(),
        str(tmp_path), refresh_stale=True,
    ) == []
    assert database["Ada"]["quotes"]["status"] == "available"
    assert database["Ada"]["quotes"]["items"] == [old_item]
    assert database["Ada"]["quotes"].get("parser_version") is None


@pytest.mark.parametrize(
    "result",
    [
        wikipedia_api.RequestResult(None, "request_exception", 1),
        wikipedia_api.RequestResult(FakeResponse(status_code=500), "http_500", 1),
        wikipedia_api.RequestResult(FakeResponse(text="<html>bad</html>"), None, 1),
    ],
)
def test_current_quote_repair_failure_preserves_usable_current_cache(
    monkeypatch,
    tmp_path,
    result,
):
    old_item = current_parser_quote(SECTION_QUOTE)
    entry = make_empty_database_entry("Ada")
    entry["quotes"].update({
        "status": "available",
        "items": [old_item],
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    })
    database = {"Ada": entry}
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(wikipedia_api, "safe_request", lambda *args, **kwargs: result)
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    assert wikipedia_api.get_quotes(
        "Ada", database, quote_stats(), threading.Lock(), threading.Lock(),
        str(tmp_path), refresh_stale=True, refresh_current=True,
    ) == []
    assert database["Ada"]["quotes"]["status"] == "available"
    assert database["Ada"]["quotes"]["items"] == [old_item]
    assert database["Ada"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION


def test_stale_quote_refresh_successful_zero_result_becomes_current_not_found(
    monkeypatch,
    tmp_path,
):
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    entry = make_empty_database_entry("Ada")
    entry["quotes"].update({
        "status": "available", "items": [canonical_quote("Ada", SECTION_QUOTE)],
        "parser_version": None,
    })
    database = {"Ada": entry}
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: success_result(FakeResponse(
            text='<div class="mw-parser-output"><ul><li>Too short</li></ul></div>'
        )),
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    assert wikipedia_api.get_quotes(
        "Ada", database, quote_stats(), threading.Lock(), threading.Lock(),
        str(tmp_path), refresh_stale=True,
    ) == []
    assert database["Ada"]["quotes"] == {
        "status": "not_found",
        "items": [],
        "failure": {"reason": "no_quotes_found", "timestamp": 123, "retries": 1},
        "fetched_at": 123,
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
    }


def test_current_parser_extracts_nested_attribution_without_adding_it_to_quote_text(
    monkeypatch,
    tmp_path,
):
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    quote_text = SECTION_QUOTE
    citation = "The Analysis of Sensations (1886), Ch. 2"
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <ul><li>{quote}<ul><li><cite>{citation}</cite></li></ul></li></ul>
    </div>
    """.format(quote=quote_text, citation=citation)
    entry = make_empty_database_entry("Ernst Mach")
    database = {"Ernst Mach": entry}
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(
        wikipedia_api, "safe_request", lambda *args, **kwargs: success_result(FakeResponse(text=html))
    )
    monkeypatch.setattr(wikipedia_api.time, "time", lambda: 123)

    quotes = wikipedia_api.get_quotes(
        "Ernst Mach", database, quote_stats(), threading.Lock(), threading.Lock(), str(tmp_path)
    )

    assert quotes == [{
        "text": quote_text,
        "length": len(quote_text),
        "word_count": len(quote_text.split()),
        "source": {
            "work": "The Analysis of Sensations",
            "year": 1886,
            "date": None,
            "details": "Ch. 2",
            "citation": citation,
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }]
    assert database["Ernst Mach"]["quotes"]["parser_version"] == CURRENT_QUOTE_PARSER_VERSION


def test_current_parser_uses_work_heading_fallback_and_ignores_reference_links(
    monkeypatch,
    tmp_path,
):
    quote_text = SECTION_QUOTE
    html = """
    <div class="mw-parser-output">
      <h2><span class="mw-headline" id="Quotes">Quotes</span></h2>
      <h3><span class="mw-headline" id="The_Analysis_of_Sensations">The Analysis of Sensations</span></h3>
      <ul><li>{quote}<ul><li><a href="#cite_note-1">[1]</a></li></ul></li></ul>
    </div>
    """.format(quote=quote_text)
    entry = make_empty_database_entry("Ernst Mach")
    database = {"Ernst Mach": entry}
    write_canonical_database(tmp_path, [entry])
    monkeypatch.setattr(
        wikipedia_api, "safe_request", lambda *args, **kwargs: success_result(FakeResponse(text=html))
    )

    quote_item = wikipedia_api.get_quotes(
        "Ernst Mach", database, quote_stats(), threading.Lock(), threading.Lock(), str(tmp_path)
    )[0]

    assert quote_item["source"] == {
        "work": "The Analysis of Sensations", "year": None, "date": None,
        "details": None, "citation": None, "url": None,
    }
