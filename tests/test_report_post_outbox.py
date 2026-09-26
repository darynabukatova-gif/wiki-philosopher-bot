import hashlib, json, copy
from pathlib import Path
from collections import Counter
from statistics import median
import pytest
from wiki_philosopher_bot.death_report_rendering import (
    render_historical_death_report, validate_historical_death_report,
)
from wiki_philosopher_bot.report_post_outbox import (
    content_fingerprint, dispatch_report_post, load_attempt, prepare_report_post,
    reconcile_report_post, verify_bundle,
)
from wiki_philosopher_bot.telegram_bot import TelegramMediaGroupResult, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, TELEGRAM_OUTCOME_DEFINITE_REJECTION, TELEGRAM_OUTCOME_AMBIGUOUS


def row(title,date,age=None):
    return {"title":title,"display_title":title,"qid":"Q1","birth_year":None,"birth_date":None,"death_year":int(date[:4]),"death_date":date,"age_at_death":age}
def report(rows):
    ages=[x["age_at_death"] for x in rows if x["age_at_death"] is not None]; years=Counter(x["death_date"][:4] for x in rows)
    return {"operation":"historical-death-report","mode":"read-only","interval":{"from":"2019-09-26","to":"2026-09-26","inclusive":True,"source":"explicit"},
      "interval_results":{"exact_deaths":len(rows),"rows":rows},"deaths_by_year":{y:years[y] for y in sorted(years)},
      "age_statistics":{"exact_ages_available":len(ages),"insufficient_age_data":len(rows)-len(ages),"mean":sum(ages)/len(ages) if ages else None,"median":median(ages) if ages else None,"minimum":min(ages) if ages else None,"maximum":max(ages) if ages else None}}
@pytest.fixture
def rendered(tmp_path):
    source=tmp_path/'report.json'; source.write_text(json.dumps(report([row('Alpha','2024-01-01',80),row('Beta','2025-01-01')]))+'\n')
    _,manifest=render_historical_death_report(source,output_directory=tmp_path/'assets',rows_per_page=1)
    return source,manifest,tmp_path/'state'
def prepared(rendered):
    source,manifest,state=rendered; result=prepare_report_post(source,manifest,state,attempt_id='12345678-1234-4234-8234-123456789abc',now=None); assert result.ok; return result,state

def test_prepare_freezes_complete_bundle_without_changing_sources(rendered):
    source,manifest,state=rendered; before=(source.read_bytes(),manifest.read_bytes())
    result=prepare_report_post(source,manifest,state,attempt_id='12345678-1234-4234-8234-123456789abc')
    assert result.ok and result.ending_state=='pending'; bundle=state/result.attempt_id; attempt=load_attempt(bundle)
    assert [x['kind'] for x in attempt['media']]==['deaths-by-year','age-at-death','deaths-table','deaths-table']
    assert [x['position'] for x in attempt['media']]==[1,2,3,4]
    assert '<b>Philosopher deaths: 2019-09-26–2026-09-26</b>' in attempt['caption_html']
    assert source.read_bytes()==before[0] and manifest.read_bytes()==before[1]
    assert verify_bundle(bundle,attempt) and set(x.name for x in bundle.iterdir())=={'attempt.json','source-report.json','render-manifest.json',*[x['filename'] for x in attempt['media']]}

def test_prepare_rejects_report_and_png_hash_mismatch_and_missing(rendered):
    source,manifest,state=rendered
    source.write_text(source.read_text()+' '); assert not prepare_report_post(source,manifest,state).ok
    source,manifest,state=rendered; data=json.loads(manifest.read_text()); asset=manifest.parent/data['assets'][0]['filename']; asset.write_bytes(asset.read_bytes()+b'x')
    assert not prepare_report_post(source,manifest,state).ok
    asset.unlink(); assert not prepare_report_post(source,manifest,state).ok

def test_prepare_rejects_malformed_traversal_and_noncontiguous_tables(rendered):
    source,manifest,state=rendered; value=json.loads(manifest.read_text()); value['assets'][0]['filename']='../escape.png'; manifest.write_text(json.dumps(value)); assert not prepare_report_post(source,manifest,state).ok
    source,manifest,state=rendered; value=json.loads(manifest.read_text()); tables=[x for x in value['assets'] if x['kind']=='deaths-table']; tables[-1]['page']=3; manifest.write_text(json.dumps(value)); assert not prepare_report_post(source,manifest,state).ok

