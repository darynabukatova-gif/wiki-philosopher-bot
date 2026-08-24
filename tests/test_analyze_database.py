import ast
import copy
import importlib.util
import json
from pathlib import Path

import analyze_database
import wiki_philosopher_bot.cache as cache
import wiki_philosopher_bot.evaluation as evaluation
import main
import pytest
import wiki_philosopher_bot.utils as utils
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.config import (
    CURRENT_EVALUATION_ALGORITHM_VERSION,
    CURRENT_QUOTE_PARSER_VERSION,
)
from wiki_philosopher_bot.database_schema import make_empty_database_entry, serialize_database_entries


def quote_item(text):
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


def entry(title, status, version, human, philosopher, content, quote_count):
    result = make_empty_database_entry(title)
    result["evaluation"].update({
        "status": status,
        "algorithm_version": version,
        "human_confidence": human,
        "philosopher_confidence": philosopher,
        "content_confidence": content,
    })
    result["quotes"]["status"] = "available" if quote_count else "unknown"
    result["quotes"]["items"] = [
        quote_item("Quote number {} for {}.".format(index, title))
        for index in range(quote_count)
    ]
    result["quotes"]["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
    return result


def sample_database():
    alpha = entry(
        "Alpha", "accepted", CURRENT_EVALUATION_ALGORITHM_VERSION,
        3, 4, 2, 2,
    )
    bravo = entry("Bravo", "accepted", None, 1, 1, -1, 1)
    bravo["posting"]["has_been_posted"] = True
    charlie = entry(
        "Charlie", "rejected", CURRENT_EVALUATION_ALGORITHM_VERSION,
        0, -1, 0, 0,
    )
    charlie["quotes"].update({
        "status": "failed",
        "failure": {
            "reason": "request_exception",
            "timestamp": 1,
            "retries": 2,
        },
    })
    delta = entry("Delta", "unprocessed", None, None, None, None, 0)
    delta["migration"]["conflicts"] = [{
        "field": "evaluation.status",
        "values": [],
        "resolution": "unprocessed",
    }, {
        "field": "quotes.failure",
        "resolution": "preserve_existing",
    }]
    epsilon = entry(
        "Epsilon", "rejected", CURRENT_EVALUATION_ALGORITHM_VERSION + 1,
        -3, 2, -2, 3,
    )
    epsilon["migration"]["conflicts"] = [{
        "field": "evaluation.status",
        "resolution": "unprocessed",
    }]

    return {
        item["title"]: item
        for item in (alpha, bravo, charlie, delta, epsilon)
    }


def candidate_database():
    database = sample_database()
    database["Beta"] = entry("Beta", "accepted", 1, 2, 2, 1, 100)
    database["Gamma"] = entry("Gamma", "accepted", 1, 4, 4, 2, 3)
    database["Zeta"] = entry("Zeta", "accepted", 1, 4, 4, 2, 1)
    return database


def margin_database():
    strong = entry("Accepted Strong", "accepted", 1, 3, 4, 2, 2)
    strong["evaluation"]["reasons"] = [
        "title bonus (+2): (philosopher)",
        "title philosopher bonus (+1): philosopher)",
        "wikidata human bonus (+2): is_human = true",
    ]
    borderline_accepted = entry(
        "Accepted Borderline", "accepted", 1, 1, 1, 2, 1
    )
    borderline_accepted["evaluation"]["reasons"] = [
        "summary philosopher bonus (+2): was a[n] .* philosopher",
        "wikidata philosopher bonus (+2): is_philosopher = true",
    ]
    borderline_rejected = entry(
        "Rejected Borderline", "rejected", 1, 0, 2, -1, 0
    )
    borderline_rejected["evaluation"]["reasons"] = [
        "summary nonhuman penalty (-1): novel",
    ]
    borderline_rejected["migration"]["conflicts"] = [{
        "field": "evaluation",
        "resolution": "unresolved_set_unprocessed",
    }]
    rejected_negative = entry("Rejected Negative", "rejected", 1, -2, -1, -1, 0)
    rejected_negative["evaluation"]["reasons"] = [
        "title nonhuman penalty (-1): bad word",
        "title nonhuman penalty (-1): :",
    ]
    ignored_none = entry("Ignored None", "unprocessed", None, None, None, None, 0)
    ignored_boolean = entry("Ignored Boolean", "unprocessed", None, True, False, None, 0)
    return {
        item["title"]: item
        for item in (
            strong,
            borderline_accepted,
            borderline_rejected,
            rejected_negative,
            ignored_none,
            ignored_boolean,
        )
    }


def distribution_values(distribution):
    return {item["value"]: item["count"] for item in distribution}


def write_database(tmp_path, database):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(list(database.values())))
    return path


