from html import escape
from copy import deepcopy
from dataclasses import dataclass
import re

from wiki_philosopher_bot.utils import clean_title
from wiki_philosopher_bot.config import MAX_QUOTES
from wiki_philosopher_bot.database_schema import (
    message_fingerprint,
    quote_fingerprint,
)
from wiki_philosopher_bot.wikipedia_api import get_random_quote


@dataclass(frozen=True)
class PreparedPhilosopherMessage:
    """Immutable snapshot of one exact future Telegram payload.

    The selected quote is copied when prepared so the result holds no mutable
    reference into the canonical in-memory database.  A later outbox phase can
    persist this exact message text without reselecting or reformatting it.
    """

    title: str
    selected_quote: dict
    message_text: str
    quote_fingerprint: str
    message_fingerprint: str


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
    """Select one quote using the unchanged existing quote-selection policy."""
    title = philosopher.get("title", "Unknown")
    kwargs = {
        "max_quotes": max_quotes,
        "limiter": limiter,
    }
    if chooser is not None:
        kwargs["chooser"] = chooser
    return get_random_quote(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        **kwargs,
    )


def prepare_philosopher_message(philosopher, selected_quote):
    """Build one deterministic Telegram payload from an exact selected quote."""
    if not isinstance(philosopher, dict):
        raise ValueError("philosopher must be an object")
    title = philosopher.get("title")
    if not isinstance(title, str) or not title:
        raise ValueError("philosopher.title must be a non-empty string")
    if not isinstance(selected_quote, dict):
        raise ValueError("selected_quote must be an object")
    if not isinstance(selected_quote.get("text"), str) or not selected_quote["text"]:
        raise ValueError("selected_quote.text must be a non-empty string")

    # This also requires the structured source required by durable quote
    # identity.  Presentation still renders citation-only fallbacks through
    # format_quote_attribution.
    selected_quote_fingerprint = quote_fingerprint(selected_quote)
    quote = deepcopy(selected_quote)

    wikidata = philosopher.get("wikidata", {})
    birth = wikidata.get("birth_year")
    death = wikidata.get("death_year")
    quote_text = normalize_quote_text(quote["text"])
    attribution = format_quote_attribution(quote)

    summary = philosopher.get("summary", {}).get("text")
    summary = summary or "No summary available."
    
    years = format_life_years(birth, death)

    wiki_title = title.replace(" ", "_")

    wiki_url = f"https://en.wikipedia.org/wiki/{wiki_title}"

    display_title = philosopher.get("display_title") or clean_title(title)

    display_title = escape(display_title)
    quote_text = escape(quote_text)
    attribution = escape(attribution) if attribution else ""
    summary = escape(summary)

    message = f"""
    <b>{display_title} {years}</b>

    <i>{quote_text}</i>

    {attribution}

    {summary}

    <a href="{wiki_url}">Wikipedia article</a>
    """

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
