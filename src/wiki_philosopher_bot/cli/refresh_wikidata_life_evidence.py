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
    select_wikidata_time_claim,
    wikidata_time_claim_evidence,
)
from wiki_philosopher_bot.config import REQUEST_TIMEOUT

REPORT_DIRECTORY = Path("reports/wikidata-life-evidence-refresh")
SEMANTIC_CATEGORIES = (
    "day", "month", "year", "approximate_year", "decade", "century",
    "millennium", "broader_than_millennium", "bounded_range", "unknown",
)
ENTITY_BATCH_SIZE = 50
MAINTENANCE_REQUEST_TIMEOUT = min(REQUEST_TIMEOUT, 10)
MAINTENANCE_MAX_RETRIES = 2
MAINTENANCE_RATE_PER_SECOND = 1


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill reviewed structured Wikidata life-date evidence."
    )
    parser.add_argument("--data-folder", default=CANONICAL_DATA_FOLDER)
    parser.add_argument("--report-dir", default=str(REPORT_DIRECTORY))
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--missing-structured-evidence", action="store_true")
    parser.add_argument("--retry-operational-failures", metavar="REPORT")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be non-negative")
    if not args.missing_structured_evidence and not args.retry_operational_failures:
        parser.error("--missing-structured-evidence is required")
    if args.retry_operational_failures and args.apply:
        parser.error("--retry-operational-failures is dry-run only")
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
        "side_status_counts": {"birth": {}, "death": {}},
        "reason_counts": {"birth": {}, "death": {}},
        "unchanged_reason_counts": {}, "record_entries": [],
    }

SIDE_STATUSES = ("proposed", "already_present", "property_absent", "no_usable_claim",
                 "malformed_claim", "unsupported_claim", "ambiguous_claim", "operational_failure")

def _side_audit(old, entity, side):
    prop = "P569" if side == "birth" else "P570"
    field = side + "_time"
    if isinstance(old.get(field), dict) and old[field].get("status") == "malformed":
        return {"status": "malformed_claim", "reason": "existing_structured_evidence_malformed", "proposal": None, "legacy_comparison": {}}
    if old.get(field) is not None:
        return {"status": "already_present", "reason": "existing_structured_evidence", "proposal": None, "legacy_comparison": {}}
    claims = entity.get("claims", {}).get(prop) if isinstance(entity, dict) else None
    if claims is None or claims == []:
        return {"status": "property_absent", "reason": "property_not_present", "proposal": None, "legacy_comparison": {}}
    if not isinstance(claims, list):
        return {"status": "malformed_claim", "reason": "claims_not_list", "proposal": None, "legacy_comparison": {}}
    selected = select_wikidata_time_claim(claims)
    if selected is not None:
        evidence = wikidata_time_claim_evidence(prop, selected)
        if evidence is not None:
            return {"status": "proposed", "reason": "selected_eligible_claim", "proposal": evidence,
                    "legacy_comparison": _source_diagnostics(old, {field: evidence}, side)}
        return {"status": "unsupported_claim", "reason": "selected_claim_not_representable", "proposal": None,
                "source": _claim_diagnostic(prop, selected), "legacy_comparison": {}}
    snaks = [c.get("mainsnak", {}) for c in claims if isinstance(c, dict)]
    snaktypes = [s.get("snaktype") for s in snaks if isinstance(s, dict) and s.get("snaktype")]
    if snaktypes and all(s in {"somevalue", "novalue"} for s in snaktypes):
        return {"status": "no_usable_claim", "reason": "snaktype=" + ",".join(sorted(set(snaktypes))), "proposal": None,
                "source": {"property": prop, "snaktypes": sorted(set(snaktypes))}, "legacy_comparison": {}}
    if any(not isinstance(c, dict) or not isinstance(c.get("mainsnak"), dict) for c in claims):
        status, reason = "malformed_claim", "malformed_claim_structure"
    else:
        status, reason = "no_usable_claim", "no_eligible_claim"
    return {"status": status, "reason": reason, "proposal": None, "legacy_comparison": {}}

def _claim_diagnostic(prop, claim):
    out = {"property": prop}
    if isinstance(claim, dict):
        out["rank"] = claim.get("rank")
        snak = claim.get("mainsnak")
        out["snaktype"] = snak.get("snaktype") if isinstance(snak, dict) else None
        value = snak.get("datavalue", {}).get("value") if isinstance(snak, dict) and isinstance(snak.get("datavalue"), dict) else None
        if isinstance(value, dict):
            for key in ("time", "precision", "calendarmodel"):
                if key in value: out[key] = value[key]
    return out


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


