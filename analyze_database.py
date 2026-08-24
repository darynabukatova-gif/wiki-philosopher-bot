"""Read-only statistics for the canonical database."""

import argparse
import copy
import json
import math
import threading
from collections import Counter
from itertools import combinations
from pathlib import Path
from unittest.mock import patch

from cache import load_database
from wiki_philosopher_bot.config import (
    CURRENT_EVALUATION_ALGORITHM_VERSION,
    CURRENT_QUOTE_PARSER_VERSION,
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
)
import evaluation
from wiki_philosopher_bot.utils import candidate_selection_weight
from wikipedia_api import quote_selection_weight


CONFIDENCE_FIELDS = (
    "human_confidence",
    "philosopher_confidence",
    "content_confidence",
)

V2_IMPACT_REGRESSION_TITLES = frozenset((
    "Adriaan Heereboord",
    "Alan Stout (philosopher)",
    "Alicja Gescinska",
    "Cheng Yi (philosopher)",
    "Pierre Hadot",
    "R. G. Collingwood",
    "Francesco D'Andrea",
    "Margaret Bryan",
    "Jean Christophe Fatio",
    "Aludel",
    "Eddie Lawrence",
    "Arthur Frederick Sheldon",
    "Helen Van Vechten",
))

MISSING_METADATA = "<missing_or_invalid>"


def linear_percentile(values, percentile):
    """Return a linear-interpolation percentile for numeric values.

    Values are sorted numerically.  The percentile rank is
    ``(len(values) - 1) * percentile / 100``; adjacent ranked values are
    linearly interpolated.  An empty input has no percentile and returns
    ``None``.
    """
    if not values:
        return None
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be between 0 and 100")

    ordered = sorted(values)
    rank = (len(ordered) - 1) * percentile / 100.0
    lower = int(math.floor(rank))
    upper = int(math.ceil(rank))
    if lower == upper:
        return ordered[lower]
    fraction = rank - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _value_distribution(values):
    """Return a JSON-friendly distribution with null before numeric values."""
    counts = Counter(values)
    result = []

    if None in counts:
        result.append({"value": None, "count": counts[None]})

    for value in sorted(value for value in counts if value is not None):
        result.append({"value": value, "count": counts[value]})

    return result


def _count_by_key(values):
    counts = Counter(values)
    return {
        key: counts[key]
        for key in sorted(counts)
    }


def _numeric_summary(values, percentiles=()):
    """Summarize numeric values without treating booleans as integers."""
    numeric_values = [
        value for value in values
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    ]
    if not numeric_values:
        result = {
            "count": 0,
            "mean": None,
            "median": None,
            "minimum": None,
            "maximum": None,
        }
    else:
        result = {
            "count": len(numeric_values),
            "mean": sum(numeric_values) / len(numeric_values),
            "median": linear_percentile(numeric_values, 50),
            "minimum": min(numeric_values),
            "maximum": max(numeric_values),
        }
    for percentile in percentiles:
        result["p{}".format(percentile)] = linear_percentile(
            numeric_values,
            percentile,
        )
    return result


def _quote_summary(quote_counts):
    summary = _numeric_summary(quote_counts, percentiles=(75, 90, 95, 99))
    summary["zero_count"] = sum(count == 0 for count in quote_counts)
    summary["nonzero_count"] = sum(count > 0 for count in quote_counts)
    return {
        "count": summary["count"],
        "zero_count": summary["zero_count"],
        "nonzero_count": summary["nonzero_count"],
        "mean": summary["mean"],
        "median": summary["median"],
        "p75": summary["p75"],
        "p90": summary["p90"],
        "p95": summary["p95"],
        "p99": summary["p99"],
        "minimum": summary["minimum"],
        "maximum": summary["maximum"],
    }


def _nonzero_quote_summary(quote_counts):
    nonzero_counts = [count for count in quote_counts if count > 0]
    summary = _numeric_summary(
        nonzero_counts,
        percentiles=(25, 75, 90, 95, 99),
    )
    return {
        "count": summary["count"],
        "mean": summary["mean"],
        "median": summary["median"],
        "p25": summary["p25"],
        "p75": summary["p75"],
        "p90": summary["p90"],
        "p95": summary["p95"],
        "p99": summary["p99"],
        "minimum": summary["minimum"],
        "maximum": summary["maximum"],
    }


def _quote_selection_weight_curve():
    """Return a small pure diagnostic curve for presentation quote selection."""
    return [
        {
            "word_count": word_count,
            "quote_selection_weight": quote_selection_weight({
                "word_count": word_count,
            }),
        }
        for word_count in range(2, 101)
    ]


def _quote_parser_version_statistics(entries):
    """Return rollout-relevant quote parser-version counts without mutation."""
    result = {
        "available_current": 0,
        "available_stale": 0,
        "accepted_unposted_stale": 0,
        "accepted_posted_stale": 0,
        "rejected_stale": 0,
        "available_by_version": [],
    }
    version_counts = {}
    for entry in entries:
        quotes = entry["quotes"]
        if quotes["status"] != "available":
            continue
        version = quotes.get("parser_version")
        version_counts[version] = version_counts.get(version, 0) + 1
        if quotes.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION:
            result["available_current"] += 1
            continue
        result["available_stale"] += 1
        if entry["evaluation"]["status"] == "accepted":
            if entry["posting"]["has_been_posted"] is True:
                result["accepted_posted_stale"] += 1
            else:
                result["accepted_unposted_stale"] += 1
        elif entry["evaluation"]["status"] == "rejected":
            result["rejected_stale"] += 1
    result["available_by_version"] = [
        {"value": version, "count": count}
        for version, count in sorted(
            version_counts.items(),
            key=lambda item: (item[0] is not None, item[0] if item[0] is not None else -1),
        )
    ]
    return result


def _purged_quote_statistics(entries):
    """Report deliberate rejected-entry payload removals separately."""
    purged_entries = [
        entry for entry in entries
        if entry["quotes"]["status"] == "purged"
    ]
    return {
        "entry_count": len(purged_entries),
        "quote_item_count": sum(
            len(entry["quotes"]["items"])
            for entry in purged_entries
        ),
    }


