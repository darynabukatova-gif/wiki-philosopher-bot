import copy
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

from wiki_philosopher_bot.channel_publication_outbox import (
    ChannelPublicationError,
    is_legal_transition,
    list_publications,
    load_publication,
    prepare_channel_publication,
    prepare_channel_publication_from_report,
    publication_fingerprint,
    source_payload_fingerprint,
    validate_publication,
    validate_publication_bundle,
    prepare_channel_publication_from_recent_death,
    discover_sent_recent_death_sources,
    RECENT_DEATH_SOURCE_TYPE,
    dispatch_channel_publication,
    reconcile_channel_publication,
)
from wiki_philosopher_bot.telegram_bot import TelegramMediaGroupResult, TelegramResult, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, TELEGRAM_OUTCOME_DEFINITE_REJECTION, TELEGRAM_OUTCOME_AMBIGUOUS
from wiki_philosopher_bot.death_report_rendering import render_historical_death_report
from wiki_philosopher_bot.report_post_outbox import (
    load_attempt,
    prepare_report_post,
    reconcile_report_post,
)
from wiki_philosopher_bot.database_schema import (
    make_empty_database_entry, make_pending_recent_death_notification,
    serialize_database_entries, transition_recent_death_notification,
)


def _report():
    rows = [
        {
            "title": "Alpha", "display_title": "Alpha", "qid": "Q1",
            "birth_year": None, "birth_date": None,
            "death_year": 2024, "death_date": "2024-01-01", "age_at_death": 80,
        },
        {
            "title": "Beta", "display_title": "Beta", "qid": "Q2",
            "birth_year": None, "birth_date": None,
            "death_year": 2025, "death_date": "2025-01-01", "age_at_death": None,
        },
    ]
    return {
        "operation": "historical-death-report",
        "mode": "read-only",
        "interval": {"from": "2019-09-26", "to": "2026-09-26", "inclusive": True, "source": "explicit"},
        "interval_results": {"exact_deaths": len(rows), "rows": rows},
        "deaths_by_year": {"2024": 1, "2025": 1},
        "age_statistics": {
            "exact_ages_available": 1, "insufficient_age_data": 1,
            "mean": 80, "median": 80, "minimum": 80, "maximum": 80,
        },
    }


def _source(tmp_path, source_id="12345678-1234-4234-8234-123456789abc", state="sent", when=None):
    report_path = tmp_path / (source_id + ".json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(_report()) + "\n", encoding="utf-8")
    _, manifest_path = render_historical_death_report(
        report_path, output_directory=tmp_path / (source_id + "-assets"), rows_per_page=1,
    )
    report_root = tmp_path / "report-posts"
    result = prepare_report_post(
        report_path, manifest_path, report_root,
        attempt_id=source_id,
        now=when or datetime(2026, 9, 26, tzinfo=timezone.utc),
    )
    assert result.ok
    if state == "sent":
        attempt = load_attempt(report_root / source_id)
        assert reconcile_report_post(
            source_id, report_root, "mark-sent",
            message_ids=list(range(10, 10 + len(attempt["media"]))),
            note="private test delivery",
        ).ok
    elif state in {"failed", "unknown", "cancelled"}:
        attempt_path = report_root / source_id / "attempt.json"
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        attempt["state"] = state
        attempt["state_changed_at"] = (when or datetime(2026, 9, 26, tzinfo=timezone.utc)).isoformat().replace("+00:00", "Z")
        if state in {"failed", "unknown"}:
            attempt["error_kind"] = "telegram_rejected" if state == "failed" else "transport_ambiguous"
            attempt["error_summary"] = "test outcome"
        if state == "cancelled":
            attempt["resolution_note"] = "cancelled test source"
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    if when is not None and state == "sent":
        attempt_path = report_root / source_id / "attempt.json"
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
        attempt["state_changed_at"] = when.isoformat().replace("+00:00", "Z")
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    return report_root, source_id


def _mark_publication_sent(bundle):
    path = bundle / "attempt.json"
    attempt = json.loads(path.read_text(encoding="utf-8"))
    attempt["state"] = "sent"
    attempt["telegram_message_ids"] = list(range(1, len(attempt["media"]) + 1))
    path.write_text(json.dumps(attempt), encoding="utf-8")


def test_schema_and_transition_validation(tmp_path):
    report_root, source_id = _source(tmp_path)
    state_root = tmp_path / "channel"
    result = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    assert result.ok
    attempt, bundle = load_publication(state_root, result.publication_id)
    assert attempt["state"] == "pending"
    assert validate_publication_bundle(bundle, attempt)
    assert is_legal_transition("pending", "sent")
    assert is_legal_transition("unknown", "cancelled")
    assert not is_legal_transition("sent", "pending")
    assert not is_legal_transition("cancelled", "sent")

    invalid = copy.deepcopy(attempt)
    invalid["publication_id"] = "bad"
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["source_type"] = "recent_death"
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["payload_kind"] = "text"
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["media"][0]["filename"] = "../escape.png"
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["media"][0]["filename"] = invalid["media"][1]["filename"]
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["media"][0]["position"] = 2
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)
    invalid = copy.deepcopy(attempt)
    invalid["media"][0]["sha256"] = "x"
    with pytest.raises(ChannelPublicationError):
        validate_publication(invalid)