def impact_entry(
    title,
    old_status="rejected",
    old_version=1,
    summary=None,
    wikidata_status="available",
    is_human=True,
    is_philosopher=None,
    quote_status="available",
    quote_count=1,
):
    result = entry(
        title,
        old_status,
        old_version,
        0,
        0,
        0,
        quote_count,
    )
    result["summary"]["text"] = summary
    result["wikidata"].update({
        "status": wikidata_status,
        "is_human": is_human,
        "is_philosopher": is_philosopher,
    })
    result["quotes"]["status"] = quote_status
    return result


def test_analyze_v2_impact_recomputes_v1_rejection_as_v2_acceptance():
    ada = impact_entry(
        "Ada",
        summary="Ada was a philosopher.",
        is_human=None,
    )

    impact = analyze_database.analyze_v2_impact({"Ada": ada})
    result = impact["entry_results"][0]

    assert result["prequote_classification"] == "positive_prequote"
    assert result["cache_only_outcome"] == "accepted"
    assert result["v2"] == {
        "human_confidence": 1,
        "philosopher_confidence": 2,
        "content_confidence": 2,
    }
    assert impact["transition_matrix"]["rejected_v1"]["accepted"] == 1


def test_analyze_v2_impact_preserves_clear_nonperson_and_false_contexts():
    object_entry = impact_entry(
        "The Old Philosopher",
        summary="The Old Philosopher is a play.",
        is_human=None,
        quote_count=0,
    )
    eddie = impact_entry(
        "Eddie Lawrence",
        summary=(
            "Eddie Lawrence was an actor whose comic creation, "
            "The Old Philosopher, became popular."
        ),
        is_human=True,
    )

    impact = analyze_database.analyze_v2_impact({
        object_entry["title"]: object_entry,
        eddie["title"]: eddie,
    })
    by_title = {item["title"]: item for item in impact["entry_results"]}

    assert by_title["The Old Philosopher"]["prequote_classification"] == (
        "guaranteed_reject_prequote"
    )
    assert by_title["The Old Philosopher"]["cache_only_outcome"] == "rejected"
    assert by_title["Eddie Lawrence"]["v2"]["philosopher_confidence"] == 0
    assert by_title["Eddie Lawrence"]["prequote_classification"] == (
        "unresolved_prequote"
    )


def test_analyze_v2_impact_never_fetches_or_persists_when_cache_is_incomplete(
    monkeypatch,
):
    incomplete = impact_entry(
        "Missing Cache",
        summary=None,
        wikidata_status="unknown",
        quote_status="failed",
        quote_count=0,
    )
    monkeypatch.setattr(
        evaluation,
        "get_summary",
        lambda *args, **kwargs: pytest.fail("impact audit must not fetch summaries"),
    )
    monkeypatch.setattr(
        evaluation,
        "prepare_entity_cached",
        lambda *args, **kwargs: pytest.fail("impact audit must not fetch Wikidata"),
    )
    monkeypatch.setattr(
        evaluation,
        "get_quotes",
        lambda *args, **kwargs: pytest.fail("impact audit must not fetch quotes"),
    )
    monkeypatch.setattr(
        cache,
        "update_database_entry",
        lambda *args, **kwargs: pytest.fail("impact audit must not persist"),
    )
    monkeypatch.setattr(
        evaluation,
        "update_database_entry",
        lambda *args, **kwargs: pytest.fail("impact audit must not persist"),
    )
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail("impact audit must not use network"),
    )

    impact = analyze_database.analyze_v2_impact({"Missing Cache": incomplete})

    assert impact["entry_results"] == [{
        "title": "Missing Cache",
        "old": {
            "status": "rejected",
            "algorithm_version": 1,
            "human_confidence": 0,
            "philosopher_confidence": 0,
            "content_confidence": 0,
            "reasons": [],
        },
        "cache_state": {
            "summary": "missing",
            "wikidata": "unknown",
            "quotes": "not_evaluated",
        },
        "prequote_classification": "insufficient_cached_evidence",
        "cache_only_outcome": "insufficient_cached_evidence",
        "v2": None,
    }]


def test_analyze_v2_impact_treats_historical_false_wikidata_as_neutral():
    legacy_false = impact_entry(
        "Ada",
        summary="Ada was a natural philosopher.",
        is_human=False,
        is_philosopher=False,
    )

    impact = analyze_database.analyze_v2_impact({"Ada": legacy_false})
    result = impact["entry_results"][0]

    assert result["v2"]["human_confidence"] == 1
    assert result["v2"]["philosopher_confidence"] == 2
    assert not any("= false" in reason for reason in result["v2_reasons"])
    assert impact["wikidata_tristate_impact"]["historical_false_is_human"] == 1
    assert impact["wikidata_tristate_impact"]["historical_false_is_philosopher"] == 1


