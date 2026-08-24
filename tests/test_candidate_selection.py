import pytest

from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION

from wiki_philosopher_bot.utils import (
    candidate_selection_weight,
    get_random_philosopher,
    is_accepted_record,
    is_rejected_record,
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


def test_get_random_philosopher_requires_nonempty_canonical_quotes():
    database = database_with_quotes({"Ada Lovelace": []})

    assert get_random_philosopher(
        database,
        chooser=choose_first,
    ) is None


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


def test_get_random_philosopher_excludes_stale_available_quote_cache():
    database = database_with_quotes({
        "Stale": [{"text": "Quote", "word_count": 1}],
        "Current": [{"text": "Quote", "word_count": 1}],
    })
    database["Stale"]["quotes"]["parser_version"] = None

    assert get_random_philosopher(database, chooser=choose_first) is database["Current"]


def test_get_random_philosopher_excludes_historical_parser_v7_quote_cache():
    database = database_with_quotes({
        "Historical v7": [{"text": "Quote", "word_count": 1}],
        "Current": [{"text": "Quote", "word_count": 1}],
    })
    database["Historical v7"]["quotes"]["parser_version"] = 7

    assert get_random_philosopher(database, chooser=choose_first) is database["Current"]


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
    entry["quotes"]["items"] = [{}, {}, {}]

    assert candidate_selection_weight(entry) == expected_weight


def test_candidate_weight_uses_content_confidence_as_primary_signal():
    lower_content = make_empty_database_entry("Lower")
    lower_content["evaluation"]["content_confidence"] = 0
    lower_content["quotes"]["items"] = [{}] * 10000
    higher_content = make_empty_database_entry("Higher")
    higher_content["evaluation"]["content_confidence"] = 1
    higher_content["quotes"]["items"] = [{}]

    assert candidate_selection_weight(higher_content) > (
        candidate_selection_weight(lower_content)
    )


def test_candidate_weight_ignores_quote_count_after_eligibility():
    one_quote = make_empty_database_entry("One")
    one_quote["evaluation"]["content_confidence"] = 2
    one_quote["quotes"]["items"] = [{}]
    many_quotes = make_empty_database_entry("Many")
    many_quotes["evaluation"]["content_confidence"] = 2
    many_quotes["quotes"]["items"] = [{}] * 500

    assert candidate_selection_weight(one_quote) == 4
    assert candidate_selection_weight(many_quotes) == 4


@pytest.mark.parametrize("raw_content", [-100, -5, -1, None, "2", [], True, False])
def test_candidate_weight_uses_baseline_for_invalid_or_low_content_confidence(
    raw_content,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["content_confidence"] = raw_content
    entry["quotes"]["items"] = [{}]

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
        4,
        2,
    ]
    assert captured["k"] == 1


def test_get_random_philosopher_returns_none_when_no_candidates():
    assert get_random_philosopher({}, chooser=choose_first) is None
