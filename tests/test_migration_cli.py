import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

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
from wiki_philosopher_bot.cli.migrate_database import SOURCE_ORDER, make_empty_database_entry


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def write_manifest(path, records):
    lines = [
        "Pre-migration legacy-source backup manifest",
        "Format: SHA-256 | byte size | filename",
    ]

    for filename in SOURCE_ORDER:
        data = records[filename]
        lines.append(
            "{} | {} | {}".format(
                sha256_bytes(data),
                len(data),
                filename,
            )
        )

    path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def make_source_bytes():
    accepted = {
        "title": "Conflict Title",
        "status": "accepted",
        "reasons": [],
    }
    rejected = {
        "title": "Conflict Title",
        "status": "rejected",
        "reasons": [],
    }

    return {
        SUMMARY_FILE: b"",
        ENTITY_FILE: b"",
        QUOTE_FILE: b"",
        QUOTE_FAILURE_FILE: b"",
        RESULT_FILE: (
            json.dumps(accepted, sort_keys=True).encode("utf-8")
            + b"\n"
        ),
        PROCESSED_FILE: (
            json.dumps(rejected, sort_keys=True).encode("utf-8")
            + b"\n"
        ),
        POSTED_FILE: b"[]\n",
    }


def make_verified_snapshot(tmp_path):
    live_source_dir = tmp_path / "live"
    backup_dir = tmp_path / "backup"
    live_source_dir.mkdir()
    backup_dir.mkdir()

    records = make_source_bytes()

    for filename, data in records.items():
        (live_source_dir / filename).write_bytes(data)
        (backup_dir / filename).write_bytes(data)

    manifest_path = backup_dir / "SHA256SUMS.txt"
    write_manifest(manifest_path, records)

    for filename in SOURCE_ORDER:
        (backup_dir / filename).chmod(0o444)

    manifest_path.chmod(0o444)
    backup_dir.chmod(0o555)

    return live_source_dir, backup_dir, manifest_path


def cli_arguments(live_source_dir, backup_dir, manifest_path, mode):
    return [
        "--source-dir",
        str(backup_dir),
        "--live-source-dir",
        str(live_source_dir),
        "--manifest",
        str(manifest_path),
        mode,
    ]


def write_cli_arguments(
    live_source_dir,
    backup_dir,
    manifest_path,
    output_path,
):
    return cli_arguments(
        live_source_dir,
        backup_dir,
        manifest_path,
        "--write",
    ) + [
        "--output",
        str(output_path),
    ]


def source_bytes(source_dir):
    return {
        filename: (source_dir / filename).read_bytes()
        for filename in SOURCE_ORDER
    }


def migration_temp_files(directory):
    return sorted(
        path.name
        for path in directory.iterdir()
        if path.is_file()
        and path.name.startswith(".database-migration-")
        and path.name.endswith(".tmp")
    )


def test_dry_run_returns_success_and_creates_no_database_file(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    before = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    )

    result = migrate_database.main(
        cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            "--dry-run",
        )
    )

    captured = capsys.readouterr()
    after = sorted(
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
    )

    assert result == 0
    assert not (tmp_path / "database.jsonl").exists()
    assert after == before
    assert "source_files" in captured.out


def test_dry_run_refuses_changed_live_source(tmp_path, capsys):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    (live_source_dir / SUMMARY_FILE).write_bytes(
        b'{"title": "Changed", "summary": "changed"}\n'
    )

    result = migrate_database.main(
        cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            "--dry-run",
        )
    )

    captured = capsys.readouterr()

    assert result != 0
    assert not (tmp_path / "database.jsonl").exists()
    assert "baseline verification failed" in captured.out.lower()


def test_dry_run_refuses_invalid_backup_permissions(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    (backup_dir / SUMMARY_FILE).chmod(0o644)

    result = migrate_database.main(
        cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            "--dry-run",
        )
    )

    captured = capsys.readouterr()

    assert result != 0
    assert not (tmp_path / "database.jsonl").exists()
    assert "backup file is writable" in captured.out.lower()


def test_dry_run_report_contains_hashes_counts_conflicts_and_output_hash(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )

    result = migrate_database.main(
        cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            "--dry-run",
        )
    )

    output = capsys.readouterr().out

    assert result == 0
    assert "source_files" in output
    assert "sha256" in output
    assert "total_records_read" in output
    assert "entry_count" in output
    assert "accepted_rejected_conflicts" in output
    assert "validation_errors" in output


