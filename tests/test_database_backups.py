import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import cache


def write_database(tmp_path, content=b'{"title":"Ada"}\n'):
    path = tmp_path / "database.jsonl"
    path.write_bytes(content)
    return path


def test_create_database_backup_is_byte_identical_with_metadata(tmp_path):
    source = write_database(tmp_path, b"canonical bytes\n")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 8, 23, 21, 35, 2, tzinfo=timezone.utc)

    result = cache.create_database_backup(
        str(tmp_path), backup_dir, "manual good state", preserve=True,
        kind="manual", now=now,
    )

    assert result.created
    assert Path(result.path).name == "database-manual-good-state-2026-08-23T21-35-02Z.jsonl"
    assert Path(result.path).read_bytes() == source.read_bytes()
    metadata = json.loads(Path(result.path + ".meta.json").read_text())
    assert metadata["kind"] == "manual"
    assert metadata["preserve"] is True
    assert metadata["sha256"] == result.sha256


def test_backup_failure_missing_source_and_collision_are_safe(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    failed = cache.create_database_backup(str(tmp_path), backup_dir, "manual")
    assert not failed.created
    write_database(tmp_path)
    first = cache.create_database_backup(
        str(tmp_path), backup_dir, "manual", now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    second = cache.create_database_backup(
        str(tmp_path), backup_dir, "manual", now=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert first.created and second.created
    assert first.path != second.path


def test_pruning_only_removes_expired_identified_operational_pairs(tmp_path):
    write_database(tmp_path)
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    now = datetime(2026, 8, 23, tzinfo=timezone.utc)
    expired = cache.create_database_backup(
        str(tmp_path), backup_dir, "before-test", retention_days=90,
        preserve=False, kind="operational", now=now - timedelta(days=91),
    )
    manual = cache.create_database_backup(
        str(tmp_path), backup_dir, "manual", preserve=True, kind="manual",
        now=now - timedelta(days=91),
    )
    historical = backup_dir / "database-historical.jsonl"
    historical.write_bytes(b"historic")
    current = cache.create_database_backup(
        str(tmp_path), backup_dir, "before-test", retention_days=90,
        preserve=False, kind="operational", now=now,
    )

    assert current.created
    assert not Path(expired.path).exists()
    assert not Path(expired.path + ".meta.json").exists()
    assert Path(manual.path).exists()
    assert historical.exists()
    assert current.pruned_paths == [expired.path]
