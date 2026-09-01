"""Read-only audit of positively evidenced external reading links."""

import argparse
import hashlib
import json
import time
from pathlib import Path

from wiki_philosopher_bot.cache import (
    DatabaseBackupResult,
    create_database_backup,
    load_database,
)
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    DATABASE_BACKUP_FOLDER,
    EXTERNAL_LINK_REPORT_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
    RATE_LIMIT,
)
from wiki_philosopher_bot.external_links import (
    ExternalLinksApplyValidationError,
    apply_reviewed_external_links,
    apply_reviewed_project_gutenberg_links,
    audit_external_links,
    audit_project_gutenberg_links,
    format_external_links_audit_summary,
    format_project_gutenberg_audit_summary,
    validate_reviewed_external_links_apply,
    validate_reviewed_project_gutenberg_apply,
)
from wiki_philosopher_bot.run_reporting import save_json_report
from wiki_philosopher_bot.runtime import persistence_lock
from wiki_philosopher_bot.utils import RateLimiter


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Audit positively evidenced Wikiquote/Wikisource enrichment "
            "candidates, or atomically apply one explicit reviewed audit report."
        )
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-folder", default=EXTERNAL_LINK_REPORT_FOLDER)
    parser.add_argument(
        "--apply-report", metavar="PATH",
        help="Atomically apply proposals from this explicit reviewed audit report.",
    )
    parser.add_argument(
        "--apply-project-gutenberg-report", metavar="PATH",
        help=(
            "Atomically apply Project Gutenberg proposals from this explicit "
            "reviewed P1938 audit report; performs no network lookup."
        ),
    )
    parser.add_argument(
        "--project-gutenberg",
        action="store_true",
        help=(
            "Run the separate, read-only Wikidata P1938 Project Gutenberg "
            "author-link audit."
        ),
    )
    parser.add_argument(
        "--batch-size", type=int, default=50,
        help="Wikidata QIDs per bounded sitelink request (default: 50).",
    )
    args = parser.parse_args(argv)
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.apply_report and args.apply_project_gutenberg_report:
        parser.error("Use only one explicit apply-report option")
    if args.project_gutenberg and (
        args.apply_report or args.apply_project_gutenberg_report
    ):
        parser.error("--project-gutenberg cannot be combined with an apply-report option")
    return args


def _load_report(path):
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            report = json.load(handle)
    except (OSError, ValueError) as error:
        raise ExternalLinksApplyValidationError(
            "Could not load audit report: {}".format(error)
        )
    if not isinstance(report, dict):
        raise ExternalLinksApplyValidationError("Audit report must be a JSON object")
    return report


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _save_report(report, report_folder, started_at):
    try:
        report_path, diagnostics = save_json_report(
            report,
            Path(report_folder),
            started_at,
            report_kind="external-links",
            temporary_prefix=".external-links-report-",
        )
    except OSError as error:
        print("Warning: external-links report could not be saved: {}".format(error))
        return None
    for diagnostic in diagnostics:
        print("Warning: {}".format(diagnostic))
    return report_path


def _apply_report(source_path, database, results=None, backup=None, error=None):
    report = {
        "mode": "apply",
        "operation": "external-links-enrichment-apply",
        "source_audit_report": str(source_path),
        "total_canonical_records": len(database),
        "backup": (
            backup.as_report(
                attempted=backup is not None,
                retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            ) if backup is not None else DatabaseBackupResult().as_report(
                attempted=False,
                retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            )
        ),
        "validation_failure": error,
    }
    if results is not None:
        report.update(results)
        report["unchanged_records"] = len(database) - results["records_updated"]
        report["skipped_records"] = 0
    else:
        report.update({
            "reviewed_proposal_count": 0,
            "records_updated": 0,
            "wikiquote_links_written": 0,
            "wikisource_links_written": 0,
            "records_receiving_both": 0,
            "unchanged_records": len(database),
            "skipped_records": 0,
            "database_sha256": None,
        })
    return report


