import sys
import copy
import pytest
import importlib
import wiki_philosopher_bot.cli.migrate_database as migrate_database
from wiki_philosopher_bot.cli.migrate_database import (
    SOURCE_ORDER,
    build_title_index,
    make_empty_database_entry,
    merge_summary_records,
    merge_entity_records,
)
from wiki_philosopher_bot.config import (
    ENTITY_FILE,
    POSTED_FILE,
    PROCESSED_FILE,
    QUOTE_FAILURE_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)

def test_importing_writable_migration_module_has_no_side_effects(
    tmp_path,
):
    before = list(tmp_path.iterdir())

    module_name = "wiki_philosopher_bot.cli.migrate_database"
    sys.modules.pop(module_name, None)

    module = importlib.import_module(module_name)

    after = list(tmp_path.iterdir())

    assert before == after
    assert callable(module.main)

def make_empty_sources():
    return {
        filename: {"records": []}
        for filename in SOURCE_ORDER
    }

def test_build_title_index_preserves_duplicate_jsonl_records():
    first = {
        "line_number": 1,
        "record_index": 1,
        "value": {
            "title": "Ada Lovelace",
            "quotes": [{"text": "First"}],
        },
    }

    second = {
        "line_number": 2,
        "record_index": 2,
        "value": {
            "title": "Ada Lovelace",
            "quotes": [{"text": "Second"}],
        },
    }

    sources = make_empty_sources()

    sources[QUOTE_FILE]["records"] = [
        first,
        second,
    ]

    index = build_title_index(sources)

    assert index["Ada Lovelace"][QUOTE_FILE] == [
        first,
        second,
    ]

    assert (
        index["Ada Lovelace"][QUOTE_FILE][0]["line_number"]
        == 1
    )

    assert (
        index["Ada Lovelace"][QUOTE_FILE][1]["line_number"]
        == 2
    )

def test_build_title_index_includes_posted_only_title():
    posted_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Only Posted",
    }

    sources = make_empty_sources()

    sources[POSTED_FILE]["records"] = [
        posted_record
    ]

    index = build_title_index(sources)

    assert "Only Posted" in index

    assert index["Only Posted"] == {
        POSTED_FILE: [posted_record]
    }

def test_build_title_index_does_not_trim_or_merge_distinct_titles():
    first = {
        "line_number": 1,
        "record_index": 1,
        "value": {
            "title": "Ada Lovelace",
            "summary": "First",
        },
    }

    second = {
        "line_number": 2,
        "record_index": 2,
        "value": {
            "title": "Ada Lovelace ",
            "summary": "Second",
        },
    }

    sources = make_empty_sources()

    sources[SUMMARY_FILE]["records"] = [
        first,
        second,
    ]

    index = build_title_index(sources)

    assert "Ada Lovelace" in index
    assert "Ada Lovelace " in index
    assert len(index) == 2

def test_build_title_index_rejects_record_without_valid_title():
    sources = make_empty_sources()

    sources[SUMMARY_FILE]["records"] = [
        {
            "line_number": 7,
            "record_index": 6,
            "value": {
                "summary": "Missing title"
            },
        }
    ]

    with pytest.raises(
        ValueError,
        match="Invalid title",
    ):
        build_title_index(sources)

def test_merge_summary_sets_text_and_source():
    entry = make_empty_database_entry("Ada Lovelace")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Ada Lovelace",
                "summary": "Ada Lovelace was a mathematician.",
            },
        }
    ]

    merge_summary_records(entry, records)

    assert entry["summary"] == {
        "text": "Ada Lovelace was a mathematician.",
        "source": "Wikipedia",
        "fetched_at": None,
    }

    assert entry["migration"]["legacy_sources"] == [
        SUMMARY_FILE
    ]

def test_merge_identical_summary_duplicates_once():
    entry = make_empty_database_entry("Ada Lovelace")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Ada Lovelace",
                "summary": "Same summary.",
            },
        },
        {
            "line_number": 2,
            "record_index": 2,
            "value": {
                "title": "Ada Lovelace",
                "summary": "Same summary.",
            },
        },
    ]

    merge_summary_records(entry, records)

    assert entry["summary"]["text"] == "Same summary."
    assert entry["migration"]["conflicts"] == []

def test_merge_conflicting_summary_duplicates_sets_text_null():
    entry = make_empty_database_entry("Ada Lovelace")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Ada Lovelace",
                "summary": "First summary.",
            },
        },
        {
            "line_number": 2,
            "record_index": 2,
            "value": {
                "title": "Ada Lovelace",
                "summary": "Second summary.",
            },
        },
    ]

    merge_summary_records(entry, records)

    assert entry["summary"]["text"] is None

    assert len(entry["migration"]["conflicts"]) == 1
    assert (
        entry["migration"]["conflicts"][0]["field"]
        == "summary.text"
    )

