import threading

import pytest

import wiki_philosopher_bot.presentation as presentation
import wiki_philosopher_bot.database_schema as database_schema
from wiki_philosopher_bot.config import (
    CURRENT_QUOTE_PARSER_VERSION,
    MAX_QUOTES,
    TELEGRAM_TEXT_MAX_LENGTH,
)
from wiki_philosopher_bot.database_schema import make_empty_database_entry


def structured_quote(text):
    return {
        "text": text,
        "source": {
            "work": None,
            "year": None,
            "date": None,
            "details": None,
            "citation": None,
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }


def set_current_quote(philosopher, quote):
    philosopher["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [quote],
    })


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("God , about freedom", "God, about freedom"),
        ("word .", "word."),
        ("word ; next", "word; next"),
        ("word : next", "word: next"),
        ("word ?", "word?"),
        ("word !", "word!"),
        ("one   two", "one two"),
    ),
)
def test_normalize_quote_text_fixes_conservative_spacing(raw, expected):
    assert presentation.normalize_quote_text(raw) == expected


def test_normalize_quote_text_preserves_newlines_and_existing_text():
    raw = "First paragraph.\n\nSecond   paragraph with 'apostrophes' and \"quotes\"."

    assert presentation.normalize_quote_text(raw) == (
        "First paragraph.\n\nSecond paragraph with 'apostrophes' and \"quotes\"."
    )


def test_normalize_quote_text_rejects_non_string_input():
    with pytest.raises(TypeError, match="quote text must be a string"):
        presentation.normalize_quote_text(None)


