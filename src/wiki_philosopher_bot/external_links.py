"""Read-only positive-evidence audits for canonical external reading links.

The audit intentionally proposes values without changing canonical entries.
The separate report-driven apply helper independently validates every reviewed
proposal before one atomic canonical rewrite.
"""

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import re

from wiki_philosopher_bot.database_schema import (
    EXTERNAL_LINK_KEYS,
    empty_external_links,
    is_valid_external_link,
)
from wiki_philosopher_bot.cache import rewrite_database
from wiki_philosopher_bot.utils import chunk_list, is_posting_candidate
from wiki_philosopher_bot.wikipedia_api import (
    get_english_wikisource_sitelink,
    get_english_wikisource_sitelink_title,
    get_wikidata_entities_batch,
    lookup_wikiquote_external_link,
)


@dataclass(frozen=True)
class ExternalLinkLookup:
    """One positive-evidence lookup result, with a safe failure category."""

    url: str = None
    reason: str = None


class ExternalLinksApplyValidationError(ValueError):
    """A reviewed enrichment report can no longer be applied safely."""


def external_links_for_entry(entry):
    """Return a detached external-link shape without adding it to *entry*."""
    stored = entry.get("external_links") if isinstance(entry, dict) else None
    links = empty_external_links()
    if isinstance(stored, dict):
        for key in EXTERNAL_LINK_KEYS:
            links[key] = stored.get(key)
    return links


def _valid_value(key, value):
    return value is not None and is_valid_external_link(key, value)


_QID_RE = re.compile(r"^Q[1-9][0-9]*$")


def _qid_for_entry(entry):
    wikidata = entry.get("wikidata") if isinstance(entry, dict) else None
    qid = wikidata.get("qid") if isinstance(wikidata, dict) else None
    return qid if isinstance(qid, str) and _QID_RE.fullmatch(qid) else None


def _wikiquote_result(value):
    """Coerce injectable lookup fakes while keeping the public seam simple."""
    if isinstance(value, ExternalLinkLookup):
        return value
    if isinstance(value, tuple) and len(value) == 2:
        return ExternalLinkLookup(value[0], value[1])
    raise TypeError("Wikiquote lookup must return (url, reason) or ExternalLinkLookup")


def _batch_wikidata(qids, limiter, wikidata_lookup, batch_size):
    entities = {}
    failures = {}
    for batch in chunk_list(qids, batch_size):
        result = wikidata_lookup(batch, limiter=limiter)
        if result.error_reason is not None:
            for qid in batch:
                failures[qid] = result.error_reason
            continue
        if not isinstance(result.data, dict):
            for qid in batch:
                failures[qid] = "malformed_response"
            continue
        entities.update(result.data)
    return entities, failures


def _report_error(message):
    raise ExternalLinksApplyValidationError(message)