def _quote_source_metadata_statistics(entries):
    """Count structured source coverage for current-parser quote items only."""
    result = {
        "total_current_parser_quote_items": 0,
        "with_any_source_metadata": 0,
        "with_citation": 0,
        "with_work": 0,
        "with_year": 0,
        "with_date": 0,
        "with_details": 0,
        "with_url": 0,
        "without_source_metadata": 0,
    }
    for entry in entries:
        quotes = entry["quotes"]
        if quotes.get("parser_version") != CURRENT_QUOTE_PARSER_VERSION:
            continue
        for item in quotes["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
                continue
            result["total_current_parser_quote_items"] += 1
            source = item["source"]
            any_metadata = any(value is not None for value in source.values())
            result["with_any_source_metadata"] += int(any_metadata)
            result["without_source_metadata"] += int(not any_metadata)
            for field_name, output_name in (
                ("citation", "with_citation"),
                ("work", "with_work"),
                ("year", "with_year"),
                ("date", "with_date"),
                ("details", "with_details"),
                ("url", "with_url"),
            ):
                result[output_name] += int(source.get(field_name) is not None)
    return result


def _quote_statistics(entries):
    quote_counts = [len(entry["quotes"]["items"]) for entry in entries]
    failure_reasons = [
        entry["quotes"]["failure"]["reason"]
        for entry in entries
        if isinstance(entry["quotes"].get("failure"), dict)
        and isinstance(entry["quotes"]["failure"].get("reason"), str)
    ]

    return {
        "zero_quotes": sum(count == 0 for count in quote_counts),
        "one_or_more_quotes": sum(count > 0 for count in quote_counts),
        "quote_count_distribution": _value_distribution(quote_counts),
        "summary": _quote_summary(quote_counts),
        "nonzero_summary": _nonzero_quote_summary(quote_counts),
        "failure_reason_counts": _count_by_key(failure_reasons),
    }


def _migration_conflict_statistics(entries):
    """Collect defensive, deterministic conflict statistics from migration data."""
    per_entry = []
    by_field = Counter()
    by_resolution = Counter()
    by_field_and_resolution = {}

    for entry in entries:
        migration = entry.get("migration")
        conflicts = migration.get("conflicts") if isinstance(migration, dict) else []
        conflicts = conflicts if isinstance(conflicts, list) else []
        count = len(conflicts)
        per_entry.append((entry.get("title", ""), count))

        for conflict in conflicts:
            field = (
                conflict.get("field")
                if isinstance(conflict, dict)
                and isinstance(conflict.get("field"), str)
                else MISSING_METADATA
            )
            resolution = (
                conflict.get("resolution")
                if isinstance(conflict, dict)
                and isinstance(conflict.get("resolution"), str)
                else MISSING_METADATA
            )
            by_field[field] += 1
            by_resolution[resolution] += 1
            by_field_and_resolution.setdefault(field, Counter())[resolution] += 1

    conflict_counts = [count for _, count in per_entry]
    conflicted_counts = [count for count in conflict_counts if count > 0]
    return {
        "entries_with_conflicts": len(conflicted_counts),
        "total_conflict_objects": sum(conflict_counts),
        "conflicts_per_entry_distribution": _value_distribution(conflict_counts),
        "conflicted_entry_summary": {
            "count": len(conflicted_counts),
            "mean": (
                sum(conflicted_counts) / len(conflicted_counts)
                if conflicted_counts else None
            ),
            "median": linear_percentile(conflicted_counts, 50),
            "maximum": max(conflicted_counts) if conflicted_counts else None,
        },
        "top_conflicted_titles": [
            {"title": title, "conflict_count": count}
            for title, count in sorted(
                (item for item in per_entry if item[1] > 0),
                key=lambda item: (-item[1], item[0]),
            )[:10]
        ],
        "by_field": _count_by_key(by_field.elements()),
        "by_resolution": _count_by_key(by_resolution.elements()),
        "by_field_and_resolution": {
            field: _count_by_key(resolutions.elements())
            for field, resolutions in sorted(by_field_and_resolution.items())
        },
    }


def _is_current_candidate(entry):
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and isinstance(entry.get("evaluation"), dict)
        and entry["evaluation"].get("status") == "accepted"
        and isinstance(entry.get("quotes"), dict)
        and isinstance(entry["quotes"].get("items"), list)
        and bool(entry["quotes"]["items"])
        and isinstance(entry.get("posting"), dict)
        and entry["posting"].get("has_been_posted") is False
    )