def test_cli_requires_explicit_mode(tmp_path):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )

    with pytest.raises(SystemExit) as error:
        migrate_database.main(
            cli_arguments(
                live_source_dir,
                backup_dir,
                manifest_path,
                "",
            )[:-1]
        )

    assert error.value.code == 2


def test_cli_rejects_dry_run_and_write_together(tmp_path):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    arguments = cli_arguments(
        live_source_dir,
        backup_dir,
        manifest_path,
        "--dry-run",
    )
    arguments.append("--write")

    with pytest.raises(SystemExit) as error:
        migrate_database.main(arguments)

    assert error.value.code == 2


def test_cli_write_requires_explicit_output(tmp_path):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )

    with pytest.raises(SystemExit) as error:
        migrate_database.main(
            cli_arguments(
                live_source_dir,
                backup_dir,
                manifest_path,
                "--write",
            )
        )

    assert error.value.code == 2
    assert not (tmp_path / "database.jsonl").exists()


def test_cli_dry_run_does_not_require_output(tmp_path, capsys):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )

    result = migrate_database.main(
        cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            "--dry-run",
        )
    )

    assert result == 0
    assert "source_files" in capsys.readouterr().out
    assert not (tmp_path / "database.jsonl").exists()


def test_cli_write_rejects_output_inside_selected_backup(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = backup_dir / "database.jsonl"
    before_live = source_bytes(live_source_dir)
    before_backup = source_bytes(backup_dir)

    result = migrate_database.main(
        write_cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            output_path,
        )
    )

    output = capsys.readouterr().out.lower()
    assert result != 0
    assert "backup" in output
    assert "forbidden" in output
    assert not output_path.exists()
    assert source_bytes(live_source_dir) == before_live
    assert source_bytes(backup_dir) == before_backup


def test_cli_write_refuses_nonempty_existing_output(tmp_path, capsys):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = tmp_path / "database.jsonl"
    sentinel = b"do not overwrite this database\n"
    output_path.write_bytes(sentinel)
    before_live = source_bytes(live_source_dir)
    before_backup = source_bytes(backup_dir)

    result = migrate_database.main(
        write_cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            output_path,
        )
    )

    assert result != 0
    assert "non-empty" in capsys.readouterr().out.lower()
    assert output_path.read_bytes() == sentinel
    assert migration_temp_files(tmp_path) == []
    assert source_bytes(live_source_dir) == before_live
    assert source_bytes(backup_dir) == before_backup


def test_cli_write_runs_full_pipeline_and_writes_valid_database(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = tmp_path / "database.jsonl"
    before_live = source_bytes(live_source_dir)
    before_backup = source_bytes(backup_dir)

    result = migrate_database.main(
        write_cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            output_path,
        )
    )

    report = json.loads(capsys.readouterr().out)
    expected_titles = {"Conflict Title"}
    entries, _ = migrate_database.run_dry_migration(
        backup_dir,
        live_source_dir,
        manifest_path,
    )

    assert result == 0
    assert output_path.exists()
    assert migrate_database.validate_serialized_database(
        output_path,
        expected_titles,
    ) == []
    assert output_path.read_bytes() == (
        migrate_database.serialize_database_entries(entries)
    )
    assert report["canonical"]["entry_count"] == 1
    assert source_bytes(live_source_dir) == before_live
    assert source_bytes(backup_dir) == before_backup
    assert migration_temp_files(tmp_path) == []