def reviewed_external_link_proposals(report):
    """Validate and extract immutable apply proposals from one audit report.

    This intentionally accepts the pre-evidence report shape used for the
    already reviewed initial audit. New ``audit_schema_version >= 2`` reports
    must carry the stronger provenance fields emitted below.
    """
    if not isinstance(report, dict):
        _report_error("Audit report must be a JSON object")
    if report.get("operation") != "external-links-enrichment-audit":
        _report_error("Report is not an external-links enrichment audit")
    if report.get("mode") != "dry-run":
        _report_error("Only a dry-run external-links audit report may be applied")
    if report.get("conflicts"):
        _report_error("Audit report contains conflicts")
    if report.get("invalid_existing_external_links"):
        _report_error("Audit report contains invalid existing external links")
    rows = report.get("changes_or_conflicts")
    if not isinstance(rows, list):
        _report_error("Audit report changes_or_conflicts must be a list")

    audit_schema_version = report.get("audit_schema_version", 1)
    if (
        not isinstance(audit_schema_version, int)
        or isinstance(audit_schema_version, bool)
        or audit_schema_version < 1
    ):
        _report_error("Audit report has an invalid audit_schema_version")
    require_evidence = audit_schema_version >= 2
    proposals = []
    seen_titles = set()
    for row in rows:
        if not isinstance(row, dict):
            _report_error("Audit report contains a malformed proposal row")
        title = row.get("title")
        if not isinstance(title, str) or not title:
            _report_error("Audit report proposal title must be non-empty")
        if title in seen_titles:
            _report_error("Audit report has duplicate proposal title: {}".format(title))
        seen_titles.add(title)
        current = row.get("current")
        proposed = row.get("proposed")
        if not isinstance(current, dict) or not isinstance(proposed, dict):
            _report_error("Audit report proposal row is missing current/proposed links")
        if proposed.get("project_gutenberg") is not None:
            _report_error("Audit report must not propose Project Gutenberg changes")

        updates = {}
        for key in ("wikiquote", "wikisource"):
            value = proposed.get(key)
            if value is None:
                continue
            if not is_valid_external_link(key, value):
                _report_error("Audit report has invalid proposed {} URL for {}".format(key, title))
            if current.get(key) is not None:
                _report_error("Audit report proposed {} although its reviewed value was not null for {}".format(key, title))
            updates[key] = value
        if not updates:
            _report_error("Audit report contains a non-proposal row for {}".format(title))

        qid = row.get("qid")
        if qid is not None and (not isinstance(qid, str) or not _QID_RE.fullmatch(qid)):
            _report_error("Audit report has invalid QID for {}".format(title))

        evidence = row.get("evidence")
        if require_evidence:
            if not isinstance(evidence, dict):
                _report_error("Audit report is missing evidence for {}".format(title))
            if "wikiquote" in updates:
                wikiquote_evidence = evidence.get("wikiquote")
                if not (
                    isinstance(wikiquote_evidence, dict)
                    and wikiquote_evidence.get("successful_quote_parse") is True
                    and wikiquote_evidence.get("final_response_url")
                    == updates["wikiquote"]
                ):
                    _report_error("Audit report has invalid Wikiquote evidence for {}".format(title))
            if "wikisource" in updates:
                wikisource_evidence = evidence.get("wikisource")
                if not (
                    isinstance(wikisource_evidence, dict)
                    and wikisource_evidence.get("qid") == qid
                    and isinstance(wikisource_evidence.get("enwikisource_sitelink_title"), str)
                    and wikisource_evidence["enwikisource_sitelink_title"].strip()
                ):
                    _report_error("Audit report has invalid Wikisource evidence for {}".format(title))
        proposals.append({"title": title, "qid": qid, "updates": updates})

    if not proposals:
        _report_error("Audit report contains no external-link proposals")
    return proposals


def validate_reviewed_external_links_apply(database, report):
    """Fail closed unless every reviewed proposal remains exactly applicable."""
    if not isinstance(database, dict):
        raise TypeError("database must be a title-to-entry dictionary")
    proposals = reviewed_external_link_proposals(report)
    for proposal in proposals:
        title = proposal["title"]
        entry = database.get(title)
        if not isinstance(entry, dict):
            _report_error("Reviewed title no longer exists: {}".format(title))
        if entry.get("title") != title:
            _report_error("Current entry title does not match reviewed title: {}".format(title))
        if not is_posting_candidate(entry):
            _report_error("Reviewed title is no longer post eligible: {}".format(title))
        reviewed_qid = proposal["qid"]
        if reviewed_qid is not None and _qid_for_entry(entry) != reviewed_qid:
            _report_error("Current QID differs from reviewed QID for {}".format(title))
        stored = entry.get("external_links")
        if stored is not None and not isinstance(stored, dict):
            _report_error("Current external_links is malformed for {}".format(title))
        for key in proposal["updates"]:
            current_value = stored.get(key) if isinstance(stored, dict) else None
            if current_value is not None:
                _report_error("Current {} is no longer null for {}".format(key, title))
    return proposals


def apply_reviewed_external_links(
    database,
    report,
    filename,
    data_folder,
    persistence_lock,
):
    """Atomically write only reviewed Wikiquote/Wikisource proposal fields."""
    proposals = validate_reviewed_external_links_apply(database, report)
    candidate_database = deepcopy(database)
    wikiquote_written = 0
    wikisource_written = 0
    both = 0
    updated_titles = []
    for proposal in proposals:
        entry = candidate_database[proposal["title"]]
        links = entry.get("external_links")
        if links is None:
            # Do not manufacture an unrelated Project Gutenberg key.
            links = {}
            entry["external_links"] = links
        for key, value in proposal["updates"].items():
            links[key] = value
        wikiquote_written += int("wikiquote" in proposal["updates"])
        wikisource_written += int("wikisource" in proposal["updates"])
        both += int(len(proposal["updates"]) == 2)
        updated_titles.append(proposal["title"])

    database_hash = rewrite_database(
        candidate_database, filename, data_folder, persistence_lock
    )
    # The live in-memory view changes only after the durable full rewrite.
    database.clear()
    database.update(candidate_database)
    return {
        "reviewed_proposal_count": len(proposals),
        "records_updated": len(updated_titles),
        "updated_titles": updated_titles,
        "wikiquote_links_written": wikiquote_written,
        "wikisource_links_written": wikisource_written,
        "records_receiving_both": both,
        "database_sha256": database_hash,
    }