def _is_integer_confidence(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _joint_confidence_matrix(entries):
    """Return a deterministic human-confidence by philosopher-confidence matrix."""
    pairs = [
        (
            entry["evaluation"]["human_confidence"],
            entry["evaluation"]["philosopher_confidence"],
        )
        for entry in entries
        if _is_integer_confidence(entry["evaluation"]["human_confidence"])
        and _is_integer_confidence(entry["evaluation"]["philosopher_confidence"])
    ]
    human_values = sorted({human for human, _ in pairs})
    philosopher_values = sorted({philosopher for _, philosopher in pairs})
    counts = Counter(pairs)
    return {
        "human_values": human_values,
        "philosopher_values": philosopher_values,
        "counts": [
            [counts[(human, philosopher)] for philosopher in philosopher_values]
            for human in human_values
        ],
    }


def _top_joint_confidence_pairs(matrix):
    pairs = [
        {
            "human_confidence": human,
            "philosopher_confidence": philosopher,
            "entry_count": matrix["counts"][row][column],
        }
        for row, human in enumerate(matrix["human_values"])
        for column, philosopher in enumerate(matrix["philosopher_values"])
        if matrix["counts"][row][column]
    ]
    return sorted(
        pairs,
        key=lambda pair: (
            -pair["entry_count"],
            pair["human_confidence"],
            pair["philosopher_confidence"],
        ),
    )[:10]


def _candidate_selection_weight_statistics(candidates):
    """Summarize actual production selection weights for canonical candidates."""
    by_candidate = [
        {
            "title": entry["title"],
            "selection_weight": candidate_selection_weight(entry),
            "content_confidence": entry["evaluation"]["content_confidence"],
            "quote_count": len(entry["quotes"]["items"]),
        }
        for entry in sorted(candidates, key=lambda entry: entry["title"])
    ]
    summary = _numeric_summary(
        [item["selection_weight"] for item in by_candidate],
        percentiles=(25, 75, 90, 95, 99),
    )
    return {
        "summary": {
            "count": summary["count"],
            "minimum": summary["minimum"],
            "mean": summary["mean"],
            "median": summary["median"],
            "p25": summary["p25"],
            "p75": summary["p75"],
            "p90": summary["p90"],
            "p95": summary["p95"],
            "p99": summary["p99"],
            "maximum": summary["maximum"],
        },
        "weight_distribution": _value_distribution(
            item["selection_weight"] for item in by_candidate
        ),
        "by_candidate": by_candidate,
        "top_candidates": sorted(
            by_candidate,
            key=lambda item: (-item["selection_weight"], item["title"]),
        )[:10],
        "bottom_candidates": sorted(
            by_candidate,
            key=lambda item: (item["selection_weight"], item["title"]),
        )[:10],
    }


def normalize_evaluation_reason(reason):
    """Map current evaluator reason templates to stable analysis categories."""
    if not isinstance(reason, str):
        return "other"

    if reason.startswith("title nonhuman + nonphilosopher penalty"):
        return "title_nonhuman_nonphilosopher_penalty"
    if reason.startswith("title nonhuman penalty"):
        return "title_nonhuman_penalty"
    if reason.startswith("title human bonus"):
        return "title_human_bonus"
    if reason.startswith("title bonus") or reason.startswith(
        "title philosopher bonus"
    ):
        return "title_philosopher_bonus"
    if reason.startswith("summary philosopher bonus"):
        return "summary_philosopher_bonus"
    if reason.startswith("summary nonhuman + nonphilosopher penalty"):
        return "summary_nonhuman_nonphilosopher_penalty"
    if reason.startswith("summary human + nonphilosopher penalty/bonus"):
        return "summary_human_nonphilosopher_mixed"
    if reason.startswith("summary nonhuman penalty"):
        return "summary_nonhuman_penalty"
    if reason.startswith("summary human bonus"):
        return "summary_human_bonus"
    if reason.startswith("quotes bonus"):
        return "quote_content_bonus"
    if reason.startswith("quotes noncontent penalty"):
        return "quote_content_penalty"
    if reason.startswith("wikidata human bonus"):
        return "wikidata_human_bonus"
    if reason.startswith("wikidata nonhuman penalty"):
        return "wikidata_human_penalty"
    if reason.startswith("wikidata philosopher bonus"):
        return "wikidata_philosopher_bonus"
    if reason.startswith("wikidata nonphilosopher penalty"):
        return "wikidata_philosopher_penalty"
    return "other"


def _decision_margin(evaluation):
    human = evaluation.get("human_confidence")
    philosopher = evaluation.get("philosopher_confidence")
    if not (_is_integer_confidence(human) and _is_integer_confidence(philosopher)):
        return None
    return min(human, philosopher)


def _decision_margin_statistics(entries):
    margins = [
        margin for margin in (
            _decision_margin(entry["evaluation"])
            for entry in entries
        )
        if margin is not None
    ]
    summary = _numeric_summary(margins, percentiles=(25, 75, 90))
    return {
        "count": summary["count"],
        "distribution": _value_distribution(margins),
        "summary": {
            "count": summary["count"],
            "minimum": summary["minimum"],
            "mean": summary["mean"],
            "median": summary["median"],
            "p25": summary["p25"],
            "p75": summary["p75"],
            "p90": summary["p90"],
            "maximum": summary["maximum"],
        },
    }


def _decision_margin_statistics_from_values(margins):
    """Summarize already-calculated integer decision margins."""
    summary = _numeric_summary(margins, percentiles=(25, 75, 90))
    return {
        "count": summary["count"],
        "distribution": _value_distribution(margins),
        "summary": {
            "count": summary["count"],
            "minimum": summary["minimum"],
            "mean": summary["mean"],
            "median": summary["median"],
            "p25": summary["p25"],
            "p75": summary["p75"],
            "p90": summary["p90"],
            "maximum": summary["maximum"],
        },
    }


def _reason_category_statistics(entries):
    entry_counts = Counter()
    occurrence_counts = Counter()
    for entry in entries:
        reasons = entry["evaluation"].get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
        categories = [normalize_evaluation_reason(reason) for reason in reasons]
        occurrence_counts.update(categories)
        entry_counts.update(set(categories))

    return [
        {
            "category": category,
            "entry_count": entry_counts[category],
            "occurrence_count": occurrence_counts[category],
        }
        for category in sorted(
            occurrence_counts,
            key=lambda category: (-occurrence_counts[category], category),
        )
    ]


def _reason_cooccurrences(entries):
    pairs = Counter()
    for entry in entries:
        reasons = entry["evaluation"].get("reasons")
        reasons = reasons if isinstance(reasons, list) else []
        categories = sorted({
            normalize_evaluation_reason(reason)
            for reason in reasons
        })
        pairs.update(combinations(categories, 2))
    return [
        {
            "category_a": first,
            "category_b": second,
            "entry_count": count,
        }
        for (first, second), count in sorted(
            pairs.items(),
            key=lambda item: (-item[1], item[0][0], item[0][1]),
        )[:20]
    ]


def _borderline_case_record(entry, margin):
    evaluation = entry["evaluation"]
    wikidata = entry["wikidata"]
    posting = entry["posting"]
    migration = entry["migration"]
    reasons = evaluation.get("reasons")
    reasons = list(reasons) if isinstance(reasons, list) else []
    conflicts = migration.get("conflicts") if isinstance(migration, dict) else []
    conflicts = conflicts if isinstance(conflicts, list) else []
    return {
        "title": entry["title"],
        "display_title": entry.get("display_title"),
        "status": evaluation["status"],
        "algorithm_version": evaluation.get("algorithm_version"),
        "human_confidence": evaluation.get("human_confidence"),
        "philosopher_confidence": evaluation.get("philosopher_confidence"),
        "content_confidence": evaluation.get("content_confidence"),
        "decision_margin": margin,
        "raw_reasons": reasons,
        "normalized_reason_categories": [
            normalize_evaluation_reason(reason) for reason in reasons
        ],
        "summary_text": entry["summary"].get("text"),
        "wikidata": {
            "status": wikidata.get("status"),
            "qid": wikidata.get("qid"),
            "is_human": wikidata.get("is_human"),
            "is_philosopher": wikidata.get("is_philosopher"),
            "birth_year": wikidata.get("birth_year"),
            "death_year": wikidata.get("death_year"),
        },
        "quote_count": len(entry["quotes"]["items"]),
        "has_been_posted": posting.get("has_been_posted"),
        "migration_conflict_count": len(conflicts),
    }


def collect_borderline_case_records(database):
    """Return complete, deterministic manual-review records without mutation."""
    records = {"accepted": [], "rejected": []}
    for entry in database.values():
        evaluation = entry.get("evaluation") if isinstance(entry, dict) else None
        if not isinstance(evaluation, dict):
            continue
        margin = _decision_margin(evaluation)
        status = evaluation.get("status")
        if status == "accepted" and margin == 1:
            records["accepted"].append(_borderline_case_record(entry, margin))
        elif status == "rejected" and margin == 0:
            records["rejected"].append(_borderline_case_record(entry, margin))
    for status in records:
        records[status].sort(key=lambda record: record["title"])
    return records


def _impact_old_group(evaluation_section):
    """Return a stable old-status/version bucket for the v1-to-v2 audit."""
    status = evaluation_section.get("status")
    version = evaluation_section.get("algorithm_version")
    if status not in ("accepted", "rejected"):
        return "unprocessed"
    if version == 1:
        return "{}_v1".format(status)
    if version is None:
        return "{}_historical_none".format(status)
    if version == CURRENT_EVALUATION_ALGORITHM_VERSION:
        return "{}_current_version".format(status)
    return "{}_other_version".format(status)


def _cache_only_summary(entry):
    summary = entry.get("summary") if isinstance(entry, dict) else None
    text = summary.get("text") if isinstance(summary, dict) else None
    return text if isinstance(text, str) and text else None


def _cache_only_prepared_wikidata(title, entry):
    """Adapt only resolved cache data, normalizing historical False to neutral."""
    wikidata = entry.get("wikidata") if isinstance(entry, dict) else None
    status = wikidata.get("status") if isinstance(wikidata, dict) else None
    if status not in ("available", "unavailable"):
        return None

    prepared = copy.deepcopy(
        evaluation.canonical_wikidata_to_prepared(title, wikidata)
    )
    # Version 2 treats the old absence-of-claim False representation as
    # unknown.  It was never confirmed contradictory evidence.
    for field_name in ("is_human", "is_philosopher"):
        if prepared.get(field_name) is False:
            prepared[field_name] = None
    return prepared


def _cache_only_quote_items(entry):
    quotes = entry.get("quotes") if isinstance(entry, dict) else None
    if not isinstance(quotes, dict) or quotes.get("status") != "available":
        return None
    items = quotes.get("items")
    return copy.deepcopy(items) if isinstance(items, list) else None


def _filter_result_confidences(filter_result):
    return {
        "human_confidence": (
            filter_result.human_bonus - filter_result.nonhuman_penalty
        ),
        "philosopher_confidence": (
            filter_result.philosopher_bonus
            - filter_result.nonphilosopher_penalty
        ),
        "content_confidence": (
            filter_result.content_bonus - filter_result.noncontent_penalty
        ),
    }


def _prequote_classification(confidences, reasons):
    if (
        confidences["human_confidence"] > 0
        and confidences["philosopher_confidence"] > 0
    ):
        return "positive_prequote"
    if any(
        reason.startswith((
            "title nonhuman + nonphilosopher penalty",
            "summary nonhuman + nonphilosopher penalty",
        ))
        for reason in reasons
    ):
        return "guaranteed_reject_prequote"
    return "unresolved_prequote"


def _impact_entry_record(entry):
    """Purely replay current filters from already-cached canonical evidence."""
    title = entry["title"]
    old_evaluation = entry["evaluation"]
    old = {
        "status": old_evaluation.get("status"),
        "algorithm_version": old_evaluation.get("algorithm_version"),
        "human_confidence": old_evaluation.get("human_confidence"),
        "philosopher_confidence": old_evaluation.get("philosopher_confidence"),
        "content_confidence": old_evaluation.get("content_confidence"),
        "reasons": list(old_evaluation.get("reasons", [])),
    }
    summary = _cache_only_summary(entry)
    wikidata_status = entry["wikidata"].get("status")
    prepared_wikidata = _cache_only_prepared_wikidata(title, entry)

    if summary is None or prepared_wikidata is None:
        return {
            "title": title,
            "old": old,
            "cache_state": {
                "summary": "available" if summary is not None else "missing",
                "wikidata": wikidata_status,
                "quotes": "not_evaluated",
            },
            "prequote_classification": "insufficient_cached_evidence",
            "cache_only_outcome": "insufficient_cached_evidence",
            "v2": None,
        }

    stats = {}
    stats_lock = threading.Lock()
    persistence_lock = threading.Lock()
    # The production filters are used directly, while their fetching adapters
    # are replaced by read-only cached values for this synchronous calculation.
    with patch.object(evaluation, "get_summary", return_value=summary), patch.object(
        evaluation,
        "prepare_entity_cached",
        return_value=prepared_wikidata,
    ):
        prequote_result = evaluation.combine_filter_results(
            evaluation.title_filter(title),
            evaluation.summary_filter(
                title, {}, stats, stats_lock, persistence_lock,
            ),
            evaluation.wikidata_filter(
                title, {}, {}, {}, stats, stats_lock, persistence_lock,
            ),
        )

    prequote_confidences = _filter_result_confidences(prequote_result)
    prequote_classification = _prequote_classification(
        prequote_confidences,
        prequote_result.reasons,
    )
    quote_items = _cache_only_quote_items(entry)

    if prequote_classification != "positive_prequote":
        return {
            "title": title,
            "old": old,
            "cache_state": {
                "summary": "available",
                "wikidata": wikidata_status,
                "quotes": "not_evaluated",
            },
            "prequote_classification": prequote_classification,
            "cache_only_outcome": (
                "rejected"
                if prequote_classification == "guaranteed_reject_prequote"
                else "unresolved"
            ),
            "v2": prequote_confidences,
            "v2_reasons": list(prequote_result.reasons),
        }

    if quote_items is None:
        return {
            "title": title,
            "old": old,
            "cache_state": {
                "summary": "available",
                "wikidata": wikidata_status,
                "quotes": entry["quotes"].get("status"),
            },
            "prequote_classification": prequote_classification,
            "cache_only_outcome": "insufficient_cached_evidence",
            "v2": prequote_confidences,
            "v2_reasons": list(prequote_result.reasons),
        }

    with patch.object(evaluation, "get_quotes", return_value=quote_items):
        quote_result = evaluation.quote_filter(
            title, {}, stats, stats_lock, persistence_lock,
        )
    combined = evaluation.combine_filter_results(prequote_result, quote_result)
    confidences = _filter_result_confidences(combined)
    return {
        "title": title,
        "old": old,
        "cache_state": {
            "summary": "available",
            "wikidata": wikidata_status,
            "quotes": "available",
        },
        "prequote_classification": prequote_classification,
        "cache_only_outcome": (
            "accepted"
            if (
                confidences["human_confidence"] > 0
                and confidences["philosopher_confidence"] > 0
            )
            else "rejected"
        ),
        "v2": confidences,
        "v2_reasons": list(combined.reasons),
    }


def _reason_category_count_summary(reason_lists):
    occurrences = Counter()
    entries = Counter()
    for reasons in reason_lists:
        categories = [normalize_evaluation_reason(reason) for reason in reasons]
        occurrences.update(categories)
        entries.update(set(categories))
    return [
        {
            "category": category,
            "entry_count": entries[category],
            "occurrence_count": occurrences[category],
        }
        for category in sorted(
            occurrences,
            key=lambda category: (-occurrences[category], category),
        )
    ]


def _impact_sample(record, entry):
    """Return a compact, deterministic comparison record for review."""
    summary = entry["summary"].get("text")
    quotes = entry["quotes"]
    wikidata = entry["wikidata"]
    return {
        "title": record["title"],
        "old": record["old"],
        "v2": record["v2"],
        "v2_reasons": record.get("v2_reasons", []),
        "summary_excerpt": summary[:240] if isinstance(summary, str) else None,
        "wikidata": {
            "status": wikidata.get("status"),
            "is_human": wikidata.get("is_human"),
            "is_philosopher": wikidata.get("is_philosopher"),
        },
        "quotes": {
            "status": quotes.get("status"),
            "count": len(quotes.get("items", [])),
        },
    }


def analyze_v2_impact(database):
    """Replay v2 against cache-only evidence without network or persistence."""
    entries = [database[title] for title in sorted(database)]
    records = [_impact_entry_record(entry) for entry in entries]
    entries_by_title = {entry["title"]: entry for entry in entries}

    transition_matrix = {}
    for record in records:
        group = _impact_old_group(record["old"])
        transition_matrix.setdefault(group, Counter())[record["cache_only_outcome"]] += 1

    def transition_samples(old_status, outcome):
        return [
            _impact_sample(record, entries_by_title[record["title"]])
            for record in records
            if record["old"]["status"] == old_status
            and record["cache_only_outcome"] == outcome
        ][:10]

    v2_confidences = [record["v2"] for record in records if record["v2"]]
    v2_margins = [
        min(item["human_confidence"], item["philosopher_confidence"])
        for item in v2_confidences
    ]
    old_margins = [
        _decision_margin(record["old"])
        for record in records
    ]
    old_margins = [margin for margin in old_margins if margin is not None]
    historical_false_human = sum(
        entry["wikidata"].get("is_human") is False for entry in entries
    )
    historical_false_philosopher = sum(
        entry["wikidata"].get("is_philosopher") is False for entry in entries
    )

    return {
        "old_population": {
            "total_entries": len(entries),
            "status_counts": _count_by_key(
                record["old"]["status"] for record in records
            ),
            "algorithm_version_counts": _value_distribution(
                record["old"]["algorithm_version"] for record in records
            ),
            "status_version_counts": _count_by_key(
                _impact_old_group(record["old"]) for record in records
            ),
        },
        "prequote_outcome_counts": _count_by_key(
            record["prequote_classification"] for record in records
        ),
        "cache_only_outcome_counts": _count_by_key(
            record["cache_only_outcome"] for record in records
        ),
        "transition_matrix": {
            group: _count_by_key(counts.elements())
            for group, counts in sorted(transition_matrix.items())
        },
        "transition_samples": {
            "rejected_to_accepted": transition_samples("rejected", "accepted"),
            "accepted_to_rejected": transition_samples("accepted", "rejected"),
            "unprocessed_to_accepted": transition_samples("unprocessed", "accepted"),
            "unprocessed_to_rejected": transition_samples("unprocessed", "rejected"),
        },
        "known_regression_cases": [
            _impact_sample(record, entries_by_title[record["title"]])
            for record in records
            if record["title"] in V2_IMPACT_REGRESSION_TITLES
        ],
        "insufficient_cached_evidence_samples": [
            _impact_sample(record, entries_by_title[record["title"]])
            for record in records
            if record["cache_only_outcome"] == "insufficient_cached_evidence"
        ][:10],
        "wikidata_tristate_impact": {
            "historical_false_is_human": historical_false_human,
            "historical_false_is_philosopher": historical_false_philosopher,
            "v2_interpretation": "false_is_neutral_unknown",
        },
        "decision_margins": {
            "stored_v1": _decision_margin_statistics_from_values(old_margins),
            "v2_cache_only": _decision_margin_statistics_from_values(v2_margins),
            "v2_borderline_accepted": sum(
                record["cache_only_outcome"] == "accepted"
                and record["v2"] is not None
                and min(
                    record["v2"]["human_confidence"],
                    record["v2"]["philosopher_confidence"],
                ) == 1
                for record in records
            ),
            "v2_borderline_rejected": sum(
                record["cache_only_outcome"] == "rejected"
                and record["v2"] is not None
                and min(
                    record["v2"]["human_confidence"],
                    record["v2"]["philosopher_confidence"],
                ) == 0
                for record in records
            ),
        },
        "reason_category_counts": {
            "stored": _reason_category_count_summary(
                record["old"]["reasons"] for record in records
            ),
            "v2": _reason_category_count_summary(
                record.get("v2_reasons", []) for record in records
            ),
        },
        "entry_results": records,
    }


def analyze_database(database):
    """Return deterministic, read-only statistics for a canonical mapping."""
    entries = list(database.values())
    status_counts = _count_by_key(
        entry["evaluation"]["status"]
        for entry in entries
    )
    accepted_entries = [
        entry for entry in entries
        if entry["evaluation"]["status"] == "accepted"
    ]
    rejected_entries = [
        entry for entry in entries
        if entry["evaluation"]["status"] == "rejected"
    ]
    processed_entries = accepted_entries + rejected_entries
    current_candidates = [
        entry for entry in entries
        if _is_current_candidate(entry)
    ]

    confidence_distributions = {}
    confidence_summaries = {}
    for field_name in CONFIDENCE_FIELDS:
        confidence_distributions[field_name] = {
            "all": _value_distribution(
                entry["evaluation"][field_name]
                for entry in entries
            ),
        }
        confidence_summaries[field_name] = {
            "all": _numeric_summary(
                entry["evaluation"][field_name]
                for entry in entries
            ),
        }
        for status in status_counts:
            confidence_distributions[field_name][status] = _value_distribution(
                entry["evaluation"][field_name]
                for entry in entries
                if entry["evaluation"]["status"] == status
            )
            confidence_summaries[field_name][status] = _numeric_summary(
                entry["evaluation"][field_name]
                for entry in entries
                if entry["evaluation"]["status"] == status
            )

    cross_tabs = {}
    for field_name in CONFIDENCE_FIELDS:
        cross_tabs["status_by_{}".format(field_name)] = {
            status: _value_distribution(
                entry["evaluation"][field_name]
                for entry in entries
                if entry["evaluation"]["status"] == status
            )
            for status in status_counts
        }

    cross_tabs["status_by_algorithm_version"] = {
        status: _value_distribution(
            entry["evaluation"]["algorithm_version"]
            for entry in entries
            if entry["evaluation"]["status"] == status
        )
        for status in status_counts
    }

    current_version = {"accepted": 0, "rejected": 0}
    historical_none = {"accepted": 0, "rejected": 0}
    noncurrent_explicit = {"accepted": 0, "rejected": 0}
    noncurrent_versions = []

    for entry in entries:
        evaluation = entry["evaluation"]
        status = evaluation["status"]
        version = evaluation["algorithm_version"]

        if status not in ("accepted", "rejected"):
            continue

        if version == CURRENT_EVALUATION_ALGORITHM_VERSION:
            current_version[status] += 1
        elif version is None:
            historical_none[status] += 1
        else:
            noncurrent_explicit[status] += 1
            noncurrent_versions.append(version)

    quote_statistics = _quote_statistics(entries)
    quote_statistics["accepted"] = _quote_statistics(accepted_entries)
    quote_statistics["parser_versions"] = _quote_parser_version_statistics(entries)
    quote_statistics["source_metadata"] = _quote_source_metadata_statistics(entries)
    quote_statistics["purged"] = _purged_quote_statistics(entries)
    quote_statistics["selection_weight_curve"] = _quote_selection_weight_curve()
    migration_conflicts = _migration_conflict_statistics(entries)
    candidate_quote_statistics = _quote_statistics(current_candidates)
    candidate_zoom_upper_bound = candidate_quote_statistics["summary"]["p90"]
    candidate_quote_counts = [
        len(entry["quotes"]["items"])
        for entry in current_candidates
    ]
    candidate_selection_weights = _candidate_selection_weight_statistics(
        current_candidates
    )
    joint_confidence = {
        "all": _joint_confidence_matrix(entries),
        "accepted": _joint_confidence_matrix(accepted_entries),
        "rejected": _joint_confidence_matrix(rejected_entries),
    }
    joint_confidence["top_pairs"] = {
        "accepted": _top_joint_confidence_pairs(joint_confidence["accepted"]),
        "rejected": _top_joint_confidence_pairs(joint_confidence["rejected"]),
    }
    borderline_records = collect_borderline_case_records(database)
    borderline_accepted_entries = [
        entry for entry in accepted_entries
        if _decision_margin(entry["evaluation"]) == 1
    ]
    borderline_rejected_entries = [
        entry for entry in rejected_entries
        if _decision_margin(entry["evaluation"]) == 0
    ]
    decision_margins = {
        "all_processed": _decision_margin_statistics(processed_entries),
        "accepted": _decision_margin_statistics(accepted_entries),
        "rejected": _decision_margin_statistics(rejected_entries),
        "borderline": {
            "accepted_count": len(borderline_records["accepted"]),
            "rejected_count": len(borderline_records["rejected"]),
            "accepted_sample_titles": [
                record["title"] for record in borderline_records["accepted"][:10]
            ],
            "rejected_sample_titles": [
                record["title"] for record in borderline_records["rejected"][:10]
            ],
        },
        "near_boundary": {
            "human_equals_1": sum(
                entry["evaluation"]["human_confidence"] == 1
                for entry in processed_entries
                if _is_integer_confidence(
                    entry["evaluation"]["human_confidence"]
                )
            ),
            "philosopher_equals_1": sum(
                entry["evaluation"]["philosopher_confidence"] == 1
                for entry in processed_entries
                if _is_integer_confidence(
                    entry["evaluation"]["philosopher_confidence"]
                )
            ),
            "human_equals_0": sum(
                entry["evaluation"]["human_confidence"] == 0
                for entry in processed_entries
                if _is_integer_confidence(
                    entry["evaluation"]["human_confidence"]
                )
            ),
            "philosopher_equals_0": sum(
                entry["evaluation"]["philosopher_confidence"] == 0
                for entry in processed_entries
                if _is_integer_confidence(
                    entry["evaluation"]["philosopher_confidence"]
                )
            ),
            "margin_equals_1": len(borderline_records["accepted"]),
            "margin_equals_0": len(borderline_records["rejected"]),
        },
    }
    reason_analysis = {
        "all_processed": _reason_category_statistics(processed_entries),
        "accepted": _reason_category_statistics(accepted_entries),
        "rejected": _reason_category_statistics(rejected_entries),
        "borderline_accepted": _reason_category_statistics(
            borderline_accepted_entries
        ),
        "borderline_rejected": _reason_category_statistics(
            borderline_rejected_entries
        ),
        "cooccurrence": {
            "accepted": _reason_cooccurrences(accepted_entries),
            "rejected": _reason_cooccurrences(rejected_entries),
        },
    }

    return {
        "total_canonical_entries": len(entries),
        "evaluation": {
            "status_counts": status_counts,
            "algorithm_version_counts": _value_distribution(
                entry["evaluation"]["algorithm_version"]
                for entry in entries
            ),
            "confidence_distributions": confidence_distributions,
            "confidence_summaries": confidence_summaries,
        },
        "cross_tabs": cross_tabs,
        "quotes": quote_statistics,
        "posting": {
            "posted_entries": sum(
                entry["posting"]["has_been_posted"] is True
                for entry in entries
            ),
            "unposted_entries": sum(
                entry["posting"]["has_been_posted"] is False
                for entry in entries
            ),
            "current_candidate_count": len(current_candidates),
            "current_candidates": {
                "content_confidence_distribution": _value_distribution(
                    entry["evaluation"]["content_confidence"]
                    for entry in current_candidates
                ),
                "quote_count_distribution": _value_distribution(
                    candidate_quote_counts
                ),
                "quote_statistics": candidate_quote_statistics,
                "quote_zoom": {
                    "upper_bound": candidate_zoom_upper_bound,
                    "above_upper_bound_count": sum(
                        quote_count > candidate_zoom_upper_bound
                        for quote_count in candidate_quote_counts
                    ) if candidate_zoom_upper_bound is not None else 0,
                },
                "selection_weights": candidate_selection_weights,
            },
        },
        "wikidata_status_counts": _count_by_key(
            entry["wikidata"]["status"]
            for entry in entries
        ),
        "quotes_status_counts": _count_by_key(
            entry["quotes"]["status"]
            for entry in entries
        ),
        "migration_version": {
            "current_version": current_version,
            "historical_none": historical_none,
            "noncurrent_explicit": {
                "accepted": noncurrent_explicit["accepted"],
                "rejected": noncurrent_explicit["rejected"],
                "by_version": _value_distribution(noncurrent_versions),
            },
            "unprocessed_entries": status_counts.get("unprocessed", 0),
            "migration_conflict_entries": migration_conflicts[
                "entries_with_conflicts"
            ],
        },
        "migration_conflict_entries": migration_conflicts[
            "entries_with_conflicts"
        ],
        "migration_conflicts": migration_conflicts,
        "joint_confidence": joint_confidence,
        "decision_margins": decision_margins,
        "reason_analysis": reason_analysis,
    }


class PlottingUnavailableError(RuntimeError):
    """Raised when the optional development plotting dependency is absent."""


def _load_pyplot():
    """Load matplotlib only when optional plot generation is requested."""
    try:
        import matplotlib
    except ImportError as error:
        raise PlottingUnavailableError(
            "--plots requires matplotlib; install requirements-dev.txt first."
        ) from error

    matplotlib.use("Agg")
    import matplotlib.pyplot as pyplot
    return pyplot


def _distribution_mapping(distribution):
    return {item["value"]: item["count"] for item in distribution}


def _plot_and_save(pyplot, output_path, draw):
    figure, axes = pyplot.subplots()
    draw(axes)
    figure.tight_layout()
    figure.savefig(str(output_path))
    pyplot.close(figure)
    return output_path


def _plot_count_bars(pyplot, output_path, counts, title, x_label):
    items = sorted(counts.items())

    def draw(axes):
        axes.bar([key for key, _ in items], [count for _, count in items])
        axes.set_title(title)
        axes.set_xlabel(x_label)
        axes.set_ylabel("Entries")
        axes.tick_params(axis="x", rotation=30)

    return _plot_and_save(pyplot, output_path, draw)


def _plot_confidence_by_status(pyplot, output_path, analysis, field_name):
    by_status = analysis["cross_tabs"]["status_by_{}".format(field_name)]
    statuses = sorted(by_status)
    mappings = {
        status: _distribution_mapping(distribution)
        for status, distribution in by_status.items()
    }
    values = set()
    for mapping in mappings.values():
        values.update(mapping)
    ordered_values = ([None] if None in values else []) + sorted(
        value for value in values if value is not None
    )
    positions = list(range(len(ordered_values)))
    width = 0.8 / max(1, len(statuses))

    def draw(axes):
        for index, status in enumerate(statuses):
            offset = (index - (len(statuses) - 1) / 2.0) * width
            axes.bar(
                [position + offset for position in positions],
                [mappings[status].get(value, 0) for value in ordered_values],
                width=width,
                label=status,
            )
        axes.set_title("{} by evaluation status".format(field_name))
        axes.set_xlabel(field_name)
        axes.set_ylabel("Entries")
        axes.set_xticks(positions)
        axes.set_xticklabels([
            "None" if value is None else str(value)
            for value in ordered_values
        ])
        axes.legend()

    return _plot_and_save(pyplot, output_path, draw)


def _values_from_distribution(distribution):
    values = []
    for item in distribution:
        values.extend([item["value"]] * item["count"])
    return values


def _histogram_bin_count(values):
    if not values:
        return 1
    minimum = min(values)
    maximum = max(values)
    return (
        1 if minimum == maximum
        else min(30, max(1, int(math.ceil(maximum - minimum + 1))))
    )


def _plot_histogram(pyplot, output_path, values, title, x_label):

    def draw(axes):
        if values:
            axes.hist(values, bins=_histogram_bin_count(values))
        axes.set_title(title)
        axes.set_xlabel(x_label)
        axes.set_ylabel("Entries")

    return _plot_and_save(pyplot, output_path, draw)


def _plot_distribution_bars(pyplot, output_path, distribution, title, x_label):
    items = [(item["value"], item["count"]) for item in distribution]

    def draw(axes):
        axes.bar(
            ["None" if value is None else str(value) for value, _ in items],
            [count for _, count in items],
        )
        axes.set_title(title)
        axes.set_xlabel(x_label)
        axes.set_ylabel("Entries")
        axes.tick_params(axis="x", rotation=30)

    return _plot_and_save(pyplot, output_path, draw)


def _plot_descending_counts(pyplot, output_path, counts, title, x_label):
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))

    def draw(axes):
        axes.bar([key for key, _ in items], [count for _, count in items])
        axes.set_title(title)
        axes.set_xlabel(x_label)
        if items and all(count > 0 for _, count in items):
            axes.set_yscale("log")
            axes.set_ylabel("Conflict objects (log scale)")
        else:
            axes.set_ylabel("Conflict objects")
        axes.tick_params(axis="x", rotation=30)

    return _plot_and_save(pyplot, output_path, draw)


