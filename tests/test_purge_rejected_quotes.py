import copy
import json
import threading

import pytest

import purge_rejected_quotes
from database_schema import make_empty_database_entry, serialize_database_entries


def rejected_entry(title, posted=False, quote_count=1):
    entry = make_empty_database_entry(title)
    entry["evaluation"].update({"status": "rejected", "algorithm_version": 2})
    entry["posting"]["has_been_posted"] = posted
    if posted:
        entry["posting"]["posted_at"] = [123]
    entry["quotes"].update({
        "status": "available",
        "items": [
            {
                "text": "A retained historical quote number {}.".format(index),
                "length": 37,
                "word_count": 7,
                "source": "Wikiquote",
            }
            for index in range(quote_count)
        ],
        "parser_version": None,
    })
    return entry


def write_database(tmp_path, entries):
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries(entries))


def test_rejected_quote_purge_eligibility_is_conservative():
    eligible = rejected_entry("Eligible")
    accepted = rejected_entry("Accepted")
    accepted["evaluation"]["status"] = "accepted"
    empty = rejected_entry("Empty", quote_count=0)
    already_purged = rejected_entry("Purged")
    already_purged["quotes"].update({
        "status": "purged", "items": [], "failure": None,
        "fetched_at": None, "parser_version": None,
    })

    assert purge_rejected_quotes.rejected_quotes_need_purge(eligible) is True
    assert purge_rejected_quotes.rejected_quotes_need_purge(accepted) is False
    assert purge_rejected_quotes.rejected_quotes_need_purge(empty) is False
    assert purge_rejected_quotes.rejected_quotes_need_purge(already_purged) is False


def test_purge_selection_is_sorted_and_explicit_order_is_preserved():
    database = {
        "Zulu": rejected_entry("Zulu"),
        "Alpha": rejected_entry("Alpha"),
        "Accepted": rejected_entry("Accepted"),
    }
    database["Accepted"]["evaluation"]["status"] = "accepted"

    assert purge_rejected_quotes.select_eligible_titles(database) == ["Alpha", "Zulu"]
    assert purge_rejected_quotes.select_explicit_titles(database, ["Zulu", "Alpha"]) == ["Zulu", "Alpha"]
    with pytest.raises(ValueError, match="duplicate"):
        purge_rejected_quotes.select_explicit_titles(database, ["Alpha", "Alpha"])
    with pytest.raises(ValueError, match="does not exist"):
        purge_rejected_quotes.select_explicit_titles(database, ["Missing"])
    with pytest.raises(ValueError, match="not eligible"):
        purge_rejected_quotes.select_explicit_titles(database, ["Accepted"])


def test_purge_dry_run_is_read_only_and_saves_compact_report(monkeypatch, tmp_path):
    write_database(tmp_path, [rejected_entry("Zulu", quote_count=2), rejected_entry("Alpha")])
    database_path = tmp_path / "database.jsonl"
    before = database_path.read_bytes()
    monkeypatch.setattr(
        purge_rejected_quotes, "PURGE_REPORTS_DIRECTORY", tmp_path / "reports/purge"
    )

    assert purge_rejected_quotes.main(["--data-folder", str(tmp_path), "--dry-run", "--limit", "1"]) == 0

    assert database_path.read_bytes() == before
    reports = list((tmp_path / "reports/purge").glob("*.json"))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text(encoding="utf-8"))
    assert report["eligible"] == {
        "count": 2,
        "quote_items": 3,
        "approx_item_bytes": purge_rejected_quotes.quote_item_bytes(
            rejected_entry("Zulu", quote_count=2)["quotes"]["items"]
        ) + purge_rejected_quotes.quote_item_bytes(
            rejected_entry("Alpha")["quotes"]["items"]
        ),
    }
    assert report["selected"]["titles"] == ["Alpha"]
    assert report["title_details"] == [{
        "title": "Alpha",
        "quote_count": 1,
        "approx_item_bytes": purge_rejected_quotes.quote_item_bytes(
            rejected_entry("Alpha")["quotes"]["items"]
        ),
    }]


def test_purge_apply_changes_only_quotes_and_is_resumable(tmp_path):
    entry = rejected_entry("Ada", posted=True, quote_count=2)
    preserved = copy.deepcopy({
        key: entry[key]
        for key in ("summary", "wikidata", "evaluation", "posting", "migration")
    })
    database = {"Ada": entry}
    write_database(tmp_path, [entry])

    results = purge_rejected_quotes.run_apply(database, ["Ada"], str(tmp_path))

    assert results["successfully_purged"] == 1
    assert results["total_quote_items_removed"] == 2
    assert database["Ada"]["quotes"] == {
        "status": "purged", "items": [], "failure": None,
        "fetched_at": None, "parser_version": None,
    }
    assert {key: database["Ada"][key] for key in preserved} == preserved
    assert purge_rejected_quotes.select_eligible_titles(database) == []


def test_purge_persistence_oserror_keeps_failed_title_and_continues(monkeypatch, tmp_path):
    database = {"Alpha": rejected_entry("Alpha"), "Zulu": rejected_entry("Zulu")}
    write_database(tmp_path, list(database.values()))
    original = purge_rejected_quotes.purge_entry_quotes

    def purge(database_value, title, *args):
        if title == "Alpha":
            raise OSError("disk full")
        return original(database_value, title, *args)

    monkeypatch.setattr(purge_rejected_quotes, "purge_entry_quotes", purge)
    results = purge_rejected_quotes.run_apply(database, ["Alpha", "Zulu"], str(tmp_path))

    assert results["operational_failures"] == 1
    assert results["successfully_purged"] == 1
    assert purge_rejected_quotes.rejected_quotes_need_purge(database["Alpha"])
    assert database["Zulu"]["quotes"]["status"] == "purged"


def test_purge_value_error_and_keyboard_interrupt_propagate(monkeypatch, tmp_path):
    database = {"Ada": rejected_entry("Ada")}
    monkeypatch.setattr(
        purge_rejected_quotes, "purge_entry_quotes",
        lambda *args: (_ for _ in ()).throw(ValueError("invalid")),
    )
    with pytest.raises(ValueError, match="invalid"):
        purge_rejected_quotes.run_apply(database, ["Ada"], str(tmp_path))

    monkeypatch.setattr(
        purge_rejected_quotes, "purge_entry_quotes",
        lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    with pytest.raises(KeyboardInterrupt):
        purge_rejected_quotes.run_apply(database, ["Ada"], str(tmp_path))


def test_purge_cli_rejects_limit_with_explicit_title():
    with pytest.raises(SystemExit):
        purge_rejected_quotes.parse_args(["--limit", "1", "--title", "Ada"])


def test_purge_report_write_failure_does_not_rollback(monkeypatch, tmp_path, capsys):
    database = {"Ada": rejected_entry("Ada")}
    monkeypatch.setattr(purge_rejected_quotes, "load_database", lambda *args: database)
    monkeypatch.setattr(
        purge_rejected_quotes,
        "save_purge_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    assert purge_rejected_quotes.main(["--data-folder", str(tmp_path), "--apply"]) == 0
    assert database["Ada"]["quotes"]["status"] == "purged"
    assert "Warning: purge report could not be saved: disk full" in capsys.readouterr().out