def test_merge_valid_entity_maps_birth_and_death_years():
    entry = make_empty_database_entry("Ada Lovelace")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Ada Lovelace",
                "valid": True,
                "qid": "Q7259",
                "instances": ["Q5"],
                "occupations": ["Q170790"],
                "birth": 1815,
                "death": 1852,
                "is_human": True,
                "is_philosopher": False,
            },
        }
    ]

    merge_entity_records(entry, records)

    assert entry["wikidata"]["status"] == "available"
    assert entry["wikidata"]["qid"] == "Q7259"
    assert entry["wikidata"]["birth_year"] == 1815
    assert entry["wikidata"]["death_year"] == 1852

def test_merge_invalid_no_qid_entity_sets_unavailable_status():
    entry = make_empty_database_entry("Unknown Person")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Unknown Person",
                "valid": False,
                "reason": "no_qid",
            },
        }
    ]

    merge_entity_records(entry, records)

    assert entry["wikidata"]["status"] == "unavailable"
    assert entry["wikidata"]["reason"] == "no_qid"
    assert entry["wikidata"]["qid"] is None

def test_merge_invalid_no_entity_preserves_qid_and_reason():
    entry = make_empty_database_entry("Unknown Person")

    records = [
        {
            "line_number": 1,
            "record_index": 1,
            "value": {
                "title": "Unknown Person",
                "valid": False,
                "reason": "no_entity",
                "qid": "Q123456",
            },
        }
    ]

    merge_entity_records(entry, records)

    assert entry["wikidata"]["status"] == "unavailable"
    assert entry["wikidata"]["reason"] == "no_entity"
    assert entry["wikidata"]["qid"] == "Q123456"


def make_quote_record(line_number, record_index, title, quotes):
    return {
        "line_number": line_number,
        "record_index": record_index,
        "value": {
            "title": title,
            "quotes": quotes,
        },
    }


def make_quote_failure_record(
    line_number,
    record_index,
    title,
    reason,
    timestamp,
    retries,
):
    return {
        "line_number": line_number,
        "record_index": record_index,
        "value": {
            "title": title,
            "reason": reason,
            "timestamp": timestamp,
            "retries": retries,
        },
    }


def test_identical_duplicate_quote_records_do_not_duplicate_quote_items():
    entry = make_empty_database_entry("Ada Lovelace")
    quote = {
        "text": "A sufficiently long quote for migration testing.",
        "length": 47,
        "word_count": 7,
        "source": "Wikiquote",
    }

    records = [
        make_quote_record(10, 1, "Ada Lovelace", [quote]),
        make_quote_record(11, 2, "Ada Lovelace", [quote]),
    ]

    migrate_database.merge_quote_records(entry, records)

    assert entry["quotes"]["status"] == "available"
    assert entry["quotes"]["items"] == [quote]
    assert entry["migration"]["conflicts"] == []
    assert entry["migration"]["legacy_sources"] == [
        QUOTE_FILE
    ]


def test_distinct_duplicate_quote_records_union_unique_quote_items():
    entry = make_empty_database_entry("Ada Lovelace")
    quote_a = {
        "text": "Quote A is sufficiently long for migration testing.",
        "length": 51,
        "word_count": 8,
        "source": "Wikiquote",
    }
    quote_b = {
        "text": "Quote B is sufficiently long for migration testing.",
        "length": 51,
        "word_count": 8,
        "source": "Wikiquote",
    }
    quote_c = {
        "text": "Quote C is sufficiently long for migration testing.",
        "length": 51,
        "word_count": 8,
        "source": "Wikiquote",
    }

    records = [
        make_quote_record(
            20,
            1,
            "Ada Lovelace",
            [quote_a, quote_b],
        ),
        make_quote_record(
            21,
            2,
            "Ada Lovelace",
            [quote_b, quote_c],
        ),
    ]

    migrate_database.merge_quote_records(entry, records)

    assert entry["quotes"]["items"] == [
        quote_a,
        quote_b,
        quote_c,
    ]
    assert entry["quotes"]["items"].count(quote_b) == 1

    conflicts = entry["migration"]["conflicts"]
    assert len(conflicts) == 1

    conflict = conflicts[0]
    assert conflict["field"] == "quotes.items"
    assert conflict["resolution"] == "merged_unique_quote_items"
    assert [
        value["source"]
        for value in conflict["values"]
    ] == [QUOTE_FILE, QUOTE_FILE]
    assert [
        value["line_number"]
        for value in conflict["values"]
    ] == [20, 21]
    assert [
        value["record_index"]
        for value in conflict["values"]
    ] == [1, 2]


