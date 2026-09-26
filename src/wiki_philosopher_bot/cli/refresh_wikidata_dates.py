"""Refresh only cached canonical Wikidata life-date fields."""

import argparse
import json
import time
from datetime import date, datetime, timezone
from pathlib import Path

from wiki_philosopher_bot.cache import DatabaseBackupResult, create_database_backup, load_database, update_database_entry
from wiki_philosopher_bot.config import (
    RECENT_DEATH_WINDOW_YEARS,
    CANONICAL_DATA_FOLDER,
    DATABASE_FILE,
    RATE_LIMIT,
    WIKIDATA_DATE_REFRESH_REPORT_FOLDER,
    DATABASE_BACKUP_FOLDER,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
)
from wiki_philosopher_bot.run_reporting import save_wikidata_date_refresh_report
from wiki_philosopher_bot.date_policy import (
    date_is_within_recent_policy, recent_death_policy,
)
from wiki_philosopher_bot.runtime import persistence_lock
from wiki_philosopher_bot.utils import (
    RateLimiter, chunk_list, is_semantically_postable_philosopher,
)
from wiki_philosopher_bot.wikipedia_api import (
    get_wikidata_entities_batch,
    get_wikidata_time_claim_value,
    parse_wikidata_time_claim_exact_date,
    parse_wikidata_time_year,
    select_wikidata_time_claim,
)


WIKIDATA_DATE_REFRESH_REPORTS_DIRECTORY = Path(
    WIKIDATA_DATE_REFRESH_REPORT_FOLDER
)


