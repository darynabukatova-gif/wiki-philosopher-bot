import wiki_philosopher_bot.database_schema as database_schema
import pytest

from wiki_philosopher_bot.cli.migrate_database import (
    make_empty_database_entry, 
    validate_database_entry, 
    validate_database_dataset, 
    add_legacy_source,
    choose_known_value,
    source_value,
)

from wiki_philosopher_bot.config import (
    CURRENT_EVALUATION_ALGORITHM_VERSION,
    ENTITY_FILE,
    POSTED_FILE,
    PROCESSED_FILE,
    QUOTE_FAILURE_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)

def test_empty_database_entry_contains_every_required_section():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    assert set(entry) == {
        "schema_version",
        "title",
        "display_title",
        "summary",
        "wikidata",
        "quotes",
        "evaluation",
        "posting",
        "migration",
    }

    assert validate_database_entry(entry) == []


def test_display_title_is_not_a_canonical_identity_key():
    alan_white = make_empty_database_entry("Alan White")
    alan_white_philosopher = make_empty_database_entry(
        "Alan White (American philosopher)"
    )

    assert alan_white["display_title"] == "Alan White"
    assert alan_white_philosopher["display_title"] == "Alan White"
    database = {
        alan_white["title"]: alan_white,
        alan_white_philosopher["title"]: alan_white_philosopher,
    }
    assert set(database) == {
        "Alan White",
        "Alan White (American philosopher)",
    }


def test_runtime_empty_entry_matches_canonical_schema():
    entry = database_schema.make_empty_database_entry(
        "Ada Lovelace (philosopher)"
    )

    assert entry == {
        "schema_version": database_schema.DATABASE_SCHEMA_VERSION,
        "title": "Ada Lovelace (philosopher)",
        "display_title": "Ada Lovelace",
        "summary": {
            "text": None,
            "source": "Wikipedia",
            "fetched_at": None,
        },
        "wikidata": {
            "status": "unknown",
            "reason": None,
            "qid": None,
            "instances": [],
            "occupations": [],
            "birth_year": None,
            "death_year": None,
            "death_date": None,
            "is_human": None,
            "is_philosopher": None,
            "fetched_at": None,
        },
        "quotes": {
            "status": "unknown",
            "items": [],
            "failure": None,
            "fetched_at": None,
            "parser_version": None,
        },
        "evaluation": {
            "status": "unprocessed",
            "algorithm_version": None,
            "human_confidence": None,
            "philosopher_confidence": None,
            "content_confidence": None,
            "reasons": [],
            "legacy_result": None,
            "processed_at": None,
        },
        "posting": {
            "has_been_posted": False,
            "posted_at": [],
            "legacy_posted_without_timestamp": False,
            "attempts": [],
        },
        "migration": {
            "legacy_sources": [],
            "conflicts": [],
        },
    }
    assert database_schema.validate_database_entry(entry) == []
    assert "legacy_records" not in entry
    assert database_schema.make_empty_database_entry is make_empty_database_entry


def test_schema_validator_accepts_negative_wikidata_life_years():
    entry = make_empty_database_entry("Thales of Miletus")
    entry["wikidata"]["birth_year"] = -650
    entry["wikidata"]["death_year"] = -548

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_historical_wikidata_without_death_date():
    entry = make_empty_database_entry("Historical philosopher")
    del entry["wikidata"]["death_date"]

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_exact_wikidata_death_date():
    entry = make_empty_database_entry("Recent philosopher")
    entry["wikidata"]["death_date"] = "2026-06-29"

    assert validate_database_entry(entry) == []


@pytest.mark.parametrize("death_date", ("2026-6-29", "2026-02-30", "not-a-date", 20260629))
def test_schema_validator_rejects_invalid_wikidata_death_date(death_date):
    entry = make_empty_database_entry("Recent philosopher")
    entry["wikidata"]["death_date"] = death_date

    assert "wikidata.death_date must be an ISO date or null" in validate_database_entry(entry)


