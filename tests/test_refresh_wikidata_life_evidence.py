import copy
import json

import wiki_philosopher_bot.cli.refresh_wikidata_life_evidence as refresh
import wiki_philosopher_bot.wikipedia_api as wikipedia_api
from wiki_philosopher_bot.database_schema import make_empty_database_entry, validate_database_entry, serialize_database_entries


def entry(title="Vātsyāyana", qid="Q380234", birth=500, death=500):
    value = make_empty_database_entry(title)
    value["wikidata"].update({
        "status": "available", "qid": qid, "instances": ["Q5"],
        "occupations": ["Q4964182"], "is_human": True,
        "is_philosopher": True, "birth_year": birth, "death_year": death,
    })
    value["evaluation"]["status"] = "accepted"
    return value


def claim(raw, precision=6, calendar="http://www.wikidata.org/entity/Q1985786", rank="preferred"):
    return {"rank": rank, "mainsnak": {"snaktype": "value", "datavalue": {"value": {
        "time": raw, "precision": precision, "calendarmodel": calendar,
    }}}}


def vats_entity():
    birth = claim("+0500-00-00T00:00:00Z")
    death = claim("+0500-00-00T00:00:00Z")
    for item in (birth, death):
        item["qualifiers"] = {
            "P1319": [{"datavalue": {"value": {
                "time": "+0350-00-00T00:00:00Z", "precision": 7,
                "calendarmodel": "http://www.wikidata.org/entity/Q1985786",
            }}}],
            "P1326": [{"datavalue": {"value": {
                "time": "+0550-00-00T00:00:00Z", "precision": 7,
                "calendarmodel": "http://www.wikidata.org/entity/Q1985786",
            }}}],
        }
    return {"claims": {"P569": [birth], "P570": [death]}}


def test_selection_is_narrow_and_skips_complete_or_nonaccepted_records():
    selected = entry()
    complete = entry("Complete", "Q2")
    complete["wikidata"]["birth_time"] = {"status": "available"}
    complete["wikidata"]["death_time"] = {"status": "available"}
    rejected = entry("Rejected", "Q3")
    rejected["evaluation"]["status"] = "rejected"
    no_qid = entry("No QID", None)
    assert refresh.select_missing_structured_evidence_titles({
        selected["title"]: selected,
        complete["title"]: complete,
        rejected["title"]: rejected,
        no_qid["title"]: no_qid,
    }) == ["Vātsyāyana"]


def test_vatsyayana_proposal_only_adds_structured_evidence(monkeypatch):
    value = entry()
    original = copy.deepcopy(value)
    monkeypatch.setattr(
        refresh, "get_wikidata_entities_batch",
        lambda qids, limiter=None: wikipedia_api.BatchLookupResult(
            {"Q380234": vats_entity()}, None
        ),
    )
    proposals, results = refresh.collect_proposals({"Vātsyāyana": value}, ["Vātsyāyana"])
    assert proposals["Vātsyāyana"]["birth_time"]["precision"] == 6
    assert proposals["Vātsyāyana"]["birth_time"]["qualifiers"]["P1319"]["year"] == 350
    assert results["category_counts"]["birth"]["bounded_range"] == 1
    refresh.apply_proposals({"Vātsyāyana": value}, proposals)
    assert value["wikidata"]["birth_year"] == original["wikidata"]["birth_year"] == 500
    assert value["wikidata"]["death_year"] == original["wikidata"]["death_year"] == 500
    assert value["wikidata"]["birth_date"] is None
    assert value["wikidata"]["death_date"] is None
    assert value["summary"] == original["summary"]


def test_bce_evidence_retains_signed_year_and_calendar(monkeypatch):
    value = entry("BCE philosopher", "Q4", birth=-470, death=-399)
    entity = {"claims": {"P569": [claim("-0470-00-00T00:00:00Z", 9, "http://www.wikidata.org/entity/Q1985727")],
                          "P570": [claim("-0399-00-00T00:00:00Z", 9, "http://www.wikidata.org/entity/Q1985727")]}}
    monkeypatch.setattr(
        refresh, "get_wikidata_entities_batch",
        lambda qids, limiter=None: wikipedia_api.BatchLookupResult({"Q4": entity}, None),
    )
    proposals, _ = refresh.collect_proposals({value["title"]: value}, [value["title"]])
    assert proposals[value["title"]]["birth_time"]["year"] == -470
    assert proposals[value["title"]]["death_time"]["year"] == -399


