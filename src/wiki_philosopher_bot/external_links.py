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
from wiki_philosopher_bot.utils import (
    chunk_list,
    is_posting_candidate,
    is_semantically_postable_philosopher,
)
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
PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY = "P1938"
PROJECT_GUTENBERG_AUTHOR_ID_PATTERN = re.compile(r"^[1-9][0-9]{0,4}$")
PROJECT_GUTENBERG_AUTHOR_URL_FORMATTER = (
    "https://gutenberg.org/ebooks/author/$1"
)
_PROJECT_GUTENBERG_AUTHOR_URL_RE = re.compile(
    r"^https://(?:www\.)?gutenberg\.org/ebooks/author/[1-9][0-9]{0,4}$"
)


def _qid_for_entry(entry):
    wikidata = entry.get("wikidata") if isinstance(entry, dict) else None
    qid = wikidata.get("qid") if isinstance(wikidata, dict) else None
    return qid if isinstance(qid, str) and _QID_RE.fullmatch(qid) else None


def project_gutenberg_author_url(author_id):
    """Return the official HTTPS author landing URL for Wikidata P1938.

    P1938's formatter is ``https://gutenberg.org/ebooks/author/$1``.  The
    ``www`` form is Project Gutenberg's current canonical public landing
    domain, while retaining the same stable author-ID path.
    """
    if (
        not isinstance(author_id, str)
        or not PROJECT_GUTENBERG_AUTHOR_ID_PATTERN.fullmatch(author_id)
    ):
        return None
    return "https://www.gutenberg.org/ebooks/author/{}".format(author_id)


def is_project_gutenberg_author_url(value):
    """Whether *value* is an exact official Project Gutenberg author URL."""
    return (
        isinstance(value, str)
        and _PROJECT_GUTENBERG_AUTHOR_URL_RE.fullmatch(value) is not None
    )


def project_gutenberg_author_identifier_resolution(entity):
    """Resolve P1938 conservatively without name matching or URL lookup.

    Wikidata marks P1938 as a single-value external identifier, but has
    documented exceptions.  A proposal is therefore allowed only for one
    distinct, valid, non-deprecated identifier.  Duplicate statements for the
    same identifier do not create ambiguity; distinct identifiers do.
    """
    claims = entity.get("claims") if isinstance(entity, dict) else None
    statements = (
        claims.get(PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY)
        if isinstance(claims, dict)
        else None
    )
    if statements is None:
        return {"status": "missing", "identifiers": [], "invalid_values": []}
    if not isinstance(statements, list):
        return {"status": "invalid", "identifiers": [], "invalid_values": [statements]}

    identifiers = set()
    invalid_values = []
    for statement in statements:
        if not isinstance(statement, dict):
            invalid_values.append(statement)
            continue
        if statement.get("rank") == "deprecated":
            continue
        mainsnak = statement.get("mainsnak")
        datavalue = (
            mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
        )
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if not isinstance(value, str) or not PROJECT_GUTENBERG_AUTHOR_ID_PATTERN.fullmatch(value):
            invalid_values.append(value)
            continue
        identifiers.add(value)

    ordered_identifiers = sorted(identifiers, key=int)
    if invalid_values:
        return {
            "status": "invalid",
            "identifiers": ordered_identifiers,
            "invalid_values": invalid_values,
        }
    if not ordered_identifiers:
        return {"status": "missing", "identifiers": [], "invalid_values": []}
    if len(ordered_identifiers) > 1:
        return {
            "status": "ambiguous",
            "identifiers": ordered_identifiers,
            "invalid_values": [],
        }
    return {
        "status": "available",
        "identifiers": ordered_identifiers,
        "invalid_values": [],
    }


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


