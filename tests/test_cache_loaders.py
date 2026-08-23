import json
import cache
import pytest
from database_schema import make_empty_database_entry, serialize_database_entries


def write_canonical_database(tmp_path, entries):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path

def fake_persist_jsonl_cache_entry(
    cache,
    title,
    cache_value,
    filename,
    file_entry,
    data_folder,
    persistence_lock,
):
    cache[title] = cache_value

def write_jsonl(tmp_path, filename, records):
    path = tmp_path / filename
    content = "".join(
        json.dumps(record, ensure_ascii=False) + "\n"
        for record in records
    )
    path.write_text(content, encoding="utf-8")
    return path

def test_load_summary_cache_returns_text_values(tmp_path):
    write_jsonl(
        tmp_path,
        "summaries.jsonl",
        [
            {
                "title": "Ada Lovelace",
                "summary": "Ada Lovelace was a mathematician.",
            }
        ],
    )

    summaries = cache.load_summary_cache(
        "summaries.jsonl",
        str(tmp_path),
    )

    assert summaries == {
        "Ada Lovelace": "Ada Lovelace was a mathematician."
    }
    assert isinstance(summaries["Ada Lovelace"], str)

def test_load_quote_cache_returns_quote_lists(tmp_path):
    quote = {
        "text": "That brain of mine is something more than merely mortal.",
        "length": 58,
        "word_count": 10,
        "source": "Wikiquote",
    }

    write_jsonl(
        tmp_path,
        "quotes.jsonl",
        [{"title": "Ada Lovelace", "quotes": [quote]}],
    )

    quotes = cache.load_quote_cache("quotes.jsonl", str(tmp_path))

    assert quotes == {"Ada Lovelace": [quote]}
    assert isinstance(quotes["Ada Lovelace"], list)

def test_load_summary_cache_keeps_last_duplicate_record(tmp_path):
    write_jsonl(
        tmp_path,
        "summaries.jsonl",
        [
            {"title": "Ada Lovelace", "summary": "Old summary"},
            {"title": "Ada Lovelace", "summary": "New summary"},
        ],
    )

    summaries = cache.load_summary_cache(
        "summaries.jsonl",
        str(tmp_path),
    )

    assert summaries == {"Ada Lovelace": "New summary"}    

def test_legacy_summary_loader_remains_available_during_cutover(
    tmp_path,
):
    title = "Ada Lovelace"
    summary_text = "Ada Lovelace was a mathematician."

    write_jsonl(
        tmp_path,
        "summaries.jsonl",
        [{"title": title, "summary": summary_text}],
    )

    loaded_cache = cache.load_summary_cache(
        "summaries.jsonl",
        str(tmp_path),
    )

    assert loaded_cache[title] == summary_text
    assert isinstance(loaded_cache[title], str)