@pytest.mark.parametrize("state", ["pending", "failed", "unknown", "cancelled"])
def test_non_sent_report_source_is_rejected(tmp_path, state):
    report_root, source_id = _source(tmp_path, state=state)
    result = prepare_channel_publication_from_report(
        report_root, tmp_path / "channel", source_id=source_id,
    )
    assert not result.ok
    assert not (tmp_path / "channel").exists()


@pytest.mark.parametrize("corruption", ["missing", "bytes", "message_ids", "schema"])
def test_corrupt_or_incomplete_sent_source_is_rejected(tmp_path, corruption):
    report_root, source_id = _source(tmp_path)
    bundle = report_root / source_id
    attempt_path = bundle / "attempt.json"
    attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    if corruption == "missing":
        (bundle / attempt["media"][0]["filename"]).unlink()
    elif corruption == "bytes":
        (bundle / attempt["media"][0]["filename"]).write_bytes(b"corrupt")
    elif corruption == "message_ids":
        attempt["telegram_message_ids"] = []
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    else:
        attempt["state"] = "not-a-state"
        attempt_path.write_text(json.dumps(attempt), encoding="utf-8")
    result = prepare_channel_publication_from_report(
        report_root, tmp_path / "channel", source_id=source_id,
    )
    assert not result.ok
    assert not (tmp_path / "channel").exists()


def test_explicit_source_freezes_caption_media_and_survives_source_deletion(tmp_path):
    report_root, source_id = _source(tmp_path)
    state_root = tmp_path / "channel"
    result = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    assert result.ok and result.telegram_called is False
    attempt, bundle = load_publication(state_root, result.publication_id)
    expected_caption = attempt["caption_html"]
    expected_order = [item["filename"] for item in attempt["media"]]
    expected_bytes = [
        (bundle / filename).read_bytes() for filename in expected_order
    ]
    source_snapshot = (report_root / source_id / "attempt.json").read_bytes()
    shutil.rmtree(report_root)
    payload = validate_publication_bundle(bundle, attempt)
    assert attempt["caption_html"] == expected_caption
    assert [item["filename"] for item in payload] == expected_order
    assert [item["bytes"] for item in payload] == expected_bytes
    assert (state_root / result.publication_id / "attempt.json").is_file()
    assert source_snapshot


def test_fingerprints_are_deterministic_and_ignore_publication_metadata(tmp_path):
    report_root, source_id = _source(tmp_path)
    first = prepare_channel_publication_from_report(
        report_root, tmp_path / "channel-a", source_id=source_id,
        publication_id="11111111-1111-4111-8111-111111111111",
        now=datetime(2026, 9, 26, tzinfo=timezone.utc),
    )
    second = prepare_channel_publication_from_report(
        report_root, tmp_path / "channel-b", source_id=source_id,
        publication_id="22222222-2222-4222-8222-222222222222",
        now=datetime(2027, 9, 26, tzinfo=timezone.utc),
    )
    assert first.ok and second.ok
    assert first.source_payload_fingerprint == second.source_payload_fingerprint
    assert first.publication_fingerprint == second.publication_fingerprint
    attempt, _ = load_publication(tmp_path / "channel-a", first.publication_id)
    assert source_payload_fingerprint(attempt["caption_html"], attempt["media"]) == attempt["source_payload_fingerprint"]
    assert publication_fingerprint(attempt) == attempt["publication_fingerprint"]