def test_format_quote_attribution_prefers_structured_source_and_ignores_legacy_source():
    assert presentation.format_quote_attribution({
        "source": {
            "work": "Tractatus Logico-Philosophicus",
            "year": 1921,
            "date": None,
            "details": "§5.6",
            "citation": "Tractatus Logico-Philosophicus (1921), §5.6",
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }) == "— Tractatus Logico-Philosophicus (1921), §5.6"
    assert presentation.format_quote_attribution({"source": "Wikiquote"}) is None


def test_format_quote_attribution_keeps_hierarchical_citation_concise_without_work():
    assert presentation.format_quote_attribution({
        "source": {
            "work": None,
            "year": 1772,
            "date": None,
            "details": "Vol. I, Part I, § 1",
            "citation": (
                "Vol. I: Part I: The Being and Attributes of God, § 1: Of "
                "the existence of God, and those attributes which art deduced "
                "from his being considered as uncaused himself, and the cause "
                "of every thing else (1772)"
            ),
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }) == "— Vol. I, Part I, § 1 (1772)"


def test_format_quote_attribution_keeps_parent_work_and_hierarchy_concise():
    assert presentation.format_quote_attribution({
        "source": {
            "work": "Institutes of Natural and Revealed Religion",
            "year": 1772,
            "date": None,
            "details": "Vol. I, Part I, § 1",
            "citation": "A deliberately longer preserved source citation.",
            "url": "https://en.wikiquote.org/wiki/Institutes",
        },
        "retrieved_from": "Wikiquote",
    }) == "— Institutes of Natural and Revealed Religion (1772), Vol. I, Part I, § 1"


def test_format_quote_attribution_renders_named_subsection_under_parent_work():
    assert presentation.format_quote_attribution({
        "source": {
            "work": "The Order of Things: An Archaeology of the Human Sciences",
            "year": 1970,
            "date": None,
            "details": "Las Meninas",
            "citation": "Las Meninas",
            "url": "https://en.wikiquote.org/wiki/The_Order_of_Things",
        },
        "retrieved_from": "Wikiquote",
    }) == "— The Order of Things: An Archaeology of the Human Sciences (1970), Las Meninas"


def test_format_philosopher_message_escapes_structured_attribution(monkeypatch):
    philosopher = make_empty_database_entry("Ada Lovelace")
    quote = {
        "text": "A canonical quote.",
        "source": {
            "work": "Work < & >",
            "year": 1843,
            "date": None,
            "details": "p. 47",
            "citation": "Work < & > (1843), p. 47",
            "url": None,
        },
        "retrieved_from": "Wikiquote",
    }
    set_current_quote(philosopher, quote)
    monkeypatch.setattr(presentation, "select_cached_quote", lambda *args, **kwargs: quote)

    message = presentation.format_philosopher_message(
        philosopher,
        {"Ada Lovelace": philosopher},
        {"cached_quotes": 0, "downloaded_quotes": 0, "failed_quotes": 0},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
    )

    assert "— Work &lt; &amp; &gt; (1843), p. 47" in message

def test_format_candidate_message_uses_only_current_cached_quote(monkeypatch):
    philosopher = make_empty_database_entry("Ada Lovelace")
    quote = structured_quote("A synthetic quotation for testing.")
    set_current_quote(philosopher, quote)
    captured = {}

    def fake_select_cached_quote(items, chooser):
        captured["items"] = items
        captured["chooser"] = chooser
        return quote

    monkeypatch.setattr(presentation, "select_cached_quote", fake_select_cached_quote)

    result = presentation.format_philosopher_message(
        philosopher,
        {"Ada Lovelace": philosopher},
        {},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
        max_quotes=MAX_QUOTES,
        limiter=object(),
    )

    assert captured["items"] == [quote]
    assert isinstance(result, str)


def test_format_philosopher_message_reads_summary_and_years_from_canonical_entry(
    monkeypatch,
):
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "A canonical summary."
    philosopher["wikidata"]["birth_year"] = 1815
    philosopher["wikidata"]["death_year"] = 1852
    database = {"Ada Lovelace": philosopher}

    quote = structured_quote("A canonical quote.")
    set_current_quote(philosopher, quote)
    monkeypatch.setattr(presentation, "select_cached_quote", lambda *args, **kwargs: quote)

    message = presentation.format_philosopher_message(
        philosopher,
        database,
        {"cached_quotes": 0, "downloaded_quotes": 0, "failed_quotes": 0},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
    )

    assert "<b>Ada Lovelace (1815–1852)</b>" in message
    assert "<i>A canonical quote.</i>" in message
    assert "A canonical summary." in message
    assert "https://en.wikipedia.org/wiki/Ada_Lovelace" in message


@pytest.mark.parametrize(
    ("birth", "death", "expected"),
    [
        (-650, -548, "(650 BCE–548 BCE)"),
        (-44, 5, "(44 BCE–5 CE)"),
        (1951, 2020, "(1951–2020)"),
        (5, 2020, "(5–2020)"),
        (-650, None, "(born 650 BCE)"),
        (1951, None, "(born 1951)"),
        (None, -44, "(died 44 BCE)"),
        (None, None, ""),
    ],
)
def test_format_life_years_handles_bce_ce_and_unknown_dates(
    birth,
    death,
    expected,
):
    assert presentation.format_life_years(birth, death) == expected


def test_summary_life_date_conflict_suppresses_only_heading_years():
    philosopher = make_empty_database_entry("Catherine Descartes")
    original_summary = (
        "Catherine Descartes (1637–1706) was a French poet and philosopher."
    )
    philosopher["summary"]["text"] = original_summary
    philosopher["wikidata"]["birth_year"] = 1637
    philosopher["wikidata"]["death_year"] = 1715

    assert presentation.summary_leading_life_year_range(philosopher) == (1637, 1706)
    assert presentation.has_definite_summary_life_date_conflict(philosopher) is True

    message = presentation.prepare_philosopher_message(philosopher, None).message_text

    assert "<b>Catherine Descartes</b>" in message
    assert "(1637–1715)" not in message
    assert original_summary in message
    assert philosopher["summary"]["text"] == original_summary


def test_matching_summary_life_years_keep_normal_heading_years():
    philosopher = make_empty_database_entry("Catherine Descartes")
    philosopher["summary"]["text"] = "Catherine Descartes (1637-1706) was a philosopher."
    philosopher["wikidata"]["birth_year"] = 1637
    philosopher["wikidata"]["death_year"] = 1706

    assert presentation.has_definite_summary_life_date_conflict(philosopher) is False
    assert "<b>Catherine Descartes (1637–1706)</b>" in (
        presentation.prepare_philosopher_message(philosopher, None).message_text
    )


@pytest.mark.parametrize(
    ("summary", "birth_year", "death_year"),
    (
        ("Catherine Descartes (1637–1706) was a philosopher.", None, None),
        ("Another person (1637–1706) was a philosopher.", 1637, 1715),
        ("Catherine Descartes (born 1949) is a philosopher.", 1949, 2000),
        ("", 1637, 1715),
        ("Catherine Descartes (c. 1637–1706) was a philosopher.", 1637, 1715),
    ),
)
def test_only_strict_leading_positive_year_ranges_can_be_conflicts(
    summary,
    birth_year,
    death_year,
):
    philosopher = make_empty_database_entry("Catherine Descartes")
    philosopher["summary"]["text"] = summary
    philosopher["wikidata"]["birth_year"] = birth_year
    philosopher["wikidata"]["death_year"] = death_year

    assert presentation.has_definite_summary_life_date_conflict(philosopher) is False


def test_format_philosopher_message_formats_thales_bce_years(monkeypatch):
    philosopher = make_empty_database_entry("Thales of Miletus")
    philosopher["summary"]["text"] = "A philosopher."
    philosopher["wikidata"]["birth_year"] = -650
    philosopher["wikidata"]["death_year"] = -548

    quote = structured_quote("A canonical quote.")
    set_current_quote(philosopher, quote)
    monkeypatch.setattr(presentation, "select_cached_quote", lambda *args, **kwargs: quote)

    message = presentation.format_philosopher_message(
        philosopher,
        {philosopher["title"]: philosopher},
        {},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
    )

    assert "<b>Thales of Miletus (650 BCE–548 BCE)</b>" in message


def test_format_philosopher_message_normalizes_display_quote_without_mutating_canonical_quote(
    monkeypatch,
):
    philosopher = make_empty_database_entry("Martin Heidegger")
    philosopher["summary"]["text"] = "A canonical summary with < & >."
    stored_quote = (
        "Existential analytics [the object of the book] decides nothing "
        "about the existence of God , about human freedom and the "
        "immortality of the soul. < & >"
    )
    quote = structured_quote(stored_quote)
    set_current_quote(philosopher, quote)
    monkeypatch.setattr(presentation, "select_cached_quote", lambda *args, **kwargs: quote)

    message = presentation.format_philosopher_message(
        philosopher,
        {"Martin Heidegger": philosopher},
        {"cached_quotes": 0, "downloaded_quotes": 0, "failed_quotes": 0},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
    )

    assert stored_quote == quote["text"]
    assert philosopher["quotes"]["items"] == [structured_quote(stored_quote)]
    assert "God, about human freedom" in message
    assert "God , about human freedom" not in message
    assert "&lt; &amp; &gt;" in message
    assert "A canonical summary with &lt; &amp; &gt;." in message


def test_prepare_philosopher_message_is_deterministic_and_snapshots_selected_quote():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "A canonical summary."
    quote = structured_quote("A canonical quote.")

    first = presentation.prepare_philosopher_message(philosopher, quote)
    second = presentation.prepare_philosopher_message(philosopher, quote)

    assert first == second
    assert first.quote_fingerprint == database_schema.quote_fingerprint(quote)
    assert first.message_fingerprint == database_schema.message_fingerprint(first.message_text)
    assert "<i>A canonical quote.</i>" in first.message_text
    quote["text"] = "Mutated after preparation."
    assert first.selected_quote["text"] == "A canonical quote."


def test_prepare_message_renders_stored_wikiquote_link_only():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["external_links"]["wikiquote"] = (
        "https://en.wikiquote.org/wiki/Ada_Lovelace"
    )

    message = presentation.prepare_philosopher_message(
        philosopher, structured_quote("A canonical quote."),
    ).message_text

    assert '<a href="https://en.wikiquote.org/wiki/Ada_Lovelace">Wikiquote</a>' in message
    assert "Wikisource" not in message
    assert "Gutenberg" not in message


def test_prepare_message_renders_all_external_reading_links_as_final_line():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["external_links"].update({
        "wikiquote": "https://en.wikiquote.org/wiki/Ada_Lovelace",
        "wikisource": "https://en.wikisource.org/wiki/Author:Ada_Lovelace",
        "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
    })

    prepared = presentation.prepare_philosopher_message(
        philosopher, structured_quote("A canonical quote."),
    )
    repeated = presentation.prepare_philosopher_message(
        philosopher, structured_quote("A canonical quote."),
    )

    assert (
        '<a href="https://en.wikiquote.org/wiki/Ada_Lovelace">Wikiquote</a> · '
        '<a href="https://en.wikisource.org/wiki/Author:Ada_Lovelace">Wikisource</a> · '
        '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>'
    ) in prepared.message_text
    assert prepared.message_fingerprint == database_schema.message_fingerprint(
        prepared.message_text
    )
    assert repeated == prepared


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        (
            {
                "wikiquote": "https://en.wikiquote.org/wiki/Ada",
                "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
                "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
            },
            '<a href="https://en.wikiquote.org/wiki/Ada">Wikiquote</a> · '
            '<a href="https://en.wikisource.org/wiki/Author:Ada">Wikisource</a> · '
            '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>',
        ),
        (
            {
                "wikiquote": "https://en.wikiquote.org/wiki/Ada",
                "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
            },
            '<a href="https://en.wikiquote.org/wiki/Ada">Wikiquote</a> · '
            '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>',
        ),
        (
            {
                "wikisource": "https://en.wikisource.org/wiki/Author:Ada",
                "project_gutenberg": "https://www.gutenberg.org/ebooks/author/380",
            },
            '<a href="https://en.wikisource.org/wiki/Author:Ada">Wikisource</a> · '
            '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>',
        ),
        (
            {"project_gutenberg": "https://www.gutenberg.org/ebooks/author/380"},
            '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>',
        ),
        ({}, ""),
    ],
)
def test_external_reading_link_renderer_supports_every_available_combination(links, expected):
    assert presentation.format_external_reading_links(links) == expected