def test_analyze_v2_impact_uses_exact_title_and_natural_philosopher_evidence():
    disambiguated = impact_entry(
        "Ada (philosopher)",
        summary="Ada was a scholar.",
        is_human=None,
    )
    natural = impact_entry(
        "Jean Fatio",
        summary="Jean Fatio was an engineer and natural philosopher.",
        is_human=None,
    )

    impact = analyze_database.analyze_v2_impact({
        disambiguated["title"]: disambiguated,
        natural["title"]: natural,
    })
    by_title = {item["title"]: item for item in impact["entry_results"]}

    assert by_title["Ada (philosopher)"]["v2"] == {
        "human_confidence": 1,
        "philosopher_confidence": 2,
        "content_confidence": 2,
    }
    assert by_title["Jean Fatio"]["v2"] == {
        "human_confidence": 1,
        "philosopher_confidence": 2,
        "content_confidence": 2,
    }


def test_analyze_v2_impact_is_deterministic_and_does_not_mutate_input():
    first = impact_entry("Zeta", summary="Zeta was a philosopher.")
    second = impact_entry("Alpha", summary="Alpha was a philosopher.")
    database = {first["title"]: first, second["title"]: second}
    before = copy.deepcopy(database)

    first_analysis = analyze_database.analyze_v2_impact(database)
    second_analysis = analyze_database.analyze_v2_impact(database)

    assert first_analysis == second_analysis
    assert [item["title"] for item in first_analysis["entry_results"]] == [
        "Alpha", "Zeta",
    ]
    assert database == before


