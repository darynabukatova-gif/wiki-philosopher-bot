from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/recent-death-monitor.yml"
POST_WORKFLOW_PATH = ROOT / ".github/workflows/manual-post.yml"


def workflow_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_recent_death_workflow_is_manual_only_and_shares_authoritative_data_lock():
    text = workflow_text()
    assert "on:\n  workflow_dispatch:" in text
    assert "schedule:" not in text
    assert "group: wiki-philosopher-authoritative-data" in text
    assert "cancel-in-progress: false" in text
    assert "group: wiki-philosopher-authoritative-data" in POST_WORKFLOW_PATH.read_text(encoding="utf-8")


def test_manual_inputs_are_optional_and_safely_passed_as_environment_values():
    text = workflow_text()
    assert "recent_days:" in text and "required: false" in text
    assert "title:" in text and "Optional exact canonical title" in text
    assert "REQUESTED_TITLE: ${{ inputs.title }}" in text
    assert "REQUESTED_RECENT_DAYS: ${{ inputs.recent_days }}" in text
    assert 'prepare_args+=(--title "$REQUESTED_TITLE")' in text
    assert 'prepare_args+=(--recent-days "$REQUESTED_RECENT_DAYS")' in text
    assert "--title \"${{ inputs.title }}\"" not in text


def test_prepare_uses_explicit_machine_result_and_has_no_telegram_secrets():
    text = workflow_text()
    prepare = text[text.index("name: Discover deaths"):text.index("id: pending_checkpoint")]
    assert "--result-json \"${result_path}\"" in prepare
    assert "new_notification_ids" in prepare
    assert "notification_ids_path" in prepare
    assert "TELEGRAM_TOKEN" not in prepare
    assert "RECENT_DEATH_TELEGRAM_CHAT_ID" not in prepare


def test_required_backup_directory_is_created_before_prepare():
    text = workflow_text()
    directory_setup = text.index("name: Create required runtime directories")
    prepare = text.index("name: Discover deaths and prepare durable notifications")

    assert directory_setup < prepare
    assert "mkdir -p backups/database" in text[directory_setup:prepare]


def test_private_checkout_and_checkpoints_are_database_only_and_ordered():
    text = workflow_text()
    assert "repository: ${{ vars.DATA_REPOSITORY }}" in text
    assert "token: ${{ secrets.DATA_REPO_TOKEN }}" in text
    assert "path: private-data" in text
    assert text.count("git -C private-data add -- database.jsonl") == 2
    assert "git add ." not in text
    assert text.index("Commit and push prepared database checkpoint") < text.index("Dispatch only notifications prepared by this workflow run")
    assert "Require a durable pending checkpoint before any notification dispatch" in text


def test_dispatch_only_uses_same_run_ids_without_pending_scan_or_retry():
    text = workflow_text()
    dispatch = text[text.index("name: Dispatch only"):]
    assert 'done < "${NOTIFICATION_IDS_PATH}"' in dispatch
    assert "wiki-philosopher-dispatch-recent-death --notification-id \"${notification_id}\"" in dispatch
    assert dispatch.count("wiki-philosopher-dispatch-recent-death") == 1
    assert "recent_death_notifications" not in dispatch
    assert "pending" not in dispatch.lower() or "checkpoint" in dispatch.lower()
    assert "continue-on-error" not in text
    assert "retry_count" not in dispatch.lower()
    assert "for retry" not in dispatch.lower()


def test_terminal_checkpoint_is_per_dispatch_and_push_failure_is_fatal():
    text = workflow_text()
    dispatch = text[text.index("name: Dispatch only"):]
    assert "if ! git -C private-data push origin HEAD; then" in dispatch
    assert "Terminal checkpoint push failed. Do not rerun dispatch" in dispatch
    assert "Notification reached a terminal non-success state" in dispatch
    assert "No actionable recent death was prepared; no Telegram dispatch was run." in text
    assert "Death facts changed and were checkpointed" in text


def test_telegram_secrets_are_scoped_to_dispatch_only():
    text = workflow_text()
    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in text
    assert "RECENT_DEATH_TELEGRAM_CHAT_ID: ${{ secrets.RECENT_DEATH_TELEGRAM_CHAT_ID }}" in text
    assert "ghp_" not in text
