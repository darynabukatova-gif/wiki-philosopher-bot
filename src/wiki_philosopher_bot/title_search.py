"""Read-only canonical-title search for manual posting workflows."""

import unicodedata

from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.utils import is_posting_candidate


def normalize_title_search_text(value, *, strip_diacritics=False):
    """Return predictable Unicode-normalized text for title lookup.

    This is deliberately search convenience only.  It never changes the
    stored title and is not used by exact-title posting preparation.
    """
    if not isinstance(value, str):
        raise TypeError("title search text must be a string")
    normalized = unicodedata.normalize("NFKD", value)
    if strip_diacritics:
        normalized = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
    return unicodedata.normalize("NFC", normalized).casefold()


def _match_rank(title, query):
    direct_title = normalize_title_search_text(title)
    direct_query = normalize_title_search_text(query)
    folded_title = normalize_title_search_text(title, strip_diacritics=True)
    folded_query = normalize_title_search_text(query, strip_diacritics=True)

    if direct_title == direct_query:
        return 0
    if folded_title == folded_query:
        return 1
    if direct_title.startswith(direct_query) or folded_title.startswith(folded_query):
        return 2
    if direct_query in direct_title or folded_query in folded_title:
        return 3
    return None


def find_canonical_titles(database, query, *, include_all=False):
    """Return matching canonical records in exact, deterministic rank order.

    By default, only records satisfying the existing live posting predicate
    are returned.  ``include_all`` is diagnostic-only and includes every
    structurally valid canonical title.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must contain non-whitespace text")
    if not isinstance(database, dict):
        raise TypeError("database must be a title-indexed dictionary")

    matches = []
    for title, entry in database.items():
        if not isinstance(title, str) or not isinstance(entry, dict):
            continue
        rank = _match_rank(title, query)
        if rank is None:
            continue
        eligible = is_posting_candidate(entry)
        if not include_all and not eligible:
            continue
        matches.append((rank, normalize_title_search_text(title), title, entry, eligible))

    matches.sort(key=lambda match: (match[0], match[1], match[2]))
    return [
        {
            "title": title,
            "entry": entry,
            "rank": rank,
            "eligible": eligible,
        }
        for rank, _normalized_title, title, entry, eligible in matches
    ]


def posting_ineligibility_reasons(entry):
    """Provide compact diagnostic context without defining a new predicate."""
    if is_posting_candidate(entry):
        return []

    reasons = []
    evaluation = entry.get("evaluation") if isinstance(entry, dict) else None
    quotes = entry.get("quotes") if isinstance(entry, dict) else None
    posting = entry.get("posting") if isinstance(entry, dict) else None
    if not isinstance(evaluation, dict) or evaluation.get("status") != "accepted":
        reasons.append("evaluation is not accepted")
    if not isinstance(quotes, dict) or quotes.get("status") != "available":
        reasons.append("quotes are not available")
    elif not isinstance(quotes.get("items"), list) or not quotes["items"]:
        reasons.append("no stored quotes")
    elif quotes.get("parser_version") != CURRENT_QUOTE_PARSER_VERSION:
        reasons.append("quote parser version is missing or stale")
    if not isinstance(posting, dict) or posting.get("has_been_posted") is not False:
        reasons.append("already posted")
    return reasons or ["does not satisfy current posting requirements"]