def _limit_argument(value):
    limit = int(value)
    if limit < 0:
        raise argparse.ArgumentTypeError("limit must be non-negative")
    return limit


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit or refresh canonical Wikidata life-date fields."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--limit", type=_limit_argument, default=None)
    parser.add_argument("--title", action="append", default=[])
    parser.add_argument(
        "--missing-exact-dates",
        action="store_true",
        help=(
            "audit/enrich accepted Wikidata-backed philosophers that lack "
            "an exact birth or death date"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.title and args.limit is not None:
        parser.error("--title cannot be combined with --limit")
    return args


def wikidata_date_refresh_needs_processing(entry):
    """Whether an available canonical Wikidata section has stored life dates."""
    if not isinstance(entry, dict):
        return False
    wikidata = entry.get("wikidata")
    return (
        isinstance(wikidata, dict)
        and wikidata.get("status") == "available"
        and (
            wikidata.get("birth_year") is not None
            or wikidata.get("death_year") is not None
        )
    )


def select_eligible_titles(database):
    return sorted(
        title
        for title, entry in database.items()
        if wikidata_date_refresh_needs_processing(entry)
    )


def missing_exact_dates_needs_processing(entry):
    """Whether an accepted Wikidata record lacks an exact known life date."""
    if not is_semantically_postable_philosopher(entry):
        return False
    wikidata = entry.get("wikidata")
    if not isinstance(wikidata, dict) or wikidata.get("status") != "available":
        return False
    qid = wikidata.get("qid")
    if not isinstance(qid, str) or not qid:
        return False
    return (
        wikidata.get("birth_date") is None
        or wikidata.get("death_date") is None
    )


def select_missing_exact_date_titles(database):
    return sorted(
        title for title, entry in database.items()
        if missing_exact_dates_needs_processing(entry)
    )


def select_explicit_titles(
    database, requested_titles, predicate=wikidata_date_refresh_needs_processing,
):
    seen = set()
    for title in requested_titles:
        if title in seen:
            raise ValueError("duplicate --title: {!r}".format(title))
        seen.add(title)
        if title not in database:
            raise ValueError("requested title does not exist: {!r}".format(title))
        if not predicate(database[title]):
            raise ValueError("requested title is not eligible: {!r}".format(title))
    return list(requested_titles)


def _summary(values):
    numeric = [
        value for value in values
        if isinstance(value, int) and not isinstance(value, bool)
    ]
    return {
        "count": len(numeric),
        "minimum": min(numeric) if numeric else None,
        "maximum": max(numeric) if numeric else None,
        "negative_count": sum(value < 0 for value in numeric),
    }


def current_date_statistics(database):
    """Read-only stored-year summary; it deliberately makes no era claims."""
    entries = [
        entry for entry in database.values()
        if isinstance(entry, dict) and isinstance(entry.get("wikidata"), dict)
    ]
    return {
        "birth_year": _summary([
            entry["wikidata"].get("birth_year") for entry in entries
        ]),
        "death_year": _summary([
            entry["wikidata"].get("death_year") for entry in entries
        ]),
    }


def build_dry_run_report(database, selected_titles, limit):
    eligible = select_eligible_titles(database)
    return {
        "mode": "dry-run",
        "total_canonical_entries": len(database),
        "eligible": {"count": len(eligible)},
        "current_date_statistics": current_date_statistics(database),
        "selected": {
            "count": len(selected_titles),
            "limit": limit,
            "titles": list(selected_titles),
        },
    }


def refresh_entry_dates(
    database, title, birth_year, death_year, death_date, data_folder,
):
    """Persist only refreshed canonical Wikidata life-date fields."""
    def update_dates(entry):
        entry["wikidata"]["birth_year"] = birth_year
        entry["wikidata"]["death_year"] = death_year
        entry["wikidata"]["death_date"] = death_date

    return update_database_entry(
        database,
        title,
        update_dates,
        DATABASE_FILE,
        data_folder,
        persistence_lock,
    )


def refreshed_life_date_evidence_from_entity(entity):
    """Return selected life-year and conservative exact-date evidence."""
    if not isinstance(entity, dict):
        return None, "entity is missing or malformed"
    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return None, "entity claims are missing or malformed"

    def claim_year(property_id):
        if property_id not in claims:
            return None, None, None
        property_claims = claims[property_id]
        if not isinstance(property_claims, list):
            return None, None, "{} claims are malformed".format(property_id)
        if not property_claims:
            return None, None, None
        selected_claim = select_wikidata_time_claim(property_claims)
        if selected_claim is not None:
            return parse_wikidata_time_year(
                get_wikidata_time_claim_value(selected_claim)
            ), selected_claim, None

        # A fully deprecated claim set carries no current date evidence. For
        # malformed current-rank value claims, preserve cached canonical data.
        current_rank_claims = [
            claim for claim in property_claims
            if not isinstance(claim, dict)
            or claim.get("rank", "normal") != "deprecated"
        ]
        if not current_rank_claims:
            return None, None, None
        for claim in current_rank_claims:
            if not isinstance(claim, dict):
                return None, None, "{} claim is malformed".format(property_id)
            mainsnak = claim.get("mainsnak")
            if not isinstance(mainsnak, dict):
                return None, None, "{} mainsnak is malformed".format(property_id)
            if mainsnak.get("snaktype", "value") != "value":
                continue
            time_value = get_wikidata_time_claim_value(claim)
            if time_value is None or parse_wikidata_time_year(time_value) is None:
                return None, None, "{} time value is malformed".format(property_id)
        return None, None, None

    def exact_date_evidence(claim):
        if claim is None:
            return None, "absent"
        mainsnak = claim.get("mainsnak") if isinstance(claim, dict) else None
        datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if not isinstance(value, dict):
            return None, "malformed"
        if value.get("precision") != 11:
            return None, "imprecise"
        if value.get("calendarmodel") != (
            "http://www.wikidata.org/entity/Q1985727"
        ):
            return None, "unsupported-calendar"
        exact_date = parse_wikidata_time_claim_exact_date(claim)
        if exact_date is None:
            return None, "malformed"
        return exact_date, "exact"

    birth_year, birth_claim, error = claim_year("P569")
    if error is not None:
        return None, error
    death_year, death_claim, error = claim_year("P570")
    if error is not None:
        return None, error
    birth_date, birth_status = exact_date_evidence(birth_claim)
    death_date, death_status = exact_date_evidence(death_claim)
    return {
        "birth": {
            "year": birth_year,
            "date": birth_date,
            "exact_date_status": birth_status,
        },
        "death": {
            "year": death_year,
            "date": death_date,
            "exact_date_status": death_status,
        },
    }, None


def refreshed_life_dates_from_entity(entity):
    """Compatibility view of selected P569/P570 evidence."""
    evidence, error = refreshed_life_date_evidence_from_entity(entity)
    if error is not None:
        return None, None, None, error
    return (
        evidence["birth"]["year"],
        evidence["death"]["year"],
        evidence["death"]["date"],
        None,
    )


def _exact_date_enrichment_decision(title, entry, evidence):
    """Classify additive exact-date evidence without mutating *entry*."""
    wikidata = entry["wikidata"]
    detail = {
        "title": title,
        "qid": wikidata.get("qid"),
        "old_birth_year": wikidata.get("birth_year"),
        "old_birth_date": wikidata.get("birth_date"),
        "new_birth_year": evidence["birth"]["year"],
        "new_birth_date": evidence["birth"]["date"],
        "birth_exact_date_status": evidence["birth"]["exact_date_status"],
        "old_death_year": wikidata.get("death_year"),
        "old_death_date": wikidata.get("death_date"),
        "new_death_year": evidence["death"]["year"],
        "new_death_date": evidence["death"]["date"],
        "death_exact_date_status": evidence["death"]["exact_date_status"],
        "fields_to_write": [],
        "conflicts": [],
    }
    for kind in ("birth", "death"):
        old_year = detail["old_{}_year".format(kind)]
        old_date = detail["old_{}_date".format(kind)]
        new_year = detail["new_{}_year".format(kind)]
        new_date = detail["new_{}_date".format(kind)]
        if new_date is None:
            continue
        parsed_year = date.fromisoformat(new_date).year
        if new_year != parsed_year:
            detail["conflicts"].append(
                "selected {} year/date evidence is inconsistent".format(kind)
            )
            continue
        if old_year is not None and old_year != new_year:
            detail["conflicts"].append(
                "stored {} year differs from current selected claim".format(kind)
            )
            continue
        if old_date is not None and old_date != new_date:
            detail["conflicts"].append(
                "stored {} date differs from current selected claim".format(kind)
            )
            continue
        if old_year is None:
            detail["fields_to_write"].append("{}_year".format(kind))
        if old_date is None:
            detail["fields_to_write"].append("{}_date".format(kind))

    if detail["conflicts"]:
        detail["classification"] = "conflict-review-required"
        detail["fields_to_write"] = []
    elif detail["fields_to_write"]:
        detail["classification"] = "proposal"
    else:
        missing_statuses = [
            detail["{}_exact_date_status".format(kind)]
            for kind in ("birth", "death")
            if detail["old_{}_date".format(kind)] is None
        ]
        detail["classification"] = (
            "unsupported-or-imprecise"
            if any(status in {"imprecise", "unsupported-calendar", "malformed"}
                   for status in missing_statuses)
            else "no-exact-date-available"
        )
    return detail


def _persist_exact_date_proposal(database, detail, data_folder):
    title = detail["title"]
    expected = {
        "birth_year": detail["old_birth_year"],
        "birth_date": detail["old_birth_date"],
        "death_year": detail["old_death_year"],
        "death_date": detail["old_death_date"],
    }

    def update_exact_dates(entry):
        wikidata = entry["wikidata"]
        current = {field: wikidata.get(field) for field in expected}
        if current != expected:
            raise ValueError(
                "canonical life-date fields changed before persistence for {!r}".format(
                    title
                )
            )
        for field in detail["fields_to_write"]:
            wikidata[field] = detail["new_{}".format(field)]

    return update_database_entry(
        database, title, update_exact_dates, DATABASE_FILE, data_folder,
        persistence_lock,
    )


def run_exact_date_enrichment(
    database, selected_titles, data_folder, *, apply=False, limiter=None,
):
    """Audit or atomically persist additive exact-date proposals."""
    if limiter is None:
        limiter = RateLimiter(RATE_LIMIT)
    results = {
        "records_selected": len(selected_titles),
        "successfully_checked": 0,
        "birth_date_proposals": 0,
        "death_date_proposals": 0,
        "both_date_proposals": 0,
        "records_updated": 0,
        "unchanged": 0,
        "no_exact_date_available": 0,
        "unsupported_or_imprecise": 0,
        "operational_failures": 0,
        "conflicts_review_required": 0,
        "errors": [],
        "proposals": [],
        "titles": [],
    }
    title_qids = {
        title: database[title]["wikidata"].get("qid")
        for title in selected_titles
    }
    qid_to_titles = {}
    for title, qid in title_qids.items():
        if not isinstance(qid, str) or not qid:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title, "type": "MissingQid",
                "message": "available Wikidata has no qid",
            })
        else:
            qid_to_titles.setdefault(qid, []).append(title)

    entities = {}
    qid_errors = {}
    for qid_batch in chunk_list(list(qid_to_titles), 50):
        batch_result = get_wikidata_entities_batch(qid_batch, limiter=limiter)
        if batch_result.error_reason is not None:
            for qid in qid_batch:
                qid_errors[qid] = batch_result.error_reason
        else:
            entities.update(batch_result.data)

    for title in selected_titles:
        qid = title_qids[title]
        if not isinstance(qid, str) or not qid:
            continue
        if qid in qid_errors:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title, "type": "WikidataRequestFailure",
                "message": qid_errors[qid],
            })
            continue
        evidence, error = refreshed_life_date_evidence_from_entity(
            entities.get(qid)
        )
        if error is not None:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title, "type": "MalformedEntity", "message": error,
            })
            continue
        results["successfully_checked"] += 1
        detail = _exact_date_enrichment_decision(
            title, database[title], evidence
        )
        results["titles"].append(detail)
        classification = detail["classification"]
        if classification == "conflict-review-required":
            results["conflicts_review_required"] += 1
            continue
        if classification == "unsupported-or-imprecise":
            results["unsupported_or_imprecise"] += 1
            continue
        if classification == "no-exact-date-available":
            results["no_exact_date_available"] += 1
            continue

        fields = set(detail["fields_to_write"])
        results["birth_date_proposals"] += "birth_date" in fields
        results["death_date_proposals"] += "death_date" in fields
        results["both_date_proposals"] += {"birth_date", "death_date"} <= fields
        results["proposals"].append(detail)
        if not apply:
            continue
        try:
            _persist_exact_date_proposal(database, detail, data_folder)
        except (OSError, ValueError) as persistence_error:
            results["operational_failures"] += 1
            results["errors"].append({
                "title": title,
                "type": type(persistence_error).__name__,
                "message": str(persistence_error),
            })
        else:
            results["records_updated"] += 1

    results["unchanged"] = (
        results["successfully_checked"]
        - results["conflicts_review_required"]
        - len(results["proposals"])
    )
    return results