def test_latest_unpublished_selection_and_duplicate_blocking(tmp_path):
    report_root = None
    ids = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    for index, source_id in enumerate(ids):
        root, _ = _source(
            tmp_path / ("source-" + str(index)), source_id=source_id,
            when=datetime(2026, 9, 26, 10 + index, tzinfo=timezone.utc),
        )
        if report_root is None:
            report_root = root
        else:
            for child in root.iterdir():
                shutil.move(str(child), str(report_root / child.name))
    state_root = tmp_path / "channel"
    latest = prepare_channel_publication_from_report(report_root, state_root)
    assert latest.ok and latest.source_id == ids[2]
    _mark_publication_sent(state_root / latest.publication_id)
    next_result = prepare_channel_publication_from_report(report_root, state_root)
    assert next_result.ok and next_result.source_id == ids[1]
    _mark_publication_sent(state_root / next_result.publication_id)
    last = prepare_channel_publication_from_report(report_root, state_root)
    assert last.ok and last.source_id == ids[0]
    _mark_publication_sent(state_root / last.publication_id)
    none = prepare_channel_publication_from_report(report_root, state_root)
    assert not none.ok and "no eligible unpublished" in none.error_summary


@pytest.mark.parametrize("state", ["pending", "failed", "unknown"])
def test_unresolved_channel_publication_blocks_prepare(tmp_path, state):
    report_root, source_id = _source(tmp_path)
    state_root = tmp_path / "channel"
    first = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    assert first.ok
    path = state_root / first.publication_id / "attempt.json"
    attempt = json.loads(path.read_text(encoding="utf-8"))
    attempt["state"] = state
    if state in {"failed", "unknown"}:
        attempt["error_kind"] = "test"
        attempt["error_summary"] = "test outcome"
    path.write_text(json.dumps(attempt), encoding="utf-8")
    blocked = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    assert not blocked.ok and "unresolved" in blocked.error_summary


def test_cancelled_publication_allows_reprepare_and_atomic_failure_leaves_no_bundle(tmp_path):
    report_root, source_id = _source(tmp_path)
    state_root = tmp_path / "channel"
    first = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    path = state_root / first.publication_id / "attempt.json"
    attempt = json.loads(path.read_text(encoding="utf-8"))
    attempt["state"] = "cancelled"
    attempt["resolution_note"] = "reviewed"
    path.write_text(json.dumps(attempt), encoding="utf-8")
    second = prepare_channel_publication_from_report(report_root, state_root, source_id=source_id)
    assert second.ok

    isolated = tmp_path / "isolated"
    report_root2, source_id2 = _source(tmp_path / "other")
    def fail_publish(_temporary, _final):
        raise OSError("simulated rename failure")
    failed = prepare_channel_publication_from_report(
        report_root2, isolated, source_id=source_id2, publish=fail_publish,
    )
    assert not failed.ok
    assert not isolated.exists() or not list(isolated.iterdir())


def test_no_database_or_network_dependency_and_unsupported_source(tmp_path):
    result = prepare_channel_publication(
        source_type="recent_death",
        report_post_root=tmp_path / "missing",
        state_root=tmp_path / "channel",
    )
    assert not result.ok
    assert result.error_kind == "missing_database"
    assert not (tmp_path / "channel").exists()


def _recent_database(tmp_path, notifications):
    Path(tmp_path).mkdir(parents=True, exist_ok=True)
    entry = make_empty_database_entry("Recent Philosopher")
    entry["wikidata"].update({"status": "available", "qid": "Q123"})
    entry["recent_death_notifications"] = notifications
    path = tmp_path / "database.jsonl"
    path.write_bytes(serialize_database_entries([entry]))
    return path


def _sent_notification(identifier, when, message="<b>Recent Philosopher</b>\nDied: 1 January 2026"):
    item = make_pending_recent_death_notification(
        "Recent Philosopher", "Q123", "2026-01-01", message,
        notification_id=identifier, now=when,
    )
    return transition_recent_death_notification(item, "sent", now=when, telegram_message_id=987)


