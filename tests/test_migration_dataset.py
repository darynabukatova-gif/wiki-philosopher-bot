import wiki_philosopher_bot.migration as migration
import wiki_philosopher_bot.cli.migrate_database as migrate_database
from wiki_philosopher_bot.config import (
    ENTITY_FILE,
    POSTED_FILE,
    PROCESSED_FILE,
    QUOTE_FAILURE_FILE,
    QUOTE_FILE,
    RESULT_FILE,
    SUMMARY_FILE,
)
from wiki_philosopher_bot.cli.migrate_database import (
    FileFingerprint,
    SOURCE_ORDER,
    make_empty_database_entry,
)


def make_sources(reverse_order=False):
    filenames = list(SOURCE_ORDER)

    if reverse_order:
        filenames.reverse()

    return {
        filename: {
            "records": [],
            "exists": True,
            "format": (
                "JSON"
                if filename == POSTED_FILE
                else "JSONL"
            ),
            "title_keyed": filename != POSTED_FILE,
            "total_lines": 0,
            "blank_lines": 0,
            "malformed_json": 0,
        }
        for filename in filenames
    }


def make_jsonl_record(line_number, record_index, value):
    return {
        "line_number": line_number,
        "record_index": record_index,
        "value": value,
    }


def make_posted_record(record_index, title):
    return {
        "line_number": None,
        "record_index": record_index,
        "value": title,
    }


def make_multi_title_sources(reverse_order=False):
    sources = make_sources(reverse_order)

    sources[SUMMARY_FILE]["records"] = [
        make_jsonl_record(
            1,
            1,
            {
                "title": "Summary Only",
                "summary": "A summary-only synthetic record.",
            },
        ),
        make_jsonl_record(
            2,
            2,
            {
                "title": "Multi Source",
                "summary": "A multi-source synthetic record.",
            },
        ),
    ]
    sources[ENTITY_FILE]["records"] = [
        make_jsonl_record(
            3,
            1,
            {
                "title": "Entity Only",
                "valid": False,
                "reason": "no_qid",
            },
        )
    ]
    sources[QUOTE_FILE]["records"] = [
        make_jsonl_record(
            4,
            1,
            {
                "title": "Multi Source",
                "quotes": [
                    {
                        "text": "A synthetic quote for dataset testing.",
                        "length": 38,
                        "word_count": 6,
                        "source": "Wikiquote",
                    }
                ],
            },
        )
    ]
    sources[RESULT_FILE]["records"] = [
        make_jsonl_record(
            5,
            1,
            {
                "title": "Accepted Only",
                "status": "accepted",
                "human_confidence": 1,
                "philosopher_confidence": 2,
                "content_confidence": 0,
                "reasons": ["synthetic accepted result"],
                "last_processed": 10.5,
            },
        )
    ]
    sources[POSTED_FILE]["records"] = [
        make_posted_record(1, "Only Posted")
    ]

    return sources


def make_audit_counts(sources):
    return migration.count_legacy_records(sources)


def make_empty_audit_conflicts():
    return {
        "accepted_rejected_conflicts": [],
        "posted_titles_absent_from_title_keyed_files": [],
    }


def make_fingerprints():
    return {
        filename: FileFingerprint(
            filename=filename,
            byte_size=index,
            sha256=("{:064x}".format(index)),
        )
        for index, filename in enumerate(SOURCE_ORDER, start=1)
    }


def test_complete_synthetic_dataset_contains_one_entry_per_union_title():
    sources = make_multi_title_sources()

    entries = migrate_database.build_database_entries(sources)

    expected_titles = [
        "Accepted Only",
        "Entity Only",
        "Multi Source",
        "Only Posted",
        "Summary Only",
    ]

    assert [entry["title"] for entry in entries] == expected_titles
    assert len(entries) == len(expected_titles)

    posted_only_entry = next(
        entry
        for entry in entries
        if entry["title"] == "Only Posted"
    )
    assert migrate_database.validate_database_entry(
        posted_only_entry
    ) == []
    assert posted_only_entry["posting"]["has_been_posted"] is True

    source_record_count = sum(
        len(source["records"])
        for source in sources.values()
    )
    assert source_record_count == 6
    assert len(entries) == 5