def test_v2_impact_cli_is_read_only_and_never_contacts_network(
    monkeypatch, tmp_path, capsys,
):
    ada = impact_entry("Ada", summary="Ada was a philosopher.")
    database = {"Ada": ada}
    database_path = write_database(tmp_path, database)
    before = database_path.read_bytes()
    monkeypatch.setattr(
        wikipedia_api,
        "safe_request",
        lambda *args, **kwargs: pytest.fail("v2 impact CLI must not use network"),
    )
    monkeypatch.setattr(
        cache,
        "update_database_entry",
        lambda *args, **kwargs: pytest.fail("v2 impact CLI must not persist"),
    )

    assert analyze_database.main([
        "--data-folder", str(tmp_path), "--v2-impact",
    ]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["cache_only_outcome_counts"] == {"accepted": 1}
    assert database_path.read_bytes() == before


def test_analyze_database_reports_summary_confidence_and_cross_tab_counts():
    analysis = analyze_database.analyze_database(sample_database())

    assert analysis["total_canonical_entries"] == 5
    assert analysis["evaluation"]["status_counts"] == {
        "accepted": 2,
        "rejected": 2,
        "unprocessed": 1,
    }
    assert distribution_values(
        analysis["evaluation"]["algorithm_version_counts"]
    ) == {
        None: 2,
        CURRENT_EVALUATION_ALGORITHM_VERSION: 2,
        CURRENT_EVALUATION_ALGORITHM_VERSION + 1: 1,
    }
    assert distribution_values(
        analysis["evaluation"]["confidence_distributions"][
            "content_confidence"
        ]["all"]
    ) == {None: 1, -2: 1, -1: 1, 0: 1, 2: 1}
    assert distribution_values(
        analysis["evaluation"]["confidence_distributions"][
            "human_confidence"
        ]["rejected"]
    ) == {-3: 1, 0: 1}
    assert distribution_values(
        analysis["cross_tabs"]["status_by_philosopher_confidence"]["accepted"]
    ) == {1: 1, 4: 1}
    assert distribution_values(
        analysis["cross_tabs"]["status_by_content_confidence"]["unprocessed"]
    ) == {None: 1}
    assert distribution_values(
        analysis["cross_tabs"]["status_by_algorithm_version"]["rejected"]
    ) == {
        CURRENT_EVALUATION_ALGORITHM_VERSION: 1,
        CURRENT_EVALUATION_ALGORITHM_VERSION + 1: 1,
    }


def test_analyze_database_sorts_none_separately_and_numeric_values_numerically():
    analysis = analyze_database.analyze_database(sample_database())
    distribution = analysis["evaluation"]["confidence_distributions"][
        "content_confidence"
    ]["all"]

    assert [item["value"] for item in distribution] == [None, -2, -1, 0, 2]


def test_analyze_database_reports_quote_posting_candidate_and_migration_statistics():
    analysis = analyze_database.analyze_database(sample_database())

    assert analysis["quotes"]["zero_quotes"] == 2
    assert analysis["quotes"]["one_or_more_quotes"] == 3
    assert analysis["quotes"]["quote_count_distribution"] == [
        {"value": 0, "count": 2},
        {"value": 1, "count": 1},
        {"value": 2, "count": 1},
        {"value": 3, "count": 1},
    ]
    assert analysis["quotes"]["summary"] == {
        "count": 5,
        "zero_count": 2,
        "nonzero_count": 3,
        "mean": 1.2,
        "median": 1,
        "p75": 2,
        "p90": 2.6,
        "p95": 2.8,
        "p99": 2.96,
        "minimum": 0,
        "maximum": 3,
    }
    assert analysis["quotes"]["nonzero_summary"] == {
        "count": 3,
        "mean": 2,
        "median": 2,
        "p25": 1.5,
        "p75": 2.5,
        "p90": 2.8,
        "p95": 2.9,
        "p99": 2.98,
        "minimum": 1,
        "maximum": 3,
    }
    assert analysis["quotes"]["failure_reason_counts"] == {"request_exception": 1}
    assert analysis["quotes"]["accepted"]["summary"] == {
        "count": 2,
        "zero_count": 0,
        "nonzero_count": 2,
        "mean": 1.5,
        "median": 1.5,
        "p75": 1.75,
        "p90": 1.9,
        "p95": 1.95,
        "p99": 1.99,
        "minimum": 1,
        "maximum": 2,
    }
    posting = analysis["posting"]
    assert posting["posted_entries"] == 1
    assert posting["unposted_entries"] == 4
    assert posting["current_candidate_count"] == 1
    assert posting["current_candidates"]["content_confidence_distribution"] == [
        {"value": 2, "count": 1},
    ]
    assert posting["current_candidates"]["quote_count_distribution"] == [
        {"value": 2, "count": 1},
    ]
    assert posting["current_candidates"]["quote_statistics"]["summary"][
        "count"
    ] == 1
    assert analysis["migration_version"] == {
        "current_version": {"accepted": 1, "rejected": 1},
        "historical_none": {"accepted": 1, "rejected": 0},
        "noncurrent_explicit": {
            "accepted": 0,
            "rejected": 1,
            "by_version": [{
                "value": CURRENT_EVALUATION_ALGORITHM_VERSION + 1,
                "count": 1,
            }],
        },
        "unprocessed_entries": 1,
        "migration_conflict_entries": 2,
    }


def test_analyze_database_reports_quote_parser_version_rollout_counts():
    current = entry("Current", "accepted", 2, 2, 2, 2, 1)
    stale_candidate = entry("Stale Candidate", "accepted", 2, 2, 2, 2, 1)
    stale_candidate["quotes"]["parser_version"] = None
    stale_posted = entry("Stale Posted", "accepted", 2, 2, 2, 2, 1)
    stale_posted["quotes"]["parser_version"] = 1
    stale_posted["posting"]["has_been_posted"] = True
    stale_rejected = entry("Stale Rejected", "rejected", 2, 0, 0, 0, 1)
    del stale_rejected["quotes"]["parser_version"]

    analysis = analyze_database.analyze_database({
        item["title"]: item
        for item in (current, stale_candidate, stale_posted, stale_rejected)
    })

    assert analysis["quotes"]["parser_versions"] == {
        "available_current": 1,
        "available_stale": 3,
        "accepted_unposted_stale": 1,
        "accepted_posted_stale": 1,
        "rejected_stale": 1,
        "available_by_version": [
            {"value": None, "count": 2},
            {"value": 1, "count": 1},
            {"value": CURRENT_QUOTE_PARSER_VERSION, "count": 1},
        ],
    }


def test_analyze_database_reports_current_parser_quote_source_coverage():
    sourced = entry("Sourced", "accepted", 2, 2, 2, 2, 1)
    sourced["quotes"]["items"][0]["source"].update({
        "work": "Example Work",
        "year": 1932,
        "date": "12 March 1932",
        "details": "Ch. 2",
        "citation": "Example Work, 12 March 1932, Ch. 2",
        "url": "https://en.wikiquote.org/wiki/Example_Work",
    })
    sourceless = entry("Sourceless", "accepted", 2, 2, 2, 2, 1)
    stale = entry("Stale", "accepted", 2, 2, 2, 2, 1)
    stale["quotes"]["parser_version"] = None

    analysis = analyze_database.analyze_database({
        item["title"]: item for item in (sourced, sourceless, stale)
    })

    assert analysis["quotes"]["source_metadata"] == {
        "total_current_parser_quote_items": 2,
        "with_any_source_metadata": 1,
        "with_citation": 1,
        "with_work": 1,
        "with_year": 1,
        "with_date": 1,
        "with_details": 1,
        "with_url": 1,
        "without_source_metadata": 1,
    }


def test_analyze_database_reports_migration_conflict_multiplicity_and_cross_tabs():
    analysis = analyze_database.analyze_database(sample_database())

    assert analysis["migration_conflicts"] == {
        "entries_with_conflicts": 2,
        "total_conflict_objects": 3,
        "conflicts_per_entry_distribution": [
            {"value": 0, "count": 3},
            {"value": 1, "count": 1},
            {"value": 2, "count": 1},
        ],
        "conflicted_entry_summary": {
            "count": 2,
            "mean": 1.5,
            "median": 1.5,
            "maximum": 2,
        },
        "top_conflicted_titles": [
            {"title": "Delta", "conflict_count": 2},
            {"title": "Epsilon", "conflict_count": 1},
        ],
        "by_field": {
            "evaluation.status": 2,
            "quotes.failure": 1,
        },
        "by_resolution": {
            "preserve_existing": 1,
            "unprocessed": 2,
        },
        "by_field_and_resolution": {
            "evaluation.status": {"unprocessed": 2},
            "quotes.failure": {"preserve_existing": 1},
        },
    }


def test_percentile_uses_linear_interpolation_and_handles_empty_and_singleton():
    assert analyze_database.linear_percentile([], 50) is None
    assert analyze_database.linear_percentile([7], 25) == 7
    assert analyze_database.linear_percentile([0, 10, 20, 30], 25) == 7.5
    assert analyze_database.linear_percentile([0, 10, 20, 30], 50) == 15
    assert analyze_database.linear_percentile([0, 10, 20, 30], 90) == 27


def test_histogram_bin_count_is_an_integer_for_float_selection_weights():
    assert analyze_database._histogram_bin_count([3.2, 3.8]) == 2


def test_analyze_database_reports_distinct_quote_selection_weight_curve():
    analysis = analyze_database.analyze_database(sample_database())
    curve = analysis["quotes"]["selection_weight_curve"]

    assert curve[0] == {
        "word_count": 2,
        "quote_selection_weight": pytest.approx(1 / 8 ** 0.5),
    }
    assert curve[6]["word_count"] == 8
    assert curve[0]["quote_selection_weight"] == pytest.approx(
        curve[6]["quote_selection_weight"]
    )
    assert curve[18]["word_count"] == 20
    assert curve[18]["quote_selection_weight"] < curve[6]["quote_selection_weight"]
    assert curve[-1]["word_count"] == 100


def test_analyze_database_reports_purged_quotes_separately():
    database = sample_database()
    entry_value = database["Alpha"]
    entry_value["evaluation"]["status"] = "rejected"
    entry_value["quotes"].update({
        "status": "purged", "items": [], "failure": None,
        "fetched_at": None, "parser_version": None,
    })

    assert analyze_database.analyze_database(database)["quotes"]["purged"] == {
        "entry_count": 1,
        "quote_item_count": 0,
    }


def test_analyze_database_adds_confidence_numeric_summaries_without_coercing_none():
    analysis = analyze_database.analyze_database(sample_database())

    assert analysis["evaluation"]["confidence_summaries"][
        "content_confidence"
    ]["all"] == {
        "count": 4,
        "minimum": -2,
        "median": -0.5,
        "mean": -0.25,
        "maximum": 2,
    }
    assert distribution_values(
        analysis["evaluation"]["confidence_distributions"][
            "content_confidence"
        ]["all"]
    )[None] == 1


def test_analyze_database_groups_malformed_conflict_metadata_defensively():
    database = sample_database()
    database["Alpha"]["migration"]["conflicts"] = [
        None,
        {"field": None, "resolution": 4},
    ]

    analysis = analyze_database.analyze_database(database)

    assert analysis["migration_conflicts"]["total_conflict_objects"] == 5
    assert analysis["migration_conflicts"]["by_field"]["<missing_or_invalid>"] == 2
    assert (
        analysis["migration_conflicts"]["by_resolution"][
            "<missing_or_invalid>"
        ]
        == 2
    )


def test_analyze_database_reports_candidate_quote_zoom_statistics():
    analysis = analyze_database.analyze_database(candidate_database())
    candidates = analysis["posting"]["current_candidates"]

    assert candidates["quote_count_distribution"] == [
        {"value": 1, "count": 1},
        {"value": 2, "count": 1},
        {"value": 3, "count": 1},
        {"value": 100, "count": 1},
    ]
    assert candidates["quote_zoom"]["upper_bound"] == pytest.approx(70.9)
    assert candidates["quote_zoom"]["above_upper_bound_count"] == 1


def test_analyze_database_reports_joint_human_philosopher_matrices():
    database = sample_database()
    boolean_entry = entry("Boolean", "unprocessed", None, True, False, None, 0)
    database["Boolean"] = boolean_entry

    matrices = analyze_database.analyze_database(database)["joint_confidence"]

    assert matrices["all"] == {
        "human_values": [-3, 0, 1, 3],
        "philosopher_values": [-1, 1, 2, 4],
        "counts": [
            [0, 0, 1, 0],
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 0, 1],
        ],
    }
    assert matrices["accepted"] == {
        "human_values": [1, 3],
        "philosopher_values": [1, 4],
        "counts": [[1, 0], [0, 1]],
    }
    assert matrices["rejected"] == {
        "human_values": [-3, 0],
        "philosopher_values": [-1, 2],
        "counts": [[0, 1], [1, 0]],
    }
    assert matrices["top_pairs"] == {
        "accepted": [
            {
                "human_confidence": 1,
                "philosopher_confidence": 1,
                "entry_count": 1,
            },
            {
                "human_confidence": 3,
                "philosopher_confidence": 4,
                "entry_count": 1,
            },
        ],
        "rejected": [
            {
                "human_confidence": -3,
                "philosopher_confidence": 2,
                "entry_count": 1,
            },
            {
                "human_confidence": 0,
                "philosopher_confidence": -1,
                "entry_count": 1,
            },
        ],
    }