@pytest.mark.parametrize("parser_version", (None, 1, 2))
def test_schema_validator_accepts_quote_parser_version(parser_version):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"]["parser_version"] = parser_version

    assert validate_database_entry(entry) == []


@pytest.mark.parametrize("parser_version", (True, False, 0, -1, "2", 2.0))
def test_schema_validator_rejects_invalid_quote_parser_version(parser_version):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"]["parser_version"] = parser_version

    assert "quotes.parser_version must be a positive integer or null" in (
        validate_database_entry(entry)
    )


def test_schema_validator_accepts_historical_quotes_without_parser_version():
    entry = make_empty_database_entry("Ada Lovelace")
    del entry["quotes"]["parser_version"]

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_explicit_purged_quote_state():
    entry = make_empty_database_entry("Rejected entry")
    entry["evaluation"]["status"] = "rejected"
    entry["quotes"].update({
        "status": "purged",
        "items": [],
        "failure": None,
        "fetched_at": None,
        "parser_version": None,
    })

    assert validate_database_entry(entry) == []


@pytest.mark.parametrize("field, value, expected_error", (
    ("items", [{"text": "retained"}], "quotes.purged items must be an empty list"),
    ("parser_version", 4, "quotes.purged parser_version must be null"),
    ("failure", {"reason": "http_404", "timestamp": 1, "retries": 1}, "quotes.purged failure must be null"),
    ("fetched_at", 1, "quotes.purged fetched_at must be null"),
))
def test_schema_validator_rejects_malformed_purged_quote_state(
    field, value, expected_error,
):
    entry = make_empty_database_entry("Rejected entry")
    entry["evaluation"]["status"] = "rejected"
    entry["quotes"].update({
        "status": "purged",
        "items": [],
        "failure": None,
        "fetched_at": None,
        "parser_version": None,
    })
    entry["quotes"][field] = value

    assert expected_error in validate_database_entry(entry)


def test_schema_validator_requires_rejected_evaluation_for_purged_quotes():
    entry = make_empty_database_entry("Accepted entry")
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"].update({
        "status": "purged",
        "items": [],
        "failure": None,
        "fetched_at": None,
        "parser_version": None,
    })

    assert "quotes.purged requires evaluation.status rejected" in (
        validate_database_entry(entry)
    )


def test_schema_validator_requires_structured_quote_items_for_current_parser_version():
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{
            "text": "A valid current parser quote.",
            "length": 29,
            "word_count": 5,
            "source": "Wikiquote",
        }],
    })

    errors = validate_database_entry(entry)

    assert any("source must be an object" in error for error in errors)
    assert any("retrieved_from" in error for error in errors)


