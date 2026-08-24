import time
import random
import copy
import math
import re
from datetime import date
import requests
import threading
from typing import Optional
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
from dataclasses import dataclass
from wiki_philosopher_bot.cache import update_database_entry
from wiki_philosopher_bot.utils import (
    chunk_list, 
    calculate_backoff, 
    is_bad_quote,
)
from wiki_philosopher_bot.config import (
    DATABASE_FILE,
    INITIAL_BACKOFF, 
    MAX_BACKOFF, 
    MAX_QUOTES, 
    SUMMARY_URL, 
    get_wikimedia_user_agent,
    REQUEST_TIMEOUT, 
    MAX_RETRIES, 
    SRLIMIT, 
    MAX_PAGES, 
    WIKIQUOTE_URL, 
    CURRENT_QUOTE_PARSER_VERSION,
    WIKIPEDIA_URL, 
    WIKIDATA_URL, 
)

def get_request_headers():
    """Build request headers after environment configuration is available."""
    return {"User-Agent": get_wikimedia_user_agent()}

QUOTE_FAILURE_HTTP_404 = "http_404"
QUOTE_FAILURE_HTTP_429 = "http_429"
QUOTE_FAILURE_REQUEST_EXCEPTION = "request_exception"
QUOTE_FAILURE_PARSING_ERROR = "parsing_error"
QUOTE_FAILURE_NO_QUOTES_FOUND = "no_quotes_found"