def test_recent_death_prepare_freezes_exact_text_and_survives_source_removal(tmp_path):
    identifier = "22345678-1234-4234-8234-123456789abc"
    when = datetime(2026, 9, 26, 12, tzinfo=timezone.utc)
    database = _recent_database(tmp_path, [_sent_notification(identifier, when)])
    state_root = tmp_path / "channel"
    result = prepare_channel_publication_from_recent_death(database, state_root)
    assert result.ok
    bundle = state_root / result.publication_id
    attempt = load_publication(state_root, result.publication_id)[0]
    assert attempt["source_type"] == RECENT_DEATH_SOURCE_TYPE
    assert attempt["payload_kind"] == "text"
    assert attempt["caption_html"] is None
    assert attempt["media"] == []
    assert attempt["message_html"].startswith("<b>Recent Philosopher</b>")
    assert result.telegram_called is False
    database.unlink()
    validate_publication_bundle(bundle, attempt)


def test_recent_death_source_states_and_message_validation(tmp_path):
    when = datetime(2026, 9, 26, tzinfo=timezone.utc)
    for state in ("pending", "failed", "unknown", "cancelled"):
        item = make_pending_recent_death_notification("Recent Philosopher", "Q123", "2026-01-01", "hello", notification_id="32345678-1234-4234-8234-123456789abc", now=when)
        if state == "failed":
            item = transition_recent_death_notification(item, state, now=when, error_kind="telegram_rejected", error_summary="rejected")
        elif state == "unknown":
            item = transition_recent_death_notification(item, state, now=when, error_kind="transport_ambiguous", error_summary="unknown")
        elif state == "cancelled":
            item = transition_recent_death_notification(item, state, now=when, resolution_note="cancelled")
        result = prepare_channel_publication_from_recent_death(_recent_database(tmp_path / state, [item]), tmp_path / (state + "-state"), source_id=item["notification_id"])
        assert not result.ok
    sent = _sent_notification("42345678-1234-4234-8234-123456789abc", when)
    sent["telegram_message_id"] = None
    result = prepare_channel_publication_from_recent_death(_recent_database(tmp_path / "missing-id", [sent]), tmp_path / "missing-id-state")
    assert not result.ok


def test_recent_death_latest_unpublished_and_cancelled_allows_retry(tmp_path):
    base = datetime(2026, 9, 26, tzinfo=timezone.utc)
    ids = ["52345678-1234-4234-8234-123456789abc", "62345678-1234-4234-8234-123456789abc", "72345678-1234-4234-8234-123456789abc"]
    notifications = [_sent_notification(identifier, base.replace(hour=index)) for index, identifier in enumerate(ids)]
    database = _recent_database(tmp_path / "latest", notifications)
    state = tmp_path / "latest-state"
    first = prepare_channel_publication_from_recent_death(database, state)
    assert first.source_id == ids[2]
    path = state / first.publication_id / "attempt.json"
    attempt = json.loads(path.read_text())
    attempt["state"] = "sent"
    attempt["telegram_message_ids"] = [1]
    path.write_text(json.dumps(attempt))
    second = prepare_channel_publication_from_recent_death(database, state)
    assert second.source_id == ids[1]


def test_recent_death_cross_source_identity_does_not_collide(tmp_path):
    # The source type is part of the publication identity and duplicate checks.
    identifier = "82345678-1234-4234-8234-123456789abc"
    report_root, _ = _source(tmp_path / "report", source_id=identifier)
    state = tmp_path / "cross-state"
    stat = prepare_channel_publication_from_report(report_root, state, source_id=identifier)
    assert stat.ok
    _mark_publication_sent(state / stat.publication_id)
    recent = _recent_database(tmp_path / "cross-db", [_sent_notification(identifier, datetime(2026, 9, 26, tzinfo=timezone.utc))])
    result = prepare_channel_publication_from_recent_death(recent, state, source_id=identifier)
    assert result.ok


