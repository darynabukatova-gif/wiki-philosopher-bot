import pytest

from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

from wiki_philosopher_bot.utils import (
    candidate_selection_weight,
    get_random_philosopher,
    has_current_usable_quotes,
    has_usable_posting_summary,
    is_accepted_record,
    is_rejected_record,
    is_posting_candidate,
    is_semantically_postable_philosopher,
)
from wiki_philosopher_bot.database_schema import make_empty_database_entry


def database_with_quotes(
    title_to_quotes,
    status="accepted",
    algorithm_version=None,
):
    database = {}
    for title, quotes in title_to_quotes.items():
        entry = make_empty_database_entry(title)
        entry["evaluation"]["status"] = status
        entry["evaluation"]["algorithm_version"] = algorithm_version
        entry["summary"]["text"] = "A canonical Wikipedia summary."
        entry["quotes"]["status"] = "available"
        entry["quotes"]["items"] = quotes
        entry["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
        database[title] = entry
    return database


def test_is_accepted_record_supports_historical_and_current_shapes():
    assert is_accepted_record({"accepted": True}) is True
    assert is_accepted_record({"accepted": False}) is False
    assert is_accepted_record({"status": "accepted"}) is True
    assert is_accepted_record({"status": "rejected"}) is False


def test_is_rejected_record_supports_historical_and_current_shapes():
    assert is_rejected_record({"accepted": False}) is True
    assert is_rejected_record({"accepted": True}) is False
    assert is_rejected_record({"status": "rejected"}) is True
    assert is_rejected_record({"status": "accepted"}) is False


def test_semantic_postability_uses_evaluation_not_transient_posting_state():
    value = make_empty_database_entry("Ada Lovelace")
    value["evaluation"]["status"] = "accepted"
    value["posting"]["has_been_posted"] = True

    assert is_semantically_postable_philosopher(value) is True


def test_summary_first_candidate_requires_usable_summary_not_quotes():
    value = make_empty_database_entry("Ada Lovelace")
    value["evaluation"]["status"] = "accepted"
    value["summary"]["text"] = "A usable summary."

    assert has_usable_posting_summary(value) is True
    assert has_current_usable_quotes(value) is False
    assert is_posting_candidate(value) is True


def test_summary_date_conflict_does_not_change_candidate_eligibility():
    value = make_empty_database_entry("Catherine Descartes")
    value["evaluation"]["status"] = "accepted"
    value["summary"]["text"] = "Catherine Descartes (1637–1706) was a philosopher."
    value["wikidata"]["birth_year"] = 1637
    value["wikidata"]["death_year"] = 1715

    assert is_posting_candidate(value) is True


@pytest.mark.parametrize("summary", (None, "   ", "No summary available."))
def test_summary_first_candidate_rejects_missing_blank_or_fallback_summary(summary):
    value = make_empty_database_entry("Ada Lovelace")
    value["evaluation"]["status"] = "accepted"
    value["summary"]["text"] = summary

    assert has_usable_posting_summary(value) is False
    assert is_posting_candidate(value) is False


def choose_first(population, weights=None, k=1):
    assert population
    assert k == 1
    return [population[0]]


def test_get_random_philosopher_uses_canonical_accepted_evaluation():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
    })

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Ada Lovelace"]


def test_get_random_philosopher_excludes_rejected_and_unprocessed_entries():
    database = database_with_quotes({
        "Accepted": [{"text": "Quote", "word_count": 1}],
        "Rejected": [{"text": "Quote", "word_count": 1}],
        "Unprocessed": [{"text": "Quote", "word_count": 1}],
    })
    database["Rejected"]["evaluation"]["status"] = "rejected"
    database["Unprocessed"]["evaluation"]["status"] = "unprocessed"

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Accepted"]


def test_get_random_philosopher_allows_usable_summary_without_quotes():
    database = database_with_quotes({"Ada Lovelace": []})

    assert get_random_philosopher(
        database,
        chooser=choose_first,
    ) is database["Ada Lovelace"]


def test_get_random_philosopher_excludes_purged_quote_state():
    database = database_with_quotes({
        "Purged": [{"text": "Quote", "word_count": 1}],
        "Current": [{"text": "Quote", "word_count": 1}],
    })
    database["Purged"]["quotes"].update({
        "status": "purged",
        "items": [],
        "failure": None,
        "fetched_at": None,
        "parser_version": None,
    })
    database["Purged"]["evaluation"]["status"] = "rejected"

    assert get_random_philosopher(database, chooser=choose_first) is database["Current"]


def test_get_random_philosopher_allows_stale_available_quote_cache_without_using_it():
    database = database_with_quotes({
        "Stale": [{"text": "Quote", "word_count": 1}],
        "Current": [{"text": "Quote", "word_count": 1}],
    })
    database["Stale"]["quotes"]["parser_version"] = None

    assert get_random_philosopher(database, chooser=choose_first) is database["Stale"]
    assert has_current_usable_quotes(database["Stale"]) is False


def test_get_random_philosopher_allows_historical_quote_cache_without_using_it():
    database = database_with_quotes({
        "Historical v7": [{"text": "Quote", "word_count": 1}],
        "Current": [{"text": "Quote", "word_count": 1}],
    })
    database["Historical v7"]["quotes"]["parser_version"] = 7

    assert get_random_philosopher(database, chooser=choose_first) is database["Historical v7"]


