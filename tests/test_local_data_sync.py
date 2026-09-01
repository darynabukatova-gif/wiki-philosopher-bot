from pathlib import Path
from types import SimpleNamespace
import threading

import pytest

import wiki_philosopher_bot.local_data_sync as local_data_sync
import wiki_philosopher_bot.cli.sync_local_data as sync_cli
from wiki_philosopher_bot.cache import load_database
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    make_pending_posting_attempt,
    serialize_database_entries,
)


class FakeGit:
    def __init__(self, *, dirty=False, branch="main", ahead=0, behind=0):
        self.dirty = dirty
        self.branch = branch
        self.ahead = ahead
        self.behind = behind
        self.commands = []
        self.fetch_error = None
        self.pull_error = None

    def __call__(self, command):
        self.commands.append(command)
        arguments = command[3:]
        if arguments == ["rev-parse", "--is-inside-work-tree"]:
            return SimpleNamespace(returncode=0, stdout="true\n", stderr="")
        if arguments == ["status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout=" M database.jsonl\n" if self.dirty else "", stderr="")
        if arguments == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=self.branch + "\n", stderr="")
        if arguments == ["rev-parse", "--verify", "origin/main"]:
            return SimpleNamespace(returncode=0, stdout="deadbeef\n", stderr="")
        if arguments == ["rev-list", "--left-right", "--count", "HEAD...origin/main"]:
            return SimpleNamespace(returncode=0, stdout="{} {}\n".format(self.ahead, self.behind), stderr="")
        if arguments == ["fetch", "origin"]:
            if self.fetch_error:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.fetch_error)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if arguments == ["pull", "--ff-only", "origin", "main"]:
            if self.pull_error:
                return SimpleNamespace(returncode=1, stdout="", stderr=self.pull_error)
            self.behind = 0
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="unexpected git command")


def entry(title="Ada"):
    return make_empty_database_entry(title)


def write_database(directory, entries):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path


def sync(private_repo, local_folder, git, **kwargs):
    backup_folder = Path(local_folder).parent / "backups"
    backup_folder.mkdir(exist_ok=True)
    return local_data_sync.synchronize_local_database(
        private_repo,
        local_folder,
        threading.Lock(),
        backup_folder=str(backup_folder),
        git_runner=git,
        **kwargs,
    )


def test_dry_run_validates_and_would_sync_without_backup_or_write(tmp_path, monkeypatch):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private = entry()
    private["posting"].update({"has_been_posted": True, "posted_at": [1]})
    write_database(private_repo, [private])
    local_path = write_database(local, [entry()])
    before = local_path.read_bytes()
    monkeypatch.setattr(local_data_sync, "create_database_backup", lambda *args, **kwargs: pytest.fail("backup"))

    result = sync(private_repo, local, FakeGit(), dry_run=True)

    assert result.ok is True
    assert result.action == "would_sync"
    assert local_path.read_bytes() == before
    assert list((tmp_path / "backups").iterdir()) == []