def _project_gutenberg_apply_report(
    source_path,
    source_sha256,
    database,
    pre_write_database_sha256,
    results=None,
    backup=None,
    error=None,
):
    """Build a credential-free, report-driven Project Gutenberg apply record."""
    report = {
        "mode": "apply",
        "operation": "project-gutenberg-external-links-apply",
        "source_reviewed_report": str(source_path),
        "source_reviewed_report_sha256": source_sha256,
        "canonical_records_before": len(database),
        "canonical_records_after": len(database),
        "pre_write_database_sha256": pre_write_database_sha256,
        "backup": (
            backup.as_report(
                attempted=backup is not None,
                retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            ) if backup is not None else DatabaseBackupResult().as_report(
                attempted=False,
                retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            )
        ),
        "backup_path": backup.path if backup is not None else None,
        "backup_sha256": backup.sha256 if backup is not None else None,
        "validation": "passed" if error is None else "failed",
        "validation_failure": error,
    }
    if results is None:
        report.update({
            "reviewed_proposal_count": 0,
            "records_updated": 0,
            "project_gutenberg_links_written": 0,
            "applied_changes": [],
            "post_write_database_sha256": None,
        })
    else:
        report.update(results)
        report["post_write_database_sha256"] = results["database_sha256"]
    return report


def _run_project_gutenberg_apply(args, database, started_at):
    """Apply one fully validated P1938 report without any network operation."""
    source_sha256 = None
    pre_write_database_sha256 = None
    backup = None
    try:
        reviewed_report = _load_report(args.apply_project_gutenberg_report)
        source_sha256 = _sha256_file(args.apply_project_gutenberg_report)
        # The complete report/current-state validation happens before a backup
        # is made, so a stale report has no operational side effects.
        validate_reviewed_project_gutenberg_apply(database, reviewed_report)
        pre_write_database_sha256 = _sha256_file(
            Path(args.data_folder) / DATABASE_FILE
        )
    except (ExternalLinksApplyValidationError, OSError) as error:
        report = _project_gutenberg_apply_report(
            args.apply_project_gutenberg_report,
            source_sha256,
            database,
            pre_write_database_sha256,
            error=str(error),
        )
        report["started_at"] = started_at
        report["finished_at"] = time.time()
        report_path = _save_report(report, args.report_folder, started_at)
        print("Project Gutenberg apply refused: {}".format(error))
        if report_path is not None:
            print("Saved external-links report: {}".format(report_path))
        return 1

    backup = create_database_backup(
        data_folder=args.data_folder,
        backup_folder=DATABASE_BACKUP_FOLDER,
        label="before-project-gutenberg-external-links-enrichment",
        retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
        preserve=False,
        kind="operational",
        persistence_lock=persistence_lock,
        filename=DATABASE_FILE,
    )
    if not backup.created:
        report = _project_gutenberg_apply_report(
            args.apply_project_gutenberg_report,
            source_sha256,
            database,
            pre_write_database_sha256,
            backup=backup,
            error="Database backup failed; no canonical mutation was attempted.",
        )
        report["started_at"] = started_at
        report["finished_at"] = time.time()
        report_path = _save_report(report, args.report_folder, started_at)
        print("Project Gutenberg apply refused: database backup failed.")
        if report_path is not None:
            print("Saved external-links report: {}".format(report_path))
        return 1

    try:
        results = apply_reviewed_project_gutenberg_links(
            database,
            reviewed_report,
            DATABASE_FILE,
            args.data_folder,
            persistence_lock,
        )
    except (ExternalLinksApplyValidationError, OSError, RuntimeError, ValueError) as error:
        report = _project_gutenberg_apply_report(
            args.apply_project_gutenberg_report,
            source_sha256,
            database,
            pre_write_database_sha256,
            backup=backup,
            error=str(error),
        )
        report["started_at"] = started_at
        report["finished_at"] = time.time()
        report_path = _save_report(report, args.report_folder, started_at)
        print("Project Gutenberg apply failed: {}".format(error))
        if report_path is not None:
            print("Saved external-links report: {}".format(report_path))
        return 1

    report = _project_gutenberg_apply_report(
        args.apply_project_gutenberg_report,
        source_sha256,
        database,
        pre_write_database_sha256,
        results=results,
        backup=backup,
    )
    report["started_at"] = started_at
    report["finished_at"] = time.time()
    report_path = _save_report(report, args.report_folder, started_at)
    print(
        "Project Gutenberg links applied: {} records".format(
            results["records_updated"]
        )
    )
    if report_path is not None:
        print("Saved external-links report: {}".format(report_path))
    return 0


