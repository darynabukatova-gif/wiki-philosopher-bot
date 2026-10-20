from pathlib import Path

ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/manual-channel-publication.yml"


def text():
    return WORKFLOW.read_text(encoding="utf-8")


def test_manual_only_inputs_and_concurrency():
    value = text()
    assert value.startswith("name: Publish private preview to channel\n")
    assert "on:\n  workflow_dispatch:" in value
    assert "schedule:" not in value
    assert "source_type:" in value
    assert "- statistical_report" in value and "- recent_death" in value
    assert "source_id:" in value and "required: false" in value
    assert "confirm_publish:" in value and "default: false" in value
    assert "group: wiki-philosopher-authoritative-data" in value
    assert "cancel-in-progress: false" in value


def test_prepare_uses_private_sources_and_has_no_telegram_secrets():
    value = text()
    prepare = value[value.index("id: prepare"):value.index("id: pending_checkpoint")]
    assert "--report-post-root" in prepare
    assert "--database" in prepare
    assert "channel-publications" in prepare
    assert "if [[ -n \"${SOURCE_ID}\" ]]" in prepare
    assert "TELEGRAM_TOKEN" not in prepare
    assert "CHANNEL_TELEGRAM_CHAT_ID" not in prepare
    assert "telegram_called" in prepare
    assert "ending_state" in prepare


def test_pending_checkpoint_precedes_exact_dispatch_and_stages_only_bundle():
    value = text()
    pending = value.index("id: pending_checkpoint")
    dispatch = value.index("id: dispatch")
    assert pending < dispatch
    assert 'git -C private-data add -f -- "${bundle}"' in value
    assert 'git add .' not in value
    assert "database.jsonl must never" in value or "database.jsonl and report-posts" in value
    assert "steps.pending_checkpoint.outputs.checkpointed == 'true'" in value
    assert value.count("wiki-philosopher-dispatch-channel-publication") == 1


def test_dispatch_secrets_and_terminal_checkpoint_are_scoped():
    value = text()
    dispatch = value[value.index("id: dispatch"):value.index("id: terminal_checkpoint")]
    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in dispatch
    assert "CHANNEL_TELEGRAM_CHAT_ID: ${{ secrets.CHANNEL_TELEGRAM_CHAT_ID }}" in dispatch
    assert "REPORT_TELEGRAM_CHAT_ID" not in dispatch
    assert "RECENT_DEATH_TELEGRAM_CHAT_ID" not in dispatch
    assert "\n          TELEGRAM_CHAT_ID:" not in dispatch
    terminal = value[value.index("id: terminal_checkpoint"):value.index("name: Fail safely")]
    assert "channel-publications/${PUBLICATION_ID}/attempt.json" in terminal
    assert "Finalize channel publication ${PUBLICATION_ID}" in terminal
    assert "DO NOT rerun" in value


def test_no_retry_and_non_secret_artifacts():
    value = text()
    assert "set +e" in value
    assert "dispatch_status=$?" in value
    assert "retry" not in value.lower() or "do not retry" in value.lower()
    artifacts = value[value.index("name: Upload non-secret publication diagnostics"):]
    assert "prepare-channel-publication.json" in artifacts
    assert "dispatch-channel-publication.json" in artifacts
    assert "database.jsonl" not in artifacts
    assert "report-posts" not in artifacts
