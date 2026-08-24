"""Read-only audit for the project's legacy cache files.

This module deliberately never calls the cache helpers: those helpers normalize
JSONL into dictionaries and would hide duplicate records and malformed lines.
It also contains no write, rename, copy, or directory-creation operation.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from wiki_philosopher_bot.config import (
    LEGACY_DATA_FOLDER,
    ENTITY_FILE,
    POSTED_FILE,
    PROCESSED_FILE,
    QUOTE_FAILURE_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)


LEGACY_SOURCES = {
    SUMMARY_FILE: {"format": "JSONL", "title_keyed": True},
    ENTITY_FILE: {"format": "JSONL", "title_keyed": True},
    QUOTE_FILE: {"format": "JSONL", "title_keyed": True},
    QUOTE_FAILURE_FILE: {"format": "JSONL", "title_keyed": True},
    RESULT_FILE: {"format": "JSONL", "title_keyed": True},
    PROCESSED_FILE: {"format": "JSONL", "title_keyed": True},
    POSTED_FILE: {"format": "JSON", "title_keyed": False},
}


def _issue(filename: str, kind: str, message: str, **location: Any) -> dict[str, Any]:
    """Create one consistently shaped audit finding."""
    return {"filename": filename, "kind": kind, "message": message, **location}


def _type_name(value: Any) -> str:
    """Return stable, JSON-oriented names for field-type reporting."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _record_location(record: dict[str, Any]) -> dict[str, Any]:
    """Return a source location, using a line number where the format has one."""
    if record["line_number"] is not None:
        return {"line_number": record["line_number"]}
    return {"record_index": record["record_index"]}


def read_legacy_sources(data_folder: str | Path = LEGACY_DATA_FOLDER) -> dict[str, dict[str, Any]]:
    """Read every expected legacy source without deduplicating or changing it.

    Parsed JSONL records retain their source line number.  A malformed line is
    recorded as an issue and does not prevent later lines from being inspected.
    """
    base_path = Path(data_folder)
    sources: dict[str, dict[str, Any]] = {}

    for filename, specification in LEGACY_SOURCES.items():
        path = base_path / filename
        source: dict[str, Any] = {
            "filename": filename,
            "path": str(path),
            "format": specification["format"],
            "title_keyed": specification["title_keyed"],
            "exists": path.exists(),
            "records": [],
            "issues": [],
            "total_lines": 0,
            "blank_lines": 0,
            "malformed_json": 0,
        }
        sources[filename] = source

        if not source["exists"]:
            source["issues"].append(
                _issue(filename, "missing_file", "Expected legacy source does not exist.")
            )
            continue

        try:
            if source["format"] == "JSONL":
                with path.open("r", encoding="utf-8") as file_handle:
                    for line_number, raw_line in enumerate(file_handle, start=1):
                        source["total_lines"] += 1
                        line = raw_line.strip()
                        if not line:
                            source["blank_lines"] += 1
                            continue
                        try:
                            value = json.loads(line)
                        except json.JSONDecodeError as error:
                            source["malformed_json"] += 1
                            source["issues"].append(
                                _issue(
                                    filename,
                                    "malformed_json",
                                    error.msg,
                                    line_number=line_number,
                                )
                            )
                            continue
                        source["records"].append(
                            {
                                "line_number": line_number,
                                "record_index": len(source["records"]) + 1,
                                "value": value,
                            }
                        )
            else:
                with path.open("r", encoding="utf-8") as file_handle:
                    raw_text = file_handle.read()
                source["total_lines"] = len(raw_text.splitlines())
                source["blank_lines"] = sum(
                    1 for line in raw_text.splitlines() if not line.strip()
                )
                try:
                    value = json.loads(raw_text)
                except json.JSONDecodeError as error:
                    source["malformed_json"] += 1
                    source["issues"].append(
                        _issue(
                            filename,
                            "invalid_json",
                            error.msg,
                            line_number=error.lineno,
                        )
                    )
                    continue
                if not isinstance(value, list):
                    source["issues"].append(
                        _issue(
                            filename,
                            "invalid_root_type",
                            "Expected posted.json to contain a JSON array.",
                        )
                    )
                    continue
                source["records"] = [
                    {"line_number": None, "record_index": index, "value": item}
                    for index, item in enumerate(value, start=1)
                ]
        except OSError as error:
            source["issues"].append(_issue(filename, "read_error", str(error)))

    return sources