def _plot_confidence_heatmap(pyplot, output_path, matrix, title):
    human_values = matrix["human_values"]
    philosopher_values = matrix["philosopher_values"]
    counts = matrix["counts"]

    def draw(axes):
        display_counts = counts if counts else [[0]]
        image = axes.imshow(display_counts, aspect="auto")
        colorbar = axes.figure.colorbar(image, ax=axes)
        colorbar.set_label("Entry count")
        axes.set_title(title)
        axes.set_xlabel("philosopher_confidence")
        axes.set_ylabel("human_confidence")
        axes.set_xticks(list(range(len(philosopher_values))))
        axes.set_xticklabels([str(value) for value in philosopher_values])
        axes.set_yticks(list(range(len(human_values))))
        axes.set_yticklabels([str(value) for value in human_values])
        if len(human_values) * len(philosopher_values) <= 100:
            for row, human_value in enumerate(human_values):
                for column, philosopher_value in enumerate(philosopher_values):
                    axes.text(
                        column,
                        row,
                        str(counts[row][column]),
                        ha="center",
                        va="center",
                    )

    return _plot_and_save(pyplot, output_path, draw)


def _plot_weight_scatter(pyplot, output_path, records):
    def draw(axes):
        axes.scatter(
            [record["quote_count"] for record in records],
            [record["selection_weight"] for record in records],
        )
        axes.set_title("Content-only candidate weight versus quote count")
        axes.set_xlabel("Quote count")
        axes.set_ylabel("Content-only selection weight")

    return _plot_and_save(pyplot, output_path, draw)