def test_content_fingerprint_ignores_regeneration_metadata_and_blocks_sent_duplicate(tmp_path):
    rows = [row("Alpha", "2024-01-01", 80), row("Beta", "2025-01-01")]
    first_report = report(rows)
    first_report.update({"generated_at": "2026-09-26T12:00:00Z", "database_sha256": "a" * 64})
    first_source = tmp_path / "first-report.json"
    first_source.write_text(json.dumps(first_report) + "\n")
    _, first_manifest = render_historical_death_report(
        first_source, output_directory=tmp_path / "first-assets", rows_per_page=1,
    )

    regenerated_report = copy.deepcopy(first_report)
    regenerated_report["generated_at"] = "2026-10-01T00:00:00Z"
    regenerated_report["database_sha256"] = "b" * 64
    regenerated_source = tmp_path / "other-location" / "regenerated.json"
    regenerated_source.parent.mkdir()
    regenerated_source.write_text(json.dumps(regenerated_report) + "\n")
    _, regenerated_manifest = render_historical_death_report(
        regenerated_source,
        output_directory=tmp_path / "other-location" / "regenerated-assets",
        rows_per_page=1,
    )

    first_state = tmp_path / "first-state"
    first = prepare_report_post(first_source, first_manifest, first_state)
    second_state = tmp_path / "second-state"
    second = prepare_report_post(regenerated_source, regenerated_manifest, second_state)
    assert first.ok and second.ok
    assert first.content_fingerprint == second.content_fingerprint
    assert first.posting_fingerprint != second.posting_fingerprint
    assert load_attempt(first_state / first.attempt_id)["source_report_sha256"] != load_attempt(second_state / second.attempt_id)["source_report_sha256"]
    assert load_attempt(first_state / first.attempt_id)["render_manifest_sha256"] != load_attempt(second_state / second.attempt_id)["render_manifest_sha256"]
    assert json.loads(first_manifest.read_text())["source_report"] != json.loads(regenerated_manifest.read_text())["source_report"]

    media_count = len(load_attempt(first_state / first.attempt_id)["media"])
    assert reconcile_report_post(
        first.attempt_id, first_state, "mark-sent",
        message_ids=list(range(1, media_count + 1)), note="sent test fixture",
    ).ok
    duplicate = prepare_report_post(regenerated_source, regenerated_manifest, first_state)
    assert not duplicate.ok
    assert "already sent" in duplicate.error_summary


def test_content_fingerprint_changes_for_material_statistical_changes():
    base = validate_historical_death_report(
        report([row("Alpha", "2024-01-01", 80)])
    )
    base_fingerprint = content_fingerprint(base)

    added_row = copy.deepcopy(base)
    added_row["rows"].append({
        "title": "Beta", "display_title": "Beta", "death_date": "2025-01-01",
        "age_at_death": None,
    })
    added_row["deaths_by_year"] = {"2024": 1, "2025": 1}
    added_row["age_statistics"]["insufficient_age_data"] = 1
    assert content_fingerprint(added_row) != base_fingerprint

    changed_date = copy.deepcopy(base)
    changed_date["rows"][0]["death_date"] = "2024-01-02"
    assert content_fingerprint(changed_date) != base_fingerprint

    changed_age = copy.deepcopy(base)
    changed_age["rows"][0]["age_at_death"] = 81
    changed_age["age_statistics"].update({"mean": 81, "median": 81, "minimum": 81, "maximum": 81})
    assert content_fingerprint(changed_age) != base_fingerprint

    changed_year_counts = copy.deepcopy(base)
    changed_year_counts["deaths_by_year"] = {"2024": 2}
    assert content_fingerprint(changed_year_counts) != base_fingerprint


def test_unresolved_and_identical_sent_block_prepare(rendered):
    result,state=prepared(rendered); source,manifest,_=rendered
    assert 'unresolved' in prepare_report_post(source,manifest,state).error_summary
    bundle=state/result.attempt_id; attempt=load_attempt(bundle); attempt['state']='sent'; attempt['telegram_message_ids']=list(range(1,len(attempt['media'])+1)); (bundle/'attempt.json').write_text(json.dumps(attempt))
    duplicate=prepare_report_post(source,manifest,state); assert not duplicate.ok and 'already sent' in duplicate.error_summary