def test_successful_sync_creates_one_backup_and_exact_private_byte_copy(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private = entry()
    private["posting"].update({"has_been_posted": True, "posted_at": [1]})
    private_path = write_database(private_repo, [private])
    local_path = write_database(local, [entry()])

    result = sync(private_repo, local, FakeGit())

    assert result.ok is True
    assert result.action == "synced"
    assert result.backup["created"] is True
    assert len(list((tmp_path / "backups").glob("database-before-local-sync-*.jsonl"))) == 1
    assert local_path.read_bytes() == private_path.read_bytes()
    assert result.local_database_sha256_after == result.private_database_sha256
    assert load_database("database.jsonl", str(local))["Ada"]["posting"]["has_been_posted"] is True


def test_first_time_local_database_creation_is_deliberate_and_needs_no_backup(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private_path = write_database(private_repo, [entry()])
    local.mkdir()

    result = sync(private_repo, local, FakeGit())

    assert result.ok is True
    assert result.action == "synced"
    assert result.local_database_exists is False
    assert result.backup["attempted"] is False
    assert (local / "database.jsonl").read_bytes() == private_path.read_bytes()


def test_malformed_private_or_local_database_aborts_without_backup(tmp_path, monkeypatch):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private_repo.mkdir()
    (private_repo / "database.jsonl").write_text("not-json\n", encoding="utf-8")
    write_database(local, [entry()])
    monkeypatch.setattr(local_data_sync, "create_database_backup", lambda *args, **kwargs: pytest.fail("backup"))
    result = sync(private_repo, local, FakeGit())
    assert result.ok is False
    assert "Private canonical database is invalid" in result.error

    write_database(private_repo, [entry()])
    (local / "database.jsonl").write_text("not-json\n", encoding="utf-8")
    result = sync(private_repo, local, FakeGit())
    assert result.ok is False
    assert "Local canonical database is invalid" in result.error


@pytest.mark.parametrize(
    ("git", "issue"),
    [
        (FakeGit(dirty=True), "private_repository_dirty"),
        (FakeGit(behind=1), "private_repository_behind"),
        (FakeGit(ahead=1), "private_repository_ahead"),
        (FakeGit(ahead=1, behind=1), "private_repository_diverged"),
    ],
)
def test_default_sync_refuses_unsafe_private_git_state(tmp_path, git, issue):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    write_database(private_repo, [entry()])
    write_database(local, [entry()])

    result = sync(private_repo, local, git)

    assert result.ok is False
    assert result.private_git["issue"] == issue
    assert not any(command[3:4] == ["push"] for command in git.commands)


def test_explicit_update_fetches_and_fast_forwards_only_then_syncs(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private = entry()
    private["posting"].update({"has_been_posted": True, "posted_at": [1]})
    write_database(private_repo, [private])
    write_database(local, [entry()])
    git = FakeGit(behind=1)

    result = sync(private_repo, local, git, update_private_repository=True)

    assert result.ok is True
    assert any(command[3:] == ["fetch", "origin"] for command in git.commands)
    assert any(command[3:] == ["pull", "--ff-only", "origin", "main"] for command in git.commands)
    assert not any(command[3:4] == ["push"] for command in git.commands)


def test_dry_run_with_update_flag_never_fetches_or_pulls(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    write_database(private_repo, [entry()])
    write_database(local, [entry()])
    git = FakeGit(behind=1)

    result = sync(private_repo, local, git, dry_run=True, update_private_repository=True)

    assert result.ok is False
    assert result.private_git["issue"] == "private_repository_behind"
    assert not any(command[3:4] in (["fetch"], ["pull"]) for command in git.commands)


def test_local_only_attempt_sent_state_and_external_link_abort_by_default(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    authoritative = entry()
    write_database(private_repo, [authoritative])
    local_entry = entry()
    attempt = make_pending_posting_attempt(
        "Ada", {"text": "Quote", "source": {"work": None, "year": None, "date": None, "details": None, "citation": None, "url": None}},
        "Stored message", attempt_id="local-attempt",
    )
    local_entry["posting"]["attempts"].append(attempt)
    local_entry["posting"].update({"has_been_posted": True, "posted_at": [2]})
    local_entry["external_links"]["project_gutenberg"] = "https://www.gutenberg.org/ebooks/author/44"
    local_path = write_database(local, [local_entry])
    before = local_path.read_bytes()

    result = sync(private_repo, local, FakeGit())

    assert result.ok is False
    assert result.differences["local_only_posting_attempts"]
    assert result.differences["local_newer_posting_state"] == ["Ada"]
    assert result.differences["local_only_external_links"]
    assert local_path.read_bytes() == before


def test_identical_database_is_a_noop_without_backup_or_rewrite(tmp_path, monkeypatch):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private_path = write_database(private_repo, [entry()])
    local_path = write_database(local, [entry()])
    assert private_path.read_bytes() == local_path.read_bytes()
    monkeypatch.setattr(local_data_sync, "create_database_backup", lambda *args, **kwargs: pytest.fail("backup"))
    monkeypatch.setattr(local_data_sync, "replace_database_from_validated_source", lambda *args, **kwargs: pytest.fail("rewrite"))

    result = sync(private_repo, local, FakeGit())

    assert result.ok is True
    assert result.action == "no_op"


def test_force_replace_discards_reported_local_only_state_but_never_changes_private(tmp_path):
    private_repo = tmp_path / "private"
    local = tmp_path / "local"
    private_path = write_database(private_repo, [entry()])
    local_entry = entry()
    local_entry["external_links"]["project_gutenberg"] = "https://www.gutenberg.org/ebooks/author/44"
    local_path = write_database(local, [local_entry])
    private_before = private_path.read_bytes()

    result = sync(private_repo, local, FakeGit(), force_replace_local=True)

    assert result.ok is True
    assert result.action == "synced"
    assert result.differences["local_only_external_links"]
    assert local_path.read_bytes() == private_before
    assert private_path.read_bytes() == private_before


def test_cli_exposes_private_path_and_safety_flags(monkeypatch, capsys):
    captured = {}
    monkeypatch.setattr(
        sync_cli,
        "synchronize_local_database",
        lambda **kwargs: captured.update(kwargs) or local_data_sync.LocalDataSyncResult(
            True, "no_op", kwargs["dry_run"], local_database_exists=True,
        ),
    )

    assert sync_cli.main([
        "--private-data-repo", "/tmp/private", "--data-folder", "/tmp/local",
        "--dry-run", "--force-replace-local",
    ]) == 0

    assert captured["private_repository"] == "/tmp/private"
    assert captured["local_data_folder"] == "/tmp/local"
    assert captured["dry_run"] is True
    assert captured["force_replace_local"] is True
    assert '"action": "no_op"' in capsys.readouterr().out
