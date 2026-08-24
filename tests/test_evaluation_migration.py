import pytest
import wiki_philosopher_bot.evaluation as evaluation
import wiki_philosopher_bot.cli.migrate_database as migrate_database
from wiki_philosopher_bot.config import (
    PROCESSED_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)

def make_evaluation_record(
    line_number,
    record_index,
    value,
):
    return {
        "line_number": line_number,
        "record_index": record_index,
        "value": value,
    }

def normalize(record, filename):
    return migrate_database.normalize_historical_evaluation(
        record,
        filename,
    )

def test_results_boolean_accepted_maps_to_accepted():
    record = make_evaluation_record(
        10,
        3,
        {
            "title": "Ada Lovelace",
            "accepted": True,
        },
    )

    normalized = normalize(record, RESULT_FILE)

    assert normalized.status == "accepted"
    assert normalized.algorithm_version is None
    assert normalized.human_confidence is None
    assert normalized.philosopher_confidence is None
    assert normalized.content_confidence is None
    assert normalized.source["source"] == RESULT_FILE
    assert normalized.source["line_number"] == 10
    assert normalized.source["record_index"] == 3


@pytest.mark.parametrize(
    "source_filename, accepted, expected_status",
    [
        (RESULT_FILE, True, "accepted"),
        (RESULT_FILE, False, "rejected"),
        (PROCESSED_FILE, True, "accepted"),
        (PROCESSED_FILE, False, "rejected"),
    ],
)
def test_explicit_accepted_boolean_is_preserved_for_both_sources(
    source_filename,
    accepted,
    expected_status,
):
    record = make_evaluation_record(
        28,
        22,
        {
            "title": "Ada Lovelace",
            "accepted": accepted,
            "reasons": [],
        },
    )

    normalized = normalize(record, source_filename)

    assert normalized.status == expected_status


@pytest.mark.parametrize(
    "source_filename, expected_status",
    [
        (RESULT_FILE, "accepted"),
        (PROCESSED_FILE, "rejected"),
    ],
)
def test_filename_is_fallback_only_without_explicit_decision(
    source_filename,
    expected_status,
):
    record = make_evaluation_record(
        29,
        23,
        {
            "title": "Ada Lovelace",
            "reasons": [],
        },
    )

    normalized = normalize(record, source_filename)

    assert normalized.status == expected_status


def test_rejected_results_candidate_reaches_conflict_resolution():
    rejected_record = make_evaluation_record(
        30,
        24,
        {
            "title": "Ada Lovelace",
            "accepted": False,
            "human_confidence": -1,
            "philosopher_confidence": -2,
            "content_confidence": 0,
            "reasons": ["rejected by historical result"],
        },
    )
    accepted_record = make_evaluation_record(
        31,
        25,
        {
            "title": "Ada Lovelace",
            "accepted": True,
            "human_confidence": 1,
            "philosopher_confidence": 2,
            "content_confidence": 0,
            "reasons": ["accepted by historical processed record"],
        },
    )

    rejected = normalize(rejected_record, RESULT_FILE)
    accepted = normalize(accepted_record, PROCESSED_FILE)
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )

    migrate_database.apply_historical_evaluation(
        entry,
        [rejected, accepted],
    )

    assert entry["evaluation"]["status"] == "unprocessed"
    assert any(
        conflict["field"] == "evaluation.status"
        for conflict in entry["migration"]["conflicts"]
    )

def test_results_without_accepted_uses_results_file_semantics():
    record = make_evaluation_record(
        11,
        4,
        {
            "title": "Ada Lovelace",
            "score": 9,
            "confidence": 8,
            "summary": "Ada Lovelace was a mathematician.",
            "is_human": True,
            "is_philosopher": False,
            "birth_w": 1815,
            "death_w": 1852,
            "quotes": [],
            "reasons": ["historical result"],
        },
    )

    normalized = normalize(record, RESULT_FILE)

    assert normalized.status == "accepted"
    assert normalized.human_confidence is None
    assert normalized.philosopher_confidence is None
    assert normalized.content_confidence is None