def test_external_reading_link_renderer_omits_invalid_gutenberg_value():
    assert presentation.format_external_reading_links({
        "project_gutenberg": "http://www.gutenberg.org/ebooks/author/380",
    }) == ""


def test_external_reading_link_renderer_escapes_urls_and_omits_unavailable_values():
    rendered = presentation.format_external_reading_links({
        "wikiquote": "https://en.wikiquote.org/wiki/Ada?one=1&two=2",
        "wikisource": None,
    })

    assert rendered == (
        '<a href="https://en.wikiquote.org/wiki/Ada?one=1&amp;two=2">Wikiquote</a>'
    )


def test_prepared_message_fingerprint_includes_stored_gutenberg_link():
    philosopher = make_empty_database_entry("Ada Lovelace")
    quote = structured_quote("A canonical quote.")
    without_gutenberg = presentation.prepare_philosopher_message(philosopher, quote)
    philosopher["external_links"]["project_gutenberg"] = (
        "https://www.gutenberg.org/ebooks/author/380"
    )
    with_gutenberg = presentation.prepare_philosopher_message(philosopher, quote)

    assert '<a href="https://www.gutenberg.org/ebooks/author/380">Gutenberg</a>' in with_gutenberg.message_text
    assert with_gutenberg.message_fingerprint == database_schema.message_fingerprint(
        with_gutenberg.message_text
    )
    assert with_gutenberg.message_fingerprint != without_gutenberg.message_fingerprint


