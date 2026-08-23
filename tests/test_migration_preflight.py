import stat
import hashlib

from migrate_database import (
    FileFingerprint,
    fingerprint_file,
    parse_backup_manifest,
    verify_backup_snapshot,
    verify_live_and_backup_baseline,
    verify_source_manifest,
)

def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()

def write_manifest(path, records):
    lines = [
        "Pre-migration legacy-source backup manifest",
        "============================================",
        "",
        "Format: SHA-256 | byte size | filename",
    ]

    for filename, data in records.items():
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

def test_parse_backup_manifest_reads_all_expected_fingerprints(
    tmp_path,
):
    records = {
        "summaries.jsonl": b"summary data\n",
        "posted.json": b'["Ada Lovelace"]\n',
    }

    manifest_path = tmp_path / "SHA256SUMS.txt"

    write_manifest(
        manifest_path,
        records,
    )

    result = parse_backup_manifest(
        manifest_path,
        records.keys(),
    )

    assert set(result) == {
        "summaries.jsonl",
        "posted.json",
    }

    assert result["summaries.jsonl"] == FileFingerprint(
        filename="summaries.jsonl",
        byte_size=len(records["summaries.jsonl"]),
        sha256=sha256_bytes(
            records["summaries.jsonl"]
        ),
    )

def test_verify_source_manifest_reports_changed_hash_without_writing(
    tmp_path,
):
    filename = "summaries.jsonl"

    original = b"original\n"
    changed = b"changed\n"

    path = tmp_path / filename
    path.write_bytes(changed)

    expected = {
        filename: FileFingerprint(
            filename=filename,
            byte_size=len(original),
            sha256=sha256_bytes(original),
        )
    }

    before = path.read_bytes()

    mismatches = verify_source_manifest(
        tmp_path,
        expected,
    )

    after = path.read_bytes()

    assert before == after

    assert any(
        "SHA-256" in message
        for message in mismatches
    )

def test_verify_backup_snapshot_rejects_writable_file(
    tmp_path,
):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    filename = "summaries.jsonl"
    data = b"summary\n"

    source_path = backup_dir / filename
    source_path.write_bytes(data)

    manifest_path = backup_dir / "SHA256SUMS.txt"

    write_manifest(
        manifest_path,
        {
            filename: data,
        },
    )

    source_path.chmod(0o644)
    manifest_path.chmod(0o444)
    backup_dir.chmod(0o555)

    mismatches = verify_backup_snapshot(
        backup_dir,
        manifest_path,
        [filename],
    )

    assert (
        "Backup file is writable: summaries.jsonl"
        in mismatches
    )

def test_verify_backup_snapshot_rejects_missing_source_file(
    tmp_path,
):
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()

    filename = "summaries.jsonl"
    data = b"summary\n"

    manifest_path = backup_dir / "SHA256SUMS.txt"

    write_manifest(
        manifest_path,
        {
            filename: data,
        },
    )

    manifest_path.chmod(0o444)
    backup_dir.chmod(0o555)

    mismatches = verify_backup_snapshot(
        backup_dir,
        manifest_path,
        [filename],
    )

    assert (
        "Missing source file: summaries.jsonl"
        in mismatches
    )

def test_verify_live_and_backup_baseline_requires_both_to_match(
    tmp_path,
):
    live_dir = tmp_path / "live"
    backup_dir = tmp_path / "backup"

    live_dir.mkdir()
    backup_dir.mkdir()

    filename = "summaries.jsonl"
    expected_data = b"correct\n"

    (live_dir / filename).write_bytes(
        b"changed live data\n"
    )

    (backup_dir / filename).write_bytes(
        expected_data
    )

    manifest_path = (
        backup_dir / "SHA256SUMS.txt"
    )

    write_manifest(
        manifest_path,
        {
            filename: expected_data,
        },
    )

    (backup_dir / filename).chmod(0o444)
    manifest_path.chmod(0o444)
    backup_dir.chmod(0o555)

    result = verify_live_and_backup_baseline(
        live_source_dir=live_dir,
        backup_dir=backup_dir,
        manifest_path=manifest_path,
        expected_filenames=[filename],
    )

    assert result["ok"] is False
    assert result["live_mismatches"]
    assert result["backup_mismatches"] == []