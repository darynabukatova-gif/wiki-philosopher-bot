import os
import re
import math
import time
import random
import threading
from config import (
    MAX_BACKOFF, 
    INITIAL_BACKOFF, 
    EXCLUDE_RE_TITLE, 
    QUOTE_BAD_CONTAINS, 
    QUOTE_BAD_STARTS, 
    YEAR_END_RE,
    CURRENT_QUOTE_PARSER_VERSION,
)

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


def candidate_selection_weight(entry):
    """Return a positive content-only selection weight for one candidate."""
    raw_content = entry["evaluation"].get("content_confidence")

    if not isinstance(raw_content, int) or isinstance(raw_content, bool):
        raw_content = -1

    return max(raw_content, -1) + 2

def get_random_philosopher(
    database,
    chooser=random.choices,
):
    philosophers = [
        entry
        for entry in database.values()
        if isinstance(entry, dict)
        and isinstance(entry.get("title"), str)
        and isinstance(entry.get("evaluation"), dict)
        and entry["evaluation"].get("status") == "accepted"
        and isinstance(entry.get("quotes"), dict)
        and entry["quotes"].get("status") == "available"
        and isinstance(entry["quotes"].get("items"), list)
        and entry["quotes"]["items"]
        and entry["quotes"].get("parser_version")
        == CURRENT_QUOTE_PARSER_VERSION
        and isinstance(entry.get("posting"), dict)
        and entry["posting"].get("has_been_posted") is False
    ]

    if not philosophers:
        return None

    weights = [
        candidate_selection_weight(entry)
        for entry in philosophers
    ]

    return chooser(philosophers, weights=weights, k=1)[0]