def _plot_margin_by_status(pyplot, output_path, decision_margins):
    accepted = _distribution_mapping(
        decision_margins["accepted"]["distribution"]
    )
    rejected = _distribution_mapping(
        decision_margins["rejected"]["distribution"]
    )
    margins = sorted(set(accepted) | set(rejected))
    width = 0.4

    def draw(axes):
        axes.bar(
            [margin - width / 2 for margin in margins],
            [accepted.get(margin, 0) for margin in margins],
            width=width,
            label="accepted",
        )
        axes.bar(
            [margin + width / 2 for margin in margins],
            [rejected.get(margin, 0) for margin in margins],
            width=width,
            label="rejected",
        )
        axes.axvline(0, color="black", linewidth=1)
        axes.set_title("Decision-margin distribution by status")
        axes.set_xlabel("Decision margin")
        axes.set_ylabel("Entries")
        axes.set_xticks(margins)
        axes.legend()

    return _plot_and_save(pyplot, output_path, draw)


def _plot_margin_distribution(pyplot, output_path, distribution, title):
    items = [(item["value"], item["count"]) for item in distribution]

    def draw(axes):
        axes.bar([value for value, _ in items], [count for _, count in items])
        axes.axvline(0, color="black", linewidth=1)
        axes.set_title(title)
        axes.set_xlabel("Decision margin")
        axes.set_ylabel("Entries")
        axes.set_xticks([value for value, _ in items])

    return _plot_and_save(pyplot, output_path, draw)