def test_analyze_database_reuses_candidate_selection_weight_and_sorts_samples(
    monkeypatch,
):
    database = candidate_database()
    calls = []

    def weight(entry_value):
        calls.append(entry_value["title"])
        return 10 + entry_value["evaluation"]["content_confidence"]

    monkeypatch.setattr(analyze_database, "candidate_selection_weight", weight)
    weights = analyze_database.analyze_database(database)["posting"][
        "current_candidates"
    ]["selection_weights"]

    assert calls == ["Alpha", "Beta", "Gamma", "Zeta"]
    assert weights["summary"] == {
        "count": 4,
        "minimum": 11,
        "mean": 11.75,
        "median": 12.0,
        "p25": 11.75,
        "p75": 12.0,
        "p90": 12.0,
        "p95": 12.0,
        "p99": 12.0,
        "maximum": 12,
    }
    assert weights["top_candidates"] == [
        {
            "title": "Alpha",
            "selection_weight": 12,
            "content_confidence": 2,
            "quote_count": 2,
        },
        {
            "title": "Gamma",
            "selection_weight": 12,
            "content_confidence": 2,
            "quote_count": 3,
        },
        {
            "title": "Zeta",
            "selection_weight": 12,
            "content_confidence": 2,
            "quote_count": 1,
        },
        {
            "title": "Beta",
            "selection_weight": 11,
            "content_confidence": 1,
            "quote_count": 100,
        },
    ]
    assert weights["bottom_candidates"] == [
        weights["top_candidates"][-1],
        *weights["top_candidates"][:-1],
    ]