_WIKIQUOTE_HEADING_TAGS = ("h2", "h3", "h4", "h5", "h6")
_WIKIQUOTE_EXCLUDED_SECTION_PREFIXES = (
    "quotes about",
    "about",
    "misattributed",
    "see also",
    "references",
    "external links",
    "further reading",
    "bibliography",
    "notes",
    "sources",
)
_WIKIQUOTE_GENERIC_QUOTE_HEADINGS = frozenset((
    "quotes", "sourced", "attributed", "unsourced",
))
_WIKIQUOTE_GENERIC_WORK_HEADINGS = _WIKIQUOTE_GENERIC_QUOTE_HEADINGS | frozenset((
    "interviews", "speeches", "letters", "lectures", "other",
    "others", "miscellaneous", "works", "general", "general sources",
    "other sources", "sources", "references", "further reading",
    "external links",
))
# Do not admit the leading digits of a Bekker/Stephanus locator (``1094b``),
# a decade (``1940s``), or an attached source/page token (``1929p. 178``)
# as a bibliographic year.
_WIKIQUOTE_YEAR_RE = re.compile(
    r"(?<!\d)(?:1[0-9]{3}|20[0-9]{2})(?![\dA-Za-z])"
)
_WIKIQUOTE_DATE_RE = re.compile(
    r"\b(?:\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}|(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s+\d{4}|\d{4}-\d{2}-\d{2})\b"
)
_WIKIQUOTE_DETAILS_RE = re.compile(
    r"(?:\b(?:Chapter|Ch\.)\s*\d+(?:[-–]\d+)?|"
    r"\bBook\s*(?:\d+|[IVXLCDM]+)|"
    r"\b(?:Page|Pages|p\.|pp\.)\s*\d+(?:[-–]\d+)?|"
    r"\b(?:Section|Sec\.|sec\.)\s*\d+(?:[-–]\d+)?|"
    r"\b(?:Volume|Vol\.)\s*(?:\d+|[IVXLCDM]+)|"
    r"\b(?:Letter|Lecture)(?:\s+\d+)?|§\s*[\d.]+)",
    re.IGNORECASE,
)
_WIKIQUOTE_TRANSLATOR_LOCATOR_RE = re.compile(
    r"^\s*(?P<locator>\d{1,4}[A-Za-z]?)\s*,\s*"
    r".+?,\s*(?:trans(?:lated by)?|ed(?:ited by)?|eds?)\.?\s*,",
    re.IGNORECASE,
)
_WIKIQUOTE_TRANSLATOR_WORK_RE = re.compile(
    r"^\s*\d{1,4}[A-Za-z]?\s*,\s*"
    r".+?,\s*(?:trans(?:lated by)?|ed(?:ited by)?|eds?)\.?\s*,\s*"
    r"(?P<work>.+?)\s*(?:\(\s*(?:1[0-9]{3}|20[0-9]{2})\s*\)|,\s*(?:1[0-9]{3}|20[0-9]{2})\b)",
    re.IGNORECASE,
)
_WIKIQUOTE_LOCATOR_IN_WORK_RE = re.compile(
    r"^\s*(?P<locator>\d{1,4}[A-Za-z]?)\s*,\s*in\s+"
    r"(?P<work>[^,;]+?)(?=\s*(?:,|\(|$))",
    re.IGNORECASE,
)
_WIKIQUOTE_CONTRIBUTOR_ROLE_RE = re.compile(
    r"\b(?:as\s+translated\s+by|translated\s+by|translator|"
    r"as\s+interpreted\s+by|interpreted\s+by|interpreter|"
    r"edited\s+by|editor|introduction\s+by|commentary\s+by|"
    r"trans\.?|ed\.?|eds\.?)(?=\s|,|\.|$)",
    re.IGNORECASE,
)
_WIKIQUOTE_SOURCE_LAYER_RE = re.compile(
    r"\b(?:as\s+cited\s+in|cited\s+in|also\s+quoted\s+in|"
    r"quoted\s+in|reprinted\s+in|reproduced\s+in)\b",
    re.IGNORECASE,
)
_WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE = re.compile(
    r"^\s*(?:as\s+attributed\s+in|attributed\s+in|"
    r"unverified\s+attribution\s+(?:noted\s+)?in|"
    r"commonly\s+attributed\s+in|reported\s+in|quoted\s+in)\b",
    re.IGNORECASE,
)
_WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE = re.compile(
    r"^\s*(?:quoted\s+by|as\s+quoted(?:\s+without\s+citation)?|"
    r"this\s+is\s+attributed\s+to|though\s+attributed\s+to|"
    r"attributed\s+to|reported\s+(?:by|in)|"
    r"commonly\s+attributed\s+(?:to|in))\b",
    re.IGNORECASE,
)
_WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE = re.compile(
    r"^\s*(?:this\s+may\s+have\s+arisen\s+as(?:\s+a\s+paraphrase\s+of)?|"
    r"paraphrase\s+of|quoted\s+from|on\s+(?:her|his|the)\s+work|"
    r"from\s+(?:remarks|comments)\s+on)\b",
    re.IGNORECASE,
)
_WIKIQUOTE_GENERIC_DOCUMENT_LABEL_RE = re.compile(
    r"^\s*(?:journal\s+entry|his\s+will)\s*$",
    re.IGNORECASE,
)
_WIKIQUOTE_GENERIC_DOCUMENT_CITATION_RE = re.compile(
    r"^\s*(?:journal\s+entry|his\s+will)(?:\b|\s*[,;:])",
    re.IGNORECASE,
)
_WIKIQUOTE_SIMPLE_TITLE_BY_AUTHOR_RE = re.compile(r"\s+by\s+\S", re.IGNORECASE)
_WIKIQUOTE_SIMPLE_TITLE_EVENT_PREFIX_RE = re.compile(
    r"^\s*(?:written\s+in\s+a\s+letter\s+to|in\s+a\s+letter\s+to|"
    r"written\s+to|letter\s+to|said\s+in\s+a\s+speech\s+to|"
    r"remarks\s+to|interview\s+with)\b",
    re.IGNORECASE,
)
_WIKIQUOTE_AUTHOR_DATE_LEAD_RE = re.compile(
    r"^\s*(?:"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+){1,3}|"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+(?:von|van|de|del|di|da|la|le)\s+"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+|"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+in\s+"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+)?"
    r")\s*$"
)
_WIKIQUOTE_AUTHOR_TITLE_RUN_RE = re.compile(
    r"^\s*[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+(?:My|His|Her|Their|The|A|An)\b"
)
_WIKIQUOTE_NAME_LIKE_LABEL_IN_WORK_RE = re.compile(
    r"^\s*(?:[\"“])?(?:[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+)(?:[\"”])?\s+in\s+"
    r"[^()]+?\s*\(\s*(?:1[0-9]{3}|20[0-9]{2})\s*\)",
)
_WIKIQUOTE_TITLE_YEAR_LOCATOR_TAIL_RE = re.compile(
    r"^\s*(?:(?:1[0-9]{3}|20[0-9]{2})\s*)?[\)\],.;:]*\s*"
    r"(?:p\.?|pp\.?|page(?:s)?|ch\.?|chapter|vol(?:ume)?\.?|"
    r"book|part|section|sec\.?|§)",
    re.IGNORECASE,
)
_WIKIQUOTE_PARENTHESES_CONTRIBUTOR_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:trans\.?|translated\s+by|ed\.?|eds\.?|"
    r"edited\s+by)\s+[^()]+\)\s*$",
    re.IGNORECASE,
)
_WIKIQUOTE_STRUCTURAL_PREFIX_RE = re.compile(
    r"^\s*(?:vol(?:ume)?\.?\s*(?:\d+|[ivxlcdm]+)|"
    r"book\s*(?:\d+|[ivxlcdm]+)|part\s*(?:\d+|[ivxlcdm]+)|"
    r"(?:chapter|ch\.)\s*(?:\d+|[ivxlcdm]+)|§\s*\d+|"
    r"sec\.?\s*\d+)",
    re.IGNORECASE,
)
_WIKIQUOTE_LEADING_PERSON_CITATION_RE = re.compile(
    r"^\s*(?:"
    r"(?:[A-Z]\.\s+){1,3}[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+|"
    r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+(?:\s+[A-Z]\.)+[ ]+[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+"
    r")\s*,"
)
_WIKIQUOTE_DOUBLE_QUOTED_TITLE_RE = re.compile(
    r'"(?P<straight>[^"]+)"|“(?P<typographic>[^”]+)”'
)
_WIKIDATA_SIGNED_TIME_YEAR_RE = re.compile(
    r"^(?P<sign>[+-])(?P<year>\d+)-\d{2}-\d{2}T"
)
_WIKIDATA_EXACT_DATE_RE = re.compile(
    r"^\+(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T"
)
_WIKIDATA_GREGORIAN_CALENDAR = "http://www.wikidata.org/entity/Q1985727"

thread_local = threading.local()


@dataclass(frozen=True)
class WikiquoteQuoteCandidate:
    element: object
    active_headings: tuple
    work_heading: Optional[str]
    work_heading_url: Optional[str]
    work_heading_year: Optional[int]
    hierarchy_details: Optional[str]


def normalize_wikiquote_section_label(label):
    """Return a comparable, conservative Wikiquote section label."""
    if not isinstance(label, str):
        return ""

    return " ".join(label.replace("_", " ").split()).casefold()


def get_wikiquote_section_label(heading):
    """Read a heading label, preferring stable HTML ids over visible text."""
    heading_id = heading.get("id")
    if isinstance(heading_id, str) and heading_id.strip():
        return normalize_wikiquote_section_label(heading_id)

    headline = heading.find("span", class_="mw-headline")
    if headline is not None:
        headline_id = headline.get("id")
        if isinstance(headline_id, str) and headline_id.strip():
            return normalize_wikiquote_section_label(headline_id)

    return normalize_wikiquote_section_label(heading.get_text(" ", strip=True))


def get_wikiquote_section_display(heading):
    return " ".join(heading.get_text(" ", strip=True).split())


def get_wikiquote_heading_link(heading):
    """Return one vetted direct work link from a heading, if it has one."""
    headline = heading.find("span", class_="mw-headline")
    display = get_wikiquote_section_display(heading)
    heading_work, _ = _bibliographic_parent_heading(display)
    expected_labels = {
        _normalized_work_text(display),
        _normalized_work_text(heading_work),
    }
    heading_content = headline if headline is not None else heading
    for link in heading_content.find_all("a"):
        if not _is_meaningful_source_link(link):
            continue
        if _normalized_work_text(link.get_text(" ", strip=True)) not in expected_labels:
            continue
        return urljoin("https://en.wikiquote.org/", link["href"])
    return None


def is_excluded_wikiquote_section(label):
    """Whether one normalized structural heading excludes quote candidates."""
    normalized = normalize_wikiquote_section_label(label)
    return any(
        normalized == prefix
        or normalized.startswith(prefix + " ")
        or normalized.startswith(prefix + ":")
        for prefix in _WIKIQUOTE_EXCLUDED_SECTION_PREFIXES
    )


def _heading_level(heading):
    return int(heading.name[1])


def _section_child_heading(child):
    if child.name in _WIKIQUOTE_HEADING_TAGS:
        return child

    # Current MediaWiki output can wrap a heading in a direct container.
    return child.find(_WIKIQUOTE_HEADING_TAGS, recursive=False)


def extract_wikiquote_candidate_text(candidate):
    """Return quote body text without mutating the parsed Wikiquote document."""
    cleaned_candidate = copy.deepcopy(candidate)

    for nested in cleaned_candidate.find_all(["ul", "ol", "dl"]):
        nested.decompose()

    for reference in cleaned_candidate.find_all("sup"):
        reference.decompose()

    return cleaned_candidate.get_text(" ", strip=True)


def _empty_quote_source():
    return {
        "work": None,
        "year": None,
        "date": None,
        "details": None,
        "citation": None,
        "url": None,
    }


def _clean_attribution_fragment(element):
    cleaned = copy.deepcopy(element)
    for nested in cleaned.find_all(["ul", "ol", "dl"]):
        nested.decompose()
    for reference in cleaned.find_all("sup"):
        reference.decompose()
    text = " ".join(cleaned.get_text(" ", strip=True).split())
    text = re.sub(r"\s+([,.;:?!])", r"\1", text)
    return re.sub(r"^(?:source|citation)\s*:\s*", "", text, flags=re.IGNORECASE)


def _is_meaningful_source_link(link):
    href = link.get("href")
    text = " ".join(link.get_text(" ", strip=True).split())
    classes = link.get("class", [])
    return (
        isinstance(href, str)
        and href
        and not href.startswith("#")
        and "reference" not in classes
        and text
        and not re.fullmatch(r"(?:\[?\d+\]?|\^)", text)
    )


def _attribution_fragments(candidate):
    fragments = []
    citation_tags = []
    links = []
    for container in candidate.find_all(["ul", "ol", "dl"], recursive=False):
        direct_items = container.find_all(["li", "dd", "dt"], recursive=False)
        for item in direct_items or [container]:
            fragment = _clean_attribution_fragment(item)
            if fragment and not re.fullmatch(r"(?:\[?\d+\]?|\^)", fragment):
                fragments.append(fragment)
            citation_tags.extend(item.find_all("cite"))
            links.extend(
                link for link in item.find_all("a")
                if _is_meaningful_source_link(link)
            )
    return fragments, citation_tags, links


def _year_from_attribution(citation):
    if not citation:
        return None
    for match in _WIKIQUOTE_YEAR_RE.finditer(citation):
        prefix = citation[max(0, match.start() - 12):match.start()].casefold()
        if re.search(r"(?:p\.|pp\.|page|pages)\s*$", prefix):
            continue
        suffix = citation[match.end():match.end() + 8]
        if re.match(r"\s*[-–]\s*\d{4}\b", suffix):
            continue
        # A bare four-digit passage immediately following a classical book
        # locator (``Book IV, 1005``) is a passage number, not a publication
        # year.  Keep this deliberately terminal: ``Book Title, 2005 edition``
        # remains eligible for ordinary bibliographic parsing.
        if (
            re.search(r"\bbook\s+[ivxlcdm]+\s*,\s*$", prefix, re.IGNORECASE)
            and re.match(r"\s*(?:[.;]|$)", suffix)
        ):
            continue
        # A date mentioned as background inside a chapter/commentary label is
        # not bibliographic evidence for the selected source.
        leading_context = citation[:match.start()].casefold()
        if re.search(
            r"\b(?:background|historical|history|period|date\s+range)\b",
            leading_context,
        ):
            continue
        return int(match.group(0))
    return None


def _date_from_attribution(citation):
    if not citation:
        return None
    match = _WIKIQUOTE_DATE_RE.search(citation)
    return match.group(0) if match else None


def _details_from_attribution(citation):
    if not citation:
        return None
    details = []
    translator_locator = _WIKIQUOTE_TRANSLATOR_LOCATOR_RE.match(citation)
    if translator_locator:
        details.append(translator_locator.group("locator"))
    locator_in_work = _WIKIQUOTE_LOCATOR_IN_WORK_RE.match(citation)
    if locator_in_work and locator_in_work.group("locator") not in details:
        details.append(locator_in_work.group("locator"))
    hierarchy = re.match(
        r"^\s*(Vol\.?\s*[IVXLCDM]+)\s*:\s*"
        r"(Part\s*[IVXLCDM]+)\s*:",
        citation,
        re.IGNORECASE,
    )
    if hierarchy:
        details.extend(
            " ".join(value.split())
            for value in hierarchy.groups()
        )
    for match in _WIKIQUOTE_DETAILS_RE.finditer(citation):
        value = match.group(0).strip()
        if value not in details:
            details.append(value)
    return ", ".join(details) if details else None


def _looks_like_person_name(value):
    """Conservatively reject initialled personal names as source works."""
    return bool(re.fullmatch(
        r"(?:[A-Z]\.?\s+){1,}[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+",
        value.strip(),
    ))


def _clean_work_candidate(value):
    value = " ".join(value.strip(" \t,.;:").split())
    if (
        not value
        or _looks_like_person_name(value)
        or value.endswith(("(", "[", "{"))
    ):
        return None
    return value


def _quoted_work_from_citation(citation):
    """Extract a quoted title only with explicit bibliographic context."""
    if not citation:
        return None
    if _WIKIQUOTE_NAME_LIKE_LABEL_IN_WORK_RE.match(citation):
        return None
    for match in _WIKIQUOTE_DOUBLE_QUOTED_TITLE_RE.finditer(citation):
        title = match.group("straight") or match.group("typographic")
        prefix = citation[:match.start()]
        if re.search(r"\battributed\s+to\s*$", prefix, re.IGNORECASE):
            continue
        if re.search(
            r"\b(?:no\s+known\s+citation|first\s+found|uncredited|"
            r"variant|describing\s+how)\b",
            prefix,
            re.IGNORECASE,
        ):
            continue
        suffix = citation[match.end():]
        bibliographic_context = re.match(
            r"\s*(?:"
            r"\((?:1[0-9]{3}|20[0-9]{2})\)|"
            r",\s*(?:(?:1[0-9]{3}|20[0-9]{2})\b|in\b|"
            r"(?:p\.|pp\.|ch\.|chapter|§|vol\.|translated\s+from|"
            r"as\s+translated\s+by|translated\s+by))|"
            r"\s+in\b|"
            r"\.\s*in\s*:?)",
            suffix,
            re.IGNORECASE,
        )
        if not bibliographic_context:
            continue

        title = title.strip().rstrip(".").strip()
        # Explicit context is primary; this narrowly rejects dialogue-like
        # quotations that happen to carry sentence punctuation as a title.
        if (
            not title
            or not title[0].isupper()
            or len(title) > 160
            or re.search(r"[?!]", title)
        ):
            continue
        return _clean_work_candidate(title)
    return None


def _is_secondary_citation(citation):
    """Whether the citation identifies another source rather than the work."""
    return bool(citation) and bool(_WIKIQUOTE_SOURCE_LAYER_RE.search(citation))


def _simple_work_candidate(value):
    """Return one bounded plain bibliographic title, never general prose."""
    value = _clean_work_candidate(value)
    if value is None:
        return None
    if _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(value):
        return None
    if _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(value):
        return None
    if _WIKIQUOTE_GENERIC_DOCUMENT_LABEL_RE.match(value):
        return None
    # This form is deliberately narrow.  Explicit quoted titles and heading
    # context handle richer title syntax; this only accepts a compact title
    # followed directly by a year/date.
    if len(value) > 120 or _WIKIQUOTE_STRUCTURAL_PREFIX_RE.match(value):
        return None
    if _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(value):
        return None
    if _WIKIQUOTE_SOURCE_LAYER_RE.search(value):
        return None
    if re.search(
        r"\b(?:isbn|edition|publisher|press|volume|vol\.|book|part|"
        r"chapter|ch\.|section|sec\.|p\.|pp\.|page|pages)\b",
        value,
        re.IGNORECASE,
    ):
        return None
    # Commas, semicolons, sentence punctuation, and quotation marks are all
    # strong signs that this is an attribution chain rather than one title.
    if re.search(r"[,;\"“”]|\.\s+", value):
        return None
    if len(value) > 90 and re.search(
        r"\b(?:that|where|someone|quotation|why|was|were|is|are)\b",
        value,
        re.IGNORECASE,
    ):
        return None
    return value


def _work_from_locator_translator_citation(citation):
    """Extract a work after a bounded dialogue locator and contributor role."""
    match = _WIKIQUOTE_TRANSLATOR_WORK_RE.match(citation)
    if match is None:
        return None
    return _simple_work_candidate(match.group("work"))


def _work_from_contributor_boundary(citation):
    """Extract a title only on an explicit contributor-role boundary."""
    match = _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(citation)
    if match is None:
        return None

    before = citation[:match.start()].rstrip(" ,.;:")
    after = citation[match.end():].lstrip(" ,.;:")
    if _WIKIQUOTE_STRUCTURAL_PREFIX_RE.match(before):
        return None
    before_work = (
        _work_from_simple_title_with_year(before)
        or _simple_work_candidate(before)
    )
    # A dated pre-role source lead that did not admit as a title must not fall
    # back to treating the whole author/reference segment as one.
    if _year_from_attribution(before) is not None and before_work is None:
        return None
    role = match.group(0).casefold().rstrip(".")

    # Long-form roles are directional: their trailing text is contributor
    # material, never a work title.  Thus "WORK as translated by PERSON" and
    # "WORK (YEAR) edited by PERSON" can retain only the preceding work.
    if role not in {"trans", "ed", "eds"}:
        return before_work

    # The abbreviated form can be either WORK, trans. PERSON or the explicit
    # bibliographic PERSON, trans., WORK form.  Only an initialled prefix is
    # sufficiently bounded evidence for the latter; the locator form is
    # handled earlier by _work_from_locator_translator_citation.
    if re.fullmatch(r"(?:[A-Z]\.?\s+){1,3}[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+", before):
        candidate = re.split(
        r"(?:\s*\(\s*(?:1[0-9]{3}|20[0-9]{2})\s*\)|"
        r",\s*(?:1[0-9]{3}|20[0-9]{2})\b)",
        after,
        maxsplit=1,
    )[0]
        return _simple_work_candidate(candidate)

    # WORK, trans. PERSON: keep the leading compact work, never the trailing
    # contributor.  Ambiguous forms remain source-less.
    return before_work


def _looks_like_undelimited_author_work(value):
    """Reject a name-plus-title run lacking a safe bibliographic delimiter."""
    return bool(re.match(
        r"^[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+"
        r"[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]+\s+.+:\s+",
        value,
    ))


def _work_from_locator_in_citation(citation):
    """Extract a work after the specific '<locator>, in <work>' form."""
    match = _WIKIQUOTE_LOCATOR_IN_WORK_RE.match(citation)
    if match is None:
        return None
    return _simple_work_candidate(match.group("work"))


def _work_from_dialogue_locator_volume(citation):
    """Extract a title after a bounded dialogue locator before a volume."""
    match = re.match(
        r"^\s*\d+[A-Za-z]?(?:[-–][A-Za-z\d]+)?\s*,\s*"
        r"(?P<work>.+?),\s*(?:Volume|Vol\.)\s*",
        citation,
        re.IGNORECASE,
    )
    if match is None:
        return None
    return _simple_work_candidate(match.group("work"))


def _work_from_parenthesized_contributor_suffix(citation):
    """Extract one title before a terminal '(trans. NAME)' style suffix."""
    stripped = _WIKIQUOTE_PARENTHESES_CONTRIBUTOR_SUFFIX_RE.sub("", citation)
    if stripped == citation:
        return None

    # The terminal contributor parenthesis establishes a bounded bibliographic
    # form: WORK by AUTHOR (trans. CONTRIBUTOR).  It is safe to remove the
    # authorship clause here without general person-name inference.
    by_author = re.match(r"^(?P<work>.+?)\s+by\s+[^,;()]+\s*$", stripped)
    if by_author is not None:
        return _simple_work_candidate(by_author.group("work"))
    return _simple_work_candidate(stripped)


def _work_from_simple_title_with_year(citation):
    """Extract only a compact unlayered title immediately before a date."""
    # Unlike the historical fallback, this rejects any compound attribution.
    match = re.match(
        r"^(?P<work>[^()]+?)(?:\.\s+|,\s+|\s*\()"
        r"(?:(?:1[0-9]{3}|20[0-9]{2})|"
        r"\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)|"
        r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},)",
        citation,
    )
    if match is None:
        return None
    candidate = match.group("work").strip(" \t,.;:")
    # This is the weakest title path.  It must fail closed for provenance
    # statements and unresolved ``TITLE by AUTHOR`` bibliographic syntax;
    # stronger quoted-title, heading, and contributor paths run separately.
    if _WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE.match(candidate):
        return None
    if _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(candidate):
        return None
    if _WIKIQUOTE_SIMPLE_TITLE_EVENT_PREFIX_RE.match(candidate):
        return None
    if _WIKIQUOTE_GENERIC_DOCUMENT_LABEL_RE.match(candidate):
        return None
    if _WIKIQUOTE_NAME_LIKE_LABEL_IN_WORK_RE.match(citation):
        return None
    if _WIKIQUOTE_SIMPLE_TITLE_BY_AUTHOR_RE.search(candidate):
        return None
    if _WIKIQUOTE_AUTHOR_TITLE_RUN_RE.match(candidate):
        return None
    # A leading person/reference name followed by a date is an author-date
    # citation lead when a distinct bibliographic segment follows the date.
    # This structural guard deliberately does not try to identify the person
    # or recover a later title.
    trailing = citation[match.end():]
    trailing_content = trailing.strip(" \t\r\n()[]{}.,;:")
    if (
        _WIKIQUOTE_AUTHOR_DATE_LEAD_RE.match(candidate)
        and trailing_content
        and not _WIKIQUOTE_TITLE_YEAR_LOCATOR_TAIL_RE.match(trailing)
    ):
        return None
    if _looks_like_undelimited_author_work(candidate):
        return None
    return _simple_work_candidate(candidate)