def test_failed_blocks_but_cancelled_allows(rendered):
    result,state=prepared(rendered); bundle=state/result.attempt_id; attempt=load_attempt(bundle); attempt['state']='failed'; (bundle/'attempt.json').write_text(json.dumps(attempt))
    source,manifest,_=rendered; assert not prepare_report_post(source,manifest,state).ok
    reconciled=reconcile_report_post(result.attempt_id,state,'cancel',note='definite rejection reviewed'); assert reconciled.ok
    assert prepare_report_post(source,manifest,state).ok

def test_atomic_prepare_failure_leaves_no_visible_attempt(rendered):
    source,manifest,state=rendered
    def fail(src,dst): raise OSError('publish failed')
    result=prepare_report_post(source,manifest,state,attempt_id='12345678-1234-4234-8234-123456789abc',publish=fail)
    assert not result.ok and list(state.iterdir())==[]

def test_dispatch_uses_frozen_payload_once_and_success_persists(rendered):
    result,state=prepared(rendered); calls=[]
    def sender(media,caption,url,chat): calls.append((media,caption,url,chat)); return TelegramMediaGroupResult(True,{},None,TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,[11,12,13,14])
    dispatched=dispatch_report_post(result.attempt_id,state,'url','chat',confirmed=True,sender=sender)
    assert dispatched.ok and dispatched.ending_state=='sent' and dispatched.telegram_message_ids==[11,12,13,14] and len(calls)==1
    assert [x['filename'] for x in calls[0][0]]==[x['filename'] for x in load_attempt(state/result.attempt_id)['media']]
    assert not dispatch_report_post(result.attempt_id,state,'url','chat',confirmed=True,sender=lambda *x: pytest.fail('no resend')).ok

def test_dispatch_uses_only_relocated_frozen_bundle_after_original_inputs_removed(rendered, monkeypatch):
    """The frozen manifest's original relative source_report is never followed."""
    import shutil

    source, manifest, state = rendered
    result = prepare_report_post(
        source, manifest, state,
        attempt_id="12345678-1234-4234-8234-123456789abc",
    )
    assert result.ok
    bundle = state / result.attempt_id
    attempt = load_attempt(bundle)
    expected_caption = attempt["caption_html"]
    expected_media = verify_bundle(bundle, attempt)
    expected_hashes = [
        hashlib.sha256(item["bytes"]).hexdigest() for item in expected_media
    ]

    original_paths = {source.absolute(), manifest.absolute()}
    original_paths.update(
        (manifest.parent / item["filename"]).absolute()
        for item in attempt["media"]
    )
    source.unlink()
    shutil.rmtree(manifest.parent)

    relocated_state = state.parent / "relocated-state"
    shutil.move(str(state), str(relocated_state))
    relocated_bundle = relocated_state / result.attempt_id
    assert (relocated_bundle / "source-report.json").is_file()
    assert (relocated_bundle / "render-manifest.json").is_file()
    assert all(
        (relocated_bundle / item["filename"]).is_file()
        for item in attempt["media"]
    )

    original_read_bytes = Path.read_bytes

    def guarded_read_bytes(path, *args, **kwargs):
        if path.absolute() in original_paths:
            raise AssertionError("dispatch attempted to read an original render input")
        return original_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", guarded_read_bytes)
    calls = []

    def sender(media, caption, url, chat):
        calls.append((media, caption, url, chat))
        return TelegramMediaGroupResult(
            True, {}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
            [31, 32, 33, 34],
        )

    dispatched = dispatch_report_post(
        result.attempt_id, relocated_state, "https://example.invalid", "chat",
        confirmed=True, sender=sender,
    )
    assert dispatched.ok
    assert len(calls) == 1
    media, caption, _url, _chat = calls[0]
    assert caption == expected_caption
    assert [item["filename"] for item in media] == [
        item["filename"] for item in expected_media
    ]
    assert [hashlib.sha256(item["bytes"]).hexdigest() for item in media] == expected_hashes


def test_dispatch_requires_confirmation_and_pending(rendered):
    result,state=prepared(rendered); calls=[]
    out=dispatch_report_post(result.attempt_id,state,'u','c',sender=lambda *x:calls.append(x)); assert not out.ok and out.error_kind=='confirmation_required' and not calls

