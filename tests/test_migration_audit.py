import wiki_philosopher_bot.migration as migration

def test_migration_audit_is_read_only_and_reports_conflicts(
    legacy_source_dir,
    snapshot_bytes,
):
    before_files = sorted(
        path.name
        for path in legacy_source_dir.iterdir()
        if path.is_file()
    )
    before_bytes = snapshot_bytes(legacy_source_dir)

    sources = migration.read_legacy_sources(legacy_source_dir)
    counts = migration.count_legacy_records(sources)
    validation = migration.validate_legacy_files(sources)
    conflicts = migration.report_conflicts(sources)

    after_files = sorted(
        path.name
        for path in legacy_source_dir.iterdir()
        if path.is_file()
    )
    after_bytes = snapshot_bytes(legacy_source_dir)

    assert before_files == after_files
    assert before_bytes == after_bytes

    assert counts["total_records_read"] == 7

    expected_legacy_files = {
        "summaries.jsonl",
        "entities.jsonl",
        "quotes.jsonl",
        "quote_failures.jsonl",
        "results.jsonl",
        "processed.jsonl",
        "posted.json",
    }

    assert set(counts["files"]) == expected_legacy_files

    assert len(conflicts["accepted_rejected_conflicts"]) == 1
    assert (
        conflicts["accepted_rejected_conflicts"][0]["title"]
        == "Conflict title"
    )

    assert (
        conflicts["posted_titles_absent_from_title_keyed_files"]
        == []
    )

    assert validation is not None