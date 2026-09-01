"""Conservative one-way synchronization of the local canonical database.

This module deliberately knows nothing about posting selection or Telegram.
It copies only a validated authoritative private-repository database into the
source checkout after inspecting Git freshness and local-only state.
"""

from dataclasses import asdict, dataclass, field
from copy import deepcopy
from pathlib import Path
import subprocess

from wiki_philosopher_bot.cache import (
    DatabaseBackupResult,
    create_database_backup,
    database_file_sha256,
    load_database,
    replace_database_from_validated_source,
)
from wiki_philosopher_bot.config import (
    DATABASE_BACKUP_FOLDER,
    DATABASE_FILE,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
)


@dataclass(frozen=True)
class PrivateRepositoryGitState:
    ok: bool
    issue: str = None
    branch: str = None
    ahead: int = None
    behind: int = None
    dirty: bool = False
    commands: tuple = ()
    error: str = None


@dataclass
class LocalDataSyncResult:
    ok: bool
    action: str
    dry_run: bool
    private_database_sha256: str = None
    local_database_sha256_before: str = None
    local_database_sha256_after: str = None
    local_database_exists: bool = False
    private_git: dict = field(default_factory=dict)
    differences: dict = field(default_factory=dict)
    backup: dict = field(default_factory=dict)
    error: str = None

    def as_report(self):
        return asdict(self)


def _default_git_runner(command):
    return subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )


def _run_git(private_repository, arguments, git_runner, commands):
    command = ["git", "-C", str(private_repository)] + list(arguments)
    commands.append(" ".join(command))
    result = git_runner(command)
    if getattr(result, "returncode", 1) != 0:
        error = (getattr(result, "stderr", "") or getattr(result, "stdout", "")).strip()
        raise RuntimeError(error or "git command failed: {}".format(" ".join(arguments)))
    return (getattr(result, "stdout", "") or "").strip()


def inspect_private_repository_git_state(private_repository, git_runner=_default_git_runner):
    """Inspect a private checkout against its locally known ``origin/main``.

    No fetch is performed here. Thus the result is intentionally limited to
    the checkout's current remote-tracking reference; callers that need a
    fresh remote check must explicitly request an update.
    """
    repository = Path(private_repository)
    commands = []
    if not repository.is_dir():
        return PrivateRepositoryGitState(False, "private_repository_missing", commands=tuple(commands))
    try:
        inside = _run_git(repository, ["rev-parse", "--is-inside-work-tree"], git_runner, commands)
        if inside != "true":
            return PrivateRepositoryGitState(False, "private_repository_not_git", commands=tuple(commands))
        dirty = bool(_run_git(repository, ["status", "--porcelain"], git_runner, commands))
        branch = _run_git(repository, ["rev-parse", "--abbrev-ref", "HEAD"], git_runner, commands)
        _run_git(repository, ["rev-parse", "--verify", "origin/main"], git_runner, commands)
        counts = _run_git(repository, ["rev-list", "--left-right", "--count", "HEAD...origin/main"], git_runner, commands)
        fields = counts.split()
        if len(fields) != 2:
            raise RuntimeError("git rev-list returned an invalid ahead/behind count")
        ahead, behind = (int(fields[0]), int(fields[1]))
    except (RuntimeError, ValueError) as error:
        return PrivateRepositoryGitState(False, "git_inspection_failed", commands=tuple(commands), error=str(error))

    if dirty:
        issue = "private_repository_dirty"
    elif branch != "main":
        issue = "private_repository_not_on_main"
    elif ahead and behind:
        issue = "private_repository_diverged"
    elif ahead:
        issue = "private_repository_ahead"
    elif behind:
        issue = "private_repository_behind"
    else:
        issue = None
    return PrivateRepositoryGitState(
        issue is None,
        issue,
        branch=branch,
        ahead=ahead,
        behind=behind,
        dirty=dirty,
        commands=tuple(commands),
    )