def test_dataset_validator_requires_every_posted_title():
    sources = make_sources()
    sources[POSTED_FILE]["records"] = [
        make_posted_record(1, "Only Posted")
    ]
    audit_counts = make_audit_counts(sources)
    audit_conflicts = make_empty_audit_conflicts()

    missing_errors = migrate_database.validate_migration_dataset(
        [],
        sources,
        audit_counts,
        audit_conflicts,
    )

    assert any(
        "Posted title missing from canonical dataset" in error
        and "Only Posted" in error
        for error in missing_errors
    )

    unmarked_entry = make_empty_database_entry("Only Posted")
    unmarked_errors = migrate_database.validate_migration_dataset(
        [unmarked_entry],
        sources,
        audit_counts,
        audit_conflicts,
    )

    assert any(
        "Posted title not marked posted" in error
        and "Only Posted" in error
        for error in unmarked_errors
    )


def test_dataset_validator_requires_all_conflicts_to_be_represented():
    sources = make_sources()
    sources[RESULT_FILE]["records"] = [
        make_jsonl_record(
            1,
            1,
            {
                "title": "Conflict Title",
                "status": "accepted",
                "reasons": [],
            },
        )
    ]
    sources[PROCESSED_FILE]["records"] = [
        make_jsonl_record(
            2,
            1,
            {
                "title": "Conflict Title",
                "status": "rejected",
                "reasons": [],
            },
        )
    ]
    audit_counts = make_audit_counts(sources)
    audit_conflicts = migration.report_conflicts(sources)

    incorrectly_resolved = make_empty_database_entry(
        "Conflict Title"
    )
    incorrectly_resolved["evaluation"]["status"] = "accepted"

    errors = migrate_database.validate_migration_dataset(
        [incorrectly_resolved],
        sources,
        audit_counts,
        audit_conflicts,
    )

    assert any(
        "Conflict Title" in error
        and "evaluation.status" in error
        for error in errors
    )

    unresolved_entry = make_empty_database_entry("Conflict Title")
    migrate_database.add_conflict(
        unresolved_entry,
        "evaluation.status",
        [
            {
                "source": RESULT_FILE,
                "line_number": 1,
                "record_index": 1,
                "value": "accepted",
            },
            {
                "source": PROCESSED_FILE,
                "line_number": 2,
                "record_index": 1,
                "value": "rejected",
            },
        ],
        "unresolved_set_unprocessed",
    )
    unresolved_entry = migrate_database.finalize_entry(
        unresolved_entry
    )

    unresolved_errors = (
        migrate_database.validate_migration_dataset(
            [unresolved_entry],
            sources,
            audit_counts,
            audit_conflicts,
        )
    )

    assert unresolved_errors == []


def test_dataset_validator_rejects_duplicate_canonical_title():
    sources = make_sources()
    sources[SUMMARY_FILE]["records"] = [
        make_jsonl_record(
            1,
            1,
            {
                "title": "Ada Lovelace",
                "summary": "A synthetic summary.",
            },
        )
    ]
    entries = [
        make_empty_database_entry("Ada Lovelace"),
        make_empty_database_entry("Ada Lovelace"),
    ]

    errors = migrate_database.validate_migration_dataset(
        entries,
        sources,
        make_audit_counts(sources),
        make_empty_audit_conflicts(),
    )

    assert any(
        "Duplicate database title" in error
        for error in errors
    )


def test_build_database_entries_is_deterministic():
    first_sources = make_multi_title_sources()
    second_sources = make_multi_title_sources(reverse_order=True)

    first_entries = migrate_database.build_database_entries(
        first_sources
    )
    second_entries = migrate_database.build_database_entries(
        second_sources
    )

    assert first_entries == second_entries
    assert [entry["title"] for entry in first_entries] == sorted(
        entry["title"] for entry in first_entries
    )