def _work_from_citation(citation, include_quoted_work=True):
    """Return a work only for deliberately bounded citation patterns."""
    if not citation:
        return None
    if _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(citation):
        return None
    if _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(citation):
        return None
    if re.match(r"^(?:said|speech|interview|letter|lecture)\s+(?:at|with|to)\b", citation, re.I):
        return None
    # Secondary-source clauses cannot establish a work.  A primary segment
    # before one may still be parsed using the bounded forms below.
    primary_citation = _WIKIQUOTE_SOURCE_LAYER_RE.split(citation, maxsplit=1)[0]
    # The tightly bounded "NAME (YEAR), cited in:" form is an author/source
    # lead, even though the source-layer split leaves only trailing punctuation.
    source_lead = re.match(
        r"^\s*(?P<lead>[^(),]+?)\s*,?\s*\((?:1[0-9]{3}|20[0-9]{2})\)"
        r"\s*,\s*cited\s+in\s*:",
        citation,
        re.IGNORECASE,
    )
    if source_lead and _WIKIQUOTE_AUTHOR_DATE_LEAD_RE.match(source_lead.group("lead")):
        return None
    # A volume/part/section hierarchy is a locator plus a section heading,
    # not reliable evidence of the title of the containing publication.
    if re.match(
        r"^\s*Vol\.?\s*[IVXLCDM]+\s*:\s*Part\s*[IVXLCDM]+\s*:",
        citation,
        re.IGNORECASE,
    ):
        return None

    parenthesized_contributor_work = _work_from_parenthesized_contributor_suffix(
        citation,
    )
    if parenthesized_contributor_work is not None:
        return parenthesized_contributor_work

    if include_quoted_work:
        quoted_work = _quoted_work_from_citation(citation)
        if quoted_work is not None:
            return quoted_work

    translator_work = _work_from_locator_translator_citation(primary_citation)
    if translator_work is not None:
        return translator_work

    contributor_work = _work_from_contributor_boundary(primary_citation)
    if contributor_work is not None:
        return contributor_work

    # A bare leading name plus a comma is author/contributor prose, not a
    # sufficiently marked work title under this deliberately narrow model.
    if _WIKIQUOTE_LEADING_PERSON_CITATION_RE.match(primary_citation):
        return None

    locator_work = _work_from_locator_in_citation(primary_citation)
    if locator_work is not None:
        return locator_work

    dialogue_work = _work_from_dialogue_locator_volume(primary_citation)
    if dialogue_work is not None:
        return dialogue_work

    return _work_from_simple_title_with_year(primary_citation)