def _derive_compatibility_summaries(results):
    """Rebuild legacy summary views from v2 per-record audit entries.

    ``no_structured_evidence`` historically means selected records for which
    this missing-evidence operation has no proposal on either side.  That is
    the same record set as the v2 ``unchanged`` outcome.
    """
    entries = results["record_entries"]
    side_counts = {"birth": {}, "death": {}}
    reason_counts = {"birth": {}, "death": {}}
    for entry in entries:
        for side in ("birth", "death"):
            status = entry[side]["status"]
            reason = entry[side]["reason"]
            side_counts[side][status] = side_counts[side].get(status, 0) + 1
            reason_counts[side][reason] = reason_counts[side].get(reason, 0) + 1
    results["side_status_counts"] = side_counts
    results["reason_counts"] = reason_counts
    results["property_absent"] = {
        side: side_counts[side].get("property_absent", 0)
        for side in ("birth", "death")
    }
    results["malformed_or_unsupported"] = sum(
        side_counts[side].get(status, 0)
        for side in ("birth", "death")
        for status in ("malformed_claim", "unsupported_claim")
    )
    results["ambiguous_or_conflict"] = sum(
        side_counts[side].get("ambiguous_claim", 0)
        for side in ("birth", "death")
    )
    results["operational_failures"] = sum(
        side_counts[side].get("operational_failure", 0)
        for side in ("birth", "death")
    )
    results["unchanged"] = sum(
        entry.get("record_status") == "unchanged" for entry in entries
    )
    results["no_structured_evidence"] = results["unchanged"]
    results["records_with_any_proposal"] = len(entries) - results["unchanged"]


def collect_proposals(database, titles, *, limiter=None):
    if limiter is None:
        limiter = RateLimiter(MAINTENANCE_RATE_PER_SECOND)
    results = _new_results()
    title_qids = {title: database[title]["wikidata"]["qid"] for title in titles}
    entities, errors = {}, {}
    batches = list(chunk_list(list(title_qids.values()), ENTITY_BATCH_SIZE))
    completed = 0
    for batch in batches:
        try:
            response = get_wikidata_entities_batch(
                batch, limiter=limiter, timeout=MAINTENANCE_REQUEST_TIMEOUT,
                max_retries=MAINTENANCE_MAX_RETRIES,
            )
        except TypeError as exc:
            # Preserve compatibility with narrow test doubles/older integrations.
            if "unexpected keyword" not in str(exc):
                raise
            response = get_wikidata_entities_batch(batch, limiter=limiter)
        if response.error_reason:
            errors.update({qid: response.error_reason for qid in batch})
        else:
            entities.update(response.data)
        completed += len(batch)
        print("Checked {}/{} records ({} batches; {} request failures)".format(
            completed, len(titles), len(batches), len(errors)), flush=True)
    proposals = {}
    for title in titles:
        results["checked"] += 1
        old = database[title]["wikidata"]
        qid = title_qids[title]
        if qid in errors:
            results["operational_failures"] += 1
            results["errors"].append({"title": title, "type": "WikidataRequestFailure", "message": errors[qid]})
            entry = {"title": title, "qid": qid}
            for side in ("birth", "death"):
                entry[side] = {"status": "operational_failure", "reason": errors[qid], "proposal": None, "legacy_comparison": {}}
            entry.update(record_status="unchanged", unchanged_reason="operational_failure", would_update=False)
            results["record_entries"].append(entry)
            continue
        entity = entities.get(qid)
        if not isinstance(entity, dict) or not isinstance(entity.get("claims"), dict):
            results["malformed_or_unsupported"] += 1
            results["errors"].append({"title": title, "type": "MalformedEntity", "message": "claims are missing or malformed"})
            entry = {"title": title, "qid": qid}
            for side in ("birth", "death"):
                entry[side] = {"status": "malformed_claim", "reason": "malformed_entity_claims", "proposal": None, "legacy_comparison": {}}
            entry.update(record_status="unchanged", unchanged_reason="malformed_claim", would_update=False)
            results["record_entries"].append(entry)
            continue
        evidence = get_life_date_evidence_from_wikidata(entity)
        detail = {"title": title, "qid": qid, "old_birth_year": old.get("birth_year"),
                  "old_death_year": old.get("death_year"), "old_birth_date": old.get("birth_date"),
                  "old_death_date": old.get("death_date"), "birth_time": evidence["birth_time"],
                  "death_time": evidence["death_time"]}
        changed = False
        audit = {"title": title, "qid": qid}
        for side in ("birth", "death"):
            field = "{}_time".format(side)
            side_audit = _side_audit(old, entity, side)
            audit[side] = side_audit
            status = side_audit["status"]
            results["side_status_counts"][side][status] = results["side_status_counts"][side].get(status, 0) + 1
            results["reason_counts"][side][side_audit["reason"]] = results["reason_counts"][side].get(side_audit["reason"], 0) + 1
            if status == "proposed":
                proposal = side_audit["proposal"]
                proposals.setdefault(title, {})[field] = proposal
                changed = True
                _count_evidence(results, {field: proposal}, side)
            diagnostic = side_audit.get("legacy_comparison", {})
            if diagnostic.get("legacy_year_disagrees"):
                results["legacy_diagnostics"]["year_disagrees"] += 1
            elif diagnostic.get("legacy_year_matches_selected_year") is True:
                results["legacy_diagnostics"]["year_agrees"] += 1
            else:
                results["legacy_diagnostics"]["no_comparable_legacy_value"] += 1
            if diagnostic.get("exact_date_disagrees"):
                results["legacy_diagnostics"]["date_disagrees"] += 1
            elif diagnostic.get("exact_date_matches_selected_day") is True:
                results["legacy_diagnostics"]["date_agrees"] += 1
        if changed:
            results["titles"].append(detail)
        else:
            results["unchanged"] += 1
        if changed and audit["birth"]["status"] == "proposed" and audit["death"]["status"] == "proposed":
            outcome = "update_both"
        elif changed and audit["birth"]["status"] == "proposed": outcome = "update_birth_only"
        elif changed: outcome = "update_death_only"
        else: outcome = "unchanged"
        audit["record_status"] = outcome
        audit["would_update"] = changed
        if not changed:
            audit["unchanged_reason"] = "no_structured_evidence_available"
            results["unchanged_reason_counts"][audit["unchanged_reason"]] = results["unchanged_reason_counts"].get(audit["unchanged_reason"], 0) + 1
        results["record_entries"].append(audit)
    results["birth_time_proposals"] = sum("birth_time" in p for p in proposals.values())
    results["death_time_proposals"] = sum("death_time" in p for p in proposals.values())
    results["both_proposed"] = sum({"birth_time", "death_time"} <= set(p) for p in proposals.values())
    results["record_entries"].sort(key=lambda x: (x["title"], x["qid"]))
    _derive_compatibility_summaries(results)
    return proposals, results