def test_dataset_build_does_not_write_database_jsonl(tmp_path, monkeypatch):
    sources = make_multi_title_sources()
    before = sorted(path.name for path in tmp_path.iterdir())
    monkeypatch.chdir(tmp_path)

    entries = migrate_database.build_database_entries(sources)

    assert entries
    assert not (tmp_path / "database.jsonl").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_dataset_validator_rejects_missing_source_title():
    sources = make_sources()
    sources[SUMMARY_FILE]["records"] = [
        make_jsonl_record(
            1,
            1,
            {
                "title": "Summary Title",
                "summary": "A synthetic summary.",
            },
        )
    ]

    errors = migrate_database.validate_migration_dataset(
        [],
        sources,
        make_audit_counts(sources),
        make_empty_audit_conflicts(),
    )

    assert any(
        "Canonical dataset is missing source titles" in error
        and "Summary Title" in error
        for error in errors
    )


def test_dataset_validator_rejects_unexpected_canonical_title():
    sources = make_sources()
    unexpected_entry = make_empty_database_entry("Unexpected Title")

    errors = migrate_database.validate_migration_dataset(
        [unexpected_entry],
        sources,
        make_audit_counts(sources),
        make_empty_audit_conflicts(),
    )

    assert any(
        "Canonical dataset contains unexpected titles" in error
        and "Unexpected Title" in error
        for error in errors
    )


def _contains_generic_score_or_confidence(value):
    if isinstance(value, dict):
        for key, nested_value in value.items():
            if key in ("score", "confidence"):
                return True
            if _contains_generic_score_or_confidence(nested_value):
                return True
        return False

    if isinstance(value, list):
        return any(
            _contains_generic_score_or_confidence(item)
            for item in value
        )

    return False


def test_canonical_entries_do_not_contain_legacy_score_or_confidence_keys():
    sources = make_sources()
    sources[RESULT_FILE]["records"] = [
        make_jsonl_record(
            1,
            1,
            {
                "title": "Historical Result",
                "accepted": True,
                "score": 7,
                "confidence": 9,
                "reasons": [],
            },
        )
    ]

    entries = migrate_database.build_database_entries(sources)

    assert len(entries) == 1
    assert not _contains_generic_score_or_confidence(entries[0])
    assert set(entries[0]["evaluation"]) >= {
        "human_confidence",
        "philosopher_confidence",
        "content_confidence",
    }


def test_build_migration_report_contains_core_counts_and_fingerprints():
    entries = [make_empty_database_entry("Ada Lovelace")]
    audit_counts = {
        "files": {
            SUMMARY_FILE: {
                "records_read": 1,
            },
        },
        "total_records_read": 1,
    }
    audit_validation = {
        "files": {},
        "issues": [],
    }
    audit_conflicts = make_empty_audit_conflicts()
    validation_errors = ["synthetic validation error"]

    report = migrate_database.build_migration_report(
        source_fingerprints=make_fingerprints(),
        audit_counts=audit_counts,
        audit_validation=audit_validation,
        audit_conflicts=audit_conflicts,
        entries=entries,
        validation_errors=validation_errors,
    )

    assert set(report["source_files"]) == set(SOURCE_ORDER)
    assert report["source_files"][SUMMARY_FILE] == {
        "byte_size": 1,
        "sha256": "{:064x}".format(1),
    }
    assert report["audit"]["counts"] == audit_counts
    assert report["canonical"]["entry_count"] == 1
    assert report["canonical"]["validation_errors"] == (
        validation_errors
    )


def test_build_migration_report_is_deterministic_for_same_inputs():
    kwargs = {
        "source_fingerprints": make_fingerprints(),
        "audit_counts": {
            "files": {},
            "total_records_read": 0,
        },
        "audit_validation": {
            "files": {},
            "issues": [],
        },
        "audit_conflicts": make_empty_audit_conflicts(),
        "entries": [make_empty_database_entry("Ada Lovelace")],
        "validation_errors": [],
    }

    first_report = migrate_database.build_migration_report(
        **kwargs
    )
    second_report = migrate_database.build_migration_report(
        **kwargs
    )

    assert first_report == second_report