def update_private_repository_fast_forward(private_repository, git_runner=_default_git_runner):
    """Explicitly fetch and fast-forward a clean private ``main`` checkout."""
    initial = inspect_private_repository_git_state(private_repository, git_runner)
    if initial.issue not in (None, "private_repository_behind"):
        return initial
    repository = Path(private_repository)
    commands = list(initial.commands)
    try:
        _run_git(repository, ["fetch", "origin"], git_runner, commands)
    except RuntimeError as error:
        return PrivateRepositoryGitState(False, "git_fetch_failed", commands=tuple(commands), error=str(error))

    refreshed = inspect_private_repository_git_state(repository, git_runner)
    commands.extend(command for command in refreshed.commands if command not in commands)
    if refreshed.issue in (
        "private_repository_dirty",
        "private_repository_not_on_main",
        "private_repository_diverged",
        "private_repository_ahead",
        "git_inspection_failed",
    ):
        return PrivateRepositoryGitState(
            False, refreshed.issue, refreshed.branch, refreshed.ahead,
            refreshed.behind, refreshed.dirty, tuple(commands), refreshed.error,
        )
    if refreshed.issue == "private_repository_behind":
        try:
            _run_git(repository, ["pull", "--ff-only", "origin", "main"], git_runner, commands)
        except RuntimeError as error:
            return PrivateRepositoryGitState(False, "git_fast_forward_failed", commands=tuple(commands), error=str(error))
        refreshed = inspect_private_repository_git_state(repository, git_runner)
        commands.extend(command for command in refreshed.commands if command not in commands)
    return PrivateRepositoryGitState(
        refreshed.ok, refreshed.issue, refreshed.branch, refreshed.ahead,
        refreshed.behind, refreshed.dirty, tuple(commands), refreshed.error,
    )


def _attempts_by_id(entry):
    posting = entry.get("posting") if isinstance(entry, dict) else None
    attempts = posting.get("attempts", []) if isinstance(posting, dict) else []
    return {
        attempt.get("attempt_id"): attempt
        for attempt in attempts
        if isinstance(attempt, dict) and isinstance(attempt.get("attempt_id"), str)
    }


def compare_local_and_private_databases(local_database, private_database):
    """Classify all local data that a one-way replacement could discard."""
    differences = {
        "local_only_titles": [],
        "private_only_titles": [],
        "local_only_posting_attempts": [],
        "posting_attempt_conflicts": [],
        "local_newer_posting_state": [],
        "local_only_external_links": [],
        "external_link_conflicts": [],
        "other_field_differences": [],
    }
    local_titles = set(local_database)
    private_titles = set(private_database)
    differences["local_only_titles"] = sorted(local_titles - private_titles)
    differences["private_only_titles"] = sorted(private_titles - local_titles)

    for title in sorted(local_titles & private_titles):
        local_entry = local_database[title]
        private_entry = private_database[title]
        if local_entry == private_entry:
            continue

        local_attempts = _attempts_by_id(local_entry)
        private_attempts = _attempts_by_id(private_entry)
        local_only_attempt_ids = sorted(set(local_attempts) - set(private_attempts))
        if local_only_attempt_ids:
            differences["local_only_posting_attempts"].append({
                "title": title, "attempt_ids": local_only_attempt_ids,
            })
        conflicted_attempt_ids = sorted(
            attempt_id for attempt_id in set(local_attempts) & set(private_attempts)
            if local_attempts[attempt_id] != private_attempts[attempt_id]
        )
        if conflicted_attempt_ids:
            differences["posting_attempt_conflicts"].append({
                "title": title, "attempt_ids": conflicted_attempt_ids,
            })

        local_posting = local_entry.get("posting", {})
        private_posting = private_entry.get("posting", {})
        local_posted_at = set(local_posting.get("posted_at", []))
        private_posted_at = set(private_posting.get("posted_at", []))
        if (
            local_posting.get("has_been_posted") is True
            and private_posting.get("has_been_posted") is not True
        ) or not local_posted_at.issubset(private_posted_at) or (
            local_posting.get("legacy_posted_without_timestamp") is True
            and private_posting.get("legacy_posted_without_timestamp") is not True
        ):
            differences["local_newer_posting_state"].append(title)

        local_links = local_entry.get("external_links", {})
        private_links = private_entry.get("external_links", {})
        if not isinstance(local_links, dict):
            local_links = {}
        if not isinstance(private_links, dict):
            private_links = {}
        local_only_keys = sorted(
            key for key, value in local_links.items()
            if value is not None and private_links.get(key) is None
        )
        conflicting_link_keys = sorted(
            key for key, value in local_links.items()
            if value is not None and private_links.get(key) is not None
            and value != private_links.get(key)
        )
        if local_only_keys:
            differences["local_only_external_links"].append({
                "title": title, "keys": local_only_keys,
            })
        if conflicting_link_keys:
            differences["external_link_conflicts"].append({
                "title": title, "keys": conflicting_link_keys,
            })

        local_other = deepcopy(local_entry)
        private_other = deepcopy(private_entry)
        local_other.pop("posting", None)
        private_other.pop("posting", None)
        local_other.pop("external_links", None)
        private_other.pop("external_links", None)
        if local_other != private_other:
            differences["other_field_differences"].append(title)

    protected_categories = (
        "local_only_titles",
        "local_only_posting_attempts",
        "posting_attempt_conflicts",
        "local_newer_posting_state",
        "local_only_external_links",
        "external_link_conflicts",
        "other_field_differences",
    )
    differences["has_protected_local_differences"] = any(
        differences[key] for key in protected_categories
    )
    differences["identical_logical_content"] = local_database == private_database
    return differences


