"""Read-only summary-quality diagnostics scoped to semantic philosophers.

The audit deliberately reports suspicious source text without rewriting it.
It uses only narrow punctuation-boundary signals seen in upstream extracts;
it is not a whitespace-normalization or data-repair mechanism.
"""

from __future__ import annotations

import re

from wiki_philosopher_bot.utils import is_semantically_postable_philosopher


_SUMMARY_QUALITY_SIGNALS = (
    (
        "fused_sentence_boundary",
        # Require a capitalized next word, not an abbreviation boundary such
        # as ``Ph.D.``.
        re.compile(r"(?<=[a-z])\.(?=[A-Z][a-z])"),
    ),
    (
        "fused_colon_boundary",
        re.compile(r":(?=[A-Z])"),
    ),
)


def summary_quality_findings(summary_text):
    """Return narrow, non-mutating suspicious-boundary findings for text."""
    if not isinstance(summary_text, str) or not summary_text:
        return []

    findings = []
    for kind, pattern in _SUMMARY_QUALITY_SIGNALS:
        match = pattern.search(summary_text)
        if match is None:
            continue
        start = max(0, match.start() - 40)
        end = min(len(summary_text), match.end() + 80)
        findings.append({
            "kind": kind,
            "excerpt": summary_text[start:end],
        })
    return findings


def audit_summary_quality(database):
    """Audit summaries without changing canonical entries or their fields.

    Findings for semantic philosophers are actionable because those records
    can participate in presentation.  The same signals on rejected/nonhuman
    records are retained only as transparent informational counts.
    """
    if not isinstance(database, dict):
        raise TypeError("database must be a title-to-entry dictionary")

    actionable_findings = []
    skipped_finding_count = 0
    semantically_postable_records = 0

    for title in sorted(database):
        entry = database[title]
        summary = entry.get("summary") if isinstance(entry, dict) else None
        text = summary.get("text") if isinstance(summary, dict) else None
        findings = summary_quality_findings(text)

        if is_semantically_postable_philosopher(entry):
            semantically_postable_records += 1
            if findings:
                actionable_findings.append({
                    "title": title,
                    "findings": findings,
                })
        else:
            skipped_finding_count += len(findings)

    return {
        "total_canonical_records": len(database),
        "semantically_postable_records_checked": semantically_postable_records,
        "rejected_or_non_postable_records_skipped": (
            len(database) - semantically_postable_records
        ),
        "suspicious_summary_findings": actionable_findings,
        "suspicious_summary_record_count": len(actionable_findings),
        "informational_skipped_summary_finding_count": skipped_finding_count,
    }