def test_schema_validator_accepts_complete_current_parser_quote_item():
    from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{
            "text": "A valid current parser quote.",
            "length": 29,
            "word_count": 5,
            "source": {
                "work": "A Work", "year": 1886, "date": None,
                "details": "Ch. 2", "citation": "A Work (1886), Ch. 2",
                "url": "https://example.test/work",
            },
            "retrieved_from": "Wikiquote",
        }],
    })

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_historical_parser_v4_structured_quote_item():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"].update({
        "status": "available",
        "parser_version": 4,
        "items": [{
            "text": "A historical structured quote.",
            "length": 28,
            "word_count": 5,
            "source": {
                "work": "Historical Work", "year": 1921, "date": None,
                "details": None, "citation": "Historical Work (1921)",
                "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
    })

    assert validate_database_entry(entry) == []


@pytest.mark.parametrize("parser_version", (2, 3))
def test_schema_validator_accepts_structured_historical_parser_items(parser_version):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"].update({
        "status": "available",
        "parser_version": parser_version,
        "items": [{
            "text": "A historical parser quote.",
            "length": 30,
            "word_count": 5,
            "source": {
                "work": None, "year": None, "date": None,
                "details": None, "citation": None, "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
    })

    assert validate_database_entry(entry) == []

def test_schema_validator_rejects_missing_required_field():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    del entry["quotes"]

    errors = validate_database_entry(entry)

    assert (
        "Missing required top-level field: quotes"
        in errors
    )

def test_schema_validator_rejects_duplicate_titles():
    first = make_empty_database_entry(
        "Ada Lovelace"
    )
    second = make_empty_database_entry(
        "Ada Lovelace"
    )

    errors = validate_database_dataset(
        [first, second]
    )

    assert (
        "Duplicate database title: 'Ada Lovelace'"
        in errors
    )

def test_schema_validator_rejects_invalid_quote_item():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    entry["quotes"]["items"] = [
        {
            "text": "A quote",
            # length deliberately missing
            "word_count": 2,
            "source": "Wikiquote",
        }
    ]

    errors = validate_database_entry(entry)

    assert any(
        "quotes.items[0] missing length"
        in error
        for error in errors
    )


def test_schema_validator_requires_complete_wikidata_section():
    entry = make_empty_database_entry("Ada Lovelace")
    del entry["wikidata"]["fetched_at"]

    assert "Missing required field: wikidata.fetched_at" in (
        validate_database_entry(entry)
    )

def test_schema_validator_rejects_invalid_posting_invariant():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    entry["posting"] = {
        "has_been_posted": False,
        "posted_at": [123],
        "legacy_posted_without_timestamp": False,
    }

    errors = validate_database_entry(entry)

    assert any(
        "has_been_posted must be true"
        in error
        for error in errors
    )


@pytest.mark.parametrize(
    "field_name",
    (
        "has_been_posted",
        "posted_at",
        "legacy_posted_without_timestamp",
    ),
)
def test_schema_validator_requires_complete_posting_section(field_name):
    entry = make_empty_database_entry("Ada Lovelace")
    del entry["posting"][field_name]

    errors = validate_database_entry(entry)

    assert "Missing required field: posting.{}".format(field_name) in errors


def test_schema_validator_accepts_never_posted_state():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": False,
        "posted_at": [],
        "legacy_posted_without_timestamp": False,
    }

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_historical_posting_without_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_new_posting_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": False,
    }

    assert validate_database_entry(entry) == []


def test_schema_validator_accepts_historical_posting_with_new_timestamp():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": True,
    }

    assert validate_database_entry(entry) == []

    entry["posting"]["posted_at"] = [1234567890, 1234567999]

    assert validate_database_entry(entry) == []


def test_schema_validator_rejects_unposted_entry_with_legacy_marker():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": False,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }

    errors = validate_database_entry(entry)

    assert "posting.has_been_posted must be true when " \
        "legacy_posted_without_timestamp is true" in errors


@pytest.mark.parametrize("timestamp", ("1234567890", None, True))
def test_schema_validator_rejects_invalid_posted_at_item_type(timestamp):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["posting"] = {
        "has_been_posted": True,
        "posted_at": [timestamp],
        "legacy_posted_without_timestamp": False,
    }

    errors = validate_database_entry(entry)

    assert "posting.posted_at must contain integer timestamps" in errors

def test_schema_validator_accepts_fractional_legacy_processed_at():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    entry["evaluation"]["processed_at"] = (
        1780580890.25
    )

    assert validate_database_entry(entry) == []

def test_conflict_values_have_source_attribution_and_locations():
    raw_record = {
        "line_number": 12,
        "record_index": 11,
        "value": {},
    }

    value = source_value(
        RESULT_FILE,
        raw_record,
        "accepted",
    )

    assert value == {
        "source": RESULT_FILE,
        "line_number": 12,
        "record_index": 11,
        "value": "accepted",
    }

def test_duplicate_legacy_source_name_is_not_added_twice():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    add_legacy_source(entry, RESULT_FILE)
    add_legacy_source(entry, RESULT_FILE)

    assert entry["migration"]["legacy_sources"] == [
        RESULT_FILE
    ]

