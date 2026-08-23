from html import escape
import re

from utils import clean_title
from config import MAX_QUOTES
from wikipedia_api import get_random_quote


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
    
    title = philosopher.get("title", "Unknown")

    wikidata = philosopher.get("wikidata", {})
    birth = wikidata.get("birth_year")
    death = wikidata.get("death_year")

    quote = get_random_quote(
        title,
        database,
        stats,
        stats_lock,
        persistence_lock,
        data_folder,
        max_quotes=MAX_QUOTES,
        limiter=limiter,
    )

    if quote:
        quote_text = normalize_quote_text(quote["text"])
        attribution = format_quote_attribution(quote)
    else:
        quote_text = "No quote found."
        attribution = None

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

    return message
