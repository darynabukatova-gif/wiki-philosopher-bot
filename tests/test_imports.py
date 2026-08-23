import sys
import cache
import pytest
import importlib
import subprocess
import telegram_bot
import wikipedia_api
from pathlib import Path
from concurrent import futures

def test_importing_main_has_no_application_side_effects(monkeypatch: pytest.MonkeyPatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("application work occurred during import")

    for loader_name in (
        "load_database",
        "load_summary_cache",
        "load_entity_cache",
        "load_processed_cache",
        "load_result_cache",
        "load_quote_cache",
        "load_quote_failure_cache",
        "load_posted_titles",
    ):
        monkeypatch.setattr(cache, loader_name, forbidden)

    monkeypatch.setattr(cache, "persist_evaluation_entry", forbidden)
    monkeypatch.setattr(cache, "save_posted_titles", forbidden)
    monkeypatch.setattr(wikipedia_api, "get_all_pages", forbidden)
    monkeypatch.setattr(wikipedia_api, "build_entity_cache", forbidden)
    monkeypatch.setattr(telegram_bot, "send_message", forbidden)
    monkeypatch.setattr(futures, "ThreadPoolExecutor", forbidden)

    sys.modules.pop("main", None)

    module = importlib.import_module("main")

    assert callable(module.main)
    assert callable(module.load_runtime_state)


def test_importing_main_does_not_load_dotenv():
    project_root = Path(__file__).resolve().parents[1]
    code = "\n".join(
        (
            "import dotenv",
            "dotenv.load_dotenv = lambda: (_ for _ in ()).throw(AssertionError('dotenv loaded'))",
            "import main",
        )
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(project_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )

    assert result.returncode == 0, result.stderr