def _plot_reason_categories(pyplot, output_path, left_rows, right_rows, title,
                            left_label, right_label):
    left_counts = {row["category"]: row["entry_count"] for row in left_rows}
    right_counts = {row["category"]: row["entry_count"] for row in right_rows}
    categories = sorted(
        set(left_counts) | set(right_counts),
        key=lambda category: (
            -max(left_counts.get(category, 0), right_counts.get(category, 0)),
            category,
        ),
    )[:12]
    positions = list(range(len(categories)))
    height = 0.4

    def draw(axes):
        axes.barh(
            [position - height / 2 for position in positions],
            [left_counts.get(category, 0) for category in categories],
            height=height,
            label=left_label,
        )
        axes.barh(
            [position + height / 2 for position in positions],
            [right_counts.get(category, 0) for category in categories],
            height=height,
            label=right_label,
        )
        axes.set_title(title)
        axes.set_xlabel("Entries containing category")
        axes.set_ylabel("Reason category")
        axes.set_yticks(positions)
        axes.set_yticklabels(categories)
        axes.invert_yaxis()
        axes.legend()

    return _plot_and_save(pyplot, output_path, draw)


def _plot_quote_selection_weight_curve(pyplot, output_path, curve):
    def draw(axes):
        axes.plot(
            [point["word_count"] for point in curve],
            [point["quote_selection_weight"] for point in curve],
        )
        axes.set_title("Quote selection weight by word count")
        axes.set_xlabel("Quote word count")
        axes.set_ylabel("Quote selection weight")

    return _plot_and_save(pyplot, output_path, draw)