def test_prepare_philosopher_message_changes_with_selected_quote_and_rejects_invalid_quote():
    philosopher = make_empty_database_entry("Ada Lovelace")
    first = presentation.prepare_philosopher_message(
        philosopher, structured_quote("First exact quote."),
    )
    second = presentation.prepare_philosopher_message(
        philosopher, structured_quote("Second exact quote."),
    )

    assert first.quote_fingerprint != second.quote_fingerprint
    assert first.message_fingerprint != second.message_fingerprint
    summary_only = presentation.prepare_philosopher_message(philosopher, None)
    assert summary_only.selected_quote is None
    assert summary_only.quote_fingerprint is None
    with pytest.raises(ValueError, match="structured"):
        presentation.prepare_philosopher_message(philosopher, {"text": "Missing source."})


def test_summary_is_rendered_before_optional_quote_and_summary_only_has_no_artifacts():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "The required summary."
    quote = structured_quote("Optional quote.")

    with_quote = presentation.prepare_philosopher_message(philosopher, quote)
    without_quote = presentation.prepare_philosopher_message(philosopher, None)

    assert with_quote.message_text.index("The required summary.") < with_quote.message_text.index("Optional quote.")
    assert "<i>" not in without_quote.message_text
    assert "— " not in without_quote.message_text
    assert without_quote.quote_fingerprint is None