def build_exact_date_enrichment_report(
    database, selected_titles, limit, results, *, apply, backup,
):
    return {
        "operation": "wikidata-exact-date-enrichment",
        "mode": "apply" if apply else "dry-run",
        "scope": "accepted-philosophers-missing-exact-dates",
        "total_canonical_entries": len(database),
        "selected": {
            "count": len(selected_titles),
            "limit": limit,
            "titles": list(selected_titles),
        },
        "results": results,
        "backup": backup,
    }

def detect_recent_death_update(
    old_death_year,
    old_death_date,
    new_death_year,
    new_death_date,
    *,
    today,
    recent_policy=None,
    recent_years=None,
    recent_days=None,
):
    """Whether a death fact is newly established, exact, and policy-recent.

    A record with either a previously known death year or exact death date is
    deliberately ineligible. Widening policy therefore cannot manufacture a
    notification backlog, and later precision enrichment is review-only.
    """
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    if old_death_year is not None or old_death_date is not None:
        return False
    if not isinstance(new_death_date, str):
        return False
    if recent_policy is None:
        recent_policy = recent_death_policy(
            today, years=recent_years, days=recent_days,
            default_years=RECENT_DEATH_WINDOW_YEARS,
        )
    return date_is_within_recent_policy(new_death_date, recent_policy)