def test_failure_selection_uses_unique_newest_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    records = [
        make_quote_failure_record(
            30,
            3,
            "Ada Lovelace",
            "http_404",
            300,
            3,
        ),
        make_quote_failure_record(
            31,
            1,
            "Ada Lovelace",
            "timeout",
            100,
            1,
        ),
        make_quote_failure_record(
            32,
            2,
            "Ada Lovelace",
            "rate_limit",
            200,
            2,
        ),
    ]

    failure = migrate_database.select_newest_quote_failure(
        records,
        entry,
    )

    assert failure == {
        "reason": "http_404",
        "timestamp": 300,
        "retries": 3,
    }
    assert entry["migration"]["conflicts"] == []


def test_tied_newest_failure_values_set_failure_null_and_record_conflict():
    entry = make_empty_database_entry("Ada Lovelace")
    records = [
        make_quote_failure_record(
            40,
            1,
            "Ada Lovelace",
            "404",
            500,
            1,
        ),
        make_quote_failure_record(
            41,
            2,
            "Ada Lovelace",
            "no_quotes_found",
            500,
            1,
        ),
    ]

    failure = migrate_database.select_newest_quote_failure(
        records,
        entry,
    )

    assert failure is None
    assert len(entry["migration"]["conflicts"]) == 1

    conflict = entry["migration"]["conflicts"][0]
    assert conflict["field"] == "quotes.failure"
    assert (
        conflict["resolution"]
        == "tied_newest_failure_unresolved"
    )
    assert len(conflict["values"]) == 2

    for value in conflict["values"]:
        assert set(value) == {
            "source",
            "line_number",
            "record_index",
            "value",
        }
        assert value["source"] == QUOTE_FAILURE_FILE


def test_quotes_and_failure_keep_available_status_and_known_quotes():
    entry = make_empty_database_entry("Ada Lovelace")
    quote = {
        "text": "A sufficiently long quote for migration testing.",
        "length": 47,
        "word_count": 7,
        "source": "Wikiquote",
    }
    entry["quotes"]["status"] = "available"
    entry["quotes"]["items"] = [quote]
    entry["quotes"]["failure"] = {
        "reason": "http_404",
        "timestamp": 500,
        "retries": 1,
    }

    migrate_database.reconcile_quotes_and_failure(entry)

    assert entry["quotes"]["status"] == "available"
    assert entry["quotes"]["items"] == [quote]
    assert entry["quotes"]["failure"] == {
        "reason": "http_404",
        "timestamp": 500,
        "retries": 1,
    }


def test_failure_only_no_quotes_found_sets_not_found():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"]["failure"] = {
        "reason": "no_quotes_found",
        "timestamp": 600,
        "retries": 2,
    }

    migrate_database.reconcile_quotes_and_failure(entry)

    assert entry["quotes"]["status"] == "not_found"
    assert entry["quotes"]["items"] == []
    assert entry["quotes"]["failure"] == {
        "reason": "no_quotes_found",
        "timestamp": 600,
        "retries": 2,
    }


def test_failure_only_http_404_sets_failed():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"]["failure"] = {
        "reason": "http_404",
        "timestamp": 700,
        "retries": 3,
    }

    migrate_database.reconcile_quotes_and_failure(entry)

    assert entry["quotes"]["status"] == "failed"
    assert entry["quotes"]["items"] == []
    assert entry["quotes"]["failure"] == {
        "reason": "http_404",
        "timestamp": 700,
        "retries": 3,
    }


def test_no_quote_information_keeps_unknown_status():
    entry = make_empty_database_entry("Ada Lovelace")

    migrate_database.reconcile_quotes_and_failure(entry)

    assert entry["quotes"]["status"] == "unknown"
    assert entry["quotes"]["items"] == []
    assert entry["quotes"]["failure"] is None


@pytest.mark.parametrize(
    "historical_reason, normalized_reason",
    [
        ("404", "http_404"),
        ("rate_limit", "http_429"),
        ("timeout", "request_exception"),
        ("http_404", "http_404"),
        ("no_quotes_found", "no_quotes_found"),
    ],
)
def test_normalize_failure_reason_supports_historical_and_canonical_values(
    historical_reason,
    normalized_reason,
):
    assert (
        migrate_database.normalize_failure_reason(
            historical_reason
        )
        == normalized_reason
    )