def test_loaded_legacy_and_fetched_current_quotes_keep_their_versioned_shapes(
    tmp_path,
    monkeypatch,
):
    import threading

    import wikipedia_api

    title = "Ada Lovelace"
    quote_text = (
        "That brain of mine is something more than merely mortal, "
        "as time will surely prove beyond all reasonable doubt."
    )
    quote_record = {
        "text": quote_text,
        "length": len(quote_text),
        "word_count": len(quote_text.split()),
        "source": "Wikiquote",
    }

    write_jsonl(
        tmp_path,
        "quotes.jsonl",
        [{"title": title, "quotes": [quote_record]}],
    )

    loaded_cache = cache.load_quote_cache(
        "quotes.jsonl",
        str(tmp_path),
    )

    class FakeResponse:
        status_code = 200
        text = (
            '<div class="mw-parser-output">'
            "<ul>"
            f"<li>{quote_text}</li>"
            "</ul>"
            "</div>"
        )

    fake_response = FakeResponse()

    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: wikipedia_api.RequestResult(
            response=fake_response,
            error_reason=None,
            attempts=1,
        ),
    )

    entry = make_empty_database_entry(title)
    database = {title: entry}
    write_canonical_database(tmp_path, [entry])
    stats = {
        "cached_quotes": 0,
        "downloaded_quotes": 0,
        "failed_quotes": 0,
    }

    fetched_quotes = wikipedia_api.get_quotes(
        title,
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    current_quote_record = {
        "text": quote_text,
        "length": len(quote_text),
        "word_count": len(quote_text.split()),
        "source": {
            "work": None,
            "year": None,
            "date": None,
            "details": None,
            "citation": None,
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }

    assert loaded_cache[title] == [quote_record]
    assert fetched_quotes == [current_quote_record]
    assert database[title]["quotes"]["items"] == [current_quote_record]
    assert isinstance(loaded_cache[title], list)

def test_record_loaders_keep_complete_latest_records(tmp_path):
    entity_old = {
        "title": "Ada Lovelace",
        "valid": False,
        "reason": "no_entity",
    }
    entity_new = {
        "title": "Ada Lovelace",
        "valid": True,
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": [],
        "birth": 1815,
        "death": 1852,
        "is_human": True,
        "is_philosopher": False,
    }

    failure_old = {
        "title": "Ada Lovelace",
        "reason": "http_404",
        "timestamp": 1,
        "retries": 1,
    }
    failure_new = {
        "title": "Ada Lovelace",
        "reason": "no_quotes_found",
        "timestamp": 2,
        "retries": 2,
    }

    historical_accepted = {
        "title": "Historical",
        "accepted": True,
        "reasons": [],
    }
    current_rejected = {
        "title": "Current",
        "status": "rejected",
        "reasons": [],
        "last_processed": 1,
    }

    write_jsonl(
        tmp_path,
        "entities.jsonl",
        [entity_old, entity_new],
    )
    write_jsonl(
        tmp_path,
        "quote_failures.jsonl",
        [failure_old, failure_new],
    )
    write_jsonl(
        tmp_path,
        "results.jsonl",
        [historical_accepted],
    )
    write_jsonl(
        tmp_path,
        "processed.jsonl",
        [current_rejected],
    )

    assert cache.load_entity_cache(
        "entities.jsonl",
        str(tmp_path),
    ) == {"Ada Lovelace": entity_new}

    assert cache.load_quote_failure_cache(
        "quote_failures.jsonl",
        str(tmp_path),
    ) == {"Ada Lovelace": failure_new}

    assert cache.load_result_cache(
        "results.jsonl",
        str(tmp_path),
    ) == {"Historical": historical_accepted}

    assert cache.load_processed_cache(
        "processed.jsonl",
        str(tmp_path),
    ) == {"Current": current_rejected}

def test_posted_titles_load_and_save_as_strings_only(tmp_path):
    path = tmp_path / "posted.json"
    path.write_text(
        '["Ada Lovelace", "Simone de Beauvoir"]',
        encoding="utf-8",
    )

    posted_titles = cache.load_posted_titles(
        "posted.json",
        str(tmp_path),
    )

    assert posted_titles == [
        "Ada Lovelace",
        "Simone de Beauvoir",
    ]

    posted_titles.append("Iris Murdoch")

    cache.save_posted_titles(
        "posted.json",
        posted_titles,
        str(tmp_path),
    )

    saved_value = json.loads(path.read_text(encoding="utf-8"))

    assert saved_value == [
        "Ada Lovelace",
        "Simone de Beauvoir",
        "Iris Murdoch",
    ]
    assert all(isinstance(title, str) for title in saved_value)

def test_load_posted_titles_rejects_dictionary_items(tmp_path):
    path = tmp_path / "posted.json"
    path.write_text(
        '[{"title": "Ada Lovelace", "timestamp": 1}]',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="title string"):
        cache.load_posted_titles("posted.json", str(tmp_path))


def test_missing_posted_titles_file_returns_empty_list(tmp_path):
    assert cache.load_posted_titles(
        "posted.json",
        str(tmp_path),
    ) == []