def main(argv=None):
    """Run a read-only audit, or apply exactly one explicit reviewed report."""
    args = parse_args(argv)
    started_at = time.time()
    database = load_database(DATABASE_FILE, args.data_folder)
    if args.apply_project_gutenberg_report:
        return _run_project_gutenberg_apply(args, database, started_at)
    if args.apply_report:
        backup = None
        try:
            reviewed_report = _load_report(args.apply_report)
            # Validate the *entire* report and current state before a backup or
            # any canonical mutation. This is deliberately all-or-nothing.
            validate_reviewed_external_links_apply(database, reviewed_report)
        except ExternalLinksApplyValidationError as error:
            report = _apply_report(args.apply_report, database, error=str(error))
            report["started_at"] = started_at
            report["finished_at"] = time.time()
            report_path = _save_report(report, args.report_folder, started_at)
            print("External-links apply refused: {}".format(error))
            if report_path is not None:
                print("Saved external-links report: {}".format(report_path))
            return 1

        backup = create_database_backup(
            data_folder=args.data_folder,
            backup_folder=DATABASE_BACKUP_FOLDER,
            label="before-external-links-enrichment",
            retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False,
            kind="operational",
            persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        if not backup.created:
            report = _apply_report(
                args.apply_report,
                database,
                backup=backup,
                error="Database backup failed; no canonical mutation was attempted.",
            )
            report["started_at"] = started_at
            report["finished_at"] = time.time()
            report_path = _save_report(report, args.report_folder, started_at)
            print("External-links apply refused: database backup failed.")
            if report_path is not None:
                print("Saved external-links report: {}".format(report_path))
            return 1

        try:
            results = apply_reviewed_external_links(
                database,
                reviewed_report,
                DATABASE_FILE,
                args.data_folder,
                persistence_lock,
            )
        except (ExternalLinksApplyValidationError, OSError, RuntimeError, ValueError) as error:
            report = _apply_report(args.apply_report, database, backup=backup, error=str(error))
            report["started_at"] = started_at
            report["finished_at"] = time.time()
            report_path = _save_report(report, args.report_folder, started_at)
            print("External-links apply failed: {}".format(error))
            if report_path is not None:
                print("Saved external-links report: {}".format(report_path))
            return 1

        report = _apply_report(args.apply_report, database, results=results, backup=backup)
        report["started_at"] = started_at
        report["finished_at"] = time.time()
        report_path = _save_report(report, args.report_folder, started_at)
        print(
            "External-links applied: {} records; Wikiquote {}; Wikisource {}; both {}".format(
                results["records_updated"], results["wikiquote_links_written"],
                results["wikisource_links_written"], results["records_receiving_both"],
            )
        )
        if report_path is not None:
            print("Saved external-links report: {}".format(report_path))
        return 0

    if args.project_gutenberg:
        report = audit_project_gutenberg_links(
            database,
            limiter=RateLimiter(RATE_LIMIT),
            batch_size=args.batch_size,
        )
        summary = format_project_gutenberg_audit_summary
    else:
        report = audit_external_links(
            database,
            limiter=RateLimiter(RATE_LIMIT),
            batch_size=args.batch_size,
        )
        summary = format_external_links_audit_summary
    report["started_at"] = started_at
    report["finished_at"] = time.time()
    report_path = _save_report(report, args.report_folder, started_at)
    print(summary(report, report_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