def test_get_random_philosopher_requires_current_quote_parser_version():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
    })

    assert get_random_philosopher(database, chooser=choose_first) is database["Ada Lovelace"]


def test_get_random_philosopher_selects_unposted_canonical_candidate():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
    })

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Ada Lovelace"]


def test_get_random_philosopher_excludes_canonical_posted_candidate():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
        "Simone de Beauvoir": [{"text": "Quote", "word_count": 1}],
    })
    database["Ada Lovelace"]["posting"] = {
        "has_been_posted": True,
        "posted_at": [],
        "legacy_posted_without_timestamp": True,
    }

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Simone de Beauvoir"]

    database["Ada Lovelace"]["posting"] = {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": False,
    }

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Simone de Beauvoir"]


def test_get_random_philosopher_excludes_historical_plus_new_timestamp_candidate():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
    })
    database["Ada Lovelace"]["posting"] = {
        "has_been_posted": True,
        "posted_at": [1234567890],
        "legacy_posted_without_timestamp": True,
    }

    assert get_random_philosopher(
        database,
        chooser=choose_first,
    ) is None


def test_get_random_philosopher_requires_no_posted_titles_argument():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "Quote", "word_count": 1}],
    })

    assert get_random_philosopher(
        database,
        chooser=choose_first,
    ) is database["Ada Lovelace"]


def test_get_random_philosopher_allows_historical_accepted_unknown_version():
    database = database_with_quotes(
        {"Ada Lovelace": [{"text": "Quote", "word_count": 1}]},
        algorithm_version=None,
    )
    database["Ada Lovelace"]["evaluation"]["content_confidence"] = 0

    selected = get_random_philosopher(
        database,
        chooser=choose_first,
    )

    assert selected is database["Ada Lovelace"]


@pytest.mark.parametrize("content_confidence, expected_weight", [
    (-1, 1),
    (0, 2),
    (1, 3),
    (2, 4),
])
def test_candidate_selection_weight_uses_content_only_formula(
    content_confidence,
    expected_weight,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["content_confidence"] = content_confidence
    entry["summary"]["text"] = "A canonical summary."

    assert candidate_selection_weight(entry) == expected_weight


def test_candidate_weight_uses_content_confidence_as_primary_signal():
    lower_content = make_empty_database_entry("Lower")
    lower_content["evaluation"]["content_confidence"] = 0
    lower_content["summary"]["text"] = "A canonical summary."
    higher_content = make_empty_database_entry("Higher")
    higher_content["evaluation"]["content_confidence"] = 1
    higher_content["summary"]["text"] = "A canonical summary."

    assert candidate_selection_weight(higher_content) > (
        candidate_selection_weight(lower_content)
    )


def test_candidate_weight_uses_fixed_quote_bonus_not_quote_count():
    one_quote = make_empty_database_entry("One")
    one_quote["evaluation"]["content_confidence"] = 2
    one_quote["summary"]["text"] = "A canonical summary."
    one_quote["quotes"].update({
        "status": "available", "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{"word_count": 5}],
    })
    many_quotes = make_empty_database_entry("Many")
    many_quotes["evaluation"]["content_confidence"] = 2
    many_quotes["summary"]["text"] = "A canonical summary."
    many_quotes["quotes"].update({
        "status": "available", "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{"word_count": 5}] * 500,
    })

    assert candidate_selection_weight(one_quote) == 5
    assert candidate_selection_weight(many_quotes) == 5


def test_candidate_weight_adds_only_current_quote_and_richer_valid_links():
    entry = make_empty_database_entry("Ada")
    entry["evaluation"]["content_confidence"] = 0
    entry["summary"]["text"] = "A canonical summary."
    entry["quotes"].update({
        "status": "available", "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{"word_count": 5}],
    })
    entry["external_links"].update({
        "wikiquote": "https://en.wikiquote.org/wiki/Ada",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/1",
    })

    assert candidate_selection_weight(entry) == 7  # base 2 + quote 1 + WS 2 + PG 2
    entry["external_links"]["wikiquote"] = None
    assert candidate_selection_weight(entry) == 7


@pytest.mark.parametrize("raw_content", [-100, -5, -1, None, "2", [], True, False])
def test_candidate_weight_uses_baseline_for_invalid_or_low_content_confidence(
    raw_content,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["content_confidence"] = raw_content
    entry["summary"]["text"] = "A canonical summary."

    assert candidate_selection_weight(entry) == 1


def test_get_random_philosopher_uses_candidate_selection_weight():
    database = database_with_quotes({
        "Ada Lovelace": [{"text": "One"}, {"text": "Two"}],
        "Simone de Beauvoir": [{"text": "One"}],
    })
    database["Ada Lovelace"]["evaluation"]["content_confidence"] = 2
    database["Simone de Beauvoir"]["evaluation"]["content_confidence"] = 0
    captured = {}

    def capture_weights(population, weights=None, k=1):
        captured["population"] = population
        captured["weights"] = weights
        captured["k"] = k
        return [population[0]]

    get_random_philosopher(database, chooser=capture_weights)

    assert captured["population"] == [
        database["Ada Lovelace"],
        database["Simone de Beauvoir"],
    ]
    assert captured["weights"] == [
        5,
        3,
    ]
    assert captured["k"] == 1


def test_get_random_philosopher_returns_none_when_no_candidates():
    assert get_random_philosopher({}, chooser=choose_first) is None