def count_legacy_records(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return source counts without changing the parsed records."""
    files = {
        filename: {
            "exists": source["exists"],
            "records_read": len(source["records"]),
            "total_lines": source["total_lines"],
            "blank_lines": source["blank_lines"],
            "malformed_json": source["malformed_json"],
        }
        for filename, source in sources.items()
    }
    return {
        "files": files,
        "total_records_read": sum(item["records_read"] for item in files.values()),
        "total_blank_lines": sum(item["blank_lines"] for item in files.values()),
        "total_malformed_json": sum(item["malformed_json"] for item in files.values()),
    }


def validate_legacy_files(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Validate record shapes, title values, duplicate titles, and field types."""
    report: dict[str, Any] = {"files": {}, "issues": []}

    for filename, source in sources.items():
        file_report: dict[str, Any] = {
            "field_names": [],
            "record_shapes": {},
            "field_types": {},
            "duplicate_titles": {},
            "valid_titles": [],
            "issues": list(source["issues"]),
        }
        report["files"][filename] = file_report
        report["issues"].extend(source["issues"])

        if not source["title_keyed"]:
            title_locations: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for record in source["records"]:
                value = record["value"]
                location = _record_location(record)
                if not isinstance(value, str):
                    finding = _issue(
                        filename, "invalid_posted_title", "Posted title must be a string.", **location
                    )
                    file_report["issues"].append(finding)
                    report["issues"].append(finding)
                elif not value.strip():
                    finding = _issue(
                        filename, "empty_posted_title", "Posted title must not be empty.", **location
                    )
                    file_report["issues"].append(finding)
                    report["issues"].append(finding)
                else:
                    file_report["valid_titles"].append(value)
                    title_locations[value].append(location)
            file_report["duplicate_titles"] = {
                title: locations for title, locations in title_locations.items() if len(locations) > 1
            }
            continue

        fields: set[str] = set()
        shapes: Counter[tuple[str, ...]] = Counter()
        types: dict[str, set[str]] = defaultdict(set)
        title_locations: dict[str, list[dict[str, Any]]] = defaultdict(list)

        for record in source["records"]:
            value = record["value"]
            location = _record_location(record)
            if not isinstance(value, dict):
                finding = _issue(
                    filename,
                    "record_not_object",
                    "Expected a JSON object record.",
                    **location,
                )
                file_report["issues"].append(finding)
                report["issues"].append(finding)
                continue

            shape = tuple(sorted(value))
            shapes[shape] += 1
            fields.update(value)
            for field, field_value in value.items():
                types[field].add(_type_name(field_value))

            if "title" not in value:
                finding = _issue(filename, "missing_title", "Record has no title field.", **location)
                file_report["issues"].append(finding)
                report["issues"].append(finding)
            elif not isinstance(value["title"], str):
                finding = _issue(filename, "invalid_title", "Title must be a string.", **location)
                file_report["issues"].append(finding)
                report["issues"].append(finding)
            elif not value["title"].strip():
                finding = _issue(filename, "empty_title", "Title must not be empty.", **location)
                file_report["issues"].append(finding)
                report["issues"].append(finding)
            else:
                title = value["title"]
                file_report["valid_titles"].append(title)
                title_locations[title].append(location)

        file_report["field_names"] = sorted(fields)
        file_report["record_shapes"] = {
            ", ".join(shape) if shape else "<empty object>": count
            for shape, count in sorted(shapes.items())
        }
        file_report["field_types"] = {
            field: sorted(field_types) for field, field_types in sorted(types.items())
        }
        file_report["duplicate_titles"] = {
            title: locations for title, locations in title_locations.items() if len(locations) > 1
        }
        if len(shapes) > 1:
            finding = _issue(
                filename,
                "multiple_record_shapes",
                "More than one object field shape was observed in this file.",
            )
            file_report["issues"].append(finding)
            report["issues"].append(finding)
        for field, field_types in types.items():
            non_null_types = field_types - {"null"}
            if len(non_null_types) > 1:
                finding = _issue(
                    filename,
                    "inconsistent_field_type",
                    f"Field '{field}' has incompatible types: {', '.join(sorted(field_types))}.",
                )
                file_report["issues"].append(finding)
                report["issues"].append(finding)

    return report


def _evaluation_status(value: dict[str, Any]) -> str | None:
    """Extract a comparable historical decision from either known result shape."""
    status = value.get("status")
    if status in {"accepted", "rejected"}:
        return status
    accepted = value.get("accepted")
    if isinstance(accepted, bool):
        return "accepted" if accepted else "rejected"
    return None


def report_conflicts(sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Report cross-file evaluation disagreements and orphaned posted titles."""
    result_records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    processed_records: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for filename, destination in ((RESULT_FILE, result_records), (PROCESSED_FILE, processed_records)):
        for record in sources[filename]["records"]:
            value = record["value"]
            if isinstance(value, dict) and isinstance(value.get("title"), str) and value["title"].strip():
                destination[value["title"]].append(
                    {**_record_location(record), "status": _evaluation_status(value)}
                )

    evaluation_conflicts = []
    for title in sorted(set(result_records) & set(processed_records)):
        statuses = {item["status"] for item in result_records[title] + processed_records[title]}
        if "accepted" in statuses and "rejected" in statuses:
            evaluation_conflicts.append(
                {
                    "title": title,
                    "results_records": result_records[title],
                    "processed_records": processed_records[title],
                }
            )

    title_keyed_titles = {
        title
        for source in sources.values()
        if source["title_keyed"]
        for record in source["records"]
        if isinstance(record["value"], dict)
        for title in [record["value"].get("title")]
        if isinstance(title, str) and title.strip()
    }
    absent_posted_titles = []
    for record in sources[POSTED_FILE]["records"]:
        title = record["value"]
        if isinstance(title, str) and title.strip() and title not in title_keyed_titles:
            absent_posted_titles.append({"title": title, **_record_location(record)})

    return {
        "accepted_rejected_conflicts": evaluation_conflicts,
        "posted_titles_absent_from_title_keyed_files": absent_posted_titles,
    }


def _print_report(counts: dict[str, Any], validation: dict[str, Any], conflicts: dict[str, Any]) -> None:
    """Print a compact, human-readable representation of the structured audit."""
    print("Legacy migration audit (read-only)")
    print("=" * 34)
    print("\nSource files")
    for filename, details in counts["files"].items():
        state = "present" if details["exists"] else "MISSING"
        print(
            f"- {filename}: {state}; records={details['records_read']}; "
            f"blank lines={details['blank_lines']}; malformed JSON={details['malformed_json']}"
        )

    print("\nValidation by file")
    for filename, details in validation["files"].items():
        print(
            f"- {filename}: fields={details['field_names']}; "
            f"shapes={len(details['record_shapes'])}; "
            f"duplicate titles={len(details['duplicate_titles'])}; "
            f"issues={len(details['issues'])}"
        )
        if details["record_shapes"]:
            print(f"  shapes: {details['record_shapes']}")
        if details["field_types"]:
            print(f"  field types: {details['field_types']}")
        if details["duplicate_titles"]:
            print(f"  duplicate titles: {details['duplicate_titles']}")
        for issue in details["issues"]:
            location = issue.get("line_number", issue.get("record_index", "-"))
            print(f"  * {issue['kind']} at {location}: {issue['message']}")

    print("\nCross-file conflicts")
    print(f"- accepted/rejected conflicts: {len(conflicts['accepted_rejected_conflicts'])}")
    for conflict in conflicts["accepted_rejected_conflicts"]:
        print(f"  * {conflict['title']}")
    print(
        "- posted titles absent from all title-keyed files: "
        f"{len(conflicts['posted_titles_absent_from_title_keyed_files'])}"
    )
    for record in conflicts["posted_titles_absent_from_title_keyed_files"]:
        location = record.get("line_number", record.get("record_index", "-"))
        print(f"  * {record['title']} at {location}")
    print("\nTotals")
    print(f"- source records read: {counts['total_records_read']}")
    print(f"- blank lines: {counts['total_blank_lines']}")
    print(f"- malformed JSON: {counts['total_malformed_json']}")
    print(f"- validation findings: {len(validation['issues'])}")


def main() -> dict[str, Any]:
    """Run the read-only audit and return its structured results."""
    sources = read_legacy_sources()
    counts = count_legacy_records(sources)
    validation = validate_legacy_files(sources)
    conflicts = report_conflicts(sources)
    report = {"sources": sources, "counts": counts, "validation": validation, "conflicts": conflicts}
    _print_report(counts, validation, conflicts)
    return report


if __name__ == "__main__":
    main()