def audit_external_links(
    database,
    limiter=None,
    wikiquote_lookup=lookup_wikiquote_external_link,
    wikidata_lookup=get_wikidata_entities_batch,
    batch_size=50,
):
    """Return a comprehensive read-only enrichment audit for *database*.

    The database is never modified, including when it contains no
    ``external_links`` field.  Existing valid values are retained and are not
    fetched again.  Invalid stored values are reported; an observed URL is
    never silently treated as an overwrite instruction.
    """
    if not isinstance(database, dict):
        raise TypeError("database must be a title-to-entry dictionary")
    if not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be positive")

    records = []
    invalid_existing = []
    wikiquote_failures = Counter()
    wikisource_failures = Counter()
    qids_to_lookup = []

    skipped_records = []
    # First pass: inspect without mutation and perform only needed Wikiquote
    # page validation for records the bot could actually post. Wikisource QIDs
    # are deduplicated for the second pass.
    for title in sorted(database):
        entry = database[title]
        if not is_posting_candidate(entry):
            skipped_records.append({"title": title})
            continue
        current = external_links_for_entry(entry)
        qid = _qid_for_entry(entry)
        invalid = {
            key: value for key, value in current.items()
            if value is not None and not is_valid_external_link(key, value)
        }
        if invalid:
            invalid_existing.append({
                "title": title,
                "qid": qid,
                "invalid": invalid,
            })

        row = {
            "title": title,
            "qid": qid,
            "current": current,
            "proposed": empty_external_links(),
            "observed": empty_external_links(),
            "evidence": {"wikiquote": None, "wikisource": None},
            "wikiquote": {"status": "preserved", "reason": None},
            "wikisource": {"status": "preserved", "reason": None},
        }
        if not _valid_value("wikiquote", current["wikiquote"]):
            observed = _wikiquote_result(wikiquote_lookup(title, limiter=limiter))
            row["observed"]["wikiquote"] = observed.url
            if observed.url is None:
                row["wikiquote"] = {"status": "unavailable", "reason": observed.reason}
                wikiquote_failures[observed.reason or "unknown"] += 1
            elif not is_valid_external_link("wikiquote", observed.url):
                row["wikiquote"] = {"status": "unavailable", "reason": "invalid_observed_url"}
                wikiquote_failures["invalid_observed_url"] += 1
            elif current["wikiquote"] is None:
                row["proposed"]["wikiquote"] = observed.url
                row["evidence"]["wikiquote"] = {
                    "final_response_url": observed.url,
                    "successful_quote_parse": True,
                }
                row["wikiquote"] = {"status": "proposed", "reason": None}
            else:
                row["wikiquote"] = {"status": "conflict", "reason": "invalid_existing_value"}

        if not _valid_value("wikisource", current["wikisource"]):
            if qid is None:
                row["wikisource"] = {"status": "unavailable", "reason": "missing_qid"}
            else:
                qids_to_lookup.append(qid)
                row["wikisource"] = {"status": "pending_lookup", "reason": None}
        records.append(row)

    entities, wikidata_failures = _batch_wikidata(
        sorted(set(qids_to_lookup)), limiter, wikidata_lookup, batch_size
    )
    conflicts = []
    detail_rows = []
    for row in records:
        current = row["current"]
        qid = row["qid"]
        if row["wikisource"]["status"] == "pending_lookup":
            failure = wikidata_failures.get(qid)
            if failure is not None:
                row["wikisource"] = {"status": "unavailable", "reason": "wikidata_{}".format(failure)}
                wikisource_failures["wikidata_{}".format(failure)] += 1
            else:
                entity = entities.get(qid)
                observed = get_english_wikisource_sitelink(entity)
                row["observed"]["wikisource"] = observed
                if observed is None:
                    row["wikisource"] = {"status": "unavailable", "reason": "no_enwikisource_sitelink"}
                elif current["wikisource"] is None:
                    row["proposed"]["wikisource"] = observed
                    row["evidence"]["wikisource"] = {
                        "qid": qid,
                        "enwikisource_sitelink_title": (
                            get_english_wikisource_sitelink_title(entity)
                        ),
                    }
                    row["wikisource"] = {"status": "proposed", "reason": None}
                else:
                    row["wikisource"] = {"status": "conflict", "reason": "invalid_existing_value"}

        for key in ("wikiquote", "wikisource"):
            observed = row["observed"][key]
            if observed is not None and current[key] is not None and observed != current[key]:
                conflict = {
                    "title": row["title"], "qid": qid, "kind": key,
                    "current": current[key], "observed": observed,
                }
                conflicts.append(conflict)
        if (
            row["proposed"]["wikiquote"] is not None
            or row["proposed"]["wikisource"] is not None
            or row["wikiquote"]["status"] == "conflict"
            or row["wikisource"]["status"] == "conflict"
        ):
            detail_rows.append(row)

    already_valid = sum(
        any(_valid_value(key, external_links_for_entry(entry)[key]) for key in EXTERNAL_LINK_KEYS)
        for entry in database.values()
    )
    proposed_wikiquote = sum(row["proposed"]["wikiquote"] is not None for row in records)
    proposed_wikisource = sum(row["proposed"]["wikisource"] is not None for row in records)
    both = sum(
        row["proposed"]["wikiquote"] is not None
        and row["proposed"]["wikisource"] is not None
        for row in records
    )
    no_change = sum(
        row["proposed"]["wikiquote"] is None
        and row["proposed"]["wikisource"] is None
        and row["wikiquote"]["status"] != "conflict"
        and row["wikisource"]["status"] != "conflict"
        for row in records
    )
    missing_qid = [
        {"title": row["title"], "current": row["current"]}
        for row in records if row["qid"] is None
        and row["wikisource"]["reason"] == "missing_qid"
    ]
    project_gutenberg_values = [
        {
            "title": row["title"],
            "qid": row["qid"],
            "project_gutenberg": row["current"]["project_gutenberg"],
        }
        for row in records
        if row["current"]["project_gutenberg"] is not None
    ]
    return {
        "audit_schema_version": 2,
        "mode": "dry-run",
        "operation": "external-links-enrichment-audit",
        "total_canonical_records": len(database),
        "post_eligible_records": len(records),
        "non_post_eligible_records_skipped": len(skipped_records),
        "counts": {
            "records_already_containing_valid_external_links": already_valid,
            "proposed_new_wikiquote_links": proposed_wikiquote,
            "proposed_new_wikisource_links": proposed_wikisource,
            "records_receiving_both": both,
            "records_with_no_proposed_change": no_change,
            "records_without_usable_wikidata_qid": len(missing_qid),
            "invalid_existing_external_link_values": len(invalid_existing),
            "conflicts": len(conflicts),
        },
        "wikiquote_lookup_failures_by_reason": dict(sorted(wikiquote_failures.items())),
        "wikisource_lookup_failures_by_reason": dict(sorted(wikisource_failures.items())),
        "invalid_existing_external_links": invalid_existing,
        "conflicts": conflicts,
        "records_without_usable_wikidata_qid": missing_qid,
        "skipped_non_post_eligible_records": skipped_records,
        "project_gutenberg_existing_values": project_gutenberg_values,
        "changes_or_conflicts": detail_rows,
    }


def format_external_links_audit_summary(report, report_path=None):
    """Return a concise human-readable summary without exposing credentials."""
    counts = report["counts"]
    text = (
        "External-links audit: scanned {total}; post eligible {eligible}; skipped {skipped}; Wikiquote proposals {wikiquote}; "
        "Wikisource proposals {wikisource}; both {both}; no change {unchanged}; "
        "invalid existing {invalid}; conflicts {conflicts}".format(
            total=report["total_canonical_records"],
            eligible=report["post_eligible_records"],
            skipped=report["non_post_eligible_records_skipped"],
            wikiquote=counts["proposed_new_wikiquote_links"],
            wikisource=counts["proposed_new_wikisource_links"],
            both=counts["records_receiving_both"],
            unchanged=counts["records_with_no_proposed_change"],
            invalid=counts["invalid_existing_external_link_values"],
            conflicts=counts["conflicts"],
        )
    )
    if report_path is not None:
        text += "; report {}".format(report_path)
    return text