def _title_result(
    title, old_birth, new_birth, old_death, new_death, old_death_date,
    new_death_date,
):
    return {
        "title": title,
        "old_birth_year": old_birth,
        "new_birth_year": new_birth,
        "old_death_year": old_death,
        "new_death_year": new_death,
        "old_death_date": old_death_date,
        "new_death_date": new_death_date,
        "changed": (old_birth, old_death, old_death_date) != (
            new_birth, new_death, new_death_date,
        ),
    }


def _record_failure(
    results, title, error_type, message, old_birth, old_death, old_death_date,
):
    results["operational_failures"] += 1
    results["errors"].append({
        "title": title,
        "type": error_type,
        "message": message,
    })
    results["titles"].append(
        _title_result(
            title, old_birth, old_birth, old_death, old_death,
            old_death_date, old_death_date,
        )
    )


def _apply_successful_dates(
    results, title, old_birth, old_death, old_death_date, new_birth,
    new_death, new_death_date, today, recent_policy,
):
    results["successfully_refreshed"] += 1
    detail = _title_result(
        title, old_birth, new_birth, old_death, new_death,
        old_death_date, new_death_date,
    )
    results["titles"].append(detail)
    if detail["changed"]:
        results["changed"] += 1
    else:
        results["unchanged"] += 1
    if old_birth is not None and new_birth is None:
        results["fields_changed_to_none"] += 1
    if old_death is not None and new_death is None:
        results["fields_changed_to_none"] += 1
    if old_birth is not None and old_birth > 0 and new_birth is not None and new_birth < 0:
        results["birth_sign_corrections"] += 1
    if old_death is not None and old_death > 0 and new_death is not None and new_death < 0:
        results["death_sign_corrections"] += 1
    if detect_recent_death_update(
        old_death, old_death_date, new_death, new_death_date, today=today,
        recent_policy=recent_policy,
    ):
        results["recent_death_updates"].append({
            "title": title,
            "death_date": new_death_date,
            "old_death_year": old_death,
            "old_death_date": old_death_date,
            "new_death_year": new_death,
            "new_death_date": new_death_date,
        })