def generate_plots(analysis, output_dir):
    """Generate static plots from a completed analysis without recalculating it."""
    pyplot = _load_pyplot()
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    paths = []
    paths.append(_plot_count_bars(
        pyplot,
        output_path / "evaluation_status_counts.png",
        analysis["evaluation"]["status_counts"],
        "Evaluation status counts",
        "Evaluation status",
    ))
    for field_name in CONFIDENCE_FIELDS:
        paths.append(_plot_confidence_by_status(
            pyplot,
            output_path / "{}_by_status.png".format(field_name),
            analysis,
            field_name,
        ))
    paths.append(_plot_count_bars(
        pyplot,
        output_path / "wikidata_status_counts.png",
        analysis["wikidata_status_counts"],
        "Wikidata status counts",
        "Wikidata status",
    ))
    paths.append(_plot_count_bars(
        pyplot,
        output_path / "quote_status_counts.png",
        analysis["quotes_status_counts"],
        "Quote status counts",
        "Quote status",
    ))
    paths.append(_plot_histogram(
        pyplot,
        output_path / "nonzero_quote_count_distribution.png",
        _values_from_distribution([
            item for item in analysis["quotes"]["quote_count_distribution"]
            if item["value"] > 0
        ]),
        "Nonzero quote-count distribution",
        "Quote count",
    ))
    paths.append(_plot_quote_selection_weight_curve(
        pyplot,
        output_path / "quote_selection_weight_curve.png",
        analysis["quotes"]["selection_weight_curve"],
    ))
    paths.append(_plot_descending_counts(
        pyplot,
        output_path / "migration_conflicts_by_field.png",
        analysis["migration_conflicts"]["by_field"],
        "Migration conflicts by field",
        "Conflict field",
    ))
    paths.append(_plot_descending_counts(
        pyplot,
        output_path / "migration_conflicts_by_resolution.png",
        analysis["migration_conflicts"]["by_resolution"],
        "Migration conflicts by resolution",
        "Conflict resolution",
    ))
    candidates = analysis["posting"]["current_candidates"]
    candidate_records = candidates["selection_weights"]["by_candidate"]
    candidate_quote_counts = [
        record["quote_count"] for record in candidate_records
    ]
    paths.append(_plot_distribution_bars(
        pyplot,
        output_path / "candidate_content_confidence.png",
        candidates["content_confidence_distribution"],
        "Candidate content-confidence distribution",
        "content_confidence",
    ))
    paths.append(_plot_histogram(
        pyplot,
        output_path / "candidate_quote_count_distribution.png",
        candidate_quote_counts,
        "Candidate quote-count distribution",
        "Quote count",
    ))
    zoom_upper_bound = candidates["quote_zoom"]["upper_bound"]
    zoom_values = [
        quote_count for quote_count in candidate_quote_counts
        if zoom_upper_bound is not None and quote_count <= zoom_upper_bound
    ]
    paths.append(_plot_histogram(
        pyplot,
        output_path / "candidate_quote_count_distribution_zoomed.png",
        zoom_values,
        "Candidate quote-count distribution (through p90)",
        "Quote count",
    ))
    for group_name in ("all", "accepted", "rejected"):
        paths.append(_plot_confidence_heatmap(
            pyplot,
            output_path / "human_vs_philosopher_{}.png".format(group_name),
            analysis["joint_confidence"][group_name],
            "Human versus philosopher confidence ({})".format(group_name),
        ))
    paths.append(_plot_histogram(
        pyplot,
        output_path / "candidate_selection_weight_distribution.png",
        [record["selection_weight"] for record in candidate_records],
        "Candidate selection-weight distribution",
        "Candidate selection weight",
    ))
    paths.append(_plot_weight_scatter(
        pyplot,
        output_path / "candidate_weight_vs_quote_count.png",
        candidate_records,
    ))
    paths.append(_plot_margin_by_status(
        pyplot,
        output_path / "decision_margin_by_status.png",
        analysis["decision_margins"],
    ))
    paths.append(_plot_margin_distribution(
        pyplot,
        output_path / "accepted_decision_margin.png",
        analysis["decision_margins"]["accepted"]["distribution"],
        "Accepted decision-margin distribution",
    ))
    paths.append(_plot_margin_distribution(
        pyplot,
        output_path / "rejected_decision_margin.png",
        analysis["decision_margins"]["rejected"]["distribution"],
        "Rejected decision-margin distribution",
    ))
    paths.append(_plot_reason_categories(
        pyplot,
        output_path / "borderline_reason_categories.png",
        analysis["reason_analysis"]["borderline_accepted"],
        analysis["reason_analysis"]["borderline_rejected"],
        "Borderline reason categories",
        "borderline accepted",
        "borderline rejected",
    ))
    paths.append(_plot_reason_categories(
        pyplot,
        output_path / "reason_categories_by_status.png",
        analysis["reason_analysis"]["accepted"],
        analysis["reason_analysis"]["rejected"],
        "Reason categories by evaluation status",
        "accepted",
        "rejected",
    ))
    return paths