def test_analyze_database_candidate_weights_match_production_helper_without_mutation():
    database = candidate_database()
    before = copy.deepcopy(database)

    records = analyze_database.analyze_database(database)["posting"][
        "current_candidates"
    ]["selection_weights"]["by_candidate"]

    assert records == [
        {
            "title": title,
            "selection_weight": utils.candidate_selection_weight(database[title]),
            "content_confidence": database[title]["evaluation"][
                "content_confidence"
            ],
            "quote_count": len(database[title]["quotes"]["items"]),
        }
        for title in ("Alpha", "Beta", "Gamma", "Zeta")
    ]
    assert records[1]["selection_weight"] == 3
    assert {
        record["selection_weight"]
        for record in records
        if record["content_confidence"] == 2
    } == {4}
    assert database == before


@pytest.mark.parametrize("reason, category", [
    (
        "title human bonus (+1): exact (philosopher)",
        "title_human_bonus",
    ),
    (
        "title philosopher bonus (+2): exact (philosopher)",
        "title_philosopher_bonus",
    ),
    (
        "summary human bonus (+1): direct biographical philosopher statement",
        "summary_human_bonus",
    ),
    (
        "summary philosopher bonus (+2): direct biographical philosopher statement",
        "summary_philosopher_bonus",
    ),
    ("title nonhuman penalty (-1): :", "title_nonhuman_penalty"),
    (
        "title nonhuman + nonphilosopher penalty (-2): bad word part",
        "title_nonhuman_nonphilosopher_penalty",
    ),
    ("title bonus (+2): (philosopher)", "title_philosopher_bonus"),
    (
        "title philosopher bonus (+1): philosopher)",
        "title_philosopher_bonus",
    ),
    (
        "summary philosopher bonus (+2): is a[n] .* philosopher",
        "summary_philosopher_bonus",
    ),
    (
        "summary nonhuman + nonphilosopher penalty (-2): pattern",
        "summary_nonhuman_nonphilosopher_penalty",
    ),
    (
        "summary human + nonphilosopher penalty/bonus (+-0): author",
        "summary_human_nonphilosopher_mixed",
    ),
    ("summary nonhuman penalty (-1): novel", "summary_nonhuman_penalty"),
    ("summary human bonus (+1): professor", "summary_human_bonus"),
    ("quotes bonus (+1): good quotes exist", "quote_content_bonus"),
    (
        "quotes noncontent penalty (-1): quotes do not exist",
        "quote_content_penalty",
    ),
    ("wikidata human bonus (+2): birth_w not None", "wikidata_human_bonus"),
    (
        "wikidata nonhuman penalty (-1): is_human = false",
        "wikidata_human_penalty",
    ),
    (
        "wikidata philosopher bonus (+2): is_philosopher = true",
        "wikidata_philosopher_bonus",
    ),
    (
        "wikidata nonphilosopher penalty (-1): is_philosopher = false",
        "wikidata_philosopher_penalty",
    ),
])
def test_normalize_evaluation_reason_handles_actual_runtime_templates(reason, category):
    assert analyze_database.normalize_evaluation_reason(reason) == category