def test_processed_without_status_uses_rejected_file_semantics():
    record = make_evaluation_record(
        12,
        5,
        {
            "title": "Ada Lovelace",
            "score": 0,
            "confidence": 0,
            "reasons": ["historical processed result"],
            "result": None,
        },
    )

    normalized = normalize(record, PROCESSED_FILE)

    assert normalized.status == "rejected"
    assert normalized.human_confidence is None
    assert normalized.philosopher_confidence is None
    assert normalized.content_confidence is None

def test_current_status_record_preserves_all_named_confidences():
    record = make_evaluation_record(
        13,
        6,
        {
            "title": "Ada Lovelace",
            "status": "accepted",
            "human_confidence": 3,
            "philosopher_confidence": 4,
            "content_confidence": -1,
            "reasons": ["reason one", "reason two"],
            "last_processed": 1780580890.25,
        },
    )

    normalized = normalize(record, RESULT_FILE)

    assert normalized.status == "accepted"
    assert normalized.human_confidence == 3
    assert normalized.philosopher_confidence == 4
    assert normalized.content_confidence == -1
    assert normalized.reasons == ["reason one", "reason two"]
    assert normalized.processed_at == 1780580890.25
    assert normalized.algorithm_version is None

def test_missing_named_confidences_become_null():
    record = make_evaluation_record(
        14,
        7,
        {
            "title": "Ada Lovelace",
            "accepted": False,
            "score": 2,
            "confidence": 1,
            "reasons": [],
            "result": None,
        },
    )

    normalized = normalize(record, PROCESSED_FILE)

    assert normalized.human_confidence is None
    assert normalized.philosopher_confidence is None
    assert normalized.content_confidence is None

def test_generic_score_and_confidence_are_not_canonical_fields():
    record = make_evaluation_record(
        15,
        8,
        {
            "title": "Ada Lovelace",
            "accepted": True,
            "score": 100,
            "confidence": 99,
            "reasons": [],
        },
    )

    normalized = normalize(record, RESULT_FILE)
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )

    assert normalized.human_confidence is None
    assert normalized.philosopher_confidence is None
    assert normalized.content_confidence is None
    assert "score" not in entry
    assert "confidence" not in entry
    assert "score" not in entry["evaluation"]
    assert "confidence" not in entry["evaluation"]

def test_processed_result_object_maps_to_legacy_result():
    payload = {"some": "historical payload"}
    record = make_evaluation_record(
        16,
        9,
        {
            "title": "Ada Lovelace",
            "accepted": False,
            "reasons": [],
            "result": payload,
        },
    )

    normalized = normalize(record, PROCESSED_FILE)

    assert normalized.legacy_result == payload

def test_null_processed_result_does_not_create_legacy_result_payload():
    record = make_evaluation_record(
        17,
        10,
        {
            "title": "Ada Lovelace",
            "accepted": False,
            "reasons": [],
            "result": None,
        },
    )

    normalized = normalize(record, PROCESSED_FILE)

    assert normalized.legacy_result is None

def test_results_non_null_result_is_rejected_until_schema_is_clarified():
    record = make_evaluation_record(
        18,
        11,
        {
            "title": "Ada Lovelace",
            "accepted": True,
            "reasons": [],
            "result": {"unsupported": "payload"},
        },
    )

    with pytest.raises(ValueError):
        normalize(record, RESULT_FILE)

def test_unknown_explicit_status_is_rejected():
    record = make_evaluation_record(
        19,
        12,
        {
            "title": "Ada Lovelace",
            "status": "maybe",
            "reasons": [],
        },
    )

    with pytest.raises(ValueError):
        normalize(record, RESULT_FILE)

@pytest.mark.parametrize(
    "invalid_confidence",
    ["3", True],
)
def test_wrong_named_confidence_type_is_rejected(invalid_confidence):
    record = make_evaluation_record(
        20,
        13,
        {
            "title": "Ada Lovelace",
            "status": "accepted",
            "human_confidence": invalid_confidence,
            "reasons": [],
        },
    )

    with pytest.raises(ValueError):
        normalize(record, RESULT_FILE)

