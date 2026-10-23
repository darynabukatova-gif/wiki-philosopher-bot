"""Reviewed backfill of structured Wikidata P569/P570 evidence."""

import argparse
import copy
import hashlib
import json
import time
from pathlib import Path

from wiki_philosopher_bot.cache import (
    DatabaseBackupResult,
    create_database_backup,
    database_file_sha256,
    load_database,
    rewrite_database,
)
from wiki_philosopher_bot.config import (
    CANONICAL_DATA_FOLDER,
    DATABASE_BACKUP_FOLDER,
    DATABASE_FILE,
    OPERATIONAL_BACKUP_RETENTION_DAYS,
    RATE_LIMIT,
)
from wiki_philosopher_bot.run_reporting import save_json_report
from wiki_philosopher_bot.runtime import persistence_lock
from wiki_philosopher_bot.utils import RateLimiter, chunk_list, is_semantically_postable_philosopher
from wiki_philosopher_bot.wikipedia_api import (
    classify_wikidata_life_date,
    get_wikidata_entities_batch,
    get_life_date_evidence_from_wikidata,
)

REPORT_DIRECTORY = Path("reports/wikidata-life-evidence-refresh")
SEMANTIC_CATEGORIES = (
    "day", "month", "year", "approximate_year", "decade", "century",
    "millennium", "broader_than_millennium", "bounded_range", "unknown",
)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill reviewed structured Wikidata life-date evidence."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-dir", default=str(REPORT_DIRECTORY))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--missing-structured-evidence", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if not args.missing_structured_evidence:
        parser.error("--missing-structured-evidence is required")
    return args


def missing_structured_evidence_needs_processing(entry):
    """Select only accepted, QID-backed records missing either evidence side."""
    if not is_semantically_postable_philosopher(entry):
        return False
    wikidata = entry.get("wikidata") if isinstance(entry, dict) else None
    if not isinstance(wikidata, dict) or wikidata.get("status") != "available":
        return False
    qid = wikidata.get("qid")
    if not isinstance(qid, str) or not qid or not qid.startswith("Q") or not qid[1:].isdigit():
        return False
    return any(
        wikidata.get(field) is None
        or (isinstance(wikidata.get(field), dict) and wikidata[field].get("status") == "malformed")
        for field in ("birth_time", "death_time")
    )


def select_missing_structured_evidence_titles(database, limit=None):
    titles = sorted(
        title for title, entry in database.items()
        if missing_structured_evidence_needs_processing(entry)
    )
    return titles if limit is None else titles[:limit]


def _empty_categories():
    return {category: 0 for category in SEMANTIC_CATEGORIES}


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_diagnostics(old, evidence, side):
    old_year = old.get("{}_year".format(side))
    old_date = old.get("{}_date".format(side))
    selected = evidence.get("{}_time".format(side))
    result = {
        "legacy_year_matches_selected_year": None,
        "exact_date_matches_selected_day": None,
        "legacy_year_disagrees": False,
        "exact_date_disagrees": False,
    }
    if not isinstance(selected, dict) or selected.get("status") != "available":
        return result
    selected_year = selected.get("year")
    if isinstance(old_year, int) and isinstance(selected_year, int):
        result["legacy_year_matches_selected_year"] = old_year == selected_year
        result["legacy_year_disagrees"] = old_year != selected_year
    if isinstance(old_date, str) and selected.get("precision_kind") == "day":
        raw = selected.get("raw_time", "")
        result["exact_date_matches_selected_day"] = old_date == raw[1:11]
        result["exact_date_disagrees"] = old_date != raw[1:11]
    return result


def _new_results():
    return {
        "checked": 0, "records_updated": 0, "birth_time_proposals": 0,
        "death_time_proposals": 0, "both_proposed": 0,
        "no_structured_evidence": 0, "property_absent": {"birth": 0, "death": 0},
        "malformed_or_unsupported": 0, "ambiguous_or_conflict": 0,
        "operational_failures": 0, "unchanged": 0, "errors": [], "titles": [],
        "category_counts": {"birth": _empty_categories(), "death": _empty_categories()},
        "calendar_counts": {"birth": {}, "death": {}},
        "rank_counts": {"birth": {}, "death": {}},
        "range_qualified": {"birth": 0, "death": 0},
        "circa_qualified": {"birth": 0, "death": 0},
        "bce_evidence": {"birth": 0, "death": 0},
        "coarse_than_year": {"birth": 0, "death": 0},
        "legacy_diagnostics": {"year_agrees": 0, "year_disagrees": 0,
                                "date_agrees": 0, "date_disagrees": 0,
                                "no_comparable_legacy_value": 0},
    }


def _count_evidence(results, evidence, side):
    item = evidence.get("{}_time".format(side))
    if item is None:
        results["property_absent"][side] += 1
        return
    if item.get("status") != "available":
        results["malformed_or_unsupported"] += 1
        return
    category = classify_wikidata_life_date(item)
    results["category_counts"][side][category] += 1
    calendar = item.get("calendar_model", "unknown")
    results["calendar_counts"][side][calendar] = results["calendar_counts"][side].get(calendar, 0) + 1
    rank = item.get("rank", "unknown")
    results["rank_counts"][side][rank] = results["rank_counts"][side].get(rank, 0) + 1
    qualifiers = item.get("qualifiers", {})
    if isinstance(qualifiers, dict) and ("P1319" in qualifiers or "P1326" in qualifiers):
        results["range_qualified"][side] += 1
    if item.get("qualifier_entity_ids", {}).get("P1480") == "Q5727902":
        results["circa_qualified"][side] += 1
    if item.get("precision", 99) < 9:
        results["coarse_than_year"][side] += 1
    if isinstance(item.get("year"), int) and item["year"] < 0:
        results["bce_evidence"][side] += 1


