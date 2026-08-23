import re
import os
from dotenv import load_dotenv


def load_environment():
    """Load environment values only from an explicit startup boundary."""
    load_dotenv()


def get_telegram_settings():
    """Return current Telegram settings after explicit environment loading."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    telegram_url = (
        "https://api.telegram.org/bot{}/sendMessage".format(token)
        if token
        else None
    )

    return telegram_url, chat_id


def get_recent_death_telegram_settings():
    """Return the explicitly configured private destination for death alerts."""
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("RECENT_DEATH_TELEGRAM_CHAT_ID")
    telegram_url = (
        "https://api.telegram.org/bot{}/sendMessage".format(token)
        if token
        else None
    )
    return telegram_url, chat_id


def get_wikimedia_user_agent():
    """Return the project user agent, optionally including public contact."""
    contact = os.getenv("WIKIMEDIA_USER_AGENT_CONTACT", "").strip()
    if contact:
        return "WikiScraperBot/3.0 ({})".format(contact)
    return "WikiScraperBot/3.0"

# URLs
WIKIPEDIA_URL = "https://en.wikipedia.org/w/api.php"
SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WIKIDATA_URL = "https://www.wikidata.org/w/api.php"
WIKIQUOTE_URL = "https://en.wikiquote.org/wiki/"
# Version 10 admits structural parent work only for compatible citation
# context, rejects locator-like pseudo-years, and deduplicates contained
# hierarchy locators.
CURRENT_QUOTE_PARSER_VERSION = 10
# The canonical runtime database and legacy migration inputs are separate.
# Runtime code must never write files in LEGACY_DATA_FOLDER.
CANONICAL_DATA_FOLDER = "data"
LEGACY_DATA_FOLDER = "data/legacy"
SUMMARY_FILE = "summaries.jsonl"
ENTITY_FILE = "entities.jsonl"
QUOTE_FILE = "quotes.jsonl"
QUOTE_FAILURE_FILE = "quote_failures.jsonl"
RESULT_FILE = "results.jsonl"
PROCESSED_FILE = "processed.jsonl"
POSTED_FILE = "posted.json"

DATABASE_FILE = "database.jsonl"
DATABASE_BACKUP_FOLDER = "backups/database"
MIGRATION_BACKUP_FOLDER = "backups/migration"
OPERATIONAL_BACKUP_RETENTION_DAYS = 90

RUN_REPORT_FOLDER = "reports/runs"
QUOTE_REFRESH_REPORT_FOLDER = "reports/quote-refresh"
WIKIDATA_DATE_REFRESH_REPORT_FOLDER = "reports/wikidata-date-refresh"
PURGE_REPORT_FOLDER = "reports/purge"
RECENT_DEATH_REPORT_FOLDER = "reports/recent-deaths"

# Version 2 adds conservative direct-biographical philosopher evidence and
# neutral Wikidata absence-of-claim semantics.
CURRENT_EVALUATION_ALGORITHM_VERSION = 2

LOG_FILE = "bot.log"

# API
# Kept as a safe, contact-free default for import-time compatibility. HTTP
# requests call get_wikimedia_user_agent() after runtime configuration loads.
USER_AGENT = "WikiScraperBot/3.0"
REQUEST_TIMEOUT = 20
MAX_RETRIES = 5
RATE_LIMIT = 2
INITIAL_BACKOFF = 2
MAX_BACKOFF = 300

# Other settings
SRLIMIT = 100
MAX_PAGES=1000
SAVE_EVERY = 100
MAX_WORKERS=5
CHUNK_SIZE=50
MAX_QUOTES=5

# Text processing
SEARCH_TERM = "philosopher"

YEAR_RE = re.compile(r"\d{4}")
YEAR_END_RE = re.compile(r"\(\d{4}\)\s*$")

# Exclusion lists for filtering out non-philosophers -- TO DO: TRANSFER TO FILES
EXCLUDE_WORD = [
    "list", 
    "category", 
    "template", 
    "disambiguation", 
    "timeline", 
    "philosophy", 
    "psychology", 
    "economy", 
    "literature", 
    "nobel", 
    "book", 
    "novel", 
    "critique", 
    "film", 
    "album", 
    "symphony", 
    "theory", 
    "experiment", 
    "paradox", 
    "problem", 
    "masterpiece", 
    "utility", 
    "analogy", 
    "allegory", 
    "machine", 
    "bibliography", 
    "element", 
    "mathematician", 
    "writer", 
    "poet", 
    "priest", 
    "neurophysiologist", 
    "the philosopher", 
    "new philosopher", 
    "philosophers", 
    "philosopher's", 
    "a philosopher", 
    "old man", 
    "kings", 
    "king", 
    "emperor", 
    "alchemist", 
    "pirate", 
    "faith", 
    "disbelief", 
    "concept", 
    "thought", 
    "ways", 
    "meditation", 
    "lecturing", 
    "generation", 
    "order", 
    "object", 
    "position", 
    "republic", 
    "democracy", 
    "anarchy", 
    "apology", 
    "clouds", 
    "god", 
    "deities", 
    "zombie", 
    "century", 
    "university", 
    "college", 
    "library", 
    "city", 
    "haus", 
    "why", 
    "and", 
    "as", 
    "or", 
    "no true", 
    "world", 
    "possible", 
    "dimensional", 
    "itself", 
    "relation", 
    "trial"
]

escaped = [re.escape(w.lower()) for w in EXCLUDE_WORD]

EXCLUDE_RE_TITLE = re.compile(
    r"\b(?:"
    + "|".join(escaped)
    + r")\b"
)

# Exclusion lists for filtering out non-quotes -- TO DO: TRANSFER TO A FILE
QUOTE_BAD_STARTS = [
    "popular usage:",
    "misattributed:",
    "unsourced",
    "letter to",
    "postcard to",
    "draft for",
    "from a letter",
    "from an interview",
    "from the interview",
    "from the speech",
    "from the lecture",
    "from the introduction",
    "objecting to",
    "source:",
    "translation:",
    "variant:",
    "notebook",
    "quotes about",
]

QUOTE_BAD_CONTAINS = [
    "specific citation needed",
    "retrieved",
    "translated by",
    "translation",
    "quotations",
    "quoted in",
    "editor",
    "isbn",
    "chapter",
    " tr. ",
    " vol. ",
]

NON_HUMAN_PATTERNS = [
    r"\bis a book\b",
    r"\bwas a book\b",
    r"\bis a novel\b",
    r"\bwas a novel\b",
    r"\bis a film\b",
    r"\bwas a film\b",
    r"\bis a concept\b",
    r"\bwas a concept\b",
    r"\bis a philosophy\b",
    r"\bwas a philosophy\b",
    r"\bis a school\b",
    r"\bwas a school\b",
]