def test_normalize_evaluation_reason_is_defensive_and_deterministic():
    assert analyze_database.normalize_evaluation_reason("unknown scoring note") == "other"
    assert analyze_database.normalize_evaluation_reason("") == "other"
    assert analyze_database.normalize_evaluation_reason(None) == "other"
    assert analyze_database.normalize_evaluation_reason(3) == "other"


def test_analyze_database_reports_decision_margins_reason_counts_and_borderlines():
    analysis = analyze_database.analyze_database(margin_database())
    margins = analysis["decision_margins"]

    assert margins["all_processed"]["distribution"] == [
        {"value": -2, "count": 1},
        {"value": 0, "count": 1},
        {"value": 1, "count": 1},
        {"value": 3, "count": 1},
    ]
    assert margins["accepted"]["summary"] == {
        "count": 2,
        "minimum": 1,
        "mean": 2,
        "median": 2.0,
        "p25": 1.5,
        "p75": 2.5,
        "p90": 2.8,
        "maximum": 3,
    }
    assert margins["rejected"]["summary"]["minimum"] == -2
    assert margins["borderline"] == {
        "accepted_count": 1,
        "rejected_count": 1,
        "accepted_sample_titles": ["Accepted Borderline"],
        "rejected_sample_titles": ["Rejected Borderline"],
    }
    assert margins["near_boundary"] == {
        "human_equals_1": 1,
        "philosopher_equals_1": 1,
        "human_equals_0": 1,
        "philosopher_equals_0": 0,
        "margin_equals_1": 1,
        "margin_equals_0": 1,
    }

    categories = analysis["reason_analysis"]["all_processed"]
    assert {
        item["category"]: (item["entry_count"], item["occurrence_count"])
        for item in categories
    }["title_philosopher_bonus"] == (1, 2)
    assert analysis["reason_analysis"]["borderline_accepted"] == [
        {"category": "summary_philosopher_bonus", "entry_count": 1, "occurrence_count": 1},
        {"category": "wikidata_philosopher_bonus", "entry_count": 1, "occurrence_count": 1},
    ]
    assert analysis["reason_analysis"]["cooccurrence"]["accepted"] == [
        {
            "category_a": "summary_philosopher_bonus",
            "category_b": "wikidata_philosopher_bonus",
            "entry_count": 1,
        },
        {
            "category_a": "title_philosopher_bonus",
            "category_b": "wikidata_human_bonus",
            "entry_count": 1,
        },
    ]


def test_borderline_records_are_complete_sorted_and_exclude_large_legacy_data():
    records = analyze_database.collect_borderline_case_records(margin_database())

    assert [record["title"] for record in records["accepted"]] == [
        "Accepted Borderline",
    ]
    assert [record["title"] for record in records["rejected"]] == [
        "Rejected Borderline",
    ]
    record = records["rejected"][0]
    assert set(record) == {
        "title", "display_title", "status", "algorithm_version",
        "human_confidence", "philosopher_confidence", "content_confidence",
        "decision_margin", "raw_reasons", "normalized_reason_categories",
        "summary_text", "wikidata", "quote_count", "has_been_posted",
        "migration_conflict_count",
    }
    assert record["quote_count"] == 0
    assert record["migration_conflict_count"] == 1
    assert "legacy_result" not in record
    assert record["wikidata"] == {
        "status": "unknown",
        "qid": None,
        "is_human": None,
        "is_philosopher": None,
        "birth_year": None,
        "death_year": None,
    }