def test_posted_title_creates_posting_only_entry():
    entry = make_empty_database_entry("Only Posted")
    raw_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Only Posted",
    }

    migrate_database.apply_posted_title(entry, raw_record)

    assert migrate_database.validate_database_entry(entry) == []
    assert entry["posting"] == {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }
    assert POSTED_FILE in entry["migration"]["legacy_sources"]


def test_posted_title_sets_legacy_marker_without_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    raw_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Ada Lovelace",
    }

    migrate_database.apply_posted_title(entry, raw_record)

    assert entry["posting"]["has_been_posted"] is True
    assert (
        entry["posting"]["legacy_posted_without_timestamp"]
        is True
    )
    assert entry["posting"]["posted_at"] == []


def test_posted_title_never_invents_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    raw_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Ada Lovelace",
    }

    migrate_database.apply_posted_title(entry, raw_record)

    assert entry["posting"]["posted_at"] == []
    assert set(entry["posting"]) == {
        "has_been_posted",
        "posted_at",
        "legacy_posted_without_timestamp",
    }


def test_legacy_sources_are_unique_and_in_documented_order():
    entry = make_empty_database_entry("Ada Lovelace")

    for filename in (
        PROCESSED_FILE,
        POSTED_FILE,
        SUMMARY_FILE,
        RESULT_FILE,
        SUMMARY_FILE,
        QUOTE_FILE,
    ):
        migrate_database.add_legacy_source(entry, filename)

    migrate_database.finalize_entry(entry)

    assert entry["migration"]["legacy_sources"] == [
        SUMMARY_FILE,
        QUOTE_FILE,
        RESULT_FILE,
        PROCESSED_FILE,
        POSTED_FILE,
    ]


def test_finalize_entry_sorts_conflicts_deterministically():
    entry = make_empty_database_entry("Ada Lovelace")

    migrate_database.add_conflict(
        entry,
        "z.field",
        [
            {
                "source": POSTED_FILE,
                "line_number": None,
                "record_index": 1,
                "value": "z",
            }
        ],
        "unresolved_safe_default",
    )
    migrate_database.add_conflict(
        entry,
        "a.field",
        [
            {
                "source": RESULT_FILE,
                "line_number": 3,
                "record_index": 3,
                "value": "result",
            }
        ],
        "unresolved_safe_default",
    )
    migrate_database.add_conflict(
        entry,
        "a.field",
        [
            {
                "source": SUMMARY_FILE,
                "line_number": 8,
                "record_index": 8,
                "value": "summary",
            }
        ],
        "unresolved_safe_default",
    )

    migrate_database.finalize_entry(entry)

    conflicts = entry["migration"]["conflicts"]
    assert [
        (
            conflict["field"],
            conflict["values"][0]["source"],
            conflict["values"][0]["line_number"],
            conflict["values"][0]["record_index"],
        )
        for conflict in conflicts
    ] == [
        ("a.field", SUMMARY_FILE, 8, 8),
        ("a.field", RESULT_FILE, 3, 3),
        ("z.field", POSTED_FILE, None, 1),
    ]


def test_finalize_entry_does_not_mutate_posting_semantics():
    entry = make_empty_database_entry("Ada Lovelace")
    raw_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Ada Lovelace",
    }

    migrate_database.apply_posted_title(entry, raw_record)
    migrate_database.finalize_entry(entry)

    assert entry["posting"] == {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }


@pytest.mark.parametrize("invalid_value", [123, ""])
def test_apply_posted_title_rejects_invalid_raw_value(invalid_value):
    entry = make_empty_database_entry("Ada Lovelace")
    raw_record = {
        "line_number": None,
        "record_index": 1,
        "value": invalid_value,
    }

    with pytest.raises(ValueError):
        migrate_database.apply_posted_title(entry, raw_record)


def test_applying_same_posted_title_twice_does_not_duplicate_provenance():
    entry = make_empty_database_entry("Ada Lovelace")
    first_record = {
        "line_number": None,
        "record_index": 1,
        "value": "Ada Lovelace",
    }
    second_record = {
        "line_number": None,
        "record_index": 2,
        "value": "Ada Lovelace",
    }

    migrate_database.apply_posted_title(entry, first_record)
    migrate_database.apply_posted_title(entry, second_record)

    assert entry["posting"] == {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }
    assert entry["migration"]["legacy_sources"] == [
        POSTED_FILE
    ]

def test_finalize_entry_rejects_unconfigured_legacy_source():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )

    entry["migration"]["legacy_sources"] = [
        "unexpected.jsonl"
    ]

    with pytest.raises(
        ValueError,
        match="Unknown migration legacy source",
    ):
        migrate_database.finalize_entry(entry)