@pytest.mark.parametrize(
    "quote_state",
    (
        {"status": "available", "parser_version": CURRENT_QUOTE_PARSER_VERSION - 1, "items": [structured_quote("Stale quote.")]},
        {"status": "failed", "parser_version": CURRENT_QUOTE_PARSER_VERSION, "items": [structured_quote("Failed quote.")]},
        {"status": "not_found", "parser_version": CURRENT_QUOTE_PARSER_VERSION, "items": []},
        {"status": "available", "parser_version": CURRENT_QUOTE_PARSER_VERSION, "items": []},
    ),
)
def test_stale_or_unavailable_quotes_are_not_selected_from_cache(monkeypatch, quote_state):
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["quotes"].update(quote_state)
    monkeypatch.setattr(
        presentation,
        "select_cached_quote",
        lambda *args, **kwargs: pytest.fail("stale quote must not be selected"),
    )

    assert presentation.select_quote_for_post(
        philosopher, {}, {}, threading.Lock(), threading.Lock(), "unused",
    ) is None


def test_over_limit_quote_is_dropped_but_summary_only_payload_is_retained():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "S" * 100
    quote = structured_quote("Q" * TELEGRAM_TEXT_MAX_LENGTH)

    prepared = presentation.prepare_philosopher_message(philosopher, quote)

    assert prepared.selected_quote is None
    assert prepared.quote_fingerprint is None
    assert "<i>" not in prepared.message_text
    assert presentation.telegram_message_fits_limit(prepared.message_text)


def test_over_limit_summary_only_payload_fails_without_truncation():
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "S" * (TELEGRAM_TEXT_MAX_LENGTH + 1)

    with pytest.raises(ValueError, match="exceeds"):
        presentation.prepare_philosopher_message(philosopher, None)


def test_selection_helper_renders_the_exact_quote_chosen_by_injected_chooser(monkeypatch):
    philosopher = make_empty_database_entry("Ada Lovelace")
    quotes = [
        structured_quote("First exact quote."),
        structured_quote("Second exact quote."),
    ]
    philosopher["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": quotes,
    })
    monkeypatch.setattr(presentation, "select_cached_quote", lambda *args, **kwargs: quotes[1])

    selected = presentation.select_quote_for_post(
        philosopher, {philosopher["title"]: philosopher}, {}, threading.Lock(), threading.Lock(), "unused",
    )
    prepared = presentation.prepare_philosopher_message(philosopher, selected)

    assert selected is quotes[1]
    assert "Second exact quote." in prepared.message_text
    assert "First exact quote." not in prepared.message_text


def test_selection_helper_forwards_an_injected_chooser(monkeypatch):
    philosopher = make_empty_database_entry("Ada Lovelace")
    quote = structured_quote("Selected by injected chooser.")
    set_current_quote(philosopher, quote)
    captured = {}

    def fake_select_cached_quote(items, chooser):
        captured["chooser"] = chooser
        return quote

    chooser = object()
    monkeypatch.setattr(presentation, "select_cached_quote", fake_select_cached_quote)

    assert presentation.select_quote_for_post(
        philosopher, {}, {}, threading.Lock(), threading.Lock(), "unused", chooser=chooser,
    ) is quote
    assert captured["chooser"] is chooser
