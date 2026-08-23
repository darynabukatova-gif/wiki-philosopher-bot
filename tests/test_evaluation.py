import threading
import copy
import pytest
import cache
import evaluation
from database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
    validate_database_entry,
)
from config import CURRENT_EVALUATION_ALGORITHM_VERSION


def write_canonical_database(tmp_path, entries):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path

def test_title_filter_returns_named_filter_result():
    result = evaluation.title_filter("Ada Lovelace (philosopher)")

    assert isinstance(result, evaluation.FilterResult)
    assert result.philosopher_bonus == 2
    assert result.human_bonus == 1
    assert result.content_bonus == 0
    assert result.nonphilosopher_penalty == 0
    assert result.nonhuman_penalty == 0
    assert result.noncontent_penalty == 0
    assert result.reasons == [
        "title human bonus (+1): exact (philosopher)",
        "title philosopher bonus (+2): exact (philosopher)",
    ]

def test_title_filter_does_not_use_unsafe_raw_substring_exclusions():
    result = evaluation.title_filter("Utilitarianism")

    assert isinstance(result, evaluation.FilterResult)
    assert result == evaluation.FilterResult()


@pytest.mark.parametrize(
    "title",
    [
        "Boethius (disambiguation)",
        "Philosopher's Stone (disambiguation)",
    ],
)
def test_title_filter_marks_disambiguation_pages_as_hard_rejections(title):
    result = evaluation.title_filter(title)

    assert result.hard_rejection is True
    assert result.reasons == [
        "title hard rejection: disambiguation page"
    ]


def test_title_filter_keeps_biographical_parenthetical_titles_evaluable():
    philosopher = evaluation.title_filter("Alan Stout (philosopher)")
    mathematician = evaluation.title_filter("Thomas Forster (mathematician)")

    assert philosopher.hard_rejection is False
    assert philosopher.human_bonus == 1
    assert philosopher.philosopher_bonus == 2
    assert mathematician.hard_rejection is False
    assert mathematician == evaluation.FilterResult()