def write_borderline_case_records(database, output_file):
    """Write explicit analysis output, refusing to overwrite an existing file."""
    output_path = Path(output_file)
    if output_path.exists():
        raise FileExistsError(
            "Refusing to overwrite existing borderline output: {}".format(
                output_path
            )
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    records = collect_borderline_case_records(database)
    output_path.write_text(
        json.dumps(records, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return output_path


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Print read-only statistics for canonical database.jsonl.",
    )
    parser.add_argument(
        "--data-folder",
        default=CANONICAL_DATA_FOLDER,
        help="Folder containing the canonical database.jsonl",
    )
    parser.add_argument(
        "--plots",
        metavar="OUTPUT_DIRECTORY",
        help="Write optional static PNG plots to this directory.",
    )
    parser.add_argument(
        "--borderline-output",
        metavar="PATH",
        help="Write complete borderline review records as new JSON output.",
    )
    parser.add_argument(
        "--v2-impact",
        action="store_true",
        help="Replay v2 from cache-only evidence without network or writes.",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    analysis = (
        analyze_v2_impact(database)
        if args.v2_impact else analyze_database(database)
    )
    if args.v2_impact and (args.plots or args.borderline_output):
        raise SystemExit(
            "--v2-impact cannot be combined with --plots or --borderline-output"
        )
    if args.borderline_output:
        try:
            write_borderline_case_records(database, args.borderline_output)
        except FileExistsError as error:
            raise SystemExit(str(error))
    if args.plots:
        try:
            generate_plots(analysis, args.plots)
        except PlottingUnavailableError as error:
            raise SystemExit(str(error))
    output = dict(analysis)
    if args.v2_impact:
        # Per-entry records are intentionally available to Python callers but
        # would make the command-line summary needlessly large.
        output.pop("entry_results", None)
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