def test_analyze_database_is_deterministic_and_does_not_mutate_input():
    database = sample_database()
    before = copy.deepcopy(database)

    first = analyze_database.analyze_database(database)
    second = analyze_database.analyze_database(database)

    assert first == second
    assert database == before


def test_cli_loads_canonical_database_without_writing_or_network(
    monkeypatch,
    tmp_path,
    capsys,
):
    database = sample_database()
    database_path = write_database(tmp_path, database)
    before = database_path.read_bytes()

    def forbidden(*args, **kwargs):
        raise AssertionError("analysis must not use network or persistence")

    monkeypatch.setattr(wikipedia_api, "safe_request", forbidden)
    monkeypatch.setattr(cache, "update_database_entry", forbidden)

    assert analyze_database.main(["--data-folder", str(tmp_path)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == analyze_database.analyze_database(database)
    assert database_path.read_bytes() == before


def test_cli_exports_borderline_cases_without_overwriting_or_touching_data(
    monkeypatch,
    tmp_path,
    capsys,
):
    database = margin_database()
    valid_database = {
        title: entry_value
        for title, entry_value in database.items()
        if title != "Ignored Boolean"
    }
    database_path = write_database(tmp_path, valid_database)
    legacy_path = tmp_path / "posted.json"
    legacy_path.write_bytes(b"legacy sentinel")
    before_database = database_path.read_bytes()
    before_legacy = legacy_path.read_bytes()
    output_path = tmp_path / "borderline.json"

    def forbidden(*args, **kwargs):
        raise AssertionError("analysis export must not use network")

    monkeypatch.setattr(wikipedia_api, "safe_request", forbidden)
    assert analyze_database.main([
        "--data-folder", str(tmp_path),
        "--borderline-output", str(output_path),
    ]) == 0

    assert json.loads(output_path.read_text(encoding="utf-8")) == (
        analyze_database.collect_borderline_case_records(valid_database)
    )
    assert database_path.read_bytes() == before_database
    assert legacy_path.read_bytes() == before_legacy
    capsys.readouterr()

    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        analyze_database.main([
            "--data-folder", str(tmp_path),
            "--borderline-output", str(output_path),
        ])


def test_cli_plots_dependency_failure_is_actionable_when_matplotlib_is_missing(
    tmp_path,
):
    if importlib.util.find_spec("matplotlib") is not None:
        pytest.skip("matplotlib is available in this environment")

    database = sample_database()
    write_database(tmp_path, database)

    with pytest.raises(SystemExit, match="requires matplotlib"):
        analyze_database.main([
            "--data-folder", str(tmp_path),
            "--plots", str(tmp_path / "plots"),
        ])


@pytest.mark.skipif(
    importlib.util.find_spec("matplotlib") is None,
    reason="matplotlib is an optional development dependency",
)
def test_generate_plots_writes_expected_static_files_without_mutating_database(
    tmp_path,
):
    database = candidate_database()
    database_path = write_database(tmp_path, database)
    before = database_path.read_bytes()
    analysis = analyze_database.analyze_database(database)

    output_directory = tmp_path / "plots"
    paths = analyze_database.generate_plots(analysis, output_directory)

    assert {path.name for path in paths} == {
        "evaluation_status_counts.png",
        "human_confidence_by_status.png",
        "philosopher_confidence_by_status.png",
        "content_confidence_by_status.png",
        "wikidata_status_counts.png",
        "quote_status_counts.png",
        "nonzero_quote_count_distribution.png",
        "quote_selection_weight_curve.png",
        "migration_conflicts_by_field.png",
        "migration_conflicts_by_resolution.png",
        "candidate_content_confidence.png",
        "candidate_quote_count_distribution.png",
        "candidate_quote_count_distribution_zoomed.png",
        "human_vs_philosopher_all.png",
        "human_vs_philosopher_accepted.png",
        "human_vs_philosopher_rejected.png",
        "candidate_selection_weight_distribution.png",
        "candidate_weight_vs_quote_count.png",
        "decision_margin_by_status.png",
        "accepted_decision_margin.png",
        "rejected_decision_margin.png",
        "borderline_reason_categories.png",
        "reason_categories_by_status.png",
    }
    assert all(path.is_file() and path.stat().st_size > 0 for path in paths)
    assert database_path.read_bytes() == before


def test_main_has_no_analysis_command_dependency():
    tree = ast.parse(Path(main.__file__).read_text(encoding="utf-8"))
    imported_modules = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    assert "analyze_database" not in imported_modules