@pytest.mark.parametrize(
    "invalid_reasons",
    ["not-a-list", ["valid", 123]],
)
def test_malformed_reasons_are_rejected(invalid_reasons):
    record = make_evaluation_record(
        21,
        14,
        {
            "title": "Ada Lovelace",
            "status": "accepted",
            "reasons": invalid_reasons,
        },
    )

    with pytest.raises(ValueError):
        normalize(record, RESULT_FILE)

def test_migration_never_calls_evaluator(monkeypatch):
    def evaluator_must_not_run(*args, **kwargs):
        raise AssertionError("Migration must not evaluate historical data")

    for name in (
        "process_title",
        "title_filter",
        "summary_filter",
        "wikidata_filter",
        "quote_filter",
        "combine_filter_results",
    ):
        monkeypatch.setattr(
            evaluation,
            name,
            evaluator_must_not_run,
        )

    results_record = make_evaluation_record(
        22,
        15,
        {
            "title": "Ada Lovelace",
            "accepted": True,
            "reasons": [],
        },
    )
    processed_record = make_evaluation_record(
        23,
        16,
        {
            "title": "Grace Hopper",
            "accepted": False,
            "reasons": [],
            "result": None,
        },
    )

    assert normalize(results_record, RESULT_FILE).status == "accepted"
    assert normalize(processed_record, PROCESSED_FILE).status == "rejected"

def test_explicit_status_takes_precedence_over_boolean_accepted():
    record = make_evaluation_record(
        24,
        17,
        {
            "title": "Ada Lovelace",
            "status": "accepted",
            "accepted": False,
            "reasons": [],
        },
    )

    normalized = normalize(record, RESULT_FILE)

    assert normalized.status == "accepted"

def test_results_file_rejects_explicit_rejected_status_conflict():
    record = make_evaluation_record(
        25,
        18,
        {
            "title": "Ada Lovelace",
            "status": "rejected",
            "reasons": [],
        },
    )

    with pytest.raises(ValueError):
        normalize(record, RESULT_FILE)

def test_processed_file_rejects_explicit_accepted_status_conflict():
    record = make_evaluation_record(
        26,
        19,
        {
            "title": "Ada Lovelace",
            "status": "accepted",
            "reasons": [],
        },
    )

    with pytest.raises(ValueError):
        normalize(record, PROCESSED_FILE)

def test_historical_evaluation_source_location_is_preserved():
    record = make_evaluation_record(
        27,
        20,
        {
            "title": "Ada Lovelace",
            "accepted": True,
            "reasons": [],
        },
    )

    normalized = normalize(record, RESULT_FILE)

    assert normalized.source == {
        "source": RESULT_FILE,
        "line_number": 27,
        "record_index": 20,
    }