def test_legacy_sources_use_canonical_source_order():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    add_legacy_source(entry, PROCESSED_FILE)
    add_legacy_source(entry, SUMMARY_FILE)
    add_legacy_source(entry, RESULT_FILE)

    assert entry["migration"]["legacy_sources"] == [
        SUMMARY_FILE,
        RESULT_FILE,
        PROCESSED_FILE,
    ]

def test_unresolvable_values_use_safe_default_and_conflict():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    candidates = [
        {
            "source": RESULT_FILE,
            "line_number": 10,
            "record_index": 10,
            "value": "accepted",
        },
        {
            "source": PROCESSED_FILE,
            "line_number": 20,
            "record_index": 20,
            "value": "rejected",
        },
    ]

    result = choose_known_value(
        field="evaluation.status",
        candidates=candidates,
        safe_default="unprocessed",
        entry=entry,
    )

    assert result == "unprocessed"

    assert entry["migration"]["conflicts"] == [
        {
            "field": "evaluation.status",
            "values": candidates,
            "resolution": "unresolved_safe_default",
        }
    ]

def test_equal_duplicate_values_do_not_create_conflict():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    candidates = [
        {
            "source": RESULT_FILE,
            "line_number": 10,
            "record_index": 10,
            "value": "accepted",
        },
        {
            "source": RESULT_FILE,
            "line_number": 11,
            "record_index": 11,
            "value": "accepted",
        },
    ]

    result = choose_known_value(
        field="evaluation.status",
        candidates=candidates,
        safe_default="unprocessed",
        entry=entry,
    )

    assert result == "accepted"
    assert entry["migration"]["conflicts"] == []

def test_known_value_wins_over_none_without_conflict():
    entry = make_empty_database_entry(
        "Ada Lovelace"
    )

    candidates = [
        {
            "source": RESULT_FILE,
            "line_number": 1,
            "record_index": 1,
            "value": None,
        },
        {
            "source": PROCESSED_FILE,
            "line_number": 2,
            "record_index": 2,
            "value": 3,
        },
    ]

    result = choose_known_value(
        field="evaluation.human_confidence",
        candidates=candidates,
        safe_default=None,
        entry=entry,
    )

    assert result == 3
    assert entry["migration"]["conflicts"] == []


@pytest.mark.parametrize(
    "field_name",
    ("status", "items", "failure", "fetched_at"),
)
def test_quote_section_requires_all_canonical_fields(field_name):
    entry = make_empty_database_entry("Ada Lovelace")
    del entry["quotes"][field_name]

    errors = validate_database_entry(entry)

    assert "Missing required field: quotes.{}".format(field_name) in errors


def test_quote_failure_requires_canonical_failure_fields():
    entry = make_empty_database_entry("Ada Lovelace")
    entry["quotes"]["failure"] = {
        "reason": "http_404",
        "timestamp": 1,
    }

    errors = validate_database_entry(entry)

    assert "Missing required field: quotes.failure.retries" in errors


@pytest.mark.parametrize(
    "field_name",
    (
        "status",
        "algorithm_version",
        "human_confidence",
        "philosopher_confidence",
        "content_confidence",
        "reasons",
        "legacy_result",
        "processed_at",
    ),
)
def test_schema_validator_requires_complete_evaluation_section(field_name):
    entry = make_empty_database_entry("Ada Lovelace")
    del entry["evaluation"][field_name]

    errors = validate_database_entry(entry)

    assert "Missing required field: evaluation.{}".format(field_name) in errors


@pytest.mark.parametrize(
    "legacy_result",
    ("historical", [], 42),
)
def test_schema_validator_rejects_invalid_legacy_result_type(legacy_result):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["legacy_result"] = legacy_result

    errors = validate_database_entry(entry)

    assert "evaluation.legacy_result must be an object or null" in errors


def test_current_evaluation_algorithm_version_is_two():
    assert CURRENT_EVALUATION_ALGORITHM_VERSION == 2