def run_apply(
    database, selected_titles, data_folder, limiter=None, today=None,
    recent_policy=None,
):
    """Fetch selected entity claims in batches and persist date-only changes."""
    if limiter is None:
        limiter = RateLimiter(RATE_LIMIT)
    if today is None:
        today = date.today()
    if recent_policy is None:
        recent_policy = recent_death_policy(
            today, default_years=RECENT_DEATH_WINDOW_YEARS,
        )
    results = {
        "successfully_refreshed": 0,
        "changed": 0,
        "unchanged": 0,
        "birth_sign_corrections": 0,
        "death_sign_corrections": 0,
        "fields_changed_to_none": 0,
        "operational_failures": 0,
        "conflicts_review_required": 0,
        "errors": [],
        "titles": [],
        "recent_death_updates": [],
    }

    title_qids = {
        title: database[title]["wikidata"].get("qid")
        for title in selected_titles
    }
    failed_titles = set()
    for title in selected_titles:
        qid = title_qids[title]
        if not isinstance(qid, str) or not qid:
            wikidata = database[title]["wikidata"]
            _record_failure(
                results, title, "MissingQid", "available Wikidata has no qid",
                wikidata.get("birth_year"), wikidata.get("death_year"),
                wikidata.get("death_date"),
            )
            failed_titles.add(title)

    valid_titles = [title for title in selected_titles if title not in failed_titles]
    qid_to_titles = {}
    for title in valid_titles:
        qid_to_titles.setdefault(title_qids[title], []).append(title)

    entities = {}
    qid_errors = {}
    for qid_batch in chunk_list(list(qid_to_titles), 50):
        batch_result = get_wikidata_entities_batch(qid_batch, limiter=limiter)
        if batch_result.error_reason is not None:
            for qid in qid_batch:
                qid_errors[qid] = batch_result.error_reason
            continue
        entities.update(batch_result.data)

    # Persist and report in the selected order, independent of request batches.
    early_failures = {detail["title"]: detail for detail in results["titles"]}
    results["titles"] = []
    for title in selected_titles:
        if title in early_failures:
            results["titles"].append(early_failures[title])
            continue
        wikidata = database[title]["wikidata"]
        old_birth = wikidata.get("birth_year")
        old_death = wikidata.get("death_year")
        old_death_date = wikidata.get("death_date")
        qid = title_qids[title]
        if qid in qid_errors:
            _record_failure(
                results, title, "WikidataRequestFailure", qid_errors[qid],
                old_birth, old_death, old_death_date,
            )
            continue
        new_birth, new_death, new_death_date, parse_error = refreshed_life_dates_from_entity(
            entities.get(qid)
        )
        if parse_error is not None:
            _record_failure(
                results, title, "MalformedEntity", parse_error,
                old_birth, old_death, old_death_date,
            )
            continue
        if (
            old_death_date is not None
            and new_death_date is not None
            and old_death_date != new_death_date
        ):
            results["conflicts_review_required"] += 1
            results["errors"].append({
                "title": title,
                "type": "ExactDateConflict",
                "message": "stored death date differs from current selected claim",
            })
            results["titles"].append(_title_result(
                title, old_birth, old_birth, old_death, old_death,
                old_death_date, old_death_date,
            ))
            continue
        effective_death_date = (
            new_death_date if new_death_date is not None else old_death_date
        )
        try:
            refresh_entry_dates(
                database, title, new_birth, new_death, effective_death_date,
                data_folder,
            )
        except ValueError:
            raise
        except OSError as error:
            _record_failure(
                results, title, type(error).__name__, str(error), old_birth,
                old_death, old_death_date,
            )
            continue
        _apply_successful_dates(
            results, title, old_birth, old_death, old_death_date, new_birth,
            new_death, effective_death_date, today, recent_policy,
        )

    results["remaining_retryable"] = results["operational_failures"]
    return results


