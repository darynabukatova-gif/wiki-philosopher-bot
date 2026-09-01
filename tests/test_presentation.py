import threading

import pytest

import wiki_philosopher_bot.presentation as presentation
import wiki_philosopher_bot.database_schema as database_schema
from wiki_philosopher_bot.config import MAX_QUOTES
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
    monkeypatch.setattr(presentation, "get_random_quote", lambda *args, **kwargs: quote)

    message = presentation.format_philosopher_message(
        philosopher,
        {"Ada Lovelace": philosopher},
        {"cached_quotes": 0, "downloaded_quotes": 0, "failed_quotes": 0},
        threading.Lock(),
        threading.Lock(),
        "temporary-data",
    )

    assert "— Work &lt; &amp; &gt; (1843), p. 47" in message

def test_format_candidate_message_forwards_runtime_state(
    monkeypatch,
):
    philosopher = make_empty_database_entry("Ada Lovelace")
    database = {"Ada Lovelace": philosopher}
    stats = {
        "cached_quotes": 0,
        "downloaded_quotes": 0,
        "failed_quotes": 0,
    }

    stats_lock = threading.Lock()
    persistence_lock = threading.Lock()
    limiter = object()

    captured = {}

    def fake_get_random_quote(
        title,
        database_arg,
        stats_arg,
        stats_lock_arg,
        persistence_lock_arg,
        data_folder_arg,
        max_quotes,
        limiter=None,
    ):
        captured["title"] = title
        captured["database"] = database_arg
        captured["stats"] = stats_arg
        captured["stats_lock"] = stats_lock_arg
        captured["persistence_lock"] = persistence_lock_arg
        captured["data_folder"] = data_folder_arg
        captured["max_quotes"] = max_quotes
        captured["limiter"] = limiter

        return structured_quote("A synthetic quotation for testing.")

    monkeypatch.setattr(
        presentation,
        "get_random_quote",
        fake_get_random_quote,
    )

    result = presentation.format_philosopher_message(
        philosopher,
        database,
        stats,
        stats_lock,
        persistence_lock,
        "temporary-data",
        max_quotes=MAX_QUOTES,
        limiter=limiter,
    )

    assert captured["title"] == "Ada Lovelace"
    assert captured["database"] is database
    assert captured["stats"] is stats
    assert captured["stats_lock"] is stats_lock
    assert captured["persistence_lock"] is persistence_lock
    assert captured["data_folder"] == "temporary-data"
    assert captured["limiter"] is limiter

    assert isinstance(result, str)


def test_format_philosopher_message_reads_summary_and_years_from_canonical_entry(
    monkeypatch,
):
    philosopher = make_empty_database_entry("Ada Lovelace")
    philosopher["summary"]["text"] = "A canonical summary."
    philosopher["wikidata"]["birth_year"] = 1815
    philosopher["wikidata"]["death_year"] = 1852
    database = {"Ada Lovelace": philosopher}

    monkeypatch.setattr(
        presentation,
        "get_random_quote",
        lambda *args, **kwargs: structured_quote("A canonical quote."),
    )

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


def test_format_philosopher_message_formats_thales_bce_years(monkeypatch):
    philosopher = make_empty_database_entry("Thales of Miletus")
    philosopher["summary"]["text"] = "A philosopher."
    philosopher["wikidata"]["birth_year"] = -650
    philosopher["wikidata"]["death_year"] = -548

    monkeypatch.setattr(
        presentation,
        "get_random_quote",
        lambda *args, **kwargs: structured_quote("A canonical quote."),
    )

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
    philosopher["quotes"]["items"] = [quote]

    monkeypatch.setattr(
        presentation,
        "get_random_quote",
        lambda *args, **kwargs: philosopher["quotes"]["items"][0],
    )

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
    with pytest.raises(ValueError, match="selected_quote"):
        presentation.prepare_philosopher_message(philosopher, None)
    with pytest.raises(ValueError, match="structured"):
        presentation.prepare_philosopher_message(philosopher, {"text": "Missing source."})


def test_selection_helper_renders_the_exact_quote_chosen_by_injected_chooser(monkeypatch):
    philosopher = make_empty_database_entry("Ada Lovelace")
    quotes = [
        structured_quote("First exact quote."),
        structured_quote("Second exact quote."),
    ]
    monkeypatch.setattr(presentation, "get_random_quote", lambda *args, **kwargs: quotes[1])

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
    captured = {}

    def fake_get_random_quote(*args, **kwargs):
        captured["chooser"] = kwargs["chooser"]
        return quote

    chooser = object()
    monkeypatch.setattr(presentation, "get_random_quote", fake_get_random_quote)

    assert presentation.select_quote_for_post(
        philosopher, {}, {}, threading.Lock(), threading.Lock(), "unused", chooser=chooser,
    ) is quote
    assert captured["chooser"] is chooser
