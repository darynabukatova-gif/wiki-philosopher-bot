from pathlib import Path


WORKFLOW_PATH = Path(__file__).parents[1] / ".github/workflows/manual-post.yml"


def workflow_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_post_workflow_supports_manual_and_daily_dublin_execution_without_overlap():
    text = workflow_text()

    assert "on:\n  workflow_dispatch:" in text
    assert "schedule:\n    - cron: '19 16 * * *'\n      timezone: 'Europe/Dublin'" in text
    assert text.count("  schedule:") == 1
    assert "group: wiki-philosopher-posting" in text
    assert "cancel-in-progress: false" in text


def test_private_data_checkout_uses_configured_repository_and_secret_token():
    text = workflow_text()

    assert "repository: ${{ vars.DATA_REPOSITORY }}" in text
    assert "token: ${{ secrets.DATA_REPO_TOKEN }}" in text
    assert "path: private-data" in text
    assert "persist-credentials: true" in text
    assert "DATA_REPOSITORY must have the form owner/private-data-repository" in text


def test_dispatch_is_after_a_successful_pending_push_and_has_no_retry():
    text = workflow_text()

    pending_push = text.index("Commit and push pending database checkpoint")
    dispatch = text.index("Dispatch exactly the checkpointed pending attempt once")
    assert pending_push < dispatch
    assert "wiki-philosopher-dispatch-post --attempt-id \"${ATTEMPT_ID}\"" in text
    assert text.count("wiki-philosopher-dispatch-post") == 1
    assert "continue-on-error" not in text
    assert "retry" not in text.lower()


def test_workflow_commits_only_database_and_terminal_push_is_fatal():
    text = workflow_text()

    assert text.count("git -C private-data add -- database.jsonl") == 2
    assert "git add ." not in text
    assert "Refusing to commit anything other than database.jsonl" in text
    assert "if ! git -C private-data push origin HEAD; then" in text
    assert "Terminal checkpoint push failed. Do not rerun dispatch" in text


def test_workflow_supplies_telegram_values_only_as_secrets():
    text = workflow_text()

    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in text
    assert "TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}" in text
    assert "ghp_" not in text
    assert "bot[0-9]" not in text
