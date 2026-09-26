from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/manual-death-report-post.yml"
POST_WORKFLOW_PATH = ROOT / ".github/workflows/manual-post.yml"
RECENT_DEATH_WORKFLOW_PATH = ROOT / ".github/workflows/recent-death-monitor.yml"


def workflow_text():
    return WORKFLOW_PATH.read_text(encoding="utf-8")


def test_workflow_is_manual_only_and_requires_explicit_send_confirmation():
    text = workflow_text()

    assert text.startswith("name: Manual philosopher death report\n")
    assert "on:\n  workflow_dispatch:\n" in text
    assert "schedule:" not in text
    assert "group: wiki-philosopher-authoritative-data" in text
    assert "cancel-in-progress: false" in text
    assert "last_years:" in text and "default: '7'" in text
    assert "to_date:" in text
    assert "rows_per_page:" in text and "default: '22'" in text
    assert "confirm_send:" in text and "default: false" in text
    assert text.index("Validate explicit manual inputs") < text.index("name: Freeze one pending")
    assert 'if [[ "${CONFIRM_SEND}" != "true" ]]; then' in text
    assert "for name in (\"LAST_YEARS\", \"ROWS_PER_PAGE\")" in text
    assert "must be a positive integer" in text
    assert "TO_DATE must be a valid YYYY-MM-DD date" in text


def test_workflow_uses_private_database_for_explicit_temp_report_and_render_paths():
    text = workflow_text()

    assert "repository: ${{ vars.DATA_REPOSITORY }}" in text
    assert "token: ${{ secrets.DATA_REPO_TOKEN }}" in text
    assert "path: private-data" in text
    assert "git -C private-data fetch origin main" in text
    assert "private-data/database.jsonl data/database.jsonl" in text
    assert 'report_path="${RUNNER_TEMP}/historical-death-report.json"' in text
    assert 'assets_dir="${RUNNER_TEMP}/death-report-assets"' in text
    assert "wiki-philosopher-death-report" in text
    assert "wiki-philosopher-render-death-report" in text
    assert "--data-folder data --json \"${report_path}\"" in text
    assert "--output-dir \"${assets_dir}\" --rows-per-page \"${ROWS_PER_PAGE}\"" in text
    assert "refresh-wikidata" not in text
    pending = text[text.index("name: Commit and push pending report-post bundle"):text.index("id: dispatch")]
    assert "git -C private-data add -- database.jsonl" not in pending
    assert "cp -- data/database.jsonl private-data/database.jsonl" not in pending


def test_prepare_uses_private_report_post_root_and_has_no_telegram_secrets():
    text = workflow_text()
    prepare = text[text.index("id: prepare"):text.index("id: pending_checkpoint")]

    assert "REPORT_POST_STATE_ROOT: ${{ github.workspace }}/private-data/report-posts" in prepare
    assert "wiki-philosopher-prepare-report-post" in prepare
    assert "--result-json \"${result_path}\"" in prepare
    assert "historical-death-report" in prepare
    assert "Record prepared report-post attempt" in text
    assert "${GITHUB_STEP_SUMMARY}" in text
    assert "attempt_id" in prepare
    assert "TELEGRAM_TOKEN" not in prepare
    assert "REPORT_TELEGRAM_CHAT_ID" not in prepare


def test_pending_checkpoint_precedes_exact_single_dispatch_and_stages_only_bundle():
    text = workflow_text()
    pending = text.index("name: Commit and push pending report-post bundle before Telegram")
    dispatch = text.index("name: Dispatch exactly the pushed report-post attempt once")

    assert pending < dispatch
    assert "git -C private-data add -f -- \"${ATTEMPT_BUNDLE}\"" in text
    assert "Prepare death report post ${ATTEMPT_ID}" in text
    assert "git -C private-data push origin HEAD" in text
    assert "steps.pending_checkpoint.outputs.checkpointed == 'true'" in text
    assert "wiki-philosopher-dispatch-report-post --attempt-id \"${ATTEMPT_ID}\"" in text
    assert text.count("wiki-philosopher-dispatch-report-post") == 1
    assert "continue-on-error" not in text
    assert "git add ." not in text
    assert "force push" not in text.lower()
    assert "database.jsonl must never be staged by this workflow" in text


def test_dispatch_has_dedicated_secrets_and_terminal_checkpoint_handles_all_terminal_outcomes():
    text = workflow_text()
    dispatch = text[text.index("id: dispatch"):text.index("id: terminal_checkpoint")]
    terminal = text[text.index("id: terminal_checkpoint"):text.index("name: Upload non-secret")]

    assert "TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}" in dispatch
    assert "REPORT_TELEGRAM_CHAT_ID: ${{ secrets.REPORT_TELEGRAM_CHAT_ID }}" in dispatch
    assert "RECENT_DEATH_TELEGRAM_CHAT_ID" not in dispatch
    assert "\n          TELEGRAM_CHAT_ID:" not in dispatch
    assert "set +e" in dispatch and "dispatch_status=$?" in dispatch
    assert '("sent", "failed", "unknown")' in dispatch
    assert "steps.dispatch.outputs.persistence_succeeded == 'true'" in terminal
    assert "report-posts/${ATTEMPT_ID}/attempt.json" in terminal
    assert "Finalize death report post ${ATTEMPT_ID}" in terminal
    assert "Terminal checkpoint push failed. Do not rerun dispatch" in terminal
    assert "manual report-post reconciliation is required" in terminal
    assert "Do not retry automatically; reconcile deliberately." in text


def test_summary_renders_attempt_id_without_shell_command_substitution():
    text = workflow_text()

    assert "printf 'Prepared attempt: `%s`\\n' \"${ATTEMPT_ID}\"" in text
    assert 'echo "Prepared attempt: `${ATTEMPT_ID}`"' not in text
    assert "${GITHUB_STEP_SUMMARY}" in text


def test_shared_authoritative_lock_and_non_secret_result_artifacts_are_preserved():
    text = workflow_text()

    assert "group: wiki-philosopher-authoritative-data" in POST_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "group: wiki-philosopher-authoritative-data" in RECENT_DEATH_WORKFLOW_PATH.read_text(encoding="utf-8")
    assert "actions/upload-artifact@v4" in text
    assert "death-report-post-prepare.json" in text
    assert "death-report-post-dispatch.json" in text
    assert "report-posts" not in text[text.index("name: Upload non-secret"):]
    assert "ghp_" not in text