def _normalized_work_text(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[^\w]+", "", value, flags=re.UNICODE).casefold()


def _source_url_for_work(work, links):
    """Use an attribution URL only when its label is the recognized work."""
    normalized_work = _normalized_work_text(work)
    if not normalized_work:
        return None
    for link in links:
        label = _normalized_work_text(link.get_text(" ", strip=True))
        if label and label == normalized_work:
            return urljoin("https://en.wikiquote.org/", link["href"])
    return None


def extract_wikiquote_quote_source(candidate):
    """Extract conservative attribution metadata without changing quote body."""
    source = _empty_quote_source()
    fragments, _, links = _attribution_fragments(candidate.element)
    citation = "; ".join(fragments) if fragments else None
    source["citation"] = citation

    explicit_work = _quoted_work_from_citation(citation)
    conservative_citation_work = _work_from_citation(
        citation, include_quoted_work=False,
    )
    parent_work = candidate.work_heading
    if not _citation_allows_parent_work(
        citation, parent_work, explicit_work, conservative_citation_work,
    ):
        parent_work = None
    source["work"] = (
        explicit_work
        or parent_work
        or conservative_citation_work
    )
    parent_selected = bool(parent_work and source["work"] == parent_work)
    if parent_selected:
        source["url"] = candidate.work_heading_url
    else:
        source["url"] = _source_url_for_work(source["work"], links)

    source["date"] = _date_from_attribution(citation)
    if parent_selected:
        # A bounded publication year on the selected structural parent is
        # authoritative.  Citation years are otherwise accepted only when
        # the citation independently identifies that same work.
        source["year"] = (
            candidate.work_heading_year
            or _citation_year_for_selected_parent_work(
                citation, parent_work, candidate.hierarchy_details,
            )
        )
    else:
        source["year"] = _year_from_attribution(citation)
    nested_attribution_detail = None
    if parent_selected:
        nested_attribution_detail = _concise_nested_attribution_detail(citation)
    source["details"] = _merge_quote_source_details(
        candidate.hierarchy_details if parent_selected else None,
        nested_attribution_detail,
        _details_from_attribution(citation),
    )
    return source


def _citation_allows_parent_work(
    citation, parent_work, explicit_work, conservative_citation_work,
):
    """Whether a structural parent can safely enrich this attribution.

    A parent heading is context, not an automatic source.  It may enrich an
    absent citation, a compact subsection/locator, or a citation which itself
    establishes the same work.  Provenance and event prose therefore veto a
    nearby but unrelated work heading.
    """
    if not parent_work:
        return False
    if not citation:
        return True
    value = " ".join(citation.split())
    if not value:
        return True
    if (
        _WIKIQUOTE_GENERIC_DOCUMENT_CITATION_RE.match(value)
        or _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE.match(value)
        or _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SIMPLE_TITLE_EVENT_PREFIX_RE.match(value)
        or _WIKIQUOTE_SOURCE_LAYER_RE.search(value)
        or _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(value)
    ):
        return False

    parent_normalized = _normalized_work_text(parent_work)
    for citation_work in (explicit_work, conservative_citation_work):
        if citation_work:
            return _normalized_work_text(citation_work) == parent_normalized

    # The nested attribution is permitted only when it is a compact named
    # subsection or structural locator; arbitrary prose cannot inherit a
    # heading merely because it happened to be nested beneath it.
    if _citation_is_parent_subsection_or_locator(value):
        return True
    if _is_compact_detail_fragment(value):
        return True
    # Citation-backed structural hierarchy may include explanatory prose after
    # a locator (for example the Priestley Vol./Part/§ citation).  The
    # hierarchy itself is still direct evidence that it belongs to the parent.
    return bool(re.match(
        r"^\s*(?:vol(?:ume)?\.?\s*[ivxlcdm]+\s*:\s*part\s*[ivxlcdm]+|"
        r"(?:part|chapter|ch\.)\s+(?:\d+|[ivxlcdm]+)\s*[:.]|"
        r"book\s+(?:\d+|[ivxlcdm]+)(?:\s+chapter|\s*[,.:])|§\s*\d+)",
        value,
        re.IGNORECASE,
    ))


def _citation_is_parent_subsection_or_locator(value):
    """Recognize a bounded subsection label without treating prose as one."""
    if _concise_nested_attribution_detail(value):
        return True
    if (
        not isinstance(value, str)
        or len(value) > 96
        or _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE.match(value)
        or _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SOURCE_LAYER_RE.search(value)
        or _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(value)
    ):
        return False
    if re.match(
        r"^\s*(?:[IVXLCDM]+\.\s+|(?:part|chapter|ch\.)\s+"
        r"(?:\d+|[ivxlcdm]+)\s*[:.]|book\s+(?:\d+|[ivxlcdm]+)\s*[:.]|"
        r"§\s*\d+)",
        value,
        re.IGNORECASE,
    ):
        return True
    # Compact classical hierarchy can be rendered as ``Vol. I, Ch. III`` or
    # ``Vol II \"On …\"`` rather than the colon-separated form handled below.
    # It is still direct subsection evidence if no provenance/contributor
    # guard above fired and it stays within a short attribution-sized label.
    return bool(re.match(
        r"^\s*(?:vol(?:ume)?\.?\s*(?:\d+|[ivxlcdm]+)(?:\s*,?\s*"
        r"(?:ch(?:apter)?\.?\s*(?:\d+|[ivxlcdm]+)|§\s*\d+|"
        r"section\s*\d+(?:\.\d+)*))?|"
        r"(?:ch(?:apter)?\.?|section|part)\s*(?:\d+|[ivxlcdm]+)|"
        r"§\s*\d+|p(?:p)?\.\s*\d+)",
        value,
        re.IGNORECASE,
    ))


def _citation_year_for_selected_parent_work(
    citation, parent_work, hierarchy_details=None,
):
    """Return a citation year only when its parsed work matches the parent."""
    citation_work = _work_from_citation(citation)
    if (
        _normalized_work_text(citation_work)
        and _normalized_work_text(citation_work)
        == _normalized_work_text(parent_work)
    ):
        return _year_from_attribution(citation)
    # A citation headed by the same compact structural hierarchy is also a
    # direct attribution to the selected parent (for example Vol./Part/§).
    # This is deliberately not a general year fallback for prose citations.
    citation_details = _details_from_attribution(citation)
    if hierarchy_details and citation_details:
        expected = [detail.strip() for detail in hierarchy_details.split(", ")]
        observed = [detail.strip() for detail in citation_details.split(", ")]
        if expected and all(detail in observed for detail in expected):
            return _year_from_attribution(citation)
    return None


def _merge_quote_source_details(*values):
    """Combine compact, independently established locators without repeats."""
    details = []
    for value in values:
        if not value:
            continue
        for detail in value.split(", "):
            detail = detail.strip()
            if not detail or detail in details:
                continue
            structural_locators = _normalized_structural_detail_locators(detail)
            compact_structural = _is_compact_structural_locator(detail)
            structural_match_indexes = [
                index
                for index, existing in enumerate(details)
                if structural_locators
                and _structural_locators_overlap(
                    structural_locators,
                    _normalized_structural_detail_locators(existing),
                )
            ]
            if compact_structural and structural_match_indexes:
                continue
            if not compact_structural:
                compact_match = next((
                    index for index in structural_match_indexes
                    if _is_compact_structural_locator(details[index])
                ), None)
                if compact_match is not None:
                    details[compact_match] = detail
                    continue
            page_locators = _normalized_page_locators(detail)
            page_only = _is_compact_page_locator(detail)
            page_match_indexes = [
                index
                for index, existing in enumerate(details)
                if page_locators
                and page_locators & _normalized_page_locators(existing)
            ]
            # A page-only fragment extracted from a citation must not repeat
            # the same bounded page reference already present in a richer
            # nested attribution fragment (for example ``Nichol P. 73``).
            if page_only and page_match_indexes:
                continue
            # Retain the richer fragment if the order is reversed: its
            # surrounding bibliographic text is distinct useful context.
            if not page_only:
                page_only_match = next((
                    index for index in page_match_indexes
                    if _is_compact_page_locator(details[index])
                ), None)
                if page_only_match is not None:
                    details[page_only_match] = detail
                    continue
            locator = _normalized_detail_locator(detail)
            matching_index = next((
                index for index, existing in enumerate(details)
                if locator and _normalized_detail_locator(existing) == locator
            ), None)
            if matching_index is None:
                details.append(detail)
            elif len(detail) > len(details[matching_index]):
                # Preserve a named subsection such as "Chapter 1: Title"
                # over its shorter duplicate locator, regardless of order.
                details[matching_index] = detail
    return ", ".join(details) if details else None


_WIKIQUOTE_PAGE_LOCATOR_RE = re.compile(
    r"(?<![A-Za-z])(?:p|pp|page|pages)\.?\s*"
    r"(?P<start>\d+)(?:\s*[-–]\s*(?P<end>\d+))?(?!\d)",
    re.IGNORECASE,
)


def _normalized_page_locators(value):
    """Return bounded page locators for comparison-only detail deduplication."""
    if not isinstance(value, str):
        return frozenset()
    return frozenset(
        "page {}{}".format(
            match.group("start"),
            "-{}".format(match.group("end")) if match.group("end") else "",
        )
        for match in _WIKIQUOTE_PAGE_LOCATOR_RE.finditer(value)
    )


def _is_compact_page_locator(value):
    """Whether a fragment consists solely of one bounded page locator."""
    if not isinstance(value, str):
        return False
    return _WIKIQUOTE_PAGE_LOCATOR_RE.fullmatch(value.strip()) is not None


def _normalized_detail_locator(value):
    """Normalize a leading compact hierarchy locator for detail deduplication."""
    if not isinstance(value, str):
        return None
    page_locators = _normalized_page_locators(value)
    if _is_compact_page_locator(value) and len(page_locators) == 1:
        return next(iter(page_locators))
    match = re.match(
        r"^\s*(?P<kind>chapter|ch\.|part|book|§)\s*"
        r"(?P<number>\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
        r"eight|nine|ten)\b",
        value,
        re.IGNORECASE,
    )
    if match is None:
        return None
    kind = match.group("kind").casefold().rstrip(".")
    if kind == "ch":
        kind = "chapter"
    return "{} {}".format(kind, match.group("number").casefold())


_WIKIQUOTE_STRUCTURAL_DETAIL_LOCATOR_RE = re.compile(
    r"(?<![A-Za-z])(?P<kind>section|sec\.?|chapter|ch\.?|part|book|§)\s*"
    r"(?P<number>\d+(?:\.\d+)*|[ivxlcdm]+|one|two|three|four|five|six|"
    r"seven|eight|nine|ten)(?![A-Za-z])",
    re.IGNORECASE,
)


def _normalized_structural_detail_locators(value):
    """Return structural locators for bounded containment-aware comparison."""
    if not isinstance(value, str):
        return frozenset()
    normalized = set()
    for match in _WIKIQUOTE_STRUCTURAL_DETAIL_LOCATOR_RE.finditer(value):
        kind = match.group("kind").casefold().rstrip(".")
        if kind == "ch":
            kind = "chapter"
        if kind == "sec":
            kind = "section"
        normalized.add("{} {}".format(kind, match.group("number").casefold()))
    return frozenset(normalized)


def _is_compact_structural_locator(value):
    if not isinstance(value, str):
        return False
    return _WIKIQUOTE_STRUCTURAL_DETAIL_LOCATOR_RE.fullmatch(value.strip()) is not None


def _structural_locators_overlap(left, right):
    """Whether structural locators are equal or a bounded sublocator pair."""
    for first in left:
        first_kind, first_number = first.split(" ", 1)
        for second in right:
            second_kind, second_number = second.split(" ", 1)
            if first_kind != second_kind:
                continue
            if first_number == second_number:
                return True
            # ``Section 1`` is represented by the richer ``Section 1.1``;
            # this is containment, not a broad numeric-prefix match.
            if (
                first_kind == "section"
                and (first_number.startswith(second_number + ".")
                     or second_number.startswith(first_number + "."))
            ):
                return True
    return False


def _is_compact_detail_fragment(value):
    return (
        _is_compact_page_locator(value)
        or _is_compact_structural_locator(value)
        or bool(re.fullmatch(r"\s*\d{1,4}[A-Za-z]?\s*", value or ""))
    )


def _is_structural_hierarchy_heading(label, display):
    value = normalize_wikiquote_section_label(display or label)
    return bool(re.match(
        r"^(?:vol(?:ume)?\.?\s+[ivxlcdm]+|part\s+[ivxlcdm]+|"
        r"(?:chapter|ch\.)\s*(?:\d+|[ivxlcdm]+|one|two|three|four|five|"
        r"six|seven|eight|nine|ten)|"
        r"book\s+(?:\d+|[ivxlcdm]+|one|two|three|four|five|six|seven|"
        r"eight|nine|ten)|§\s*\d+|\d+[a-z]?\b)",
        value,
        re.IGNORECASE,
    ))


def _concise_hierarchy_detail(label, display):
    value = " ".join((display or label).split())
    match = re.match(
        r"^(Vol\.?\s*[IVXLCDM]+|Part\s*[IVXLCDM]+|"
        r"(?:Chapter|Ch\.)\s*(?:\d+|[IVXLCDM]+|One|Two|Three|Four|Five|"
        r"Six|Seven|Eight|Nine|Ten)|"
        r"Book\s+(?:\d+|[IVXLCDM]+|One|Two|Three|Four|Five|Six|Seven|"
        r"Eight|Nine|Ten)|§\s*\d+|\d+[a-z]?\b)",
        value,
        re.IGNORECASE,
    )
    return match.group(1) if match else None


def _bibliographic_parent_heading(display):
    """Return bounded work/year evidence from a heading, if present."""
    if not display:
        return None, None
    match = re.match(
        r"^(?P<work>.+?)\s*\(\s*(?P<year>1[0-9]{3}|20[0-9]{2})\s*\)\s*$",
        display,
    )
    if not match:
        return None, None
    work = match.group("work").strip()
    return (work, int(match.group("year"))) if work else (None, None)


def _clean_parent_work_heading(display):
    """Return a parent heading only when it is one clean work title.

    Heading context is valuable, but Wikiquote headings can also be source
    notes, edition strings, or author-attribution prose.  This intentionally
    narrow guard leaves those headings source-less rather than preserving a
    misleading partial title.
    """
    heading_work, heading_year = _bibliographic_parent_heading(display)
    work = heading_work or (display or "").strip()
    if not work or work.rstrip()[-1:] in {",", ";", ":", "(", "[", "{"}:
        return None, None
    if _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(work):
        return None, None
    if _WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE.match(work):
        return None, None
    if _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(work):
        return None, None
    if _WIKIQUOTE_SOURCE_LAYER_RE.search(work):
        return None, None
    if _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(work):
        return None, None
    if re.search(r"\b(?:edition|\d+(?:st|nd|rd|th)\s+ed\.?|edited\s+by|"
                 r"translated\s+by)\b", work, re.IGNORECASE):
        return None, None
    # This is bounded bibliographic syntax, not general person-name parsing.
    if re.search(r"\s+by\s+[A-Z][^,;()]+$", work):
        return None, None
    return work, heading_year


def _has_structural_parent_work_evidence(display, heading_url):
    """Require bounded evidence before an ancestor can establish a work."""
    bibliographic_work, _ = _bibliographic_parent_heading(display)
    if bibliographic_work:
        return True
    if heading_url:
        return True
    # A longer named heading can be a work in legacy Wikiquote structure
    # without a date/link (for example Institutes of Natural and Revealed
    # Religion).  This deliberately excludes thematic, person, and short
    # subsection labels such as Themes, Michel Foucault, and Las Meninas.
    return len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][\w'’.-]*", display or "")) >= 3


def _concise_named_hierarchy_detail(label, display):
    """Keep a short named subsection, never a prose heading, as a detail."""
    value = " ".join((display or label).split())
    if (
        not value
        or len(value) > 96
        or len(value.split()) > 12
        or re.search(r"[;!?]", value)
        or value.endswith(".")
    ):
        return None
    return value


def _concise_nested_attribution_detail(citation):
    """Return a safe named nested attribution when a parent work is known.

    Modern Wikiquote pages commonly put a chapter or named subsection in a
    nested attribution list item, rather than in a heading.  This helper is
    deliberately narrower than citation parsing: it never establishes a work
    and admits only a short, title-like subsection label.
    """
    if not isinstance(citation, str):
        return None
    value = " ".join(citation.split())
    if (
        not value
        or len(value) > 96
        or len(value.split()) > 12
        or re.search(r"[;!?]", value)
        or value.endswith(".")
        or _WIKIQUOTE_ATTRIBUTION_NOTE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SIMPLE_TITLE_PROVENANCE_RE.match(value)
        or _WIKIQUOTE_EXPLANATORY_PROVENANCE_PREFIX_RE.match(value)
        or _WIKIQUOTE_SOURCE_LAYER_RE.search(value)
        or _WIKIQUOTE_CONTRIBUTOR_ROLE_RE.search(value)
        or _WIKIQUOTE_YEAR_RE.search(value)
    ):
        return None

    # Explicit document/subsection labels are safe.  Otherwise require a
    # compact title-like label such as "Las Meninas", never arbitrary prose.
    if re.match(
        r"^(?:preface|foreword|introduction|epilogue|prologue|afterword|"
        r"appendix|(?:part|chapter|ch\.)\s+|book\s+|§\s*\d+|"
        r"[IVXLCDM]+\.\s+)",
        value,
        re.IGNORECASE,
    ):
        return value
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*", value)
    if 2 <= len(words) <= 8 and all(word[0].isupper() for word in words):
        return value
    return None


def _work_heading_from_stack(active_headings):
    """Return a structural parent work, direct URL, and concise descendants."""
    quote_container_index = None
    for index, (_, label, _, _) in enumerate(active_headings):
        if label in _WIKIQUOTE_GENERIC_QUOTE_HEADINGS:
            quote_container_index = index

    # Existing generic quote containers allow the established heading fallback.
    # Without one, fail closed unless a heading independently carries bounded
    # bibliographic work evidence (currently an exact trailing publication year).
    headings = active_headings[quote_container_index + 1:] if quote_container_index is not None else active_headings
    require_bibliographic_evidence = quote_container_index is None
    parent_work = None
    parent_url = None
    parent_year = None
    details = []
    for _, label, display, heading_url in headings:
        display_label = normalize_wikiquote_section_label(display)
        if (
            not display
            or label in _WIKIQUOTE_GENERIC_WORK_HEADINGS
            or display_label in _WIKIQUOTE_GENERIC_WORK_HEADINGS
            or is_excluded_wikiquote_section(label)
            or is_excluded_wikiquote_section(display_label)
        ):
            continue
        if _is_structural_hierarchy_heading(label, display):
            detail = _concise_hierarchy_detail(label, display)
            if detail and detail not in details:
                details.append(detail)
            continue
        if parent_work is None:
            bibliographic_work, _ = _bibliographic_parent_heading(display)
            heading_work, heading_year = _clean_parent_work_heading(display)
            if (
                (require_bibliographic_evidence and bibliographic_work is None)
                or not _has_structural_parent_work_evidence(display, heading_url)
            ):
                continue
            if heading_work is None:
                continue
            parent_work = heading_work
            parent_year = heading_year
            parent_url = heading_url
            continue
        detail = _concise_named_hierarchy_detail(label, display)
        if detail and detail not in details:
            details.append(detail)
    return parent_work, parent_url, parent_year, ", ".join(details) if details else None


def _wikiquote_traversal_children(content):
    """Yield direct traversal nodes, flattening one known parser artifact.

    With ``html.parser``, a modern Wikiquote TOC metadata tag can incorrectly
    enclose all following document content.  Its descendants are still normal
    page siblings for quote traversal, so only that known metadata wrapper is
    transparent here; arbitrary elements retain their normal boundaries.
    """
    for child in content.children:
        if (
            getattr(child, "name", None) == "meta"
            and child.get("property") == "mw:PageProp/toc"
        ):
            yield from child.children
        else:
            yield child


def iter_wikiquote_quote_candidates(content):
    """Yield allowed top-level quote list items in document section order."""
    active_headings = []

    for child in _wikiquote_traversal_children(content):
        if not getattr(child, "name", None):
            continue

        heading = _section_child_heading(child)
        if heading is not None:
            level = _heading_level(heading)
            active_headings = [
                item for item in active_headings if item[0] < level
            ]
            active_headings.append((
                level,
                get_wikiquote_section_label(heading),
                get_wikiquote_section_display(heading),
                get_wikiquote_heading_link(heading),
            ))
            continue

        if any(is_excluded_wikiquote_section(label) for _, label, _, _ in active_headings):
            continue

        # A direct content child can contain a list wrapper, but nested list
        # items are citations/attribution candidates rather than new quotes.
        for candidate in child.find_all("li"):
            if candidate.find_parent("li") is not None:
                continue
            work_heading, work_heading_url, work_heading_year, hierarchy_details = _work_heading_from_stack(
                active_headings
            )
            yield WikiquoteQuoteCandidate(
                element=candidate,
                active_headings=tuple(active_headings),
                work_heading=work_heading,
                work_heading_url=work_heading_url,
                work_heading_year=work_heading_year,
                hierarchy_details=hierarchy_details,
            )

@dataclass
class RequestResult:
    response: Optional[requests.Response]
    error_reason: Optional[str]
    attempts: int

    @property
    def ok(self) -> bool:
        return self.response is not None and self.error_reason is None


@dataclass(frozen=True)
class BatchLookupResult:
    """Data from one Wikidata batch plus any upstream lookup failure."""
    data: dict
    error_reason: Optional[str]


@dataclass(frozen=True)
class PageProperties:
    """Structural facts returned by one successful Wikipedia pageprops lookup."""
    qid: Optional[str]
    is_disambiguation: bool

def get_session():
    if not hasattr(thread_local, "session"):
        thread_local.session = requests.Session()
    return thread_local.session    

def safe_request(
    url,
    params=None,
    limiter=None,
    session=None,
    timeout=REQUEST_TIMEOUT,
    max_retries=MAX_RETRIES,
    sleep=None,
    jitter=None,
):
    if sleep is None:
        sleep = time.sleep

    if jitter is None:
        jitter = random.uniform

    last_response = None

    for attempt in range(1, max_retries + 1):
        if limiter is not None:
            limiter.wait()

        try:
            if session is None:
                session = get_session()

            response = session.get(
                url,
                headers=get_request_headers(),
                params=params,
                timeout=timeout,
            )
            last_response = response

            if response.status_code == 200:
                return RequestResult(
                    response=response,
                    error_reason=None,
                    attempts=attempt,
                )

            error_reason = "http_{}".format(response.status_code)

            if response.status_code not in (429, 500, 502, 503, 504):
                print("HTTP error:", response.status_code)
                return RequestResult(
                    response=response,
                    error_reason=error_reason,
                    attempts=attempt,
                )

            if attempt == max_retries:
                return RequestResult(
                    response=response,
                    error_reason=error_reason,
                    attempts=attempt,
                )

            wait = calculate_backoff(attempt) + jitter(0, 0.5)
            print("{} → retry in {:.2f}s".format(response.status_code, wait))
            sleep(wait)

        except requests.RequestException as error:
            if attempt == max_retries:
                return RequestResult(
                    response=last_response,
                    error_reason="request_exception",
                    attempts=attempt,
                )

            wait = calculate_backoff(attempt)
            print("Request failed:", error, "→ retry in {}s".format(wait))
            sleep(wait)

    return RequestResult(
        response=last_response,
        error_reason="request_exception",
        attempts=max_retries,
    )

def get_all_pages(search_term, limiter=None):
    pages = []

    params = {
        "action": "query",
        "list": "search",
        "srsearch": search_term,
        "srlimit": SRLIMIT,
        "format": "json",
    }

    while True:
        request_result = safe_request(
            WIKIPEDIA_URL,
            params,
            limiter=limiter,
        )

        if not request_result.ok:
            return pages

        response = request_result.response

        if response is None:
            return pages

        try:
            data = response.json()
        except ValueError:
            return pages

        if not isinstance(data, dict):
            return pages

        query = data.get("query")

        if not isinstance(query, dict):
            return pages

        search = query.get("search")

        if not isinstance(search, list):
            return pages

        pages.extend(
            item
            for item in search
            if isinstance(item, dict)
            and isinstance(item.get("title"), str)
            and item["title"].strip()
        )

        continuation = data.get("continue")

        if not isinstance(continuation, dict):
            return pages

        params.update(continuation)

        if len(pages) > MAX_PAGES:
            return pages

def get_summary(
    title,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    limiter=None,
):
    """Return a canonical summary, fetching and persisting it if necessary."""
    entry = database.get(title)
    summary_section = entry.get("summary") if isinstance(entry, dict) else None
    summary_text = (
        summary_section.get("text")
        if isinstance(summary_section, dict)
        else None
    )

    if isinstance(summary_text, str) and summary_text:

        with stats_lock:
            stats["cached_summaries"] += 1

        return summary_text

    encoded_title = quote(title.replace(" ", "_"))
    summary_url = f"{SUMMARY_URL}{encoded_title}"
    request_result = safe_request(summary_url, limiter=limiter)

    if not request_result.ok:
        return None

    response = request_result.response

    if response is None:
        return None

    try:
        data = response.json()
    except ValueError:
        print("Invalid JSON:", title)
        print("URL:", summary_url)
        return None

    if not isinstance(data, dict):
        return None

    summary = data.get("extract")

    if not isinstance(summary, str) or not summary:
        return None
    
    def update_summary(entry):
        entry["summary"]["text"] = summary
        entry["summary"]["source"] = "Wikipedia"
        entry["summary"]["fetched_at"] = int(time.time())

    update_database_entry(
        database=database,
        title=title,
        update_callback=update_summary,
        filename=DATABASE_FILE,
        data_folder=data_folder,
        persistence_lock=persistence_lock,
    )

    with stats_lock:
        stats["downloaded_summaries"] += 1

    return summary

def get_page_properties_batch(titles, limiter=None):
    """Return QID and disambiguation facts from one Wikipedia pageprops batch.

    An absent property in a successful response is a genuine False.  Lookup
    failures are represented by ``error_reason`` and never manufacture a
    page-type result.
    """
    params = {
        "action": "query",
        "prop": "pageprops",
        "titles": "|".join(titles),
        "format": "json",
    }

    request_result = safe_request(
        WIKIPEDIA_URL,
        params,
        limiter=limiter,
    )

    if not request_result.ok:
        return BatchLookupResult({}, request_result.error_reason)

    response = request_result.response

    if response is None:
        return BatchLookupResult({}, "request_exception")

    try:
        data = response.json()
    except ValueError:
        return BatchLookupResult({}, "invalid_json")

    if not isinstance(data, dict):
        return BatchLookupResult({}, "malformed_response")

    query = data.get("query")

    if not isinstance(query, dict):
        return BatchLookupResult({}, "malformed_response")

    pages = query.get("pages")

    if not isinstance(pages, dict):
        return BatchLookupResult({}, "malformed_response")

    result = {}

    for page in pages.values():
        if not isinstance(page, dict):
            continue

        pageprops = page.get("pageprops", {})

        if not isinstance(pageprops, dict):
            continue

        title = page.get("title")
        if isinstance(title, str) and title:
            qid = pageprops.get("wikibase_item")
            result[title] = PageProperties(
                qid=qid if isinstance(qid, str) and qid else None,
                is_disambiguation="disambiguation" in pageprops,
            )

    return BatchLookupResult(result, None)


def get_wikidata_ids_batch(titles, limiter=None):
    """Backward-compatible QID-only view of the shared pageprops response."""
    page_properties = get_page_properties_batch(titles, limiter=limiter)
    if page_properties.error_reason is not None:
        return BatchLookupResult({}, page_properties.error_reason)

    return BatchLookupResult(
        {
            title: properties.qid
            for title, properties in page_properties.data.items()
            if properties.qid is not None
        },
        None,
    )

def get_wikidata_entities_batch(qids, limiter=None):
    """Fetch entity claims without unrelated labels, aliases, or sitelinks."""
    params = {
        "action": "wbgetentities",
        "ids": "|".join(qids),
        "props": "claims",
        "format": "json",
    }

    request_result = safe_request(
        WIKIDATA_URL,
        params,
        limiter=limiter,
    )

    if not request_result.ok:
        return BatchLookupResult({}, request_result.error_reason)

    response = request_result.response

    if response is None:
        return BatchLookupResult({}, "request_exception")

    try:
        data = response.json()
    except ValueError:
        return BatchLookupResult({}, "invalid_json")

    if not isinstance(data, dict):
        return BatchLookupResult({}, "malformed_response")

    entities = data.get("entities")

    if not isinstance(entities, dict):
        return BatchLookupResult({}, "malformed_response")

    return BatchLookupResult(entities, None)

def build_page_properties_cache(titles, limiter=None):
    """Batch all pageprops requests while retaining per-title errors."""
    page_properties = {}
    pageprops_errors = {}

    for batch in chunk_list(titles, 50):
        result = get_page_properties_batch(batch, limiter=limiter)
        if result.error_reason is not None:
            for title in batch:
                pageprops_errors[title] = result.error_reason
            continue
        page_properties.update(result.data)

    return page_properties, pageprops_errors


def build_entity_cache(
    titles,
    limiter=None,
    page_properties=None,
    pageprops_errors=None,
):
    all_qids = {}
    all_entities = {}
    wikidata_errors = dict(pageprops_errors or {})

    if page_properties is None:
        for batch in chunk_list(titles, 50):
            result = get_wikidata_ids_batch(batch, limiter=limiter)
            if result.error_reason is not None:
                for title in batch:
                    wikidata_errors[title] = result.error_reason
                continue
            all_qids.update(result.data)
    else:
        for title in titles:
            properties = page_properties.get(title)
            if properties is not None and properties.qid is not None:
                all_qids[title] = properties.qid

    qids = list(all_qids.values())

    for batch in chunk_list(qids, 50):
        result = get_wikidata_entities_batch(batch, limiter=limiter)
        if result.error_reason is not None:
            for title, qid in all_qids.items():
                if qid in batch:
                    wikidata_errors[title] = result.error_reason
            continue
        all_entities.update(result.data)

    return all_qids, all_entities, wikidata_errors

def get_entity_cached(qid, all_entities):

    entity = all_entities.get(qid)

    return entity

def get_instances(entity):

    if entity is None:
        return []

    instances = []

    claims = entity.get("claims", {})

    if "P31" not in claims:
        return instances

    for claim in claims["P31"]:
        mainsnak = claim.get("mainsnak")

        if not mainsnak:
            continue

        datavalue = mainsnak.get("datavalue")

        if not datavalue:
            continue

        value = datavalue.get("value")

        if not value:
            continue

        qid = value.get("id")

        if qid:
            instances.append(qid)

    return instances

def get_occupations(entity):

    if entity is None:
        return []

    occupations = []

    claims = entity.get("claims", {})

    if "P106" not in claims:
        return occupations

    for claim in claims["P106"]:
        mainsnak = claim.get("mainsnak")

        if not mainsnak:
            continue

        datavalue = mainsnak.get("datavalue")

        if not datavalue:
            continue

        value = datavalue.get("value")

        if not value:
            continue

        qid = value.get("id")

        if qid:
            occupations.append(qid)

    return occupations

def parse_wikidata_time_year(time_value):
    """Return the signed leading year from one Wikidata time value."""
    if not isinstance(time_value, str):
        return None

    match = _WIKIDATA_SIGNED_TIME_YEAR_RE.match(time_value)
    if match is None:
        return None

    year = int(match.group("year"))
    return -year if match.group("sign") == "-" else year


def get_wikidata_time_claim_value(claim):
    """Return a structurally valid Wikidata time string from one statement."""
    if not isinstance(claim, dict):
        return None
    mainsnak = claim.get("mainsnak")
    if not isinstance(mainsnak, dict):
        return None
    # Historical test fixtures omitted snaktype; a datavalue remains enough
    # evidence that those fixtures represent ordinary value snaks.
    if mainsnak.get("snaktype", "value") != "value":
        return None
    datavalue = mainsnak.get("datavalue")
    if not isinstance(datavalue, dict):
        return None
    value = datavalue.get("value")
    if not isinstance(value, dict):
        return None
    time_value = value.get("time")
    return time_value if isinstance(time_value, str) else None


def select_wikidata_time_claim(claims):
    """Select the first usable time statement by Wikidata rank semantics.

    Usable preferred statements take precedence over usable normal statements.
    Deprecated statements are intentionally not fallback evidence.  Statements
    of the same rank retain API list order as the deterministic tie-breaker.
    """
    if not isinstance(claims, list):
        return None

    for rank in ("preferred", "normal"):
        for claim in claims:
            if not isinstance(claim, dict) or claim.get("rank", "normal") != rank:
                continue
            time_value = get_wikidata_time_claim_value(claim)
            if parse_wikidata_time_year(time_value) is not None:
                return claim
    return None


def parse_wikidata_time_claim_exact_date(claim):
    """Return an ISO date for one selected Gregorian day-precision claim."""
    time_value = get_wikidata_time_claim_value(claim)
    if time_value is None:
        return None
    mainsnak = claim.get("mainsnak", {})
    datavalue = mainsnak.get("datavalue", {})
    value = datavalue.get("value", {})
    if value.get("precision") != 11:
        return None
    if value.get("calendarmodel") != _WIKIDATA_GREGORIAN_CALENDAR:
        return None
    match = _WIKIDATA_EXACT_DATE_RE.match(time_value)
    if match is None:
        return None
    try:
        return date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        ).isoformat()
    except ValueError:
        return None