def _git_report(state):
    return asdict(state)


def synchronize_local_database(
    private_repository,
    local_data_folder,
    persistence_lock,
    *,
    filename=DATABASE_FILE,
    backup_folder=DATABASE_BACKUP_FOLDER,
    retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
    dry_run=False,
    update_private_repository=False,
    force_replace_local=False,
    git_runner=_default_git_runner,
):
    """Safely copy the private authoritative database into the local checkout."""
    private_repository = Path(private_repository)
    local_data_folder = Path(local_data_folder)
    private_database_path = private_repository / filename
    local_database_path = local_data_folder / filename

    if update_private_repository and not dry_run:
        git_state = update_private_repository_fast_forward(
            private_repository, git_runner
        )
    else:
        git_state = inspect_private_repository_git_state(
            private_repository, git_runner
        )
    if not git_state.ok:
        return LocalDataSyncResult(
            False, "refused", dry_run, private_git=_git_report(git_state),
            error="Private repository is not safe to synchronize: {}".format(git_state.issue),
        )
    if not private_database_path.is_file():
        return LocalDataSyncResult(
            False, "refused", dry_run, private_git=_git_report(git_state),
            error="Private repository database is missing or is not a regular file.",
        )
    if not local_data_folder.is_dir():
        return LocalDataSyncResult(
            False, "refused", dry_run, private_git=_git_report(git_state),
            error="Local canonical data directory does not exist.",
        )

    try:
        private_database = load_database(filename, str(private_repository))
        private_sha256 = database_file_sha256(filename, str(private_repository))
    except (OSError, ValueError) as error:
        return LocalDataSyncResult(
            False, "refused", dry_run, private_git=_git_report(git_state),
            error="Private canonical database is invalid: {}".format(error),
        )

    local_exists = local_database_path.exists()
    if local_exists and not local_database_path.is_file():
        return LocalDataSyncResult(
            False, "refused", dry_run, private_database_sha256=private_sha256,
            private_git=_git_report(git_state),
            error="Local canonical database is not a regular file.",
        )
    local_database = {}
    local_sha256 = None
    if local_exists:
        try:
            local_database = load_database(filename, str(local_data_folder))
            local_sha256 = database_file_sha256(filename, str(local_data_folder))
        except (OSError, ValueError) as error:
            return LocalDataSyncResult(
                False, "refused", dry_run, private_database_sha256=private_sha256,
                local_database_exists=True, private_git=_git_report(git_state),
                error="Local canonical database is invalid: {}".format(error),
            )

    differences = compare_local_and_private_databases(local_database, private_database)
    base_result = {
        "private_database_sha256": private_sha256,
        "local_database_sha256_before": local_sha256,
        "local_database_exists": local_exists,
        "private_git": _git_report(git_state),
        "differences": differences,
    }
    if local_exists and local_sha256 == private_sha256:
        return LocalDataSyncResult(True, "no_op", dry_run, local_database_sha256_after=local_sha256, **base_result)
    if differences["has_protected_local_differences"] and not force_replace_local:
        return LocalDataSyncResult(
            False, "refused", dry_run, **base_result,
            error="Local-only canonical differences would be discarded; inspect and reconcile or use --force-replace-local.",
        )
    if dry_run:
        return LocalDataSyncResult(True, "would_sync", True, **base_result)

    backup_result = DatabaseBackupResult()
    if local_exists:
        backup_result = create_database_backup(
            data_folder=str(local_data_folder),
            backup_folder=backup_folder,
            label="before-local-sync",
            retention_days=retention_days,
            preserve=False,
            kind="operational",
            persistence_lock=persistence_lock,
            filename=filename,
        )
        if not backup_result.created:
            return LocalDataSyncResult(
                False, "refused", False, **base_result,
                backup=backup_result.as_report(
                    attempted=True, retention_days=retention_days,
                ),
                error="Local safety backup failed; no local database replacement was attempted.",
            )

    try:
        local_after_sha256 = replace_database_from_validated_source(
            private_database_path,
            filename,
            str(local_data_folder),
            persistence_lock,
        )
    except (OSError, ValueError, RuntimeError) as error:
        return LocalDataSyncResult(
            False, "failed", False, **base_result,
            backup=backup_result.as_report(
                attempted=local_exists, retention_days=retention_days,
            ),
            error="Local database replacement failed: {}".format(error),
        )
    return LocalDataSyncResult(
        True, "synced", False, **base_result,
        local_database_sha256_after=local_after_sha256,
        backup=backup_result.as_report(
            attempted=local_exists, retention_days=retention_days,
        ),
    )
