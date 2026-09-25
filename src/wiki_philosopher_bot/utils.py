import os
import re
import math
import time
import random
import threading
from wiki_philosopher_bot.config import (
    MAX_BACKOFF, 
    INITIAL_BACKOFF, 
    EXCLUDE_RE_TITLE, 
    QUOTE_BAD_CONTAINS, 
    QUOTE_BAD_STARTS, 
    YEAR_END_RE,
    CURRENT_QUOTE_PARSER_VERSION,
)
from wiki_philosopher_bot.database_schema import is_valid_external_link

class RateLimiter:

    def __init__(self, rate_per_sec):
        self.interval = 1.0 / rate_per_sec
        self.last = 0
        self.lock = threading.Lock()

    def wait(self):

        with self.lock:

            now = time.time()

            elapsed = now - self.last

            if elapsed < self.interval:
                time.sleep(self.interval - elapsed)

            self.last = time.time()
            
def get_data_path(filename, data_folder):
    return os.path.join(data_folder, filename)

def chunk_list(lst, size):
    for i in range(0, len(lst), size):
        yield lst[i:i + size]

def calculate_backoff(attempt):
    return min(
        MAX_BACKOFF,
        INITIAL_BACKOFF * (2 ** attempt)
    )

# Text processing functions
def clean_title(title):
    # remove disambiguation parentheses
    title = re.sub(r"\s*\([^)]*\)", "", title)
    return title.strip()

def normalize(title):
    return title.lower().replace("_", " ").strip()

def should_exclude_word(text):
    return bool(EXCLUDE_RE_TITLE.search(text.lower()))

def should_exclude_part(text, keywords):
    text_lower = text.lower()
    return any(word in text_lower for word in keywords)

def extract_clean_text(li):

    # Remove nested lists
    for nested in li.find_all(["ul", "ol", "dl"]):
        nested.decompose()

    # Remove references
    for sup in li.find_all("sup"):
        sup.decompose()

    text = li.get_text(" ", strip=True)

    return text

def is_bad_quote(text):

    text = re.sub(r"\[\d+\]", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    text_lower = text.lower().strip()

    # Starts with metadata
    if any(text_lower.startswith(x) for x in QUOTE_BAD_STARTS):
        return True

    # Contains metadata markers
    if any(x in text_lower for x in QUOTE_BAD_CONTAINS):
        return True

    # Probably bibliography/citation
    if len(text.split()) < 8:
        return True

    # bibliography-like ending
    if YEAR_END_RE.search(text):
        return True

    # too short
    if len(text.split()) < 8:
        return True

    # usually metadata, not quotes
    if text.count(",") > 6:
        return True

    # no sentence punctuation
    if "." not in text and "!" not in text and "?" not in text:
        return True

    # Too short
    if len(text) < 40:
        return True

    # Too long
    if len(text) > 400:
        return True

    # Meta
    if "toggle" in text_lower:
        return True

    return False

def is_accepted_record(record):
    status = record.get("status")

    if status in ("accepted", "rejected"):
        return status == "accepted"

    return record.get("accepted") is True


def is_rejected_record(record):
    status = record.get("status")

    if status in ("accepted", "rejected"):
        return status == "rejected"

    return record.get("accepted") is False


def has_usable_posting_summary(entry):
    """Whether an entry has the required stored Wikipedia post core.

    The summary-quality audit remains deliberately separate: its heuristic
    findings are not a posting blocker and this helper never rewrites text.
    """
    if not isinstance(entry, dict):
        return False
    summary = entry.get("summary")
    if not isinstance(summary, dict) or summary.get("source") != "Wikipedia":
        return False
    text = summary.get("text")
    return (
        isinstance(text, str)
        and bool(text.strip())
        and text.strip() != "No summary available."
    )


def has_current_usable_quotes(entry):
    """Whether an entry has a current cached quote set safe to select from.

    This is intentionally data-only. It never calls the fetch-capable quote
    API for failed, empty, or stale caches.
    """
    if not isinstance(entry, dict):
        return False
    quotes = entry.get("quotes")
    return (
        isinstance(quotes, dict)
        and quotes.get("status") == "available"
        and isinstance(quotes.get("items"), list)
        and bool(quotes["items"])
        and quotes.get("parser_version") == CURRENT_QUOTE_PARSER_VERSION
    )


def candidate_selection_weight(entry):
    """Return the stored-data-only summary-first selection weight.

    Every eligible philosopher starts with the established content-confidence
    base. A current quote adds modest enrichment; Wikisource and Gutenberg
    receive larger fixed bonuses for additional reading value. Wikiquote is
    intentionally not a separate bonus because it is highly correlated with
    current quote availability and would double-count that signal.
    """
    raw_content = entry["evaluation"].get("content_confidence")

    if not isinstance(raw_content, int) or isinstance(raw_content, bool):
        raw_content = -1

    weight = max(raw_content, -1) + 2
    if has_current_usable_quotes(entry):
        weight += 1
    external_links = entry.get("external_links")
    if isinstance(external_links, dict):
        if (
            external_links.get("wikisource")
            and is_valid_external_link("wikisource", external_links.get("wikisource"))
        ):
            weight += 2
        if (
            external_links.get("project_gutenberg")
            and is_valid_external_link(
                "project_gutenberg", external_links.get("project_gutenberg")
            )
        ):
            weight += 2
    return weight


def is_semantically_postable_philosopher(entry):
    """Whether an entry passed the durable philosopher-evaluation decision.

    This intentionally differs from :func:`is_posting_candidate`: it does not
    depend on quote-cache freshness, quote availability, or whether the
    philosopher has already been posted.  Read-only quality diagnostics use
    this stable semantic boundary so a posted philosopher remains relevant,
    while rejected topics, lists, and non-human entities do not create
    actionable presentation warnings.
    """
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and bool(entry["title"].strip())
        and isinstance(entry.get("evaluation"), dict)
        and entry["evaluation"].get("status") == "accepted"
    )


def is_posting_candidate(entry):
    """Whether *entry* satisfies the summary-first posting predicate.

    Keep this deliberately narrow and data-only: unresolved outbox attempts
    are a separate global operation guard, not candidate-selection semantics.
    Quote-specific enrichment audits preserve their own explicit historical
    scope rather than inheriting this broader posting predicate implicitly.
    """
    return (
        isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and isinstance(entry.get("evaluation"), dict)
        and entry["evaluation"].get("status") == "accepted"
        and has_usable_posting_summary(entry)
        and isinstance(entry.get("posting"), dict)
        and entry["posting"].get("has_been_posted") is False
    )

def get_random_philosopher(
    database,
    chooser=random.choices,
):
    philosophers = [
        entry
        for entry in database.values()
        if is_posting_candidate(entry)
    ]

    if not philosophers:
        return None

    weights = [
        candidate_selection_weight(entry)
        for entry in philosophers
    ]

    return chooser(philosophers, weights=weights, k=1)[0]
