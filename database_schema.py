"""Pure canonical database schema validation and serialization helpers."""

import json
import re
from datetime import date
from typing import List


DATABASE_SCHEMA_VERSION = 1


EVALUATION_SERIALIZATION_ORDER = (
    "status",
    "algorithm_version",
    "philosopher_confidence",
    "human_confidence",
    "content_confidence",
    "reasons",
    "processed_at",
    "legacy_result",
)


def make_empty_database_entry(title: str) -> dict:
    """Return a fresh canonical database entry for one exact title."""
    if not isinstance(title, str) or not title:
        raise ValueError("Database entry title must be a non-empty string")

    display_title = re.sub(r"\s*\([^)]*\)", "", title).strip() or title

    return {
        "schema_version": DATABASE_SCHEMA_VERSION,
        "title": title,
        "display_title": display_title,
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
        },
        "migration": {
            "legacy_sources": [],
            "conflicts": [],
        },
    }


def _is_int_not_bool(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number_not_bool(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
    )


def _require_keys(
    section_name,
    section,
    required_keys,
):
    errors = []

    if not isinstance(section, dict):
        return [
            "{} must be an object".format(
                section_name
            )
        ]

    for key in required_keys:
        if key not in section:
            errors.append(
                "Missing required field: {}.{}".format(
                    section_name,
                    key,
                )
            )

    return errors


def validate_database_entry(entry: dict) -> List[str]:
    errors = []

    if not isinstance(entry, dict):
        return ["Entry must be an object"]

    required_top_level = (
        "schema_version",
        "title",
        "display_title",
        "summary",
        "wikidata",
        "quotes",
        "evaluation",
        "posting",
        "migration",
    )

    for key in required_top_level:
        if key not in entry:
            errors.append(
                "Missing required top-level field: {}".format(key)
            )

    if errors:
        return errors

    if entry["schema_version"] != DATABASE_SCHEMA_VERSION:
        errors.append(
            "schema_version must be {}".format(
                DATABASE_SCHEMA_VERSION
            )
        )

    if (
        not isinstance(entry["title"], str)
        or not entry["title"]
    ):
        errors.append(
            "title must be a non-empty string"
        )

    if (
        not isinstance(entry["display_title"], str)
        or not entry["display_title"]
    ):
        errors.append(
            "display_title must be a non-empty string"
        )

    summary = entry["summary"]

    errors.extend(
        _require_keys(
            "summary",
            summary,
            (
                "text",
                "source",
                "fetched_at",
            ),
        )
    )

    if isinstance(summary, dict):
        text = summary.get("text")

        if text is not None and not isinstance(text, str):
            errors.append(
                "summary.text must be a string or null"
            )

        if summary.get("source") != "Wikipedia":
            errors.append(
                "summary.source must be 'Wikipedia'"
            )

        fetched_at = summary.get("fetched_at")

        if (
            fetched_at is not None
            and not _is_int_not_bool(fetched_at)
        ):
            errors.append(
                "summary.fetched_at must be an integer or null"
            )

    wikidata = entry["wikidata"]

    errors.extend(
        _require_keys(
            "wikidata",
            wikidata,
            (
                "status",
                "reason",
                "qid",
                "instances",
                "occupations",
                "birth_year",
                "death_year",
                "is_human",
                "is_philosopher",
                "fetched_at",
            ),
        )
    )

    if isinstance(wikidata, dict):
        if wikidata.get("status") not in (
            "unknown",
            "available",
            "unavailable",
        ):
            errors.append(
                "wikidata.status has invalid value"
            )

        reason = wikidata.get("reason")

        if reason is not None and not isinstance(reason, str):
            errors.append(
                "wikidata.reason must be a string or null"
            )

        qid = wikidata.get("qid")

        if qid is not None and not isinstance(qid, str):
            errors.append(
                "wikidata.qid must be a string or null"
            )

        for field_name in (
            "instances",
            "occupations",
        ):
            value = wikidata.get(field_name)

            if (
                not isinstance(value, list)
                or not all(
                    isinstance(item, str)
                    for item in value
                )
            ):
                errors.append(
                    "wikidata.{} must be a list of strings".format(
                        field_name
                    )
                )

        for field_name in (
            "birth_year",
            "death_year",
        ):
            value = wikidata.get(field_name)

            if (
                value is not None
                and not _is_int_not_bool(value)
            ):
                errors.append(
                    "wikidata.{} must be an integer or null".format(
                        field_name
                    )
                )

        # Historical canonical entries predate exact death-date storage, so a
        # missing key is deliberately equivalent to a null value.
        if "death_date" in wikidata:
            death_date = wikidata.get("death_date")
            valid_death_date = isinstance(death_date, str) and re.fullmatch(
                r"\d{4}-\d{2}-\d{2}", death_date
            )
            if valid_death_date:
                try:
                    date.fromisoformat(death_date)
                except ValueError:
                    valid_death_date = False
            if death_date is not None and not valid_death_date:
                errors.append(
                    "wikidata.death_date must be an ISO date or null"
                )

        for field_name in (
            "is_human",
            "is_philosopher",
        ):
            value = wikidata.get(field_name)

            if (
                value is not None
                and not isinstance(value, bool)
            ):
                errors.append(
                    "wikidata.{} must be boolean or null".format(
                        field_name
                    )
                )

        fetched_at = wikidata.get("fetched_at")

        if (
            fetched_at is not None
            and not _is_int_not_bool(fetched_at)
        ):
            errors.append(
                "wikidata.fetched_at must be an integer or null"
            )

    quotes = entry["quotes"]

    errors.extend(
        _require_keys(
            "quotes",
            quotes,
            (
                "status",
                "items",
                "failure",
                "fetched_at",
            ),
        )
    )

    if isinstance(quotes, dict):
        if quotes.get("status") not in (
            "unknown",
            "available",
            "not_found",
            "failed",
            "purged",
        ):
            errors.append(
                "quotes.status has invalid value"
            )

        items = quotes.get("items")

        structured_parser_quote = (
            isinstance(quotes.get("parser_version"), int)
            and not isinstance(quotes.get("parser_version"), bool)
            and quotes["parser_version"] >= 2
        )

        if not isinstance(items, list):
            errors.append("quotes.items must be a list")
        else:
            for index, item in enumerate(items):
                if not isinstance(item, dict):
                    errors.append(
                        "quotes.items[{}] must be an object".format(index)
                    )
                    continue

                for key in (
                    "text",
                    "length",
                    "word_count",
                    "source",
                ):
                    if key not in item:
                        errors.append(
                            "quotes.items[{}] missing {}".format(index, key)
                        )

                if (
                    "text" in item
                    and (
                        not isinstance(item["text"], str)
                        or not item["text"]
                    )
                ):
                    errors.append(
                        "quotes.items[{}].text must be a non-empty string".format(
                            index
                        )
                    )

                for key in ("length", "word_count"):
                    if (
                        key in item
                        and not _is_int_not_bool(item[key])
                    ):
                        errors.append(
                            "quotes.items[{}].{} must be an integer".format(
                                index,
                                key,
                            )
                        )

                if structured_parser_quote:
                    expected_item_keys = {
                        "text", "length", "word_count", "source", "retrieved_from",
                    }
                    if set(item) != expected_item_keys:
                        errors.append(
                            "quotes.items[{}] must use the current parser item shape".format(index)
                        )
                    if "retrieved_from" not in item:
                        errors.append(
                            "quotes.items[{}] missing retrieved_from".format(index)
                        )
                    elif item.get("retrieved_from") != "Wikiquote":
                        errors.append(
                            "quotes.items[{}].retrieved_from must be 'Wikiquote'".format(index)
                        )

                    source = item.get("source")
                    if not isinstance(source, dict):
                        errors.append(
                            "quotes.items[{}].source must be an object for current parser data".format(index)
                        )
                        continue

                    expected_source_keys = (
                        "work", "year", "date", "details", "citation", "url",
                    )
                    for key in expected_source_keys:
                        if key not in source:
                            errors.append(
                                "quotes.items[{}].source missing {}".format(index, key)
                            )
                    unexpected_keys = set(source) - set(expected_source_keys)
                    if unexpected_keys:
                        errors.append(
                            "quotes.items[{}].source has unexpected fields".format(index)
                        )
                    for key in ("work", "date", "details", "citation", "url"):
                        if key in source and source[key] is not None and not isinstance(source[key], str):
                            errors.append(
                                "quotes.items[{}].source.{} must be a string or null".format(index, key)
                            )
                    if (
                        "year" in source
                        and source["year"] is not None
                        and (
                            not _is_int_not_bool(source["year"])
                            or source["year"] < 0
                        )
                    ):
                        errors.append(
                            "quotes.items[{}].source.year must be a non-negative integer or null".format(index)
                        )
                elif not isinstance(item.get("source"), str):
                    errors.append(
                        "quotes.items[{}].source must be a string for historical data".format(index)
                    )

        failure = quotes.get("failure")

        if failure is not None:
            errors.extend(
                _require_keys(
                    "quotes.failure",
                    failure,
                    ("reason", "timestamp", "retries"),
                )
            )

            if isinstance(failure, dict):
                if not isinstance(failure.get("reason"), str):
                    errors.append("quotes.failure.reason must be a string")

                if not _is_number_not_bool(failure.get("timestamp")):
                    errors.append("quotes.failure.timestamp must be a number")

                if not _is_int_not_bool(failure.get("retries")):
                    errors.append("quotes.failure.retries must be an integer")
            else:
                errors.append("quotes.failure must be an object or null")

        fetched_at = quotes.get("fetched_at")

        if fetched_at is not None and not _is_int_not_bool(fetched_at):
            errors.append("quotes.fetched_at must be an integer or null")

        # Historical canonical entries predate parser-version provenance, so
        # omission remains valid and is interpreted by callers as stale.
        if "parser_version" in quotes:
            parser_version = quotes["parser_version"]
            if (
                parser_version is not None
                and (
                    not _is_int_not_bool(parser_version)
                    or parser_version <= 0
                )
            ):
                errors.append(
                    "quotes.parser_version must be a positive integer or null"
                )

        if quotes.get("status") == "purged":
            if quotes.get("items") != []:
                errors.append("quotes.purged items must be an empty list")
            if quotes.get("failure") is not None:
                errors.append("quotes.purged failure must be null")
            if quotes.get("fetched_at") is not None:
                errors.append("quotes.purged fetched_at must be null")
            if (
                "parser_version" not in quotes
                or quotes.get("parser_version") is not None
            ):
                errors.append("quotes.purged parser_version must be null")

    evaluation = entry["evaluation"]

    errors.extend(
        _require_keys(
            "evaluation",
            evaluation,
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
    )

    if isinstance(evaluation, dict):
        if evaluation.get("status") not in (
            "unprocessed",
            "accepted",
            "rejected",
        ):
            errors.append(
                "evaluation.status has invalid value"
            )

        if (
            isinstance(quotes, dict)
            and quotes.get("status") == "purged"
            and evaluation.get("status") != "rejected"
        ):
            errors.append("quotes.purged requires evaluation.status rejected")

        algorithm_version = evaluation.get(
            "algorithm_version"
        )

        if (
            algorithm_version is not None
            and (
                not _is_int_not_bool(
                    algorithm_version
                )
                or algorithm_version <= 0
            )
        ):
            errors.append(
                "evaluation.algorithm_version "
                "must be a positive integer or null"
            )

        for field_name in (
            "human_confidence",
            "philosopher_confidence",
            "content_confidence",
        ):
            value = evaluation.get(field_name)

            if (
                value is not None
                and not _is_int_not_bool(value)
            ):
                errors.append(
                    "evaluation.{} must be an integer or null".format(
                        field_name
                    )
                )

        reasons = evaluation.get("reasons")

        if (
            not isinstance(reasons, list)
            or not all(
                isinstance(reason, str)
                for reason in reasons
            )
        ):
            errors.append(
                "evaluation.reasons must be a list of strings"
            )

        processed_at = evaluation.get(
            "processed_at"
        )

        if (
            processed_at is not None
            and not _is_number_not_bool(
                processed_at
            )
        ):
            errors.append(
                "evaluation.processed_at must be a number or null"
            )

        legacy_result = evaluation.get("legacy_result")

        if (
            legacy_result is not None
            and not isinstance(legacy_result, dict)
        ):
            errors.append(
                "evaluation.legacy_result must be an object or null"
            )

    posting = entry["posting"]

    errors.extend(
        _require_keys(
            "posting",
            posting,
            (
                "has_been_posted",
                "posted_at",
                "legacy_posted_without_timestamp",
            ),
        )
    )

    if isinstance(posting, dict):
        has_been_posted = posting.get(
            "has_been_posted"
        )
        posted_at = posting.get("posted_at")
        legacy_marker = posting.get(
            "legacy_posted_without_timestamp"
        )

        if not isinstance(has_been_posted, bool):
            errors.append(
                "posting.has_been_posted must be boolean"
            )

        if not isinstance(posted_at, list):
            errors.append(
                "posting.posted_at must be a list"
            )
        elif not all(
            _is_int_not_bool(timestamp)
            for timestamp in posted_at
        ):
            errors.append(
                "posting.posted_at must contain integer timestamps"
            )

        if not isinstance(legacy_marker, bool):
            errors.append(
                "posting.legacy_posted_without_timestamp "
                "must be boolean"
            )

        if (
            isinstance(posted_at, list)
            and posted_at
            and has_been_posted is not True
        ):
            errors.append(
                "posting.has_been_posted must be true "
                "when posted_at is non-empty"
            )

        if (
            legacy_marker is True
            and has_been_posted is not True
        ):
            errors.append(
                "posting.has_been_posted must be true "
                "when legacy_posted_without_timestamp is true"
            )

    return errors


def validate_database_dataset(
    entries: List[dict],
) -> List[str]:
    errors = []
    seen_titles = set()

    for index, entry in enumerate(entries):
        entry_errors = validate_database_entry(
            entry
        )

        for error in entry_errors:
            errors.append(
                "entry[{}]: {}".format(
                    index,
                    error,
                )
            )

        if isinstance(entry, dict):
            title = entry.get("title")

            if isinstance(title, str):
                if title in seen_titles:
                    errors.append(
                        "Duplicate database title: {!r}".format(
                            title
                        )
                    )
                else:
                    seen_titles.add(title)

    return errors


def serialize_database_entries(
    entries: List[dict],
) -> bytes:
    ordered_entries = sorted(
        entries,
        key=lambda entry: entry["title"],
    )

    serialized_lines = []

    for entry in ordered_entries:
        serialized_lines.append(
            json.dumps(
                _canonicalize_for_serialization(entry),
                ensure_ascii=False,
                sort_keys=False,
                separators=(",", ":"),
            )
        )

    if not serialized_lines:
        return b""

    text = "\n".join(serialized_lines) + "\n"

    return text.encode("utf-8")


def _canonicalize_for_serialization(value, is_evaluation=False):
    """Return recursively deterministic JSON data with ordered evaluation keys."""
    if isinstance(value, dict):
        if is_evaluation:
            keys = [
                key for key in EVALUATION_SERIALIZATION_ORDER
                if key in value
            ]
            keys.extend(
                sorted(
                    key for key in value
                    if key not in EVALUATION_SERIALIZATION_ORDER
                )
            )
        else:
            keys = sorted(value)

        return {
            key: _canonicalize_for_serialization(
                value[key],
                is_evaluation=(key == "evaluation"),
            )
            for key in keys
        }

    if isinstance(value, list):
        return [
            _canonicalize_for_serialization(item)
            for item in value
        ]

    return value