def test_apply_proposals_remains_schema_valid():
    value = entry()
    value["wikidata"]["birth_time"] = {
        "status": "available", "property": "P569", "raw_time": "+0500-00-00T00:00:00Z",
        "precision": 6, "precision_kind": "millennium",
        "calendar_model": "http://www.wikidata.org/entity/Q1985786",
        "rank": "preferred", "year": 500,
    }
    assert validate_database_entry(value) == []

def test_report_retains_side_statuses_and_accounting(monkeypatch):
    a = entry("Both", "Q10")
    b = entry("Some", "Q11"); c = entry("None", "Q12")
    some = {"claims": {"P569": [claim("+1950-00-00T00:00:00Z", 9)], "P570": [{"rank":"normal", "mainsnak":{"snaktype":"somevalue"}}]}}
    none = {"claims": {}}
    both = vats_entity()
    def fake(qids, limiter=None):
        return wikipedia_api.BatchLookupResult({"Q10": both, "Q11": some, "Q12": none}, None)
    monkeypatch.setattr(refresh, "get_wikidata_entities_batch", fake)
    proposals, results = refresh.collect_proposals({x["title"]: x for x in (a,b,c)}, ["Both","Some","None"])
    entries = {x["title"]: x for x in results["record_entries"]}
    assert entries["Some"]["death"]["status"] == "no_usable_claim"
    assert entries["Some"]["death"]["reason"] == "snaktype=somevalue"
    assert entries["Some"]["birth"]["status"] == "proposed"
    assert entries["None"]["birth"]["status"] == "property_absent"
    assert results["records_with_any_proposal"] == 2
    assert results["records_with_any_proposal"] + results["unchanged"] == 3
    assert results["birth_time_proposals"] + results["death_time_proposals"] - results["both_proposed"] == 2


def test_run_refresh_dry_run_does_not_write_or_backup(tmp_path, monkeypatch):
    value = entry()
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries([value]))
    monkeypatch.setattr(refresh, "collect_proposals", lambda database, titles: ({}, refresh._new_results()))
    report, _ = refresh.run_refresh(
        {value["title"]: value}, str(tmp_path), str(tmp_path / "reports"), apply=False,
    )
    assert report["database_sha256_before"] == report["database_sha256_after"]
    assert list((tmp_path / "backups").glob("*") if (tmp_path / "backups").exists() else []) == []
    assert json.loads((tmp_path / "database.jsonl").read_text().splitlines()[0])["wikidata"]["birth_year"] == 500


def test_run_refresh_apply_uses_one_backup_and_only_adds_evidence(tmp_path, monkeypatch):
    value = entry()
    (tmp_path / "database.jsonl").write_bytes(serialize_database_entries([value]))
    monkeypatch.setattr(refresh, "DATABASE_BACKUP_FOLDER", str(tmp_path / "backups"))
    evidence = {"birth_time": {"status": "available", "property": "P569", "raw_time": "+0500-00-00T00:00:00Z", "precision": 6, "precision_kind": "millennium", "calendar_model": "http://www.wikidata.org/entity/Q1985786", "rank": "preferred", "year": 500}}
    monkeypatch.setattr(refresh, "collect_proposals", lambda database, titles: ({"Vātsyāyana": evidence}, refresh._new_results()))
    report, _ = refresh.run_refresh(
        {value["title"]: value}, str(tmp_path), str(tmp_path / "reports"), apply=True,
    )
    assert report["results"]["records_updated"] == 1
    assert report["backup"]["created"] is True
    assert report["database_sha256_before"] != report["database_sha256_after"]
    updated = json.loads((tmp_path / "database.jsonl").read_text().splitlines()[0])
    assert updated["wikidata"]["birth_year"] == 500
    assert updated["wikidata"]["birth_time"]["precision"] == 6
    assert updated["evaluation"]["status"] == "accepted"

def test_apply_refuses_operational_failure_before_backup(tmp_path, monkeypatch):
    value = entry()
    raw = serialize_database_entries([value]); (tmp_path / "database.jsonl").write_bytes(raw)
    monkeypatch.setattr(refresh, "DATABASE_BACKUP_FOLDER", str(tmp_path / "backups"))
    results = refresh._new_results(); results["operational_failures"] = 1
    monkeypatch.setattr(refresh, "collect_proposals", lambda database, titles: ({"Vātsyāyana": {"birth_time": {"status": "available"}}}, results))
    report, _ = refresh.run_refresh({value["title"]: value}, str(tmp_path), str(tmp_path / "reports"), apply=True)
    assert report["backup"]["created"] is False
    assert (tmp_path / "database.jsonl").read_bytes() == raw