def get_years_from_wikidata(entity):
    birth, death, _ = get_life_dates_from_wikidata(entity)
    return birth, death


def get_life_dates_from_wikidata(entity):
    """Return signed life years plus a supported exact death date."""
    if not isinstance(entity, dict):
        return None, None, None

    claims = entity.get("claims", {})
    if not isinstance(claims, dict):
        return None, None, None

    def get_selected_year(prop):
        claim = select_wikidata_time_claim(claims.get(prop))
        if claim is None:
            return None, None
        return (
            parse_wikidata_time_year(get_wikidata_time_claim_value(claim)),
            claim,
        )

    birth, _ = get_selected_year("P569")
    death, death_claim = get_selected_year("P570")
    death_date = (
        parse_wikidata_time_claim_exact_date(death_claim)
        if death_claim is not None else None
    )

    return birth, death, death_date

def quote_retry_base_days(reason):
    if reason in ("rate_limit", QUOTE_FAILURE_HTTP_429):
        return 1

    if reason == "banned":
        return 3

    if reason in ("timeout", QUOTE_FAILURE_REQUEST_EXCEPTION):
        return 7

    if reason == QUOTE_FAILURE_PARSING_ERROR:
        return 14

    if reason in ("404", QUOTE_FAILURE_HTTP_404):
        return 30

    if reason == QUOTE_FAILURE_NO_QUOTES_FOUND:
        return 60

    return 30