def test_rejection_and_ambiguity_transitions(rendered):
    for outcome,expected in ((TELEGRAM_OUTCOME_DEFINITE_REJECTION,'failed'),(TELEGRAM_OUTCOME_AMBIGUOUS,'unknown')):
        source,manifest,state=rendered
        result=prepare_report_post(source,manifest,state)
        response=TelegramMediaGroupResult(False,None,'request_exception',outcome)
        out=dispatch_report_post(result.attempt_id,state,'u','c',confirmed=True,sender=lambda *x,r=response:r)
        assert not out.ok and out.ending_state==expected and load_attempt(state/result.attempt_id)['state']==expected
        # clear root for fixture reuse
        import shutil; shutil.rmtree(state)

def test_incomplete_success_is_unknown(rendered):
    result,state=prepared(rendered); response=TelegramMediaGroupResult(True,{},None,TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,[1])
    out=dispatch_report_post(result.attempt_id,state,'u','c',confirmed=True,sender=lambda *x:response); assert out.ending_state=='unknown'

def test_payload_corruption_blocks_telegram_and_persists_failed(rendered):
    result,state=prepared(rendered); bundle=state/result.attempt_id; attempt=load_attempt(bundle); (bundle/attempt['media'][0]['filename']).write_bytes(b'bad'); calls=[]
    out=dispatch_report_post(result.attempt_id,state,'u','c',confirmed=True,sender=lambda *x:calls.append(x)); assert not calls and out.ending_state=='failed' and out.error_kind=='payload_integrity'

def test_reconciliation_show_mark_sent_and_unknown_cancel_safety(rendered):
    result,state=prepared(rendered); shown=reconcile_report_post(result.attempt_id,state,'show'); assert shown.ok and load_attempt(state/result.attempt_id)['state']=='pending'
    assert not reconcile_report_post(result.attempt_id,state,'mark-sent',message_ids=[1],note='evidence').ok
    count=len(load_attempt(state/result.attempt_id)['media']); marked=reconcile_report_post(result.attempt_id,state,'mark-sent',message_ids=list(range(1,count+1)),note='Telegram evidence'); assert marked.ok
    assert not reconcile_report_post(result.attempt_id,state,'cancel',note='no').ok

def test_unknown_requires_unsafe_confirmation_to_cancel(rendered):
    result,state=prepared(rendered); dispatch_report_post(result.attempt_id,state,'u','c',confirmed=True,sender=lambda *x:TelegramMediaGroupResult(False,None,'timeout',TELEGRAM_OUTCOME_AMBIGUOUS))
    assert not reconcile_report_post(result.attempt_id,state,'cancel',note='not delivered').ok
    assert reconcile_report_post(result.attempt_id,state,'cancel',note='verified absent',confirm_unsafe=True).ok


def test_confirmed_delivery_persistence_failure_requires_manual_reconciliation(rendered,monkeypatch):
    result,state=prepared(rendered)
    import wiki_philosopher_bot.report_post_outbox as outbox
    def fail(*args,**kwargs): raise OSError("disk full")
    monkeypatch.setattr(outbox,"_atomic_json",fail)
    response=TelegramMediaGroupResult(True,{},None,TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,[11,12,13,14])
    out=dispatch_report_post(result.attempt_id,state,"u","c",confirmed=True,sender=lambda *x:response)
    assert not out.ok and out.telegram_called and out.error_kind=="persistence_error"
    assert out.manual_reconciliation_required is True
    assert load_attempt(state/result.attempt_id)["state"]=="pending"

def test_tampered_attempt_media_order_blocks_telegram(rendered):
    result,state=prepared(rendered); bundle=state/result.attempt_id; attempt=load_attempt(bundle)
    attempt["media"][0],attempt["media"][1]=attempt["media"][1],attempt["media"][0]
    attempt["media"][0]["position"],attempt["media"][1]["position"]=1,2
    (bundle/"attempt.json").write_text(json.dumps(attempt))
    calls=[]; out=dispatch_report_post(result.attempt_id,state,"u","c",confirmed=True,sender=lambda *x:calls.append(x))
    assert not calls and out.error_kind=="payload_integrity" and out.manual_reconciliation_required
