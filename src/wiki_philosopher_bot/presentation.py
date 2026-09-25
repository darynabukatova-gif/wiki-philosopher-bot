from html import escape
from copy import deepcopy
from dataclasses import dataclass
import random
import re
from typing import Optional

from wiki_philosopher_bot.utils import clean_title
from wiki_philosopher_bot.config import MAX_QUOTES, TELEGRAM_TEXT_MAX_LENGTH
from wiki_philosopher_bot.database_schema import (
    is_valid_external_link,
    message_fingerprint,
    quote_fingerprint,
)
from wiki_philosopher_bot.wikipedia_api import select_cached_quote
from wiki_philosopher_bot.utils import has_current_usable_quotes


@dataclass(frozen=True)
class PreparedPhilosopherMessage:
    """Immutable snapshot of one exact future Telegram payload.

    The selected quote is copied when prepared so the result holds no mutable
    reference into the canonical in-memory database.  A later outbox phase can
    persist this exact message text without reselecting or reformatting it.
    """

    title: str
    selected_quote: Optional[dict]
    message_text: str
    quote_fingerprint: Optional[str]
    message_fingerprint: str


def format_external_reading_links(external_links):
    """Return compact Telegram HTML links for stored external reading pages.

    These links are record-level reading resources, deliberately separate from
    the selected quote's bibliographic ``source``.
    """
    if not isinstance(external_links, dict):
        return ""
    available = []
    for key, label in (
        ("wikiquote", "Wikiquote"),
        ("wikisource", "Wikisource"),
        ("project_gutenberg", "Gutenberg"),
    ):
        url = external_links.get(key)
        # Preserve established Wikiquote/Wikisource rendering, including URLs
        # with escaped query strings. Gutenberg has no legacy presentation
        # behaviour, so admit it only when its stored schema value is valid.
        valid_for_presentation = (
            isinstance(url, str)
            and bool(url)
            and (
                key != "project_gutenberg"
                or is_valid_external_link(key, url)
            )
        )
        if valid_for_presentation:
            available.append(
                '<a href="{}">{}</a>'.format(
                    escape(url, quote=True), escape(label, quote=False),
                )
            )
    return " · ".join(available)