def apply_proposals(database, proposals):
    for title, fields in proposals.items():
        database[title]["wikidata"].update(copy.deepcopy(fields))


def run_refresh(database, data_folder, report_dir, *, apply=False, limit=None):
    started = time.time()
    checkpoint = Path(report_dir) / ".wikidata-life-evidence.in-progress.json"
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    before_hash = database_file_sha256(DATABASE_FILE, data_folder)
    titles = select_missing_structured_evidence_titles(database, limit)
    try:
        checkpoint.write_text(json.dumps({"report_schema_version": 2, "completed": False,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "database_sha256": before_hash, "selected_record_count": len(titles),
            "record_entries": []}, ensure_ascii=False), encoding="utf-8")
        proposals, results = collect_proposals(database, titles)
    except KeyboardInterrupt:
        print("Interrupted; in-progress checkpoint: {}".format(checkpoint), flush=True)
        raise
    backup = DatabaseBackupResult().as_report(False, OPERATIONAL_BACKUP_RETENTION_DAYS)
    after_hash = before_hash
    if apply and results.get("operational_failures"):
        results["errors"].append({"type": "IncompleteSourcePass", "message": "apply refused because operational failures occurred"})
    elif apply and proposals:
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
        "report_schema_version": 2,
        "operation": "wikidata-life-evidence-refresh", "mode": "apply" if apply else "dry-run",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "database_sha256_before": before_hash, "database_sha256_after": after_hash,
        "selected_record_count": len(titles), "checked_record_count": results["checked"],
        "selected_titles": titles, "results": results, "backup": backup,
        "proposed_records": list(proposals.values()) if False else proposals,
        "record_entries": results.get("record_entries", []),
        "exceptions": {
            "legacy_year_disagreements": [
                {"title": e["title"], "qid": e["qid"], "property": side,
                 "legacy": e[side].get("legacy_comparison")}
                for e in results.get("record_entries", []) for side in ("birth", "death")
                if e[side].get("legacy_comparison", {}).get("legacy_year_disagrees")
            ],
            "exact_date_disagreements": [
                {"title": e["title"], "qid": e["qid"], "property": side,
                 "legacy": e[side].get("legacy_comparison")}
                for e in results.get("record_entries", []) for side in ("birth", "death")
                if e[side].get("legacy_comparison", {}).get("exact_date_disagrees")
            ],
            "malformed_or_unsupported": [
                {"title": e["title"], "qid": e["qid"], "property": side,
                 "status": e[side]["status"], "reason": e[side]["reason"],
                 "source": e[side].get("source")}
                for e in results.get("record_entries", []) for side in ("birth", "death")
                if e[side]["status"] in {"malformed_claim", "unsupported_claim"}
            ],
            "unchanged": [
                {"title": e["title"], "qid": e["qid"],
                 "birth": {"status": e["birth"]["status"], "reason": e["birth"]["reason"]},
                 "death": {"status": e["death"]["status"], "reason": e["death"]["reason"]},
                 "reason": e.get("unchanged_reason")}
                for e in results.get("record_entries", []) if not e.get("would_update")
            ],
        },
        "duration_seconds": round(time.time() - started, 6),
    }
    paths = save_json_report(
        report, report_dir, started, report_kind="wikidata-life-evidence",
        temporary_prefix=".wikidata-life-evidence-report-",
    )
    checkpoint.unlink(missing_ok=True)
    return report, paths[0] if isinstance(paths, tuple) else paths