def collect_proposals(database, titles, *, limiter=None):
    if limiter is None:
        limiter = RateLimiter(RATE_LIMIT)
    results = _new_results()
    title_qids = {title: database[title]["wikidata"]["qid"] for title in titles}
    entities, errors = {}, {}
    for batch in chunk_list(list(title_qids.values()), 50):
        response = get_wikidata_entities_batch(batch, limiter=limiter)
        if response.error_reason:
            errors.update({qid: response.error_reason for qid in batch})
        else:
            entities.update(response.data)
    proposals = {}
    for title in titles:
        results["checked"] += 1
        old = database[title]["wikidata"]
        qid = title_qids[title]
        if qid in errors:
            results["operational_failures"] += 1
            results["errors"].append({"title": title, "type": "WikidataRequestFailure", "message": errors[qid]})
            continue
        entity = entities.get(qid)
        if not isinstance(entity, dict) or not isinstance(entity.get("claims"), dict):
            results["malformed_or_unsupported"] += 1
            results["errors"].append({"title": title, "type": "MalformedEntity", "message": "claims are missing or malformed"})
            continue
        evidence = get_life_date_evidence_from_wikidata(entity)
        detail = {"title": title, "qid": qid, "old_birth_year": old.get("birth_year"),
                  "old_death_year": old.get("death_year"), "old_birth_date": old.get("birth_date"),
                  "old_death_date": old.get("death_date"), "birth_time": evidence["birth_time"],
                  "death_time": evidence["death_time"]}
        changed = False
        for side in ("birth", "death"):
            field = "{}_time".format(side)
            _count_evidence(results, evidence, side)
            if old.get(field) is not None:
                continue
            if evidence.get(field) is not None and evidence[field].get("status") == "available":
                proposals.setdefault(title, {})[field] = evidence[field]
                changed = True
            diagnostic = _source_diagnostics(old, evidence, side)
            if diagnostic["legacy_year_disagrees"]:
                results["legacy_diagnostics"]["year_disagrees"] += 1
            elif diagnostic["legacy_year_matches_selected_year"] is True:
                results["legacy_diagnostics"]["year_agrees"] += 1
            else:
                results["legacy_diagnostics"]["no_comparable_legacy_value"] += 1
            if diagnostic["exact_date_disagrees"]:
                results["legacy_diagnostics"]["date_disagrees"] += 1
            elif diagnostic["exact_date_matches_selected_day"] is True:
                results["legacy_diagnostics"]["date_agrees"] += 1
        if changed:
            results["titles"].append(detail)
        else:
            results["unchanged"] += 1
    results["birth_time_proposals"] = sum("birth_time" in p for p in proposals.values())
    results["death_time_proposals"] = sum("death_time" in p for p in proposals.values())
    results["both_proposed"] = sum({"birth_time", "death_time"} <= set(p) for p in proposals.values())
    return proposals, results


def apply_proposals(database, proposals):
    for title, fields in proposals.items():
        database[title]["wikidata"].update(copy.deepcopy(fields))


def run_refresh(database, data_folder, report_dir, *, apply=False, limit=None):
    started = time.time()
    before_hash = database_file_sha256(DATABASE_FILE, data_folder)
    titles = select_missing_structured_evidence_titles(database, limit)
    proposals, results = collect_proposals(database, titles)
    backup = DatabaseBackupResult().as_report(False, OPERATIONAL_BACKUP_RETENTION_DAYS)
    after_hash = before_hash
    if apply and proposals:
        Path(DATABASE_BACKUP_FOLDER).mkdir(parents=True, exist_ok=True)
        backup_result = create_database_backup(
            data_folder, DATABASE_BACKUP_FOLDER, "before-wikidata-life-evidence-refresh",
            OPERATIONAL_BACKUP_RETENTION_DAYS, preserve=False, kind="operational",
            persistence_lock=persistence_lock, filename=DATABASE_FILE,
        )
        backup = backup_result.as_report(True, OPERATIONAL_BACKUP_RETENTION_DAYS)
        if not backup_result.created:
            results["operational_failures"] += 1
            results["errors"].append({"type": "BackupFailure", "message": backup_result.error_reason})
        else:
            apply_proposals(database, proposals)
            rewrite_database(database, DATABASE_FILE, data_folder, persistence_lock)
            results["records_updated"] = len(proposals)
            after_hash = database_file_sha256(DATABASE_FILE, data_folder)
    report = {
        "operation": "wikidata-life-evidence-refresh", "mode": "apply" if apply else "dry-run",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "database_sha256_before": before_hash, "database_sha256_after": after_hash,
        "selected_record_count": len(titles), "checked_record_count": results["checked"],
        "selected_titles": titles, "results": results, "backup": backup,
        "proposed_records": list(proposals.values()) if False else proposals,
        "duration_seconds": round(time.time() - started, 6),
    }
    paths = save_json_report(
        report, report_dir, started, report_kind="wikidata-life-evidence",
        temporary_prefix=".wikidata-life-evidence-report-",
    )
    return report, paths[0] if isinstance(paths, tuple) else paths


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    report, path = run_refresh(
        database, args.data_folder, args.report_dir,
        apply=args.apply, limit=args.limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Saved report: {}".format(path))
    return 1 if report["results"]["operational_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