def test_embedded_summary_is_only_fallback():
    record = make_evaluation_record(
        28,
        21,
        {
            "title": "Ada Lovelace",
            "summary": "Embedded historical summary.",
        },
    )

    empty_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    migrate_database.merge_embedded_legacy_facts(
        empty_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert empty_entry["summary"]["text"] == (
        "Embedded historical summary."
    )

    specialized_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    specialized_entry["summary"]["text"] = (
        "Specialized summary."
    )
    migrate_database.add_legacy_source(
        specialized_entry,
        SUMMARY_FILE,
    )

    migrate_database.merge_embedded_legacy_facts(
        specialized_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert specialized_entry["summary"]["text"] == (
        "Specialized summary."
    )

def test_embedded_wikidata_facts_are_only_fallback():
    record = make_evaluation_record(
        29,
        22,
        {
            "title": "Ada Lovelace",
            "is_human": True,
            "is_philosopher": False,
            "birth_w": 1815,
            "death_w": 1852,
        },
    )

    empty_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    migrate_database.merge_embedded_legacy_facts(
        empty_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert empty_entry["wikidata"]["is_human"] is True
    assert empty_entry["wikidata"]["is_philosopher"] is False
    assert empty_entry["wikidata"]["birth_year"] == 1815
    assert empty_entry["wikidata"]["death_year"] == 1852

    specialized_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    specialized_entry["wikidata"] = {
        "status": "available",
        "reason": None,
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q170790"],
        "birth_year": 1815,
        "death_year": 1852,
        "is_human": True,
        "is_philosopher": True,
        "fetched_at": None,
    }

    migrate_database.merge_embedded_legacy_facts(
        specialized_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert specialized_entry["wikidata"]["qid"] == "Q7259"
    assert specialized_entry["wikidata"]["is_philosopher"] is True

def test_embedded_quotes_are_only_fallback():
    quote = {
        "text": "Embedded quote with enough text for migration testing.",
        "length": 53,
        "word_count": 8,
        "source": "Wikiquote",
    }
    record = make_evaluation_record(
        30,
        23,
        {
            "title": "Ada Lovelace",
            "quotes": [quote],
        },
    )

    empty_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    migrate_database.merge_embedded_legacy_facts(
        empty_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert empty_entry["quotes"]["items"] == [quote]
    assert empty_entry["quotes"]["status"] == "available"

    specialized_entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    specialized_quote = {
        "text": "Specialized quote with enough text for testing.",
        "length": 49,
        "word_count": 7,
        "source": "Wikiquote",
    }
    specialized_entry["quotes"]["status"] = "available"
    specialized_entry["quotes"]["items"] = [specialized_quote]
    migrate_database.add_legacy_source(
        specialized_entry,
        QUOTE_FILE,
    )

    migrate_database.merge_embedded_legacy_facts(
        specialized_entry,
        record,
        RESULT_FILE,
        None,
    )

    assert specialized_entry["quotes"]["items"] == [
        specialized_quote
    ]

@pytest.mark.parametrize(
    "invalid_value",
    [
        "1780580890",
        True,
    ],
)
def test_invalid_last_processed_type_is_rejected(
    invalid_value,
):
    raw_record = {
        "line_number": 1,
        "record_index": 1,
        "value": {
            "title": "Ada Lovelace",
            "status": "accepted",
            "human_confidence": 1,
            "philosopher_confidence": 1,
            "content_confidence": 0,
            "reasons": [],
            "last_processed": invalid_value,
        },
    }

    with pytest.raises(
        ValueError,
        match="last_processed",
    ):
        migrate_database.normalize_historical_evaluation(
            raw_record,
            RESULT_FILE,
        )


def make_historical_evaluation(
    status,
    human_confidence,
    philosopher_confidence,
    content_confidence,
    reasons,
    processed_at,
    legacy_result,
    source_filename,
    line_number,
    record_index,
):
    return migrate_database.HistoricalEvaluation(
        status=status,
        algorithm_version=None,
        human_confidence=human_confidence,
        philosopher_confidence=philosopher_confidence,
        content_confidence=content_confidence,
        reasons=reasons,
        processed_at=processed_at,
        legacy_result=legacy_result,
        source={
            "source": source_filename,
            "line_number": line_number,
            "record_index": record_index,
        },
    )


def apply_candidates(entry, candidates):
    migrate_database.apply_historical_evaluation(
        entry,
        candidates,
    )


def test_accepted_only_preserves_one_historical_evaluation():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    candidate = make_historical_evaluation(
        "accepted",
        3,
        4,
        -1,
        ["accepted reason one", "accepted reason two"],
        1780580890.25,
        None,
        RESULT_FILE,
        100,
        1,
    )

    apply_candidates(entry, [candidate])

    assert entry["evaluation"] == {
        "status": "accepted",
        "algorithm_version": None,
        "human_confidence": 3,
        "philosopher_confidence": 4,
        "content_confidence": -1,
        "reasons": [
            "accepted reason one",
            "accepted reason two",
        ],
        "legacy_result": None,
        "processed_at": 1780580890.25,
    }


def test_rejected_only_preserves_one_historical_evaluation():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    candidate = make_historical_evaluation(
        "rejected",
        -3,
        -4,
        1,
        ["rejected reason"],
        1780580891.5,
        None,
        PROCESSED_FILE,
        101,
        2,
    )

    apply_candidates(entry, [candidate])

    assert entry["evaluation"] == {
        "status": "rejected",
        "algorithm_version": None,
        "human_confidence": -3,
        "philosopher_confidence": -4,
        "content_confidence": 1,
        "reasons": ["rejected reason"],
        "legacy_result": None,
        "processed_at": 1780580891.5,
    }


def test_untimestamped_accepted_rejected_conflict_becomes_unprocessed():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    accepted = make_historical_evaluation(
        "accepted", 3, 4, 5, ["accepted"], None, None,
        RESULT_FILE, 110, 3,
    )
    rejected = make_historical_evaluation(
        "rejected", -3, -4, -5, ["rejected"], None, None,
        PROCESSED_FILE, 120, 4,
    )

    apply_candidates(entry, [accepted, rejected])

    evaluation = entry["evaluation"]
    assert evaluation["status"] == "unprocessed"
    assert evaluation["algorithm_version"] is None
    assert evaluation["human_confidence"] is None
    assert evaluation["philosopher_confidence"] is None
    assert evaluation["content_confidence"] is None
    assert evaluation["reasons"] == []
    assert evaluation["processed_at"] is None

    status_conflict = next(
        conflict
        for conflict in entry["migration"]["conflicts"]
        if conflict["field"] == "evaluation.status"
    )
    assert status_conflict["resolution"] == (
        "unresolved_set_unprocessed"
    )
    assert [
        value["source"]
        for value in status_conflict["values"]
    ] == [RESULT_FILE, PROCESSED_FILE]


def test_newer_timestamped_conflicting_evaluation_is_selected():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    accepted = make_historical_evaluation(
        "accepted", 1, 2, 3, ["older accepted"], 100,
        None, RESULT_FILE, 130, 5,
    )
    rejected = make_historical_evaluation(
        "rejected", -10, -20, -30, ["newer rejected"], 200,
        {"result": "newer"}, PROCESSED_FILE, 140, 6,
    )

    apply_candidates(entry, [accepted, rejected])

    assert entry["evaluation"] == {
        "status": "rejected",
        "algorithm_version": None,
        "human_confidence": -10,
        "philosopher_confidence": -20,
        "content_confidence": -30,
        "reasons": ["newer rejected"],
        "legacy_result": {"result": "newer"},
        "processed_at": 200,
    }
    status_conflict = next(
        conflict
        for conflict in entry["migration"]["conflicts"]
        if conflict["field"] == "evaluation.status"
    )
    assert status_conflict["resolution"] == (
        "selected_unique_latest_timestamp"
    )


def test_equal_timestamp_conflicting_evaluations_become_unprocessed():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    accepted = make_historical_evaluation(
        "accepted", 1, 2, 3, ["accepted"], 300,
        None, RESULT_FILE, 150, 7,
    )
    rejected = make_historical_evaluation(
        "rejected", -1, -2, -3, ["rejected"], 300,
        None, PROCESSED_FILE, 151, 8,
    )

    apply_candidates(entry, [accepted, rejected])

    assert entry["evaluation"] == {
        "status": "unprocessed",
        "algorithm_version": None,
        "human_confidence": None,
        "philosopher_confidence": None,
        "content_confidence": None,
        "reasons": [],
        "legacy_result": None,
        "processed_at": None,
    }
    assert any(
        conflict["field"] == "evaluation.status"
        for conflict in entry["migration"]["conflicts"]
    )


def test_conflict_does_not_mix_confidences_reasons_or_processed_at():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    accepted = make_historical_evaluation(
        "accepted", 10, 11, 12, ["accepted reason"], None,
        None, RESULT_FILE, 160, 9,
    )
    rejected = make_historical_evaluation(
        "rejected", -20, -21, -22, ["rejected reason"], None,
        None, PROCESSED_FILE, 161, 10,
    )

    apply_candidates(entry, [accepted, rejected])

    evaluation = entry["evaluation"]
    assert evaluation["human_confidence"] is None
    assert evaluation["philosopher_confidence"] is None
    assert evaluation["content_confidence"] is None
    assert evaluation["reasons"] == []
    assert evaluation["processed_at"] is None


def test_identical_historical_evaluation_candidates_are_collapsed_without_conflict():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    first = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], 400,
        {"payload": "same"}, PROCESSED_FILE, 170, 11,
    )
    second = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], 400,
        {"payload": "same"}, PROCESSED_FILE, 171, 12,
    )

    apply_candidates(entry, [first, second])

    assert entry["evaluation"]["status"] == "rejected"
    assert entry["evaluation"]["human_confidence"] == 1
    assert entry["migration"]["conflicts"] == []


def test_same_status_different_untimestamped_bundles_become_unprocessed():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    first = make_historical_evaluation(
        "accepted", 1, 2, 3, ["first"], None,
        None, RESULT_FILE, 180, 13,
    )
    second = make_historical_evaluation(
        "accepted", 4, 5, 6, ["second"], None,
        None, RESULT_FILE, 181, 14,
    )

    apply_candidates(entry, [first, second])

    assert entry["evaluation"]["status"] == "unprocessed"
    assert entry["evaluation"]["human_confidence"] is None
    assert entry["evaluation"]["reasons"] == []
    assert any(
        conflict["field"] == "evaluation"
        and conflict["resolution"] == "unresolved_set_unprocessed"
        for conflict in entry["migration"]["conflicts"]
    )


def test_unique_latest_timestamp_selects_complete_same_status_bundle():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    older = make_historical_evaluation(
        "accepted", 1, 2, 3, ["older"], 500,
        None, RESULT_FILE, 190, 15,
    )
    newer = make_historical_evaluation(
        "accepted", 4, 5, 6, ["newer"], 600,
        None, RESULT_FILE, 191, 16,
    )

    apply_candidates(entry, [older, newer])

    assert entry["evaluation"] == {
        "status": "accepted",
        "algorithm_version": None,
        "human_confidence": 4,
        "philosopher_confidence": 5,
        "content_confidence": 6,
        "reasons": ["newer"],
        "legacy_result": None,
        "processed_at": 600,
    }
    assert any(
        conflict["field"] == "evaluation"
        and conflict["resolution"]
        == "selected_unique_latest_timestamp"
        for conflict in entry["migration"]["conflicts"]
    )


def test_conflicting_legacy_results_are_cleared_and_reported():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    first = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        {"payload": "first"}, PROCESSED_FILE, 200, 17,
    )
    second = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        {"payload": "second"}, PROCESSED_FILE, 201, 18,
    )

    apply_candidates(entry, [first, second])

    assert entry["evaluation"]["legacy_result"] is None
    conflict = next(
        conflict
        for conflict in entry["migration"]["conflicts"]
        if conflict["field"] == "evaluation.legacy_result"
    )
    assert [
        value["source"]
        for value in conflict["values"]
    ] == [PROCESSED_FILE, PROCESSED_FILE]


def test_identical_non_null_legacy_results_are_preserved():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    first = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        {"payload": "same"}, PROCESSED_FILE, 210, 19,
    )
    second = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        {"payload": "same"}, PROCESSED_FILE, 211, 20,
    )

    apply_candidates(entry, [first, second])

    assert entry["evaluation"]["legacy_result"] == {
        "payload": "same"
    }
    assert not any(
        conflict["field"] == "evaluation.legacy_result"
        for conflict in entry["migration"]["conflicts"]
    )


def test_null_legacy_result_does_not_conflict_with_identical_known_legacy_result():
    entry = migrate_database.make_empty_database_entry(
        "Ada Lovelace"
    )
    unknown = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        None, PROCESSED_FILE, 220, 21,
    )
    known = make_historical_evaluation(
        "rejected", 1, 2, 3, ["same"], None,
        {"payload": "known"}, PROCESSED_FILE, 221, 22,
    )

    apply_candidates(entry, [unknown, known])

    assert entry["evaluation"]["legacy_result"] == {
        "payload": "known"
    }
    assert not any(
        conflict["field"] == "evaluation.legacy_result"
        for conflict in entry["migration"]["conflicts"]
    )