def recover_report(database, data_folder, report_dir, source_path):
    """Retry only sides marked operational_failure in a completed v2 report."""
    source = json.loads(Path(source_path).read_text(encoding="utf-8"))
    if source.get("report_schema_version", 0) < 2 or source.get("operation") != "wikidata-life-evidence-refresh":
        raise ValueError("source report is not a compatible life-evidence report")
    if source.get("completed", True) is False:
        raise ValueError("source report is incomplete")
    before = database_file_sha256(DATABASE_FILE, data_folder)
    if before != source.get("database_sha256_before"):
        raise ValueError("canonical database SHA does not match source report")
    entries = copy.deepcopy(source.get("record_entries", []))
    if len(entries) != source.get("selected_record_count"):
        raise ValueError("source report record entry count mismatch")
    failed_titles = [e["title"] for e in entries if any(e[s].get("status") == "operational_failure" for s in ("birth", "death"))]
    for e in entries:
        if e["title"] not in database or database[e["title"]]["wikidata"].get("qid") != e["qid"]:
            raise ValueError("source report title/QID identity mismatch")
    mini = {title: database[title] for title in failed_titles}
    proposals, fresh = collect_proposals(mini, failed_titles)
    fresh_by_title = {e["title"]: e for e in fresh["record_entries"]}
    for e in entries:
        if e["title"] not in fresh_by_title: continue
        n = fresh_by_title[e["title"]]
        for side in ("birth", "death"):
            if e[side].get("status") == "operational_failure":
                e[side] = n[side]
        e["would_update"] = any(e[s].get("status") == "proposed" for s in ("birth", "death"))
        if e["would_update"]:
            e.pop("unchanged_reason", None)
        statuses = [e[s]["status"] for s in ("birth", "death")]
        e["record_status"] = "update_both" if statuses == ["proposed", "proposed"] else ("update_birth_only" if statuses[0] == "proposed" else ("update_death_only" if statuses[1] == "proposed" else "unchanged"))
    # Reuse a fresh aggregate pass by temporarily feeding merged entries through
    # the same stable report fields; preserve all source observations.
    results = _new_results(); results["checked"] = len(entries); results["record_entries"] = entries
    results["birth_time_proposals"] = sum(e["birth"]["status"]=="proposed" for e in entries)
    results["death_time_proposals"] = sum(e["death"]["status"]=="proposed" for e in entries)
    results["both_proposed"] = sum(e["birth"]["status"]==e["death"]["status"]=="proposed" for e in entries)
    _derive_compatibility_summaries(results)
    started=time.time(); report={"report_schema_version":2,"operation":"wikidata-life-evidence-refresh","mode":"dry-run","completed":True,"recovery_run":True,"source_report":str(source_path),"root_report":source.get("root_report",str(source_path)),"generated_at":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),"database_sha256_before":before,"database_sha256_after":before,"selected_record_count":len(entries),"checked_record_count":len(entries),"record_entries":entries,"results":results,"proposed_records":proposals,"backup":DatabaseBackupResult().as_report(False, OPERATIONAL_BACKUP_RETENTION_DAYS),"duration_seconds":round(time.time()-started,6)}
    paths=save_json_report(report, report_dir, started, report_kind="wikidata-life-evidence", temporary_prefix=".wikidata-life-evidence-report-")
    return report, paths[0] if isinstance(paths,tuple) else paths


def main(argv=None):
    args = parse_args(argv)
    database = load_database(DATABASE_FILE, args.data_folder)
    if args.retry_operational_failures:
        report, path = recover_report(database, args.data_folder, args.report_dir, args.retry_operational_failures)
        print(json.dumps(report, ensure_ascii=False, indent=2)); print("Saved report: {}".format(path))
        return 0
    report, path = run_refresh(
        database, args.data_folder, args.report_dir,
        apply=args.apply, limit=args.limit,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("Saved report: {}".format(path))
    return 1 if report["results"]["operational_failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