def reviewed_project_gutenberg_proposals(report):
    """Validate and extract immutable Project Gutenberg apply proposals.

    The Project Gutenberg audit has its own operation and evidence contract.
    Keeping this separate from :func:`reviewed_external_link_proposals`
    prevents the established Wikiquote/Wikisource apply path from accepting a
    different identity source by accident.
    """
    if not isinstance(report, dict):
        _report_error("Project Gutenberg audit report must be a JSON object")
    if report.get("audit_schema_version") != 1:
        _report_error("Project Gutenberg audit report has an unsupported audit_schema_version")
    if report.get("operation") != "project-gutenberg-external-links-audit":
        _report_error("Report is not a Project Gutenberg external-links audit")
    if report.get("mode") != "dry-run":
        _report_error("Only a dry-run Project Gutenberg audit report may be applied")

    identity_source = report.get("identity_source")
    if not isinstance(identity_source, dict) or (
        identity_source.get("wikidata_property")
        != PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY
    ) or (
        identity_source.get("formatter_url")
        != PROJECT_GUTENBERG_AUTHOR_URL_FORMATTER
    ):
        _report_error("Project Gutenberg audit report has unexpected identity-source metadata")

    counts = report.get("counts")
    if not isinstance(counts, dict):
        _report_error("Project Gutenberg audit report is missing counts")
    count_keys = (
        "records_with_usable_wikidata_qid",
        "records_missing_usable_wikidata_qid",
        "records_with_authoritative_gutenberg_identifier",
        "proposed_new_project_gutenberg_links",
        "records_with_no_gutenberg_identifier",
        "ambiguous_multiple_identifiers",
        "invalid_identifiers",
        "invalid_resulting_urls",
        "valid_existing_project_gutenberg_links",
        "invalid_existing_project_gutenberg_links",
        "conflicts",
        "records_with_no_proposed_change",
    )
    for key in count_keys:
        value = counts.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _report_error("Project Gutenberg audit report has invalid count: {}".format(key))

    total = report.get("total_canonical_records")
    eligible = report.get("semantically_eligible_philosopher_records")
    skipped = report.get("non_eligible_records_skipped")
    for label, value in (("total_canonical_records", total),
                         ("semantically_eligible_philosopher_records", eligible),
                         ("non_eligible_records_skipped", skipped)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _report_error("Project Gutenberg audit report has invalid {}".format(label))
    if total != eligible + skipped:
        _report_error("Project Gutenberg audit report has inconsistent eligibility counts")
    if (
        counts["records_with_usable_wikidata_qid"]
        + counts["records_missing_usable_wikidata_qid"]
        != eligible
    ):
        _report_error("Project Gutenberg audit report has inconsistent QID counts")
    failures = report.get("lookup_failures_by_reason")
    if not isinstance(failures, dict) or any(
        not isinstance(reason, str)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
        for reason, count in failures.items()
    ):
        _report_error("Project Gutenberg audit report has malformed lookup failures")
    if (
        counts["records_with_authoritative_gutenberg_identifier"]
        + counts["records_with_no_gutenberg_identifier"]
        + counts["ambiguous_multiple_identifiers"]
        + counts["invalid_identifiers"]
        + sum(failures.values())
        != counts["records_with_usable_wikidata_qid"]
    ):
        _report_error("Project Gutenberg audit report has inconsistent identifier counts")
    if (
        counts["records_with_authoritative_gutenberg_identifier"]
        != counts["proposed_new_project_gutenberg_links"]
        + counts["valid_existing_project_gutenberg_links"]
        + counts["conflicts"]
    ):
        _report_error("Project Gutenberg audit report has inconsistent proposal counts")
    if (
        counts["records_with_no_proposed_change"]
        != eligible
        - counts["proposed_new_project_gutenberg_links"]
        - counts["conflicts"]
    ):
        _report_error("Project Gutenberg audit report has inconsistent unchanged count")

    counted_lists = (
        ("records_missing_usable_wikidata_qid", "records_missing_usable_wikidata_qid"),
        ("records_with_no_gutenberg_identifier", "records_with_no_gutenberg_identifier"),
        ("ambiguous_multiple_identifiers", "ambiguous_multiple_identifiers"),
        ("invalid_identifiers", "invalid_identifiers"),
        ("invalid_resulting_urls", "invalid_resulting_urls"),
        ("valid_existing_project_gutenberg_links", "valid_existing_project_gutenberg_links"),
        ("invalid_existing_project_gutenberg_links", "invalid_existing_project_gutenberg_links"),
        ("conflicts", "conflicts"),
        ("skipped_non_eligible_records", None),
    )
    for report_key, count_key in counted_lists:
        value = report.get(report_key)
        expected = skipped if count_key is None else counts[count_key]
        if not isinstance(value, list) or len(value) != expected:
            _report_error("Project Gutenberg audit report has inconsistent {}".format(report_key))

    safety_lists = (
        ("conflicts", "conflicts"),
        ("ambiguous_multiple_identifiers", "ambiguous multiple identifiers"),
        ("invalid_identifiers", "invalid identifiers"),
        ("invalid_resulting_urls", "invalid resulting URLs"),
        ("invalid_existing_project_gutenberg_links", "invalid existing Gutenberg links"),
    )
    for key, description in safety_lists:
        value = report.get(key)
        if not isinstance(value, list) or len(value) != counts[key]:
            _report_error("Project Gutenberg audit report has inconsistent {}".format(description))
        if value:
            _report_error("Project Gutenberg audit report contains {}".format(description))
    if failures:
        _report_error("Project Gutenberg audit report contains lookup failures")

    rows = report.get("changes_or_conflicts")
    if not isinstance(rows, list):
        _report_error("Project Gutenberg audit report changes_or_conflicts must be a list")

    proposals = []
    seen_titles = set()
    for row in rows:
        if not isinstance(row, dict):
            _report_error("Project Gutenberg audit report contains a malformed proposal row")
        title = row.get("title")
        if not isinstance(title, str) or not title:
            _report_error("Project Gutenberg audit report proposal title must be non-empty")
        if title in seen_titles:
            _report_error("Project Gutenberg audit report has duplicate proposal title: {}".format(title))
        seen_titles.add(title)
        qid = row.get("qid")
        if not isinstance(qid, str) or not _QID_RE.fullmatch(qid):
            _report_error("Project Gutenberg audit report has invalid QID for {}".format(title))
        current = row.get("current")
        proposed = row.get("proposed")
        evidence = row.get("evidence")
        status = row.get("project_gutenberg")
        if not isinstance(current, dict) or current.get("project_gutenberg") is not None:
            _report_error("Project Gutenberg audit report has non-null reviewed target for {}".format(title))
        if not isinstance(proposed, dict) or set(proposed) != {"project_gutenberg"}:
            _report_error("Project Gutenberg audit report has malformed proposal for {}".format(title))
        if not isinstance(status, dict) or status.get("status") != "proposed" or status.get("reason") is not None:
            _report_error("Project Gutenberg audit report has non-proposal row for {}".format(title))
        if not isinstance(evidence, dict):
            _report_error("Project Gutenberg audit report is missing evidence for {}".format(title))
        author_id = evidence.get("raw_project_gutenberg_author_id")
        expected_url = project_gutenberg_author_url(author_id)
        if (
            evidence.get("wikidata_property") != PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY
            or evidence.get("formatter_url") != PROJECT_GUTENBERG_AUTHOR_URL_FORMATTER
            or expected_url is None
            or evidence.get("constructed_author_url") != expected_url
            or proposed.get("project_gutenberg") != expected_url
        ):
            _report_error("Project Gutenberg audit report has invalid P1938 evidence for {}".format(title))
        final_url = evidence.get("verified_final_url")
        if final_url is not None and final_url != expected_url:
            _report_error("Project Gutenberg audit report has unexpected verified URL for {}".format(title))
        proposals.append({
            "title": title,
            "qid": qid,
            "project_gutenberg": expected_url,
        })

    if not proposals:
        _report_error("Project Gutenberg audit report contains no proposals")
    if len(proposals) != counts["proposed_new_project_gutenberg_links"]:
        _report_error("Project Gutenberg audit report proposal rows do not match proposal count")
    return proposals


def validate_reviewed_project_gutenberg_apply(database, report):
    """Fail closed unless every reviewed P1938 proposal remains applicable."""
    if not isinstance(database, dict):
        raise TypeError("database must be a title-to-entry dictionary")
    proposals = reviewed_project_gutenberg_proposals(report)
    for proposal in proposals:
        title = proposal["title"]
        entry = database.get(title)
        if not isinstance(entry, dict):
            _report_error("Reviewed title no longer exists: {}".format(title))
        if entry.get("title") != title:
            _report_error("Current entry title does not match reviewed title: {}".format(title))
        if _qid_for_entry(entry) != proposal["qid"]:
            _report_error("Current QID differs from reviewed QID for {}".format(title))
        if not is_semantically_postable_philosopher(entry):
            _report_error("Reviewed title is no longer semantically eligible: {}".format(title))
        stored = entry.get("external_links")
        if stored is not None and not isinstance(stored, dict):
            _report_error("Current external_links is malformed for {}".format(title))
        current_value = stored.get("project_gutenberg") if isinstance(stored, dict) else None
        if current_value is not None:
            _report_error("Current project_gutenberg is no longer null for {}".format(title))
    return proposals


def apply_reviewed_project_gutenberg_links(
    database,
    report,
    filename,
    data_folder,
    persistence_lock,
):
    """Atomically write only reviewed Project Gutenberg author-link fields."""
    proposals = validate_reviewed_project_gutenberg_apply(database, report)
    candidate_database = deepcopy(database)
    applied_changes = []
    for proposal in proposals:
        entry = candidate_database[proposal["title"]]
        links = entry.get("external_links")
        if links is None:
            # Preserve the historical absence of unrelated reading-link keys.
            links = {}
            entry["external_links"] = links
        links["project_gutenberg"] = proposal["project_gutenberg"]
        applied_changes.append({
            "title": proposal["title"],
            "qid": proposal["qid"],
            "previous_project_gutenberg": None,
            "project_gutenberg": proposal["project_gutenberg"],
        })

    database_hash = rewrite_database(
        candidate_database, filename, data_folder, persistence_lock
    )
    # Do not change the caller's view until the validated rewrite succeeds.
    database.clear()
    database.update(candidate_database)
    return {
        "reviewed_proposal_count": len(proposals),
        "records_updated": len(applied_changes),
        "project_gutenberg_links_written": len(applied_changes),
        "applied_changes": applied_changes,
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


def audit_project_gutenberg_links(
    database,
    limiter=None,
    wikidata_lookup=get_wikidata_entities_batch,
    batch_size=50,
):
    """Read-only P1938 audit for Project Gutenberg author landing links.

    Unlike the Wikiquote/Wikisource audit, this uses stable semantic
    philosopher eligibility. It never searches Gutenberg by name, never
    requests Wikiquote, and never mutates a canonical entry.
    """
    if not isinstance(database, dict):
        raise TypeError("database must be a title-to-entry dictionary")
    if not isinstance(batch_size, int) or batch_size <= 0:
        raise ValueError("batch_size must be positive")

    eligible_rows = []
    skipped_records = []
    missing_qid = []
    qids = []
    invalid_existing = []

    for title in sorted(database):
        entry = database[title]
        if not is_semantically_postable_philosopher(entry):
            skipped_records.append({"title": title})
            continue

        current = external_links_for_entry(entry)["project_gutenberg"]
        qid = _qid_for_entry(entry)
        row = {
            "title": title,
            "qid": qid,
            "current": {"project_gutenberg": current},
            "proposed": {"project_gutenberg": None},
            "evidence": None,
            "project_gutenberg": {"status": "pending_lookup", "reason": None},
        }
        if current is not None and not is_project_gutenberg_author_url(current):
            invalid_existing.append({
                "title": title,
                "qid": qid,
                "project_gutenberg": current,
            })
        if qid is None:
            row["project_gutenberg"] = {
                "status": "unavailable", "reason": "missing_qid"
            }
            missing_qid.append({"title": title, "current": row["current"]})
        else:
            qids.append(qid)
        eligible_rows.append(row)

    entities, lookup_failures = _batch_wikidata(
        sorted(set(qids)), limiter, wikidata_lookup, batch_size
    )
    failure_counts = Counter()
    no_identifier = []
    ambiguous = []
    invalid_identifiers = []
    invalid_urls = []
    conflicts = []
    existing_valid = []
    detail_rows = []

    for row in eligible_rows:
        qid = row["qid"]
        current = row["current"]["project_gutenberg"]
        if qid is None:
            continue
        failure = lookup_failures.get(qid)
        if failure is not None:
            reason = "wikidata_{}".format(failure)
            row["project_gutenberg"] = {"status": "unavailable", "reason": reason}
            failure_counts[reason] += 1
            continue

        resolution = project_gutenberg_author_identifier_resolution(entities.get(qid))
        status = resolution["status"]
        if status == "missing":
            row["project_gutenberg"] = {"status": "unavailable", "reason": "no_p1938"}
            no_identifier.append({"title": row["title"], "qid": qid})
            continue
        if status == "invalid":
            row["project_gutenberg"] = {
                "status": "unavailable", "reason": "invalid_p1938"
            }
            invalid_identifiers.append({
                "title": row["title"], "qid": qid,
                "identifiers": resolution["identifiers"],
                "invalid_values": resolution["invalid_values"],
            })
            detail_rows.append(row)
            continue
        if status == "ambiguous":
            row["project_gutenberg"] = {
                "status": "ambiguous", "reason": "multiple_p1938"
            }
            ambiguous.append({
                "title": row["title"], "qid": qid,
                "identifiers": resolution["identifiers"],
            })
            detail_rows.append(row)
            continue

        author_id = resolution["identifiers"][0]
        url = project_gutenberg_author_url(author_id)
        if not is_project_gutenberg_author_url(url):
            row["project_gutenberg"] = {
                "status": "unavailable", "reason": "invalid_constructed_url"
            }
            invalid_urls.append({
                "title": row["title"], "qid": qid,
                "author_id": author_id,
                "url": url,
            })
            detail_rows.append(row)
            continue

        row["evidence"] = {
            "wikidata_property": PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY,
            "raw_project_gutenberg_author_id": author_id,
            "formatter_url": PROJECT_GUTENBERG_AUTHOR_URL_FORMATTER,
            "constructed_author_url": url,
            "verified_final_url": None,
        }
        if current is None:
            row["proposed"]["project_gutenberg"] = url
            row["project_gutenberg"] = {"status": "proposed", "reason": None}
            detail_rows.append(row)
        elif current == url:
            row["project_gutenberg"] = {"status": "preserved", "reason": None}
            existing_valid.append({
                "title": row["title"], "qid": qid,
                "project_gutenberg": current,
            })
        else:
            row["project_gutenberg"] = {
                "status": "conflict", "reason": "stored_url_differs_from_p1938"
            }
            conflict = {
                "title": row["title"], "qid": qid,
                "current": current, "observed": url,
            }
            conflicts.append(conflict)
            detail_rows.append(row)

    proposals = sum(
        row["proposed"]["project_gutenberg"] is not None for row in eligible_rows
    )
    return {
        "audit_schema_version": 1,
        "mode": "dry-run",
        "operation": "project-gutenberg-external-links-audit",
        "identity_source": {
            "wikidata_property": PROJECT_GUTENBERG_AUTHOR_ID_PROPERTY,
            "formatter_url": PROJECT_GUTENBERG_AUTHOR_URL_FORMATTER,
        },
        "total_canonical_records": len(database),
        "semantically_eligible_philosopher_records": len(eligible_rows),
        "non_eligible_records_skipped": len(skipped_records),
        "counts": {
            "records_with_usable_wikidata_qid": len(eligible_rows) - len(missing_qid),
            "records_missing_usable_wikidata_qid": len(missing_qid),
            "records_with_authoritative_gutenberg_identifier": sum(
                row["evidence"] is not None for row in eligible_rows
            ),
            "proposed_new_project_gutenberg_links": proposals,
            "records_with_no_gutenberg_identifier": len(no_identifier),
            "ambiguous_multiple_identifiers": len(ambiguous),
            "invalid_identifiers": len(invalid_identifiers),
            "invalid_resulting_urls": len(invalid_urls),
            "valid_existing_project_gutenberg_links": len(existing_valid),
            "invalid_existing_project_gutenberg_links": len(invalid_existing),
            "conflicts": len(conflicts),
            "records_with_no_proposed_change": (
                len(eligible_rows) - proposals - len(conflicts)
            ),
        },
        "lookup_failures_by_reason": dict(sorted(failure_counts.items())),
        "records_missing_usable_wikidata_qid": missing_qid,
        "records_with_no_gutenberg_identifier": no_identifier,
        "ambiguous_multiple_identifiers": ambiguous,
        "invalid_identifiers": invalid_identifiers,
        "invalid_resulting_urls": invalid_urls,
        "valid_existing_project_gutenberg_links": existing_valid,
        "invalid_existing_project_gutenberg_links": invalid_existing,
        "conflicts": conflicts,
        "skipped_non_eligible_records": skipped_records,
        "changes_or_conflicts": detail_rows,
    }


def format_project_gutenberg_audit_summary(report, report_path=None):
    """Return a concise, credential-free Project Gutenberg audit summary."""
    counts = report["counts"]
    text = (
        "Project Gutenberg audit: scanned {total}; semantically eligible {eligible}; "
        "skipped {skipped}; P1938 identifiers {identifiers}; proposals {proposals}; "
        "no identifier {missing}; ambiguous {ambiguous}; conflicts {conflicts}".format(
            total=report["total_canonical_records"],
            eligible=report["semantically_eligible_philosopher_records"],
            skipped=report["non_eligible_records_skipped"],
            identifiers=counts["records_with_authoritative_gutenberg_identifier"],
            proposals=counts["proposed_new_project_gutenberg_links"],
            missing=counts["records_with_no_gutenberg_identifier"],
            ambiguous=counts["ambiguous_multiple_identifiers"],
            conflicts=counts["conflicts"],
        )
    )
    if report_path is not None:
        text += "; report {}".format(report_path)
    return text