def normalize_quote_text(text: str) -> str:
    """Conservatively clean display-only spacing in quote text.

    Canonical quote text is intentionally left untouched; callers should use
    this only while constructing presentation output.
    """
    if not isinstance(text, str):
        raise TypeError("quote text must be a string")

    text = re.sub(r"[ \t]+([,.;:?!])", r"\1", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def format_quote_attribution(quote):
    """Return one display-safe source line for a current-parser quote."""
    if not isinstance(quote, dict):
        return None
    source = quote.get("source")
    if not isinstance(source, dict):
        return None

    work = source.get("work")
    year = source.get("year")
    date = source.get("date")
    details = source.get("details")
    citation = source.get("citation")
    if isinstance(work, str) and work.strip():
        line = work.strip()
        temporal = date if isinstance(date, str) and date.strip() else year
        if temporal is not None:
            line += " ({})".format(temporal)
        if isinstance(details, str) and details.strip():
            line += ", " + details.strip()
        return "— " + line
    # For a structurally parsed hierarchy, concise locators are safer and
    # more readable than replaying an arbitrary long section citation.
    if (
        isinstance(details, str)
        and details.strip()
        and re.match(r"^Vol\.?\s+", details.strip(), re.IGNORECASE)
    ):
        temporal = date if isinstance(date, str) and date.strip() else year
        line = details.strip()
        if temporal is not None:
            line += " ({})".format(temporal)
        return "— " + line
    if isinstance(citation, str) and citation.strip():
        return "— " + citation.strip()
    return None


def format_life_year(year, *, force_ce=False):
    """Format one signed canonical life year without inferring precision."""
    if not isinstance(year, int) or isinstance(year, bool):
        raise TypeError("life year must be an integer")
    if year < 0:
        return "{} BCE".format(-year)
    if force_ce:
        return "{} CE".format(year)
    return str(year)


def format_life_years(birth_year, death_year):
    """Return a compact source-neutral BCE/CE life-date suffix."""
    for year in (birth_year, death_year):
        if year is not None and (
            not isinstance(year, int) or isinstance(year, bool)
        ):
            raise TypeError("life years must be integers or null")

    if birth_year is not None and death_year is not None:
        mixed_eras = (birth_year < 0) != (death_year < 0)
        return "({}–{})".format(
            format_life_year(birth_year, force_ce=mixed_eras and birth_year >= 0),
            format_life_year(death_year, force_ce=mixed_eras and death_year >= 0),
        )
    if birth_year is not None:
        return "(born {})".format(format_life_year(birth_year))
    if death_year is not None:
        return "(died {})".format(format_life_year(death_year))
    return ""


def summary_leading_life_year_range(entry):
    """Return an unambiguous leading Wikipedia life-year range, if present.

    This deliberately recognises only the narrow form used for presentation
    conflict protection: the stored summary must begin with the canonical or
    display title followed by ``(YYYY–YYYY)`` (or a plain hyphen). It is not a
    general-purpose natural-language date parser and must not be used to
    backfill structured Wikidata dates.
    """
    if not isinstance(entry, dict):
        return None
    summary = entry.get("summary")
    if (
        not isinstance(summary, dict)
        or summary.get("source") != "Wikipedia"
        or not isinstance(summary.get("text"), str)
        or not summary["text"]
    ):
        return None

    titles = []
    for value in (entry.get("display_title"), entry.get("title")):
        if isinstance(value, str) and value and value not in titles:
            titles.append(value)

    for title in titles:
        match = re.match(
            r"^{}\s*\(([1-9]\d{{3}})\s*(?:–|-)\s*([1-9]\d{{3}})\)".format(
                re.escape(title)
            ),
            summary["text"],
        )
        if match is not None:
            return int(match.group(1)), int(match.group(2))
    return None


def has_definite_summary_life_date_conflict(entry):
    """Whether structured dates contradict a strict stored summary range.

    Neither source is chosen as authoritative here. A positive result merely
    prevents one Telegram message from displaying mutually contradictory date
    claims in its heading and summary.
    """
    summary_years = summary_leading_life_year_range(entry)
    if summary_years is None or not isinstance(entry, dict):
        return False
    wikidata = entry.get("wikidata")
    if not isinstance(wikidata, dict):
        return False
    birth_year = wikidata.get("birth_year")
    death_year = wikidata.get("death_year")
    if (
        not isinstance(birth_year, int)
        or isinstance(birth_year, bool)
        or not isinstance(death_year, int)
        or isinstance(death_year, bool)
    ):
        return False
    return summary_years != (birth_year, death_year)


def select_quote_for_post(
    philosopher,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    max_quotes=MAX_QUOTES,
    limiter=None,
    chooser=None,
):
    """Select only a current cached quote; never fetch during preparation."""
    # Keep the established call signature while making all non-selection
    # runtime arguments intentionally irrelevant to this pure cached path.
    del database, stats, stats_lock, persistence_lock, data_folder, max_quotes, limiter
    if not has_current_usable_quotes(philosopher):
        return None
    return select_cached_quote(
        philosopher["quotes"]["items"],
        chooser=chooser if chooser is not None else random.choices,
    )


def telegram_message_fits_limit(message_text):
    """Conservatively enforce Telegram's configured text payload limit."""
    return isinstance(message_text, str) and len(message_text) <= TELEGRAM_TEXT_MAX_LENGTH


def _render_philosopher_message(
    display_title,
    years,
    summary,
    quote_text,
    attribution,
    wiki_url,
    external_reading_links,
):
    heading = "{} {}".format(display_title, years).strip()
    lines = ["<b>{}</b>".format(heading), "", summary]
    if quote_text is not None:
        lines.extend(("", "<i>{}</i>".format(quote_text)))
        if attribution:
            lines.extend(("", attribution))
    lines.extend(("", '<a href="{}">Wikipedia article</a>'.format(wiki_url)))
    if external_reading_links:
        lines.extend(("", external_reading_links))
    return "\n".join(lines)


def prepare_philosopher_message(philosopher, selected_quote=None):
    """Build one deterministic summary-first Telegram payload.

    Quotes are optional enrichment. A quote that makes the exact HTML payload
    exceed Telegram's limit is omitted rather than truncated or reselected.
    """
    if not isinstance(philosopher, dict):
        raise ValueError("philosopher must be an object")
    title = philosopher.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError("philosopher.title must be a non-empty string")
    if selected_quote is not None:
        if not isinstance(selected_quote, dict):
            raise ValueError("selected_quote must be an object or null")
        if not isinstance(selected_quote.get("text"), str) or not selected_quote["text"]:
            raise ValueError("selected_quote.text must be a non-empty string")
        selected_quote_fingerprint = quote_fingerprint(selected_quote)
        quote = deepcopy(selected_quote)
    else:
        selected_quote_fingerprint = None
        quote = None

    wikidata = philosopher.get("wikidata", {})
    birth = wikidata.get("birth_year")
    death = wikidata.get("death_year")
    quote_text = normalize_quote_text(quote["text"]) if quote is not None else None
    attribution = format_quote_attribution(quote) if quote is not None else None

    summary = philosopher.get("summary", {}).get("text")
    summary = summary or "No summary available."
    
    years = (
        "" if has_definite_summary_life_date_conflict(philosopher)
        else format_life_years(birth, death)
    )

    wiki_title = title.replace(" ", "_")

    wiki_url = f"https://en.wikipedia.org/wiki/{wiki_title}"
    external_reading_links = format_external_reading_links(
        philosopher.get("external_links")
    )

    display_title = philosopher.get("display_title") or clean_title(title)

    display_title = escape(display_title)
    quote_text = escape(quote_text) if quote_text is not None else None
    attribution = escape(attribution) if attribution else ""
    summary = escape(summary)

    message = _render_philosopher_message(
        display_title,
        years,
        summary,
        quote_text,
        attribution,
        wiki_url,
        external_reading_links,
    )
    if quote is not None and not telegram_message_fits_limit(message):
        quote = None
        selected_quote_fingerprint = None
        message = _render_philosopher_message(
            display_title,
            years,
            summary,
            None,
            None,
            wiki_url,
            external_reading_links,
        )
    if not telegram_message_fits_limit(message):
        raise ValueError(
            "Prepared Telegram message exceeds the {}-character limit".format(
                TELEGRAM_TEXT_MAX_LENGTH
            )
        )

    return PreparedPhilosopherMessage(
        title=title,
        selected_quote=quote,
        message_text=message,
        quote_fingerprint=selected_quote_fingerprint,
        message_fingerprint=message_fingerprint(message),
    )


def format_philosopher_message(
    philosopher,
    database,
    stats,
    stats_lock,
    persistence_lock,
    data_folder,
    max_quotes=MAX_QUOTES,
    limiter=None,
):
    """Compatibility adapter for the pre-outbox one-process posting flow."""
    selected_quote = select_quote_for_post(
        philosopher,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        max_quotes=max_quotes,
        limiter=limiter,
    )
    return prepare_philosopher_message(
        philosopher,
        selected_quote,
    ).message_text