def build_apply_report(
    database, eligible_before, selected_titles, limit, results, recent_policy=None,
):
    return {
        "mode": "apply",
        "recent_window": recent_policy,
        "total_canonical_entries": len(database),
        "eligible_before": eligible_before,
        "selected": {"count": len(selected_titles), "limit": limit},
        "results": results,
        "remaining_retryable": results["remaining_retryable"],
        "recent_death_updates": {
            "count": len(results["recent_death_updates"]),
            "titles": results["recent_death_updates"],
        },
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
    if args.missing_exact_dates:
        eligible_titles = select_missing_exact_date_titles(database)
        predicate = missing_exact_dates_needs_processing
    else:
        eligible_titles = select_eligible_titles(database)
        predicate = wikidata_date_refresh_needs_processing
    try:
        selected_titles = (
            select_explicit_titles(database, args.title, predicate=predicate)
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
            "before-wikidata-date-refresh", OPERATIONAL_BACKUP_RETENTION_DAYS,
            preserve=False, kind="operational", persistence_lock=persistence_lock,
            filename=DATABASE_FILE,
        )
        backup_result = backup.as_report(True, OPERATIONAL_BACKUP_RETENTION_DAYS)
        if not backup.created:
            print(json.dumps({
                "mode": "apply", "backup": backup_result,
                "error": "Database backup failed; no canonical mutation was attempted.",
            }, indent=2, ensure_ascii=False, sort_keys=False))
            return 1

    if args.missing_exact_dates:
        results = run_exact_date_enrichment(
            database, selected_titles, args.data_folder, apply=args.apply,
        )
        report = build_exact_date_enrichment_report(
            database, selected_titles, args.limit, results, apply=args.apply,
            backup=backup_result,
        )
    elif args.apply:
        run_date = date.today()
        policy = recent_death_policy(
            run_date, default_years=RECENT_DEATH_WINDOW_YEARS,
        )
        results = run_apply(
            database, selected_titles, args.data_folder, today=run_date,
            recent_policy=policy,
        )
        report = build_apply_report(
            database, len(eligible_titles), selected_titles, args.limit, results,
            policy,
        )
        report["backup"] = backup_result
    else:
        report = build_dry_run_report(database, selected_titles, args.limit)
        report["backup"] = backup_result

    report = add_report_timing(report, started_at, time.time())
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=False))
    try:
        report_path, diagnostics = save_wikidata_date_refresh_report(
            report, WIKIDATA_DATE_REFRESH_REPORTS_DIRECTORY, started_at
        )
    except OSError as error:
        print("Warning: Wikidata date refresh report could not be saved: {}".format(error))
    else:
        print("Saved Wikidata date refresh report: {}".format(report_path))
        for diagnostic in diagnostics:
            print("Warning: {}".format(diagnostic))
    recent_deaths = report.get("recent_death_updates", {})
    if recent_deaths.get("count"):
        print("Recent death updates: {}".format(recent_deaths["count"]))
        for update in recent_deaths["titles"]:
            print("- {} — {}".format(update["title"], update["death_date"]))
    if args.apply and report.get("results", {}).get("operational_failures"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