def record_quote_failure(
    title,
    reason,
    retries,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    successful_parse=False,
):
    timestamp = int(time.time())

    def update_quotes(entry):
        quotes = entry["quotes"]
        # Any quote-section write upgrades legacy omission to explicit stale
        # provenance without falsely claiming a successful current parse.
        quotes.setdefault("parser_version", None)
        failure = {
            "reason": reason,
            "timestamp": timestamp,
            "retries": retries + 1,
        }

        # A transport or parser failure must never destroy a known quote cache,
        # including a stale available cache being refreshed.
        if successful_parse and reason == QUOTE_FAILURE_NO_QUOTES_FOUND:
            quotes["items"] = []
            quotes["status"] = "not_found"
            quotes["failure"] = failure
            quotes["fetched_at"] = timestamp
            quotes["parser_version"] = CURRENT_QUOTE_PARSER_VERSION
            return

        if quotes["status"] == "available":
            quotes["failure"] = failure
            return

        quotes["items"] = []
        quotes["status"] = (
            "not_found"
            if reason == QUOTE_FAILURE_NO_QUOTES_FOUND
            else "failed"
        )
        quotes["failure"] = failure

    update_database_entry(
        database,
        title,
        update_quotes,
        DATABASE_FILE,
        data_folder,
        persistence_lock,
    )

    with stats_lock:
        stats["failed_quotes"] += 1

    return []