def test_disambiguation_rejects_before_summary_wikidata_or_quotes(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("structural disambiguation must short-circuit")

    monkeypatch.setattr(evaluation, "summary_filter", forbidden)
    monkeypatch.setattr(evaluation, "wikidata_filter", forbidden)
    monkeypatch.setattr(evaluation, "quote_filter", forbidden)

    result = evaluation.process_title(
        {"title": "Boethius (disambiguation)"},
        {},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "rejected"
    assert result["human_confidence"] == 0
    assert result["philosopher_confidence"] == 0
    assert result["content_confidence"] == 0
    assert result["reasons"] == [
        "title hard rejection: disambiguation page"
    ]


def test_pageprops_disambiguation_rejects_before_summary_wikidata_or_quotes(
    monkeypatch,
):
    """An ordinary title can still be structurally a disambiguation page."""
    def forbidden(*args, **kwargs):
        raise AssertionError("pageprops disambiguation must short-circuit")

    monkeypatch.setattr(evaluation, "summary_filter", forbidden)
    monkeypatch.setattr(evaluation, "wikidata_filter", forbidden)
    monkeypatch.setattr(evaluation, "quote_filter", forbidden)

    result = evaluation.process_title(
        {"title": "Alan White", "is_disambiguation": True},
        {},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "rejected"
    assert result["human_confidence"] == 0
    assert result["philosopher_confidence"] == 0
    assert result["content_confidence"] == 0
    assert result["reasons"] == [
        "page hard rejection: Wikipedia disambiguation page"
    ]


def test_non_disambiguation_page_metadata_keeps_biography_evaluable(monkeypatch):
    calls = []

    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: calls.append("summary") or evaluation.FilterResult(
            philosopher_bonus=2,
            human_bonus=1,
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: calls.append("wikidata") or evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: calls.append("quotes") or evaluation.FilterResult(),
    )

    result = evaluation.process_title(
        {
            "title": "Alan White (American philosopher)",
            "is_disambiguation": False,
        },
        {},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "accepted"
    assert calls == ["summary", "wikidata", "quotes"]


def test_alan_r_white_remains_independent_of_alan_white_disambiguation():
    assert evaluation.page_structure_filter({
        "title": "Alan R. White",
        "is_disambiguation": False,
    }) == evaluation.FilterResult()


def test_stale_v2_disambiguation_entry_is_known_future_reevaluation_case(
    monkeypatch,
):
    """A valid stale v2 record remains stored until a future version bump."""
    assert CURRENT_EVALUATION_ALGORITHM_VERSION == 2
    title = "Boethius (disambiguation)"
    stale_entry = make_empty_database_entry(title)
    stale_entry["evaluation"].update({
        "status": "accepted",
        "algorithm_version": 2,
    })

    assert validate_database_entry(stale_entry) == []
    assert evaluation.evaluation_needs_processing(
        stale_entry["evaluation"]
    ) is False

    # Simulate the same title being evaluated under the current title policy.
    # Its actual stale v2 state is intentionally not rewritten by this test.
    policy_entry = copy.deepcopy(stale_entry)
    policy_entry["evaluation"].update({
        "status": "unprocessed",
        "algorithm_version": None,
    })

    def forbidden(*args, **kwargs):
        raise AssertionError("hard disambiguation rejection must be title-only")

    monkeypatch.setattr(evaluation, "summary_filter", forbidden)
    monkeypatch.setattr(evaluation, "wikidata_filter", forbidden)
    monkeypatch.setattr(evaluation, "quote_filter", forbidden)

    result = evaluation.process_title(
        {"title": title},
        {},
        {title: policy_entry},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "rejected"
    assert result["human_confidence"] == 0
    assert result["philosopher_confidence"] == 0
    assert result["content_confidence"] == 0
    assert "title hard rejection: disambiguation page" in result["reasons"]

def test_summary_filter_returns_neutral_result_when_summary_unavailable(
    monkeypatch,
):
    import threading

    monkeypatch.setattr(
        evaluation,
        "get_summary",
        lambda *args, **kwargs: None,
    )

    result = evaluation.summary_filter(
        "Ada Lovelace",
        {},
        {
            "cached_summaries": 0,
            "downloaded_summaries": 0,
        },
        threading.Lock(),
        threading.Lock(),
    )

    assert result == evaluation.FilterResult()

def test_summary_filter_keeps_its_reasons(monkeypatch):
    import threading

    monkeypatch.setattr(
        evaluation,
        "get_summary",
        lambda *args, **kwargs: (
            "Ada Lovelace was a philosopher and professor."
        ),
    )

    result = evaluation.summary_filter(
        "Ada Lovelace",
        {},
        {
            "cached_summaries": 0,
            "downloaded_summaries": 0,
        },
        threading.Lock(),
        threading.Lock(),
    )

    assert result.philosopher_bonus == 2
    assert result.human_bonus == 2
    assert result.reasons == [
        "summary human bonus (+1): direct biographical philosopher statement",
        "summary philosopher bonus (+2): direct biographical philosopher statement",
        "summary human bonus (+1): professor",
    ]


def test_summary_filter_receives_same_effective_values_after_cutover(monkeypatch):
    captured = {}
    database = {"Ada Lovelace": {"summary": {"text": "unused"}}}
    stats = {"cached_summaries": 0, "downloaded_summaries": 0}

    def fake_get_summary(
        title,
        received_database,
        received_stats,
        stats_lock,
        persistence_lock,
        data_folder,
        limiter=None,
    ):
        captured["title"] = title
        captured["database"] = received_database
        captured["stats"] = received_stats
        captured["data_folder"] = data_folder
        return "Ada Lovelace was a philosopher and professor."

    monkeypatch.setattr(evaluation, "get_summary", fake_get_summary)

    result = evaluation.summary_filter(
        "Ada Lovelace",
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        data_folder="temporary-data",
    )

    assert captured == {
        "title": "Ada Lovelace",
        "database": database,
        "stats": stats,
        "data_folder": "temporary-data",
    }
    assert result.philosopher_bonus == 2
    assert result.human_bonus == 2
    assert result.reasons == [
        "summary human bonus (+1): direct biographical philosopher statement",
        "summary philosopher bonus (+2): direct biographical philosopher statement",
        "summary human bonus (+1): professor",
    ]


def _summary_result_for_text(monkeypatch, text, title="Ada"):
    monkeypatch.setattr(evaluation, "get_summary", lambda *args, **kwargs: text)
    return evaluation.summary_filter(
        title,
        {},
        {"cached_summaries": 0, "downloaded_summaries": 0},
        threading.Lock(),
        threading.Lock(),
    )


@pytest.mark.parametrize("summary", [
    "Ada is a philosopher.",
    "Ada was a philosopher.",
    "Ada is an American philosopher.",
    "Ada was a natural philosopher.",
    "Ada is a political philosopher.",
    "Ada was a moral philosopher.",
    "Ada is a philosopher of science.",
    "Ada was a philosopher and historian.",
    "Ada was a poet and philosopher.",
    "Ada was a physician and philosopher.",
    "Ada was a theologian and philosopher.",
])
def test_summary_filter_direct_subject_philosopher_constructions(
    monkeypatch, summary,
):
    result = _summary_result_for_text(monkeypatch, summary)

    assert result.human_bonus == 1
    assert result.philosopher_bonus == 2
    assert result.reasons == [
        "summary human bonus (+1): direct biographical philosopher statement",
        "summary philosopher bonus (+2): direct biographical philosopher statement",
    ]


@pytest.mark.parametrize("title, summary", [
    ("The Old Philosopher", "The Old Philosopher is a play."),
    ("Philosopher Press", "Philosopher Press is a publishing company."),
    (
        "Philosopher's stone",
        "A philosopher's stone is an alchemical substance.",
    ),
    ("Philosopher's Egg", "Philosopher's Egg is a novel."),
    (
        "Eddie Lawrence",
        "Eddie Lawrence was an actor and performer best known for "
        "The Old Philosopher."
    ),
    ("Ada", "Ada is a business philosopher."),
])
def test_summary_filter_does_not_infer_subject_philosopher_from_false_contexts(
    monkeypatch, title, summary,
):
    result = _summary_result_for_text(monkeypatch, summary, title=title)

    assert result.philosopher_bonus == 0
    assert not any("direct biographical philosopher" in reason for reason in result.reasons)


def test_author_and_novelist_are_neutral_for_philosopher_confidence(monkeypatch):
    result = _summary_result_for_text(
        monkeypatch,
        "Ada was an author and novelist.",
    )

    assert result.philosopher_bonus == 0
    assert result.nonphilosopher_penalty == 0


@pytest.mark.parametrize("title, summary", [
    (
        "Antoni Kępiński",
        "Antoni Ignacy Tadeusz Kępiński was a Polish psychiatrist and philosopher.",
    ),
    (
        "Bernd Ladwig",
        "Bernd Ladwig is a German political philosopher who teaches political theory.",
    ),
    (
        "Thomas Forster (mathematician)",
        "Thomas Edward Forster is a British set theorist and philosopher.",
    ),
    (
        "Jean Christophe Fatio",
        "Jean-Christophe Fatio de Duillier was a Genevan engineer, mathematician, politician, and natural philosopher.",
    ),
])
def test_summary_filter_matches_expanded_lead_subject_philosopher_regressions(
    monkeypatch, title, summary,
):
    result = _summary_result_for_text(monkeypatch, summary, title=title)

    assert result.human_bonus == 1
    assert result.philosopher_bonus == 2
    assert result.reasons == [
        "summary human bonus (+1): direct biographical philosopher statement",
        "summary philosopher bonus (+2): direct biographical philosopher statement",
    ]


@pytest.mark.parametrize("title, summary", [
    (
        "Antoni Kępiński",
        "Antoni Ignacy Tadeusz Kępiński was a Polish psychiatrist and philosopher.",
    ),
    (
        "Bernd Ladwig",
        "Bernd Ladwig is a German political philosopher who teaches political theory.",
    ),
    (
        "Thomas Forster (mathematician)",
        "Thomas Edward Forster is a British set theorist and philosopher.",
    ),
    (
        "Jean Christophe Fatio",
        "Jean-Christophe Fatio de Duillier was a Genevan engineer, mathematician, politician, and natural philosopher.",
    ),
])
def test_v2_prequote_replay_uses_current_filters_for_expanded_subjects(
    monkeypatch, title, summary,
):
    monkeypatch.setattr(evaluation, "get_summary", lambda *args, **kwargs: summary)
    monkeypatch.setattr(
        evaluation,
        "prepare_entity_cached",
        lambda *args, **kwargs: {
            "valid": True,
            "title": title,
            "is_human": True,
            "is_philosopher": None,
            "birth": 1900,
        },
    )
    stats = {
        "cached_summaries": 0,
        "downloaded_summaries": 0,
        "cached_entities": 0,
        "prepared_entities": 0,
    }
    title_result = evaluation.title_filter(title)
    summary_result = evaluation.summary_filter(
        title, {}, stats, threading.Lock(), threading.Lock(),
    )
    wikidata_result = evaluation.wikidata_filter(
        title, {}, {}, {}, stats, threading.Lock(), threading.Lock(),
    )
    combined = evaluation.combine_filter_results(
        title_result,
        summary_result,
        wikidata_result,
    )

    assert combined.human_bonus - combined.nonhuman_penalty > 0
    assert (
        combined.philosopher_bonus - combined.nonphilosopher_penalty
    ) > 0


@pytest.mark.parametrize("title, lead_subject, expected", [
    ("Ada Lovelace", "Ada Lovelace", True),
    ("Antoni Kępiński", "Antoni Ignacy Tadeusz Kępiński", True),
    ("Thomas Forster", "Thomas Edward Forster", True),
    ("Jean Christophe Fatio", "Jean-Christophe Fatio de Duillier", True),
    ("R. G. Collingwood", "Robin George Collingwood", True),
    ("Charles A. Baylis", "Charles Augustus Baylis", True),
    ("William Leon McBride", "William McBride", True),
    ("Ada Lovelace (philosopher)", "Ada Lovelace Smith", True),
    ("Ada Lovelace", "Lovelace Ada", False),
    ("Ada Lovelace", "Grace Hopper", False),
    ("Ann Lee", "Anna Lee", False),
    ("A. B.", "Alice Brown", False),
])
def test_title_matches_lead_subject_uses_ordered_whole_name_tokens(
    title, lead_subject, expected,
):
    assert evaluation.title_matches_lead_subject(title, lead_subject) is expected


@pytest.mark.parametrize("summary", [
    "Ada was a natural philosopher.",
    "Ada was a mathematician and natural philosopher.",
    "Ada was an engineer, mathematician, and natural philosopher.",
    "Ada was an engineer, mathematician, politician, and natural philosopher.",
    "Ada was a philosopher who wrote about science.",
    "Ada was a philosopher whose work influenced later thinkers.",
    "Ada was a Neoplatonic philosopher in the Byzantine Empire.",
    "Ada is an Italian philosopher at the CNRS in Paris.",
    "Ada is a professor of philosophy and political philosopher.",
])
def test_summary_filter_supports_bounded_multi_role_and_relative_clause_forms(
    monkeypatch, summary,
):
    result = _summary_result_for_text(monkeypatch, summary)

    assert result.human_bonus >= 1
    assert result.philosopher_bonus == 2
    assert (
        "summary human bonus (+1): direct biographical philosopher statement"
        in result.reasons
    )
    assert (
        "summary philosopher bonus (+2): direct biographical philosopher statement"
        in result.reasons
    )


@pytest.mark.parametrize("title, summary", [
    (
        "Eddie Lawrence",
        "Eddie Lawrence was an actor and performer whose comic creation, \"The Old Philosopher\", became popular.",
    ),
    (
        "Historian X",
        "Historian X was a historian who studied the philosopher Y.",
    ),
    (
        "Writer X",
        "Writer X was an author who wrote a book called The Philosopher.",
    ),
])
def test_summary_filter_keeps_subject_anchor_for_later_philosopher_references(
    monkeypatch, title, summary,
):
    result = _summary_result_for_text(monkeypatch, summary, title=title)

    assert result.philosopher_bonus == 0
    assert not any("direct biographical philosopher" in reason for reason in result.reasons)


def test_exact_philosopher_disambiguator_overrides_weak_lexical_exclusion():
    result = evaluation.title_filter("Poet Name (philosopher)")

    assert result.human_bonus == 1
    assert result.philosopher_bonus == 2
    assert result.nonhuman_penalty == 0


def test_title_filter_regressions_for_philosopher_and_mathematician_titles():
    philosopher = evaluation.title_filter("Terence Cuneo (philosopher)")
    mathematician = evaluation.title_filter("Thomas Forster (mathematician)")

    assert (philosopher.human_bonus, philosopher.philosopher_bonus) == (1, 2)
    assert not any("bad word part" in reason for reason in mathematician.reasons)


def _entity_with_claims(instances=(), occupations=()):
    def claims_for(values):
        return [
            {"mainsnak": {"datavalue": {"value": {"id": value}}}}
            for value in values
        ]

    return {
        "claims": {
            "P31": claims_for(instances),
            "P106": claims_for(occupations),
        },
    }


@pytest.mark.parametrize(
    "instances, occupations, expected_human, expected_philosopher",
    [
        (["Q5"], ["Q4964182"], True, True),
        ([], ["Q4964182"], None, True),
        (["Q5"], [], True, None),
        ([], [], None, None),
    ],
)
def test_prepare_entity_uses_wikidata_tri_state(
    instances, occupations, expected_human, expected_philosopher,
):
    prepared = evaluation.prepare_entity(
        "Ada",
        {"Ada": "Q1"},
        {"Q1": _entity_with_claims(instances, occupations)},
    )

    assert prepared["is_human"] is expected_human
    assert prepared["is_philosopher"] is expected_philosopher


def test_prepare_entity_captures_day_precision_death_date():
    entity = {
        "claims": {
            "P31": [],
            "P106": [],
            "P570": [{
                "rank": "normal",
                "mainsnak": {"snaktype": "value", "datavalue": {"value": {
                    "time": "+2026-06-29T00:00:00Z",
                    "precision": 11,
                    "calendarmodel": "http://www.wikidata.org/entity/Q1985727",
                }}},
            }],
        },
    }

    prepared = evaluation.prepare_entity(
        "Ervin László", {"Ervin László": "Q964137"}, {"Q964137": entity},
    )

    assert prepared["death"] == 2026
    assert prepared["death_date"] == "2026-06-29"
    assert evaluation.prepared_entity_to_canonical_wikidata(prepared)[
        "death_date"
    ] == "2026-06-29"


def test_unavailable_wikidata_prepares_neutral_facts():
    prepared = evaluation.canonical_wikidata_to_prepared(
        "Ada",
        {"status": "unavailable", "reason": "no_qid"},
    )

    assert prepared.get("is_human") is None
    assert prepared.get("is_philosopher") is None


@pytest.mark.parametrize("title, summary", [
    ("Adriaan Heereboord", "Adriaan Heereboord was a philosopher."),
    ("Alan Stout (philosopher)", "Alan Stout was an academic."),
    ("Alicja Gescinska", "Alicja Gescinska is a political philosopher."),
    ("Arvydas Šliogeris", "Arvydas Šliogeris was a philosopher."),
    ("Atticus (philosopher)", "Atticus was an ancient writer."),
    ("Cheng Yi (philosopher)", "Cheng Yi was a scholar."),
    ("Daniel Callus", "Daniel Callus was a natural philosopher."),
    ("Heather Douglas (philosopher)", "Heather Douglas is an academic."),
    ("Michael Kremer (philosopher)", "Michael Kremer is a professor."),
    ("Pierre Hadot", "Pierre Hadot was a philosopher and historian."),
    ("R. G. Collingwood", "R. G. Collingwood was a philosopher."),
    ("William Leon McBride", "William Leon McBride is a moral philosopher."),
    ("Francesco D'Andrea", "Francesco D'Andrea was a natural philosopher."),
    ("Margaret Bryan", "Margaret Bryan was a natural philosopher."),
    ("Jean Christophe Fatio", "Jean Christophe Fatio was a natural philosopher."),
])
def test_v2_borderline_philosopher_regressions_become_accepted(
    monkeypatch, title, summary,
):
    monkeypatch.setattr(evaluation, "get_summary", lambda *args, **kwargs: summary)
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )

    result = evaluation.process_title(
        {"title": title},
        {"cached_summaries": 0, "downloaded_summaries": 0},
        {}, {}, {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "accepted"
    assert result["human_confidence"] > 0
    assert result["philosopher_confidence"] > 0


@pytest.mark.parametrize("title, summary", [
    ("Aludel", "An aludel is a vessel used in alchemy."),
    (
        "Eddie Lawrence",
        "Eddie Lawrence was an actor and performer known for The Old Philosopher.",
    ),
])
def test_v2_borderline_nonphilosopher_regressions_remain_rejected(
    monkeypatch, title, summary,
):
    monkeypatch.setattr(evaluation, "get_summary", lambda *args, **kwargs: summary)
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: pytest.fail("guaranteed rejection must not fetch quotes"),
    )

    result = evaluation.process_title(
        {"title": title},
        {"cached_summaries": 0, "downloaded_summaries": 0},
        {}, {}, {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "rejected"

def test_quote_filter_returns_result_with_reasons(monkeypatch):
    import threading

    monkeypatch.setattr(
        evaluation,
        "get_quotes",
        lambda *args, **kwargs: [
            {
                "text": "A useful quotation.",
                "word_count": 10,
            }
        ],
    )

    result = evaluation.quote_filter(
        "Ada Lovelace",
        {},
        {
            "cached_quotes": 0,
            "downloaded_quotes": 0,
            "failed_quotes": 0,
        },
        threading.Lock(),
        threading.Lock(),
    )

    assert result.content_bonus == 2
    assert result.noncontent_penalty == 0
    assert result.reasons == [
        "quotes bonus (+1): quotes exist",
        "quotes bonus (+1): good quotes exist",
    ]

def test_wikidata_filter_returns_result_with_reasons(monkeypatch):
    import threading

    monkeypatch.setattr(
        evaluation,
        "prepare_entity_cached",
        lambda *args, **kwargs: {
            "valid": True,
            "title": "Ada Lovelace",
            "is_human": True,
            "is_philosopher": True,
            "birth": 1815,
        },
    )

    result = evaluation.wikidata_filter(
        "Ada Lovelace",
        {},
        {},
        {},
        {
            "cached_entities": 0,
            "prepared_entities": 0,
        },
        threading.Lock(),
        threading.Lock(),
    )

    assert result.human_bonus == 4
    assert result.philosopher_bonus == 2
    assert result.reasons == [
        "wikidata human bonus (+2): is_human = true",
        "wikidata philosopher bonus (+2): is_philosopher = true",
        "wikidata human bonus (+2): birth_w not None",
    ]


def test_wikidata_filter_treats_signed_birth_year_as_human_evidence(monkeypatch):
    monkeypatch.setattr(
        evaluation,
        "prepare_entity_cached",
        lambda *args, **kwargs: {
            "valid": True,
            "title": "Thales of Miletus",
            "is_human": True,
            "is_philosopher": True,
            "birth": -650,
        },
    )

    result = evaluation.wikidata_filter(
        "Thales of Miletus", {}, {}, {}, {"cached_entities": 0},
        threading.Lock(), threading.Lock(),
    )

    assert result.human_bonus == 4
    assert "wikidata human bonus (+2): birth_w not None" in result.reasons

def make_result(
    philosopher_bonus=0,
    human_bonus=0,
    content_bonus=0,
    nonphilosopher_penalty=0,
    nonhuman_penalty=0,
    noncontent_penalty=0,
    reasons=None,
):
    return evaluation.FilterResult(
        philosopher_bonus=philosopher_bonus,
        human_bonus=human_bonus,
        content_bonus=content_bonus,
        nonphilosopher_penalty=nonphilosopher_penalty,
        nonhuman_penalty=nonhuman_penalty,
        noncontent_penalty=noncontent_penalty,
        reasons=[] if reasons is None else reasons,
    )

def test_process_title_combines_all_filter_components_and_reasons(
    monkeypatch,
):
    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: make_result(
            philosopher_bonus=1,
            human_bonus=2,
            content_bonus=3,
            nonphilosopher_penalty=1,
            nonhuman_penalty=1,
            noncontent_penalty=1,
            reasons=["title reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: make_result(
            philosopher_bonus=10,
            human_bonus=20,
            content_bonus=30,
            nonphilosopher_penalty=2,
            nonhuman_penalty=3,
            noncontent_penalty=4,
            reasons=["summary reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: make_result(
            philosopher_bonus=100,
            human_bonus=200,
            content_bonus=300,
            nonphilosopher_penalty=3,
            nonhuman_penalty=5,
            noncontent_penalty=6,
            reasons=["wikidata reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: make_result(
            philosopher_bonus=1000,
            human_bonus=2000,
            content_bonus=3000,
            nonphilosopher_penalty=4,
            nonhuman_penalty=7,
            noncontent_penalty=8,
            reasons=["quote reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation.time,
        "time",
        lambda: 123.0,
    )

    entry = evaluation.process_title(
        {"title": "Ada Lovelace"},
        {"cached_encountered": 0},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert entry == {
        "title": "Ada Lovelace",
        "status": "accepted",
        "philosopher_confidence": 1101,
        "human_confidence": 2206,
        "content_confidence": 3314,
        "reasons": [
            "title reason",
            "summary reason",
            "wikidata reason",
            "quote reason",
        ],
        "last_processed": 123.0,
    }

@pytest.mark.parametrize(
    (
        "human_bonus",
        "nonhuman_penalty",
        "philosopher_bonus",
        "nonphilosopher_penalty",
        "expected_human_confidence",
        "expected_philosopher_confidence",
    ),
    [
        (-1, 0, 1, 0, -1, 1),
        (0, 0, 1, 0, 0, 1),
        (1, 0, -1, 0, 1, -1),
        (1, 0, 0, 0, 1, 0),
    ],
    ids=(
        "negative-human",
        "zero-human",
        "negative-philosopher",
        "zero-philosopher",
    ),
)
def test_process_title_skips_quote_filter_for_guaranteed_rejection(
    monkeypatch,
    human_bonus,
    nonhuman_penalty,
    philosopher_bonus,
    nonphilosopher_penalty,
    expected_human_confidence,
    expected_philosopher_confidence,
):
    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: make_result(
            human_bonus=human_bonus,
            nonhuman_penalty=nonhuman_penalty,
            philosopher_bonus=philosopher_bonus,
            nonphilosopher_penalty=nonphilosopher_penalty,
            reasons=["title reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: make_result(reasons=["summary reason"]),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: make_result(reasons=["wikidata reason"]),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: pytest.fail(
            "quote_filter must not run for a guaranteed rejection"
        ),
    )
    monkeypatch.setattr(evaluation.time, "time", lambda: 123.0)

    result = evaluation.process_title(
        {"title": "Clearly not a philosopher"},
        {"cached_encountered": 0},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result == {
        "title": "Clearly not a philosopher",
        "status": "rejected",
        "philosopher_confidence": expected_philosopher_confidence,
        "human_confidence": expected_human_confidence,
        "content_confidence": 0,
        "reasons": ["title reason", "summary reason", "wikidata reason"],
        "last_processed": 123.0,
    }


def test_process_title_does_not_enter_get_quotes_for_guaranteed_rejection(
    monkeypatch,
):
    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: make_result(
            human_bonus=0,
            philosopher_bonus=1,
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "get_quotes",
        lambda *args, **kwargs: pytest.fail(
            "get_quotes/retry path must not run for a guaranteed rejection"
        ),
    )

    result = evaluation.process_title(
        {"title": "Known non-philosopher"},
        {"cached_encountered": 0},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result["status"] == "rejected"
    assert result["content_confidence"] == 0
    assert result["reasons"] == []


def test_process_title_runs_quote_filter_when_prequote_confidences_are_positive(
    monkeypatch,
):
    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: make_result(
            human_bonus=1,
            philosopher_bonus=1,
            reasons=["title reason"],
        ),
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: make_result(reasons=["summary reason"]),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: make_result(reasons=["wikidata reason"]),
    )
    quote_calls = []

    def quote_filter_that_records(*args, **kwargs):
        quote_calls.append(True)
        return make_result(content_bonus=2, reasons=["quote reason"])

    monkeypatch.setattr(evaluation, "quote_filter", quote_filter_that_records)
    monkeypatch.setattr(evaluation.time, "time", lambda: 123.0)

    result = evaluation.process_title(
        {"title": "Plausible philosopher"},
        {"cached_encountered": 0},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert quote_calls == [True]
    assert result == {
        "title": "Plausible philosopher",
        "status": "accepted",
        "philosopher_confidence": 1,
        "human_confidence": 1,
        "content_confidence": 2,
        "reasons": [
            "title reason",
            "summary reason",
            "wikidata reason",
            "quote reason",
        ],
        "last_processed": 123.0,
    }


# @pytest.mark.parametrize(
#     "human_bonus, human_penalty, philosopher_bonus, philosopher_penalty, expected_status",
#     [
#         (2, 0, 2, 0, "accepted"),
#         (0, 0, 2, 0, "rejected"),
#         (2, 0, 0, 0, "rejected"),
#         (0, 1, 0, 1, "rejected"),
#     ],
# )
@pytest.mark.parametrize(
    (
        "human_bonus",
        "human_penalty",
        "philosopher_bonus",
        "philosopher_penalty",
        "expected_status",
    ),
    [
        (2, 0, 2, 0, "accepted"),
        (0, 0, 2, 0, "rejected"),
        (2, 0, 0, 0, "rejected"),
        (1, 2, 1, 2, "rejected"),
    ],
)
def test_process_title_uses_positive_human_and_philosopher_rule(
    monkeypatch,
    human_bonus,
    human_penalty,
    philosopher_bonus,
    philosopher_penalty,
    expected_status,
):
    result = make_result(
        human_bonus=human_bonus,
        nonhuman_penalty=human_penalty,
        philosopher_bonus=philosopher_bonus,
        nonphilosopher_penalty=philosopher_penalty,
    )

    monkeypatch.setattr(
        evaluation,
        "title_filter",
        lambda *args, **kwargs: result,
    )
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation.time,
        "time",
        lambda: 123.0,
    )

    entry = evaluation.process_title(
        {"title": "Ada Lovelace"},
        {"cached_encountered": 0},
        {},
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert entry["status"] == expected_status

def test_accept_and_reject_return_the_same_entry_shape(monkeypatch):
    monkeypatch.setattr(
        evaluation.time,
        "time",
        lambda: 456.0,
    )

    accepted = evaluation.accept(
        "Ada Lovelace",
        philosopher_confidence=2,
        human_confidence=3,
        content_confidence=1,
        reasons=["accepted reason"],
    )

    rejected = evaluation.reject(
        "Book title",
        philosopher_confidence=-1,
        human_confidence=0,
        content_confidence=-1,
        reasons=["rejected reason"],
    )

    assert set(accepted) == set(rejected)

    assert accepted == {
        "title": "Ada Lovelace",
        "status": "accepted",
        "philosopher_confidence": 2,
        "human_confidence": 3,
        "content_confidence": 1,
        "reasons": ["accepted reason"],
        "last_processed": 456.0,
    }

    assert rejected == {
        "title": "Book title",
        "status": "rejected",
        "philosopher_confidence": -1,
        "human_confidence": 0,
        "content_confidence": -1,
        "reasons": ["rejected reason"],
        "last_processed": 456.0,
    }

def assert_process_title_runs(monkeypatch, database):
    calls = []

    def title_filter_that_records(title):
        calls.append(title)
        return evaluation.FilterResult(
            philosopher_bonus=1,
            human_bonus=1,
        )

    monkeypatch.setattr(evaluation, "title_filter", title_filter_that_records)
    monkeypatch.setattr(
        evaluation,
        "summary_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "wikidata_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )
    monkeypatch.setattr(
        evaluation,
        "quote_filter",
        lambda *args, **kwargs: evaluation.FilterResult(),
    )

    result = evaluation.process_title(
        {"title": "Ada Lovelace"},
        {"cached_encountered": 0},
        database,
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert calls == ["Ada Lovelace"]
    assert result is not None
    return result


def canonical_evaluation_entry(status, algorithm_version):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["status"] = status
    entry["evaluation"]["algorithm_version"] = algorithm_version
    return {entry["title"]: entry}


def test_process_title_skips_current_accepted_canonical_evaluation(monkeypatch):
    def filter_should_not_run(*args, **kwargs):
        raise AssertionError("filters should not run")

    monkeypatch.setattr(
        evaluation,
        "title_filter",
        filter_should_not_run,
    )

    stats = {"cached_encountered": 0}

    result = evaluation.process_title(
        {"title": "Ada Lovelace"},
        stats,
        canonical_evaluation_entry(
            "accepted",
            CURRENT_EVALUATION_ALGORITHM_VERSION,
        ),
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(), 
    )

    assert result is None
    assert stats["cached_encountered"] == 1


def test_process_title_skips_current_rejected_canonical_evaluation(monkeypatch):
    def filter_should_not_run(*args, **kwargs):
        raise AssertionError("filters should not run")

    monkeypatch.setattr(
        evaluation,
        "title_filter",
        filter_should_not_run,
    )

    stats = {"cached_encountered": 0}

    result = evaluation.process_title(
        {"title": "Ada Lovelace"},
        stats,
        canonical_evaluation_entry(
            "rejected",
            CURRENT_EVALUATION_ALGORITHM_VERSION,
        ),
        {},
        {},
        stats_lock=threading.Lock(),
        persistence_lock=threading.Lock(),
    )

    assert result is None
    assert stats["cached_encountered"] == 1


def test_process_title_reprocesses_historical_accepted_unknown_version(
    monkeypatch,
):
    assert_process_title_runs(
        monkeypatch,
        canonical_evaluation_entry("accepted", None),
    )


def test_process_title_reprocesses_historical_rejected_unknown_version(
    monkeypatch,
):
    assert_process_title_runs(
        monkeypatch,
        canonical_evaluation_entry("rejected", None),
    )


def test_process_title_reprocesses_mismatched_algorithm_version(monkeypatch):
    assert_process_title_runs(
        monkeypatch,
        canonical_evaluation_entry(
            "accepted",
            CURRENT_EVALUATION_ALGORITHM_VERSION + 1,
        ),
    )


def test_process_title_reprocesses_rejected_mismatched_algorithm_version(
    monkeypatch,
):
    assert_process_title_runs(
        monkeypatch,
        canonical_evaluation_entry(
            "rejected",
            CURRENT_EVALUATION_ALGORITHM_VERSION + 1,
        ),
    )


def test_process_title_evaluates_unprocessed_migration_conflict_entry(
    monkeypatch,
):
    database = canonical_evaluation_entry("unprocessed", None)
    database["Ada Lovelace"]["migration"]["conflicts"] = [{
        "field": "evaluation.status",
        "values": [],
        "resolution": "unprocessed",
    }]
    conflicts_before = copy.deepcopy(
        database["Ada Lovelace"]["migration"]["conflicts"]
    )

    assert_process_title_runs(monkeypatch, database)

    assert database["Ada Lovelace"]["migration"]["conflicts"] == (
        conflicts_before
    )


def test_process_title_evaluates_missing_canonical_title(monkeypatch):
    assert_process_title_runs(monkeypatch, {})

def test_prepare_entity_cached_uses_available_canonical_wikidata_without_prepare(
    monkeypatch,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["wikidata"].update({
        "status": "available",
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth_year": 1815,
        "death_year": 1852,
        "death_date": None,
        "is_human": True,
        "is_philosopher": True,
        "fetched_at": 7,
    })
    database = {entry["title"]: entry}
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(
        evaluation,
        "prepare_entity",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("available canonical entry must not prepare")
        ),
    )

    prepared = evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), "unused-data-folder",
    )

    assert prepared == {
        "valid": True,
        "title": "Ada Lovelace",
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth": 1815,
        "death": 1852,
        "is_human": True,
        "is_philosopher": True,
    }
    assert stats == {"cached_entities": 1, "prepared_entities": 0}


def test_prepare_entity_cached_uses_unavailable_canonical_wikidata_without_prepare(
    monkeypatch,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["wikidata"].update({
        "status": "unavailable",
        "reason": "no_entity",
        "qid": "Q7259",
    })
    database = {entry["title"]: entry}
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(
        evaluation,
        "prepare_entity",
        lambda *args, **kwargs: pytest.fail(
            "unavailable canonical entry must not prepare"
        ),
    )

    assert evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), "unused-data-folder",
    ) == {
        "valid": False,
        "title": "Ada Lovelace",
        "reason": "no_entity",
        "qid": "Q7259",
    }
    assert stats == {"cached_entities": 1, "prepared_entities": 0}


def test_prepare_entity_cached_persists_available_canonical_wikidata(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    unchanged_sections = {
        name: copy.deepcopy(entry[name])
        for name in ("summary", "quotes", "evaluation", "posting", "migration")
    }
    prepared = {
        "valid": True,
        "title": "Ada Lovelace",
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth": 1815,
        "death": 1852,
        "is_human": True,
        "is_philosopher": True,
    }
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args: prepared)
    monkeypatch.setattr(evaluation.time, "time", lambda: 123456)

    assert evaluation.prepare_entity_cached(
        "Ada Lovelace", database,
        {"Ada Lovelace": "Q7259"}, {"Q7259": {}}, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
    ) == prepared

    assert database["Ada Lovelace"]["wikidata"] == {
        "status": "available",
        "reason": None,
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth_year": 1815,
        "death_year": 1852,
        "death_date": None,
        "is_human": True,
        "is_philosopher": True,
        "fetched_at": 123456,
    }
    assert all(
        database["Ada Lovelace"][name] == value
        for name, value in unchanged_sections.items()
    )
    assert stats == {"cached_entities": 0, "prepared_entities": 1}


def test_prepare_entity_cached_persists_no_qid_as_unavailable(monkeypatch, tmp_path):
    database = {}
    prepared = {"valid": False, "reason": "no_qid", "title": "Ada Lovelace"}
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args: prepared)

    assert evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
    ) == prepared
    assert database["Ada Lovelace"]["wikidata"] == {
        "status": "unavailable",
        "reason": "no_qid",
        "qid": None,
        "instances": [],
        "occupations": [],
        "birth_year": None,
        "death_year": None,
        "death_date": None,
        "is_human": None,
        "is_philosopher": None,
        "fetched_at": None,
    }
    assert stats["prepared_entities"] == 1


def test_prepare_entity_cached_persists_no_entity_as_unavailable(monkeypatch, tmp_path):
    database = {}
    prepared = {
        "valid": False,
        "reason": "no_entity",
        "title": "Ada Lovelace",
        "qid": "Q7259",
    }
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args: prepared)

    assert evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
    ) == prepared
    assert database["Ada Lovelace"]["wikidata"]["status"] == "unavailable"
    assert database["Ada Lovelace"]["wikidata"]["reason"] == "no_entity"
    assert database["Ada Lovelace"]["wikidata"]["qid"] == "Q7259"
    assert database["Ada Lovelace"]["wikidata"]["fetched_at"] is None


def test_wikidata_request_failure_does_not_persist_no_qid(tmp_path):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    before = copy.deepcopy(database)
    before_bytes = (tmp_path / "database.jsonl").read_bytes()
    stats = {"cached_entities": 0, "prepared_entities": 0}

    with pytest.raises(evaluation.WikidataLookupError, match="request_exception"):
        evaluation.prepare_entity_cached(
            "Ada Lovelace", database, {}, {}, stats,
            threading.Lock(), threading.Lock(), str(tmp_path),
            wikidata_errors={"Ada Lovelace": "request_exception"},
        )

    assert database == before
    assert (tmp_path / "database.jsonl").read_bytes() == before_bytes
    assert stats == {"cached_entities": 0, "prepared_entities": 0}


def test_successful_wikidata_response_without_qid_persists_no_qid(
    monkeypatch,
    tmp_path,
):
    database = {}
    stats = {"cached_entities": 0, "prepared_entities": 0}
    prepared = {"valid": False, "reason": "no_qid", "title": "Ada Lovelace"}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args, **kwargs: prepared)

    evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
        wikidata_errors={},
    )

    assert database["Ada Lovelace"]["wikidata"]["status"] == "unavailable"
    assert database["Ada Lovelace"]["wikidata"]["reason"] == "no_qid"


def test_wikidata_entity_request_failure_does_not_persist_no_entity(tmp_path):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    before = copy.deepcopy(database)
    before_bytes = (tmp_path / "database.jsonl").read_bytes()
    stats = {"cached_entities": 0, "prepared_entities": 0}

    with pytest.raises(evaluation.WikidataLookupError, match="request_exception"):
        evaluation.prepare_entity_cached(
            "Ada Lovelace",
            database,
            {"Ada Lovelace": "Q7259"},
            {},
            stats,
            threading.Lock(),
            threading.Lock(),
            str(tmp_path),
            wikidata_errors={"Ada Lovelace": "request_exception"},
        )

    assert database == before
    assert (tmp_path / "database.jsonl").read_bytes() == before_bytes


def test_wikidata_filter_receives_equivalent_prepared_facts_from_canonical_entry():
    wikidata = {
        "status": "available",
        "reason": None,
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth_year": 1815,
        "death_year": 1852,
        "is_human": True,
        "is_philosopher": True,
        "fetched_at": 1,
    }

    assert evaluation.canonical_wikidata_to_prepared(
        "Ada Lovelace", wikidata,
    ) == {
        "valid": True,
        "title": "Ada Lovelace",
        "qid": "Q7259",
        "instances": ["Q5"],
        "occupations": ["Q4964182"],
        "birth": 1815,
        "death": 1852,
        "is_human": True,
        "is_philosopher": True,
    }


def test_entity_persistence_failure_keeps_canonical_memory_unchanged(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    before = copy.deepcopy(database)
    prepared = {
        "valid": True, "title": "Ada Lovelace", "qid": "Q7259",
        "instances": [], "occupations": [], "birth": None, "death": None,
        "is_human": False, "is_philosopher": False,
    }
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args: prepared)
    monkeypatch.setattr(
        evaluation,
        "update_database_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        evaluation.prepare_entity_cached(
            "Ada Lovelace", database, {}, {}, stats,
            threading.Lock(), threading.Lock(), str(tmp_path),
        )

    assert database == before
    assert stats["prepared_entities"] == 0


def test_prepare_entity_cached_does_not_append_legacy_entities_jsonl(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    prepared = {
        "valid": False, "reason": "no_qid", "title": "Ada Lovelace",
    }
    stats = {"cached_entities": 0, "prepared_entities": 0}

    monkeypatch.setattr(evaluation, "prepare_entity", lambda *args: prepared)
    monkeypatch.setattr(
        cache,
        "persist_jsonl_cache_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("entity preparation must not append entities.jsonl")
        ),
    )

    assert evaluation.prepare_entity_cached(
        "Ada Lovelace", database, {}, {}, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
    ) == prepared


def make_flat_evaluation_result(title="Ada Lovelace", status="accepted"):
    return {
        "title": title,
        "status": status,
        "human_confidence": 3,
        "philosopher_confidence": 4,
        "content_confidence": -1,
        "reasons": ["first reason", "second reason"],
        "last_processed": 1780580890.25,
    }


def test_persist_canonical_accepted_evaluation_updates_only_evaluation_section(
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["summary"]["text"] = "Recognizable summary"
    entry["wikidata"]["status"] = "unavailable"
    entry["wikidata"]["reason"] = "no_qid"
    entry["quotes"]["status"] = "available"
    entry["quotes"]["items"] = [{
        "text": "Recognizable quote",
        "length": 18,
        "word_count": 2,
        "source": "Wikiquote",
    }]
    entry["posting"]["has_been_posted"] = True
    entry["evaluation"]["legacy_result"] = {"historical": "payload"}
    entry["migration"]["conflicts"] = [{
        "field": "evaluation.status",
        "values": [],
        "resolution": "unprocessed",
    }]
    database = {entry["title"]: copy.deepcopy(entry)}
    before_non_evaluation = {
        section: copy.deepcopy(entry[section])
        for section in ("summary", "wikidata", "quotes", "posting", "migration")
    }
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result(), database, stats, threading.Lock(),
        threading.Lock(), str(tmp_path),
    )

    persisted = database["Ada Lovelace"]
    assert persisted["evaluation"] == {
        "status": "accepted",
            "algorithm_version": CURRENT_EVALUATION_ALGORITHM_VERSION,
        "human_confidence": 3,
        "philosopher_confidence": 4,
        "content_confidence": -1,
        "reasons": ["first reason", "second reason"],
        "legacy_result": {"historical": "payload"},
        "processed_at": 1780580890.25,
    }
    for section, expected in before_non_evaluation.items():
        assert persisted[section] == expected
    assert cache.load_database("database.jsonl", str(tmp_path)) == database


def test_persist_canonical_rejected_evaluation_sets_current_algorithm_version(
    tmp_path,
):
    entry = make_empty_database_entry("Rejected title")
    database = {entry["title"]: copy.deepcopy(entry)}
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result("Rejected title", "rejected"),
        database,
        stats,
        threading.Lock(),
        threading.Lock(),
        str(tmp_path),
    )

    persisted = database["Rejected title"]["evaluation"]
    assert persisted["status"] == "rejected"
    assert persisted["algorithm_version"] == CURRENT_EVALUATION_ALGORITHM_VERSION
    assert persisted["human_confidence"] == 3
    assert persisted["philosopher_confidence"] == 4
    assert persisted["content_confidence"] == -1
    assert persisted["reasons"] == ["first reason", "second reason"]
    assert persisted["processed_at"] == 1780580890.25
    assert stats == {"new_accepted": 0, "new_rejected": 1}


def test_new_canonical_evaluation_preserves_legacy_result_and_migration_conflicts(
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    entry["evaluation"]["legacy_result"] = {"legacy": ["value"]}
    entry["migration"]["conflicts"] = [{
        "field": "quotes.failure",
        "values": [{"source": "quote_failures.jsonl", "value": "old"}],
        "resolution": "unresolved",
    }]
    database = {entry["title"]: copy.deepcopy(entry)}
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result(), database, stats, threading.Lock(),
        threading.Lock(), str(tmp_path),
    )

    assert database["Ada Lovelace"]["evaluation"]["legacy_result"] == {
        "legacy": ["value"],
    }
    assert database["Ada Lovelace"]["migration"]["conflicts"] == entry[
        "migration"
    ]["conflicts"]


def test_canonical_evaluation_persistence_failure_keeps_memory_and_disk_unchanged(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: copy.deepcopy(entry)}
    database_path = write_canonical_database(tmp_path, [entry])
    before_database = copy.deepcopy(database)
    before_bytes = database_path.read_bytes()
    stats = {"new_accepted": 0, "new_rejected": 0}

    monkeypatch.setattr(
        evaluation,
        "update_database_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        evaluation.persist_canonical_evaluation(
            make_flat_evaluation_result(), database, stats, threading.Lock(),
            threading.Lock(), str(tmp_path),
        )

    assert database == before_database
    assert database_path.read_bytes() == before_bytes
    assert stats == {"new_accepted": 0, "new_rejected": 0}


def test_persist_canonical_evaluation_increments_new_accepted_only_after_success(
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result(), database, stats, threading.Lock(),
        threading.Lock(), str(tmp_path),
    )

    assert stats == {"new_accepted": 1, "new_rejected": 0}


def test_persist_canonical_evaluation_increments_new_rejected_only_after_success(
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result(status="rejected"), database, stats,
        threading.Lock(), threading.Lock(), str(tmp_path),
    )

    assert stats == {"new_accepted": 0, "new_rejected": 1}


def test_persist_canonical_evaluation_rejects_unknown_status(monkeypatch, tmp_path):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: copy.deepcopy(entry)}
    database_path = write_canonical_database(tmp_path, [entry])
    before_bytes = database_path.read_bytes()
    stats = {"new_accepted": 0, "new_rejected": 0}

    monkeypatch.setattr(
        evaluation,
        "update_database_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("invalid result must not persist")
        ),
    )

    with pytest.raises(ValueError, match="Unexpected evaluation status"):
        evaluation.persist_canonical_evaluation(
            make_flat_evaluation_result(status="maybe"), database, stats,
            threading.Lock(), threading.Lock(), str(tmp_path),
        )

    assert database == {entry["title"]: entry}
    assert database_path.read_bytes() == before_bytes
    assert stats == {"new_accepted": 0, "new_rejected": 0}


def test_persist_canonical_evaluation_does_not_write_legacy_result_files(
    monkeypatch,
    tmp_path,
):
    entry = make_empty_database_entry("Ada Lovelace")
    database = {entry["title"]: entry}
    write_canonical_database(tmp_path, [entry])
    stats = {"new_accepted": 0, "new_rejected": 0}

    monkeypatch.setattr(
        cache,
        "persist_evaluation_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("canonical persistence must not write legacy results")
        ),
    )
    monkeypatch.setattr(
        cache,
        "persist_jsonl_cache_entry",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("canonical persistence must not append legacy JSONL")
        ),
    )

    evaluation.persist_canonical_evaluation(
        make_flat_evaluation_result(), database, stats, threading.Lock(),
        threading.Lock(), str(tmp_path),
    )

    assert database["Ada Lovelace"]["evaluation"]["status"] == "accepted"
