from copy import deepcopy

from wiki_philosopher_bot.database_schema import make_empty_database_entry
from wiki_philosopher_bot.summary_quality import audit_summary_quality
from wiki_philosopher_bot.utils import (
    is_posting_candidate,
    is_semantically_postable_philosopher,
)


def entry(title, status, summary, *, posted=False):
    value = make_empty_database_entry(title)
    value["evaluation"]["status"] = status
    value["summary"]["text"] = summary
    value["posting"]["has_been_posted"] = posted
    return value


def test_rejected_disambiguation_and_surname_summaries_are_informational_only():
    database = {
        "Edward Lowe": entry(
            "Edward Lowe", "rejected",
            "Edward Lowe may refer to:Ordered chronologicallyEdward Lowe.",
        ),
        "Schultze": entry(
            "Schultze", "rejected",
            "A surname list includes:Bernhard Schultze, and swamiFritz Schultze.",
        ),
    }
    original = deepcopy(database)

    report = audit_summary_quality(database)

    assert report["semantically_postable_records_checked"] == 0
    assert report["rejected_or_non_postable_records_skipped"] == 2
    assert report["suspicious_summary_findings"] == []
    assert report["informational_skipped_summary_finding_count"] == 2
    assert database == original


def test_semantically_postable_philosopher_with_suspicious_summary_is_reported():
    database = {
        "Ada": entry(
            "Ada", "accepted", "Ada was a philosopher.She wrote clearly."
        ),
    }

    report = audit_summary_quality(database)

    assert report["semantically_postable_records_checked"] == 1
    assert report["suspicious_summary_record_count"] == 1
    assert report["suspicious_summary_findings"] == [{
        "title": "Ada",
        "findings": [{
            "kind": "fused_sentence_boundary",
            "excerpt": "Ada was a philosopher.She wrote clearly.",
        }],
    }]


def test_good_semantically_postable_summary_is_not_reported_or_mutated():
    database = {
        "Ada": entry(
            "Ada", "accepted", "Ada was a philosopher. She wrote clearly."
        ),
    }
    original = deepcopy(database)

    report = audit_summary_quality(database)

    assert report["suspicious_summary_findings"] == []
    assert database == original


def test_abbreviation_is_not_treated_as_a_fused_sentence_boundary():
    database = {
        "Ada": entry(
            "Ada", "accepted", "Ada earned a Ph.D. from Oxford."
        ),
    }

    assert audit_summary_quality(database)["suspicious_summary_findings"] == []


def test_semantic_predicate_is_independent_of_posting_and_quote_cache_state():
    value = entry("Posted Ada", "accepted", "Ada was a philosopher.", posted=True)

    assert is_semantically_postable_philosopher(value) is True
    assert is_posting_candidate(value) is False
