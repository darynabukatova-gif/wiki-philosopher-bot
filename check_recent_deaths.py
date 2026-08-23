"""Check accepted living philosophers for newly recorded Wikidata deaths."""

import argparse
import html
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

from cache import DatabaseBackupResult, create_database_backup, load_database, update_database_entry
from config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    RECENT_DEATH_REPORT_FOLDER,
    DATABASE_BACKUP_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
    RATE_LIMIT,
    get_recent_death_telegram_settings,
    load_environment,
)
from refresh_wikidata_dates import detect_recent_death_update
from run_reporting import save_recent_death_report
from runtime import persistence_lock
from telegram_bot import send_message_to_chat
from utils import RateLimiter, chunk_list
from wikipedia_api import (
    get_wikidata_entities_batch,
    get_wikidata_time_claim_value,
    parse_wikidata_time_claim_exact_date,
    parse_wikidata_time_year,
    select_wikidata_time_claim,
)


RECENT_DEATH_REPORTS_DIRECTORY = Path(RECENT_DEATH_REPORT_FOLDER)


def format_recent_death_notification(updates):
    """Format one HTML-safe private notification for recent death updates."""
    lines = ["<b>Recent philosopher death updates</b>", ""]
    for update in updates:
        title = html.escape(update["title"])
        death_date = date.fromisoformat(update["death_date"])
        lines.append("{} — {}".format(title, death_date.strftime("%-d %B %Y")))
    return "\n".join(lines)


def notify_recent_deaths(updates, sender=None):
    """Attempt one private notification, without affecting canonical results."""
    if not updates:
        return {"attempted": False, "sent": False, "error": None}
    if sender is None:
        sender = send_message_to_chat
    telegram_url, chat_id = get_recent_death_telegram_settings()
    if not chat_id:
        return {
            "attempted": False,
            "sent": False,
            "error": "private chat not configured",
        }
    if not telegram_url:
        return {
            "attempted": False,
            "sent": False,
            "error": "telegram not configured",
        }
    result = sender(
        format_recent_death_notification(updates), telegram_url, chat_id,
    )
    return {
        "attempted": True,
        "sent": result.ok,
        "error": result.error_reason,
    }


def _limit_argument(value):
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Check accepted living philosophers for Wikidata deaths."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--limit", type=_limit_argument, default=None)
    parser.add_argument("--title", action="append", default=[])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.title and args.limit is not None:
        parser.error("--title cannot be combined with --limit")
    return args


def recent_death_monitor_needs_processing(entry):
    """Whether an accepted current Wikidata record has no known death year."""
    if not isinstance(entry, dict):
        return False
    evaluation = entry.get("evaluation")
    wikidata = entry.get("wikidata")
    return (
        isinstance(evaluation, dict)
        and evaluation.get("status") == "accepted"
        and isinstance(wikidata, dict)
        and wikidata.get("status") == "available"
        and wikidata.get("death_year") is None
    )


def select_eligible_titles(database):
    return sorted(
        title for title, entry in database.items()
        if recent_death_monitor_needs_processing(entry)
    )


def select_explicit_titles(database, requested_titles):
    seen = set()
    for title in requested_titles:
        if title in seen:
            raise ValueError("duplicate --title: {!r}".format(title))
        seen.add(title)
        if title not in database:
            raise ValueError("requested title does not exist: {!r}".format(title))
        if not recent_death_monitor_needs_processing(database[title]):
            raise ValueError("requested title is not eligible: {!r}".format(title))
    return list(requested_titles)


def _current_rank_claims(property_claims):
    return [
        claim for claim in property_claims
        if not isinstance(claim, dict) or claim.get("rank", "normal") != "deprecated"
    ]


def death_from_entity(entity):
    """Return selected P570 year/date, or a malformed-response error.

    Missing P570 and deprecated-only P570 claims are successful no-death
    results. Current-rank malformed value claims are operational failures so
    they cannot erase or masquerade as a living-person result.
    """
    if not isinstance(entity, dict):
        return None, None, "entity is missing or malformed"
    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return None, None, "entity claims are missing or malformed"
    if "P570" not in claims:
        return None, None, None
    property_claims = claims["P570"]
    if not isinstance(property_claims, list):
        return None, None, "P570 claims are malformed"
    if not property_claims:
        return None, None, None

    selected = select_wikidata_time_claim(property_claims)
    if selected is not None:
        return (
            parse_wikidata_time_year(get_wikidata_time_claim_value(selected)),
            parse_wikidata_time_claim_exact_date(selected),
            None,
        )

    current_claims = _current_rank_claims(property_claims)
    if not current_claims:
        return None, None, None
    for claim in current_claims:
        if not isinstance(claim, dict):
            return None, None, "P570 claim is malformed"
        mainsnak = claim.get("mainsnak")
        if not isinstance(mainsnak, dict):
            return None, None, "P570 mainsnak is malformed"
        if mainsnak.get("snaktype", "value") != "value":
            continue
        time_value = get_wikidata_time_claim_value(claim)
        if time_value is None or parse_wikidata_time_year(time_value) is None:
            return None, None, "P570 time value is malformed"
    return None, None, None