def test_dispatch_statistical_media_group_uses_frozen_payload_once(tmp_path):
    report_root, source_id = _source(tmp_path / "report")
    state = tmp_path / "state"
    prepared = prepare_channel_publication_from_report(report_root, state, source_id=source_id)
    calls = []
    media_ids = [11, 12, 13, 14]
    def sender(payload, caption, url, chat):
        calls.append((payload, caption, url, chat))
        return TelegramMediaGroupResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, media_ids)
    result = dispatch_channel_publication(prepared.publication_id, state, confirmed=True, sender_media=sender, settings=("text-url", "media-url", "channel"))
    assert result.ok and result.ending_state == "sent"
    assert len(calls) == 1 and calls[0][1].startswith("<b>Philosopher deaths")
    assert [item["filename"] for item in calls[0][0]] == [item["filename"] for item in load_publication(state, prepared.publication_id)[0]["media"]]
    assert load_publication(state, prepared.publication_id)[0]["telegram_message_ids"] == media_ids


def test_dispatch_recent_text_and_reconciliation(tmp_path):
    identifier = "92345678-1234-4234-8234-123456789abc"
    database = _recent_database(tmp_path / "db", [_sent_notification(identifier, datetime(2026, 9, 26, tzinfo=timezone.utc), "<b>Exact frozen text</b>")])
    state = tmp_path / "state"
    prepared = prepare_channel_publication_from_recent_death(database, state, source_id=identifier)
    calls = []
    def sender(text, url, chat):
        calls.append((text, url, chat))
        return TelegramResult(True, {"ok": True}, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, 42)
    database.unlink()
    result = dispatch_channel_publication(prepared.publication_id, state, confirmed=True, sender_text=sender, settings=("text-url", "media-url", "channel"))
    assert result.ok and result.telegram_message_ids == [42]
    assert calls == [("<b>Exact frozen text</b>", "text-url", "channel")]
    shown = reconcile_channel_publication(prepared.publication_id, state, "show")
    assert "message_html" not in shown
    assert reconcile_channel_publication(prepared.publication_id, state, "show", show_message=True)["message_html"] == "<b>Exact frozen text</b>"


def test_dispatch_rejection_unknown_and_confirmation_checks(tmp_path):
    report_root, source_id = _source(tmp_path / "report")
    state = tmp_path / "state"
    prepared = prepare_channel_publication_from_report(report_root, state, source_id=source_id)
    no_confirm = dispatch_channel_publication(prepared.publication_id, state, settings=("t", "m", "c"))
    assert not no_confirm.ok and no_confirm.error_kind == "confirmation_required"
    rejected = dispatch_channel_publication(prepared.publication_id, state, confirmed=True,
        sender_media=lambda *args: TelegramMediaGroupResult(False, {"ok": False}, "telegram_error", TELEGRAM_OUTCOME_DEFINITE_REJECTION, None), settings=("t", "m", "c"))
    assert not rejected.ok and rejected.ending_state == "failed"


def test_dispatch_integrity_failure_does_not_contact_telegram(tmp_path):
    report_root, source_id = _source(tmp_path / "report")
    state = tmp_path / "state"
    prepared = prepare_channel_publication_from_report(report_root, state, source_id=source_id)
    media = state / prepared.publication_id / "deaths-by-year.png"
    media.write_bytes(b"corrupt")
    calls = []
    result = dispatch_channel_publication(prepared.publication_id, state, confirmed=True, sender_media=lambda *args: calls.append(args), settings=("t", "m", "c"))
    assert not result.ok and not result.telegram_called and not calls


def test_reconciliation_unknown_requires_unsafe_confirmation(tmp_path):
    report_root, source_id = _source(tmp_path / "report")
    state = tmp_path / "state"
    prepared = prepare_channel_publication_from_report(report_root, state, source_id=source_id)
    def ambiguous(*args):
        return TelegramMediaGroupResult(False, None, "request_exception", TELEGRAM_OUTCOME_AMBIGUOUS, None)
    result = dispatch_channel_publication(prepared.publication_id, state, confirmed=True, sender_media=ambiguous, settings=("t", "m", "c"))
    assert result.ending_state == "unknown"
    assert not reconcile_channel_publication(prepared.publication_id, state, "cancel", note="x")["ok"]
    assert reconcile_channel_publication(prepared.publication_id, state, "cancel", note="x", confirm_unsafe=True)["ok"]