def test_migration_script_executes_real_write_cli_with_synthetic_data(
    tmp_path,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = tmp_path / "database.jsonl"
    script_path = Path(migrate_database.__file__).resolve()
    before_live = source_bytes(live_source_dir)
    before_backup = source_bytes(backup_dir)

    completed = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--source-dir",
            str(backup_dir),
            "--live-source-dir",
            str(live_source_dir),
            "--manifest",
            str(manifest_path),
            "--write",
            "--output",
            str(output_path),
        ],
        cwd=str(script_path.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout
    report = json.loads(completed.stdout)
    assert output_path.exists()
    assert migrate_database.validate_serialized_database(
        output_path,
        {"Conflict Title"},
    ) == []
    assert report["write"]["output_path"] == str(output_path)
    assert report["write"]["sha256"] == hashlib.sha256(
        output_path.read_bytes()
    ).hexdigest()
    assert report["canonical"]["entry_count"] == 1
    assert report["write"]["validation_errors"] == []
    assert report["write"]["post_write_baseline"]["ok"] is True
    assert source_bytes(live_source_dir) == before_live
    assert source_bytes(backup_dir) == before_backup
    assert migration_temp_files(tmp_path) == []


def test_cli_write_rechecks_live_and_backup_after_replacement(
    tmp_path,
    monkeypatch,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = tmp_path / "database.jsonl"
    before_live = source_bytes(live_source_dir)
    before_backup = source_bytes(backup_dir)
    original_verify = (
        migrate_database.verify_live_and_backup_baseline
    )
    calls = []

    def fail_only_post_write(*args, **kwargs):
        result = original_verify(*args, **kwargs)
        calls.append(result)

        if len(calls) == 2:
            result = dict(result)
            result["ok"] = False
            result["live_mismatches"] = [
                "synthetic post-write live mismatch"
            ]

        return result

    monkeypatch.setattr(
        migrate_database,
        "verify_live_and_backup_baseline",
        fail_only_post_write,
    )

    result = migrate_database.main(
        write_cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            output_path,
        )
    )

    output = capsys.readouterr().out.lower()
    assert result != 0
    assert len(calls) == 2
    assert "critical" in output
    assert "post-write" in output
    assert output_path.exists()
    assert migrate_database.validate_serialized_database(
        output_path,
        {"Conflict Title"},
    ) == []
    assert source_bytes(live_source_dir) == before_live
    assert source_bytes(backup_dir) == before_backup


def test_cli_write_success_report_contains_required_fields(
    tmp_path,
    capsys,
):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    output_path = tmp_path / "database.jsonl"

    result = migrate_database.main(
        write_cli_arguments(
            live_source_dir,
            backup_dir,
            manifest_path,
            output_path,
        )
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["write"] == {
        "output_path": str(output_path),
        "sha256": hashlib.sha256(
            output_path.read_bytes()
        ).hexdigest(),
        "validation_errors": [],
        "post_write_baseline": {
            "ok": True,
            "live_mismatches": [],
            "backup_mismatches": [],
        },
    }


def test_print_migration_report_is_deterministic(capsys):
    report = {
        "source_files": {
            SUMMARY_FILE: {
                "byte_size": 1,
                "sha256": "a" * 64,
            }
        },
        "audit": {
            "counts": {"total_records_read": 1},
            "validation": {"issues": []},
            "conflicts": {
                "accepted_rejected_conflicts": [],
                "posted_titles_absent_from_title_keyed_files": [],
            },
        },
        "canonical": {
            "entry_count": 1,
            "validation_errors": [],
        },
    }

    migrate_database.print_migration_report(report)
    first_output = capsys.readouterr().out
    migrate_database.print_migration_report(report)
    second_output = capsys.readouterr().out

    assert first_output == second_output
    assert json.loads(first_output) == report


def test_migration_script_executes_real_dry_run_cli(tmp_path):
    live_source_dir, backup_dir, manifest_path = (
        make_verified_snapshot(tmp_path)
    )
    script_path = Path(migrate_database.__file__).resolve()
    project_root = script_path.parent
    before_live = {
        filename: (live_source_dir / filename).read_bytes()
        for filename in SOURCE_ORDER
    }
    before_backup = {
        filename: (backup_dir / filename).read_bytes()
        for filename in SOURCE_ORDER
    }

    completed = subprocess.run(
        [
            sys.executable,
            str(script_path),
            "--source-dir",
            str(backup_dir),
            "--live-source-dir",
            str(live_source_dir),
            "--manifest",
            str(manifest_path),
            "--dry-run",
        ],
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout

    report = json.loads(completed.stdout)

    assert set(report["source_files"]) == set(SOURCE_ORDER)
    assert report["audit"]["counts"]["total_records_read"] == 2
    assert report["canonical"]["entry_count"] == 1
    assert report["canonical"]["validation_errors"] == []
    assert len(
        report["audit"]["conflicts"][
            "accepted_rejected_conflicts"
        ]
    ) == 1
    assert not (live_source_dir / "database.jsonl").exists()
    assert not (backup_dir / "database.jsonl").exists()
    assert {
        filename: (live_source_dir / filename).read_bytes()
        for filename in SOURCE_ORDER
    } == before_live
    assert {
        filename: (backup_dir / filename).read_bytes()
        for filename in SOURCE_ORDER
    } == before_backup