def update_entry_death(database, title, death_year, death_date, data_folder):
    """Persist only a newly established canonical death year/date pair."""
    def update_death(entry):
        entry["wikidata"]["death_year"] = death_year
        entry["wikidata"]["death_date"] = death_date

    return update_database_entry(
        database, title, update_death, DATABASE_FILE, data_folder,
        persistence_lock,
    )


def _title_detail(title, old_year, old_date, new_year, new_date, outcome):
    return {
        "title": title,
        "old_death_year": old_year,
        "new_death_year": new_year,
        "old_death_date": old_date,
        "new_death_date": new_date,
        "outcome": outcome,
    }


def _record_failure(results, title, message, old_year, old_date):
    results["operational_failures"] += 1
    results["errors"].append({
        "title": title,
        "type": "RecentDeathCheckFailure",
        "message": message,
    })
    results["title_details"].append(
        _title_detail(title, old_year, old_date, old_year, old_date, "failure")
    )


def _new_results():
    return {
        "successfully_checked": 0,
        "no_death_found": 0,
        "newly_deceased": 0,
        "recent_deaths": 0,
        "historical_deaths": 0,
        "imprecise_deaths": 0,
        "suspicious_future_deaths": 0,
        "operational_failures": 0,
        "errors": [],
        "recent_death_updates": [],
        "title_details": [],
    }


def run_apply(database, selected_titles, data_folder, limiter=None, today=None):
    """Check selected titles and persist only newly established death facts."""
    if limiter is None:
        limiter = RateLimiter(RATE_LIMIT)
    if today is None:
        today = date.today()
    results = _new_results()
    title_qids = {
        title: database[title]["wikidata"].get("qid")
        for title in selected_titles
    }
    qid_to_titles = {}
    for title in selected_titles:
        qid = title_qids[title]
        wikidata = database[title]["wikidata"]
        if not isinstance(qid, str) or not qid:
            _record_failure(
                results, title, "available Wikidata has no qid",
                wikidata.get("death_year"), wikidata.get("death_date"),
            )
            continue
        qid_to_titles.setdefault(qid, []).append(title)

    entities = {}
    qid_errors = {}
    for qid_batch in chunk_list(list(qid_to_titles), 50):
        batch_result = get_wikidata_entities_batch(qid_batch, limiter=limiter)
        if batch_result.error_reason is not None:
            for qid in qid_batch:
                qid_errors[qid] = batch_result.error_reason
            continue
        entities.update(batch_result.data)

    prior_failures = {detail["title"] for detail in results["title_details"]}
    for title in selected_titles:
        if title in prior_failures:
            continue
        wikidata = database[title]["wikidata"]
        old_year = wikidata.get("death_year")
        old_date = wikidata.get("death_date")
        qid = title_qids[title]
        if qid in qid_errors:
            _record_failure(results, title, qid_errors[qid], old_year, old_date)
            continue
        new_year, new_date, error = death_from_entity(entities.get(qid))
        if error is not None:
            _record_failure(results, title, error, old_year, old_date)
            continue

        results["successfully_checked"] += 1
        if new_year is None:
            results["no_death_found"] += 1
            results["title_details"].append(
                _title_detail(title, old_year, old_date, old_year, old_date, "no_death_found")
            )
            continue

        if new_date is not None:
            try:
                exact_date = date.fromisoformat(new_date)
            except ValueError:
                _record_failure(results, title, "P570 exact date is malformed", old_year, old_date)
                continue
            if exact_date > today:
                results["suspicious_future_deaths"] += 1
                results["title_details"].append(
                    _title_detail(title, old_year, old_date, old_year, old_date, "future_death_suspicious")
                )
                continue

        try:
            update_entry_death(database, title, new_year, new_date, data_folder)
        except ValueError:
            raise
        except OSError as error:
            _record_failure(results, title, str(error), old_year, old_date)
            continue

        results["newly_deceased"] += 1
        if new_date is None:
            outcome = "imprecise_death"
            results["imprecise_deaths"] += 1
        elif detect_recent_death_update(
            old_year, old_date, new_year, new_date, today=today,
        ):
            outcome = "recent_death"
            results["recent_deaths"] += 1
            results["recent_death_updates"].append({
                "title": title,
                "death_date": new_date,
                "old_death_year": old_year,
                "old_death_date": old_date,
                "new_death_year": new_year,
                "new_death_date": new_date,
            })
        else:
            outcome = "historical_death"
            results["historical_deaths"] += 1
        results["title_details"].append(
            _title_detail(title, old_year, old_date, new_year, new_date, outcome)
        )
    return results


