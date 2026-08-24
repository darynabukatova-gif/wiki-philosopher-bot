import hashlib
import json

import pytest

import wiki_philosopher_bot.cli.migrate_database as migrate_database
from wiki_philosopher_bot.database_schema import serialize_database_entries
from wiki_philosopher_bot.cli.migrate_database import make_empty_database_entry


def make_valid_entries_in_reverse_order():
    return [
        make_empty_database_entry("Zeno"),
        make_empty_database_entry("Ada Lovelace"),
    ]


def canonical_jsonl_bytes(entries):
    return serialize_database_entries(entries)


def directory_contents(directory):
    return sorted(
        path.name
        for path in directory.iterdir()
    )


def test_writer_refuses_nonempty_existing_database(tmp_path):
    output_path = tmp_path / "database.jsonl"
    sentinel = b"do not overwrite this database\n"
    output_path.write_bytes(sentinel)

    with pytest.raises(ValueError):
        migrate_database.ensure_output_path_is_safe(output_path)

    assert output_path.read_bytes() == sentinel


def test_writer_allows_absent_output_path(tmp_path):
    output_path = tmp_path / "database.jsonl"

    migrate_database.ensure_output_path_is_safe(output_path)

    assert not output_path.exists()


def test_writer_allows_zero_byte_placeholder(tmp_path):
    output_path = tmp_path / "database.jsonl"
    output_path.write_bytes(b"")

    migrate_database.ensure_output_path_is_safe(output_path)

    assert output_path.read_bytes() == b""


def test_writer_validates_temp_file_before_replace(tmp_path, monkeypatch):
    output_path = tmp_path / "database.jsonl"
    serialized_bytes = canonical_jsonl_bytes(
        make_valid_entries_in_reverse_order()
    )
    expected_titles = {"Ada Lovelace", "Zeno"}
    validated_paths = []

    def fail_validation(path, titles):
        validated_paths.append((path, titles, path.read_bytes()))
        return ["synthetic serialized validation error"]

    monkeypatch.setattr(
        migrate_database,
        "validate_serialized_database",
        fail_validation,
        raising=False,
    )

    with pytest.raises(ValueError):
        migrate_database.write_database_atomically(
            output_path,
            serialized_bytes,
            expected_titles,
        )

    assert len(validated_paths) == 1
    validation_path, validation_titles, validation_bytes = (
        validated_paths[0]
    )
    assert validation_path.parent == output_path.parent
    assert validation_path != output_path
    assert validation_titles == expected_titles
    assert validation_bytes == serialized_bytes
    assert not output_path.exists()


def test_writer_leaves_existing_output_unchanged_when_validation_fails(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "database.jsonl"
    output_path.write_bytes(b"")
    serialized_bytes = canonical_jsonl_bytes(
        make_valid_entries_in_reverse_order()
    )

    monkeypatch.setattr(
        migrate_database,
        "validate_serialized_database",
        lambda path, titles: ["synthetic invalid output"],
        raising=False,
    )

    with pytest.raises(ValueError):
        migrate_database.write_database_atomically(
            output_path,
            serialized_bytes,
            {"Ada Lovelace", "Zeno"},
        )

    assert output_path.read_bytes() == b""


def test_writer_emits_sorted_deterministic_jsonl():
    entries = make_valid_entries_in_reverse_order()

    first_serialized = migrate_database.serialize_database_entries(
        entries
    )
    second_serialized = migrate_database.serialize_database_entries(
        entries
    )
    expected_serialized = canonical_jsonl_bytes(entries)

    assert first_serialized == second_serialized
    assert first_serialized == expected_serialized
    assert first_serialized.endswith(b"\n")
    assert first_serialized.count(b"\n") == len(entries)

    parsed_entries = [
        json.loads(line)
        for line in first_serialized.decode("utf-8").splitlines()
    ]

    assert [entry["title"] for entry in parsed_entries] == [
        "Ada Lovelace",
        "Zeno",
    ]
    assert parsed_entries == sorted(
        entries,
        key=lambda entry: entry["title"],
    )


def test_validate_serialized_database_accepts_valid_database(tmp_path):
    output_path = tmp_path / "database.jsonl"
    entries = make_valid_entries_in_reverse_order()
    output_path.write_bytes(canonical_jsonl_bytes(entries))

    errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace", "Zeno"},
    )

    assert errors == []


