import hashlib

import pytest
import requests

import wiki_philosopher_bot.cli.find_title as find_title_cli
from wiki_philosopher_bot.config import CURRENT_QUOTE_PARSER_VERSION
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry,
    serialize_database_entries,
)
from wiki_philosopher_bot.title_search import find_canonical_titles
import wiki_philosopher_bot.telegram_bot as telegram_bot


def postable_entry(title):
    entry = make_empty_database_entry(title)
    entry["evaluation"]["status"] = "accepted"
    entry["quotes"].update({
        "status": "available",
        "parser_version": CURRENT_QUOTE_PARSER_VERSION,
        "items": [{
            "text": "A complete canonical quote.",
            "word_count": 5,
            "length": 27,
            "source": {
                "work": None, "year": None, "date": None, "details": None,
                "citation": None, "url": None,
            },
            "retrieved_from": "Wikiquote",
        }],
    })
    return entry


def write_database(tmp_path, entries):
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries(entries))
    return path


def titles(matches):
    return [match["title"] for match in matches]


def test_matching_is_case_insensitive_partial_and_diacritic_insensitive():
    desc = postable_entry("René Descartes")
    zizek = postable_entry("Slavoj Žižek")
    database = {desc["title"]: desc, zizek["title"]: zizek}

    assert titles(find_canonical_titles(database, "RENÉ DESCARTES")) == ["René Descartes"]
    assert titles(find_canonical_titles(database, "cart")) == ["René Descartes"]
    assert titles(find_canonical_titles(database, "zizek")) == ["Slavoj Žižek"]


def test_exact_match_ranks_before_start_and_substring_matches():
    exact = postable_entry("Descartes")
    starts = postable_entry("Descartes, René")
    contains = postable_entry("René Descartes")
    database = {entry["title"]: entry for entry in (contains, starts, exact)}

    assert titles(find_canonical_titles(database, "descartes")) == [
        "Descartes", "Descartes, René", "René Descartes",
    ]


def test_default_search_uses_existing_posting_candidate_and_all_is_diagnostic():
    eligible = postable_entry("René Descartes")
    posted = postable_entry("René Descartes (posted)")
    posted["posting"]["has_been_posted"] = True
    rejected = postable_entry("René Descartes (topic)")
    rejected["evaluation"]["status"] = "rejected"
    database = {entry["title"]: entry for entry in (eligible, posted, rejected)}

    assert titles(find_canonical_titles(database, "descartes")) == ["René Descartes"]
    all_matches = find_canonical_titles(database, "descartes", include_all=True)
    assert titles(all_matches) == [
        "René Descartes", "René Descartes (posted)", "René Descartes (topic)",
    ]
    assert [match["eligible"] for match in all_matches] == [True, False, False]


def test_cli_displays_exact_stored_title_without_mutation_network_or_telegram(
    tmp_path, monkeypatch, capsys,
):
    entry = postable_entry("Slavoj Žižek")
    path = write_database(tmp_path, [entry])
    before = path.read_bytes()
    monkeypatch.setattr(
        requests, "request", lambda *args, **kwargs: pytest.fail("network call"),
    )
    monkeypatch.setattr(
        telegram_bot, "send_message", lambda *args, **kwargs: pytest.fail("Telegram call"),
    )

    assert find_title_cli.main(["zizek", "--data-folder", str(tmp_path)]) == 0

    assert capsys.readouterr().out == "Slavoj Žižek\neligible: yes\nposted: no\nquotes: 1\n"
    assert path.read_bytes() == before
    assert hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(before).digest()


def test_cli_all_includes_ineligible_titles_with_compact_reason(tmp_path, capsys):
    entry = postable_entry("René Descartes")
    entry["posting"]["has_been_posted"] = True
    write_database(tmp_path, [entry])

    assert find_title_cli.main(["descartes", "--all", "--data-folder", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "René Descartes\n" in output
    assert "eligible: no\n" in output
    assert "posted: yes\n" in output
    assert "status: already posted\n" in output


def test_cli_reports_no_eligible_matches_and_all_can_diagnose_them(tmp_path, capsys):
    entry = postable_entry("René Descartes")
    entry["posting"]["has_been_posted"] = True
    write_database(tmp_path, [entry])

    assert find_title_cli.main(["descartes", "--data-folder", str(tmp_path)]) == 1
    assert capsys.readouterr().out == 'No eligible canonical titles matched "descartes".\n'

    assert find_title_cli.main(["descartes", "--all", "--data-folder", str(tmp_path)]) == 0


def test_empty_query_is_rejected_before_database_access():
    with pytest.raises(SystemExit) as error:
        find_title_cli.parse_args(["   "])
    assert error.value.code == 2