def build_dry_run_report(database, selected_titles, limit):
    return {
        "mode": "dry-run",
        "total_canonical_entries": len(database),
        "eligible_before": len(select_eligible_titles(database)),
        "selected": {
            "count": len(selected_titles), "limit": limit,
            "titles": list(selected_titles),
        },
        "notification": {"attempted": False, "sent": False, "error": None},
        "title_details": [],
    }


def build_apply_report(database, eligible_before, selected_titles, limit, results):
    return {
        "mode": "apply",
        "total_canonical_entries": len(database),
        "eligible_before": eligible_before,
        "selected": {"count": len(selected_titles), "limit": limit},
        "results": {
            key: results[key]
            for key in (
                "successfully_checked", "no_death_found", "newly_deceased",
                "recent_deaths", "historical_deaths", "imprecise_deaths",
                "suspicious_future_deaths", "operational_failures", "errors",
            )
        },
        "recent_death_updates": {
            "count": len(results["recent_death_updates"]),
            "titles": results["recent_death_updates"],
        },
        "notification": {"attempted": False, "sent": False, "error": None},
        "title_details": results["title_details"],
    }


def _iso_timestamp(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def add_report_timing(report, started_at, finished_at):
    return {
        "started_at": _iso_timestamp(started_at),
        "finished_at": _iso_timestamp(finished_at),
        "duration_seconds": round(finished_at - started_at, 6),
        **report,
    }


def main(argv=None):
    started_at = time.time()
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    eligible_titles = select_eligible_titles(database)
    try:
        selected_titles = (
            select_explicit_titles(database, args.title)
            if args.title else eligible_titles if args.limit is None
            else eligible_titles[:args.limit]
        )
    except ValueError as error:
        raise SystemExit(str(error))

    backup_result = DatabaseBackupResult().as_report(
        attempted=False, retention_days=OPERATIONAL_BACKUP_RETENTION_DAYS,
    )
    if args.apply:
        backup = create_database_backup(
            args.data_folder, DATABASE_BACKUP_FOLDER,
            "before-recent-death-check", OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False, kind="operational", persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        backup_result = backup.as_report(True, OPERATIONAL_BACKUP_RETENTION_DAYS)
        if not backup.created:
            print(json.dumps({"mode": "apply", "backup": backup_result,
                "error": "Database backup failed; no canonical mutation was attempted."},
                indent=2, ensure_ascii=False, sort_keys=False))
            return 1
        results = run_apply(database, selected_titles, args.data_folder)
        report = build_apply_report(
            database, len(eligible_titles), selected_titles, args.limit, results,
        )
    else:
        report = build_dry_run_report(database, selected_titles, args.limit)
    report["backup"] = backup_result
    if args.apply:
        load_environment()
        notification = notify_recent_deaths(report["recent_death_updates"]["titles"])
        report["notification"] = notification
        if notification["attempted"] and not notification["sent"]:
            print("Warning: private recent-death notification failed: {}".format(
                notification["error"]
            ))
        elif notification["error"] is not None:
            print("Warning: private recent-death notification unavailable: {}".format(
                notification["error"]
            ))
    report = add_report_timing(report, started_at, time.time())
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    try:
        report_path, diagnostics = save_recent_death_report(
            report, RECENT_DEATH_REPORTS_DIRECTORY, started_at,
        )
    except OSError as error:
        print("Warning: recent-death report could not be saved: {}".format(error))
    else:
        print("Saved recent-death report: {}".format(report_path))
        for diagnostic in diagnostics:
            print("Warning: {}".format(diagnostic))
    recent_updates = report.get("recent_death_updates", {})
    print("Recent death updates: {}".format(recent_updates.get("count", 0)))
    for update in recent_updates.get("titles", []):
        print("- {} — {}".format(update["title"], update["death_date"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