def test_validate_serialized_database_rejects_duplicate_titles(tmp_path):
    output_path = tmp_path / "database.jsonl"
    entry = make_empty_database_entry("Ada Lovelace")
    output_path.write_bytes(canonical_jsonl_bytes([entry, entry]))

    errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )
    repeated_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )

    assert errors == repeated_errors
    assert any(
        "Duplicate database title" in error
        for error in errors
    )


def test_validate_serialized_database_rejects_missing_expected_title(
    tmp_path,
):
    output_path = tmp_path / "database.jsonl"
    entry = make_empty_database_entry("Ada Lovelace")
    output_path.write_bytes(canonical_jsonl_bytes([entry]))

    errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace", "Zeno"},
    )
    repeated_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace", "Zeno"},
    )

    assert errors == repeated_errors
    assert any(
        "Zeno" in error
        and "missing" in error.lower()
        for error in errors
    )


def test_validate_serialized_database_rejects_unexpected_title(tmp_path):
    output_path = tmp_path / "database.jsonl"
    entry = make_empty_database_entry("Unexpected")
    output_path.write_bytes(canonical_jsonl_bytes([entry]))

    errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )
    repeated_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )

    assert errors == repeated_errors
    assert any(
        "Unexpected" in error
        and "unexpected" in error.lower()
        for error in errors
    )


def test_validate_serialized_database_rejects_malformed_jsonl(tmp_path):
    output_path = tmp_path / "database.jsonl"
    valid_entry = make_empty_database_entry("Ada Lovelace")
    output_path.write_bytes(
        canonical_jsonl_bytes([valid_entry])
        + b'{"broken":\n'
    )

    first_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )
    second_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )

    assert first_errors == second_errors
    assert first_errors
    assert all(isinstance(error, str) for error in first_errors)
    assert any("line 2" in error.lower() for error in first_errors)


def test_validate_serialized_database_rejects_invalid_canonical_entry(
    tmp_path,
):
    output_path = tmp_path / "database.jsonl"
    output_path.write_bytes(
        json.dumps(
            {"title": "Ada Lovelace"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )

    errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )
    repeated_errors = migrate_database.validate_serialized_database(
        output_path,
        {"Ada Lovelace"},
    )

    assert errors == repeated_errors
    assert any(
        "Missing required top-level field" in error
        for error in errors
    )


def test_successful_atomic_write_returns_final_sha256(tmp_path):
    output_path = tmp_path / "database.jsonl"
    serialized_bytes = canonical_jsonl_bytes(
        make_valid_entries_in_reverse_order()
    )

    result = migrate_database.write_database_atomically(
        output_path,
        serialized_bytes,
        {"Ada Lovelace", "Zeno"},
    )

    assert output_path.exists()
    assert output_path.read_bytes() == serialized_bytes
    assert result == hashlib.sha256(serialized_bytes).hexdigest()


def test_atomic_writer_does_not_modify_unrelated_files(tmp_path):
    output_path = tmp_path / "database.jsonl"
    first_sentinel = tmp_path / "unrelated-one.txt"
    second_sentinel = tmp_path / "unrelated-two.jsonl"
    first_sentinel.write_bytes(b"first sentinel")
    second_sentinel.write_bytes(b"second sentinel")
    before = {
        first_sentinel: first_sentinel.read_bytes(),
        second_sentinel: second_sentinel.read_bytes(),
    }

    migrate_database.write_database_atomically(
        output_path,
        canonical_jsonl_bytes(make_valid_entries_in_reverse_order()),
        {"Ada Lovelace", "Zeno"},
    )

    assert {
        first_sentinel: first_sentinel.read_bytes(),
        second_sentinel: second_sentinel.read_bytes(),
    } == before


def test_atomic_writer_cleans_up_temporary_file_on_pre_replace_failure(
    tmp_path,
    monkeypatch,
):
    output_path = tmp_path / "database.jsonl"
    output_path.write_bytes(b"")
    before = directory_contents(tmp_path)
    validated_paths = []

    def fail_after_temp_creation(path, titles):
        validated_paths.append(path)
        raise OSError("synthetic validation read failure")

    monkeypatch.setattr(
        migrate_database,
        "validate_serialized_database",
        fail_after_temp_creation,
        raising=False,
    )

    with pytest.raises((OSError, ValueError)):
        migrate_database.write_database_atomically(
            output_path,
            canonical_jsonl_bytes(
                make_valid_entries_in_reverse_order()
            ),
            {"Ada Lovelace", "Zeno"},
        )

    assert validated_paths
    assert output_path.read_bytes() == b""
    assert directory_contents(tmp_path) == before