# max_quotes is retained for caller compatibility.
# Phase 3 preserves the existing behaviour: all valid quotes are stored.
def get_quotes(
    title,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    max_quotes=MAX_QUOTES,
    limiter=None,
    refresh_stale=False,
    refresh_current=False,
):
    entry = database.get(title)
    canonical_quotes = entry.get("quotes") if entry else None

    # A purged section deliberately retains no cache or retry evidence.  It is
    # therefore a fresh acquisition state if a later evaluation needs quotes.
    if (
        isinstance(canonical_quotes, dict)
        and canonical_quotes.get("status") == "purged"
    ):
        canonical_quotes = None

    # Normal callers retain legacy available caches without unexpectedly
    # fetching. The dedicated maintenance command opts into stale refresh.
    if (
        isinstance(canonical_quotes, dict)
        and canonical_quotes.get("status") == "available"
        and (
            not refresh_stale
            or (
                canonical_quotes.get("parser_version")
                == CURRENT_QUOTE_PARSER_VERSION
                and not refresh_current
            )
        )
    ):
        with stats_lock:
            stats["cached_quotes"] += 1

        return copy.deepcopy(canonical_quotes.get("items", []))

    # known failure
    previous = (
        canonical_quotes.get("failure")
        if isinstance(canonical_quotes, dict)
        else None
    )
    
    if previous:
        retries = previous.get("retries", 0)
    else:
        retries = 0
    
    if previous:
        failure = previous

        timestamp = failure["timestamp"]
        retries = failure.get("retries", 0)
        reason = failure["reason"]

        base_days = quote_retry_base_days(reason)

        RETRY_AFTER_DAYS = min(MAX_BACKOFF, base_days * (INITIAL_BACKOFF ** retries))

        RETRY_AFTER_SECONDS = RETRY_AFTER_DAYS * 24 * 60 * 60

        age = time.time() - timestamp

        if age < RETRY_AFTER_SECONDS:

            return []

        else:
            print("Retrying old failure:", title)

    seen = set()

    url_title = quote(title.replace(" ", "_"))

    url = f"{WIKIQUOTE_URL}{url_title}"

    request_result = safe_request(url, limiter=limiter)

    if not request_result.ok:
        reason = (
            request_result.error_reason
            or QUOTE_FAILURE_REQUEST_EXCEPTION
        )

        return record_quote_failure(
            title,
            reason,
            retries,
            database,
            stats,
            stats_lock,
            persistence_lock,
            data_folder,
        )

    response = request_result.response

    if response is None:
        return record_quote_failure(
            title,
            QUOTE_FAILURE_REQUEST_EXCEPTION,
            retries,
            database,
            stats,
            stats_lock,
            persistence_lock,
            data_folder,
        )

    soup = BeautifulSoup(
        response.text,
        "html.parser",
    )

    quotes = []

    # Main content only
    content = soup.find("div", class_="mw-parser-output")

    if not content:
        return record_quote_failure(
            title,
            QUOTE_FAILURE_PARSING_ERROR,
            retries,
            database,
            stats,
            stats_lock,
            persistence_lock,
            data_folder,
        )

    for candidate in iter_wikiquote_quote_candidates(content):

        text = extract_wikiquote_candidate_text(candidate.element)

        if is_bad_quote(text):
            continue

        # seen
        if text in seen:
            continue

        seen.add(text)

        quote_data = {
            "text": text,
            "length": len(text),
            "word_count": len(text.split()),
            "source": extract_wikiquote_quote_source(candidate),
            "retrieved_from": "Wikiquote",
        }

        quotes.append(quote_data)

    if quotes:

        def update_quotes(entry):
            entry["quotes"] = {
                "status": "available",
                "items": copy.deepcopy(quotes),
                "failure": None,
                "fetched_at": int(time.time()),
                "parser_version": CURRENT_QUOTE_PARSER_VERSION,
            }

        update_database_entry(
            database,
            title,
            update_quotes,
            DATABASE_FILE,
            data_folder,
            persistence_lock,
        )

        with stats_lock:
            stats["downloaded_quotes"] += 1

    if not quotes:
        return record_quote_failure(
            title,
            QUOTE_FAILURE_NO_QUOTES_FOUND,
            retries,
            database,
            stats,
            stats_lock,
            persistence_lock,
            data_folder,
            successful_parse=True,
        )

    return quotes

def get_random_quote(
    title,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    max_quotes=MAX_QUOTES,
    limiter=None,
    chooser=random.choices,
):

    quotes = get_quotes(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        max_quotes=MAX_QUOTES,
        limiter=limiter,
    )

    if not quotes:
        return None

    good_quotes = [
        q for q in quotes
        if 2 <= q["word_count"] <= 50
    ]

    if not good_quotes:
        good_quotes = quotes

    if not good_quotes:
        return None

    weights = [quote_selection_weight(quote) for quote in good_quotes]
    return chooser(good_quotes, weights=weights, k=1)[0]


def quote_selection_weight(quote):
    """Return a positive, length-based presentation weight for one quote."""
    if not isinstance(quote, dict):
        raise TypeError("quote must be an object")

    word_count = quote.get("word_count")
    if not isinstance(word_count, int) or isinstance(word_count, bool):
        raise TypeError("quote word_count must be an integer")

    return 1 / math.sqrt(max(word_count, 8))
