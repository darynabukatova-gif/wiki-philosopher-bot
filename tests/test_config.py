import importlib
import sys

import dotenv


def test_importing_config_does_not_call_load_dotenv(monkeypatch):
    calls = []
    module_name = "wiki_philosopher_bot.config"
    original_config = sys.modules.get(module_name)

    monkeypatch.setattr(
        dotenv,
        "load_dotenv",
        lambda: calls.append(True),
    )
    monkeypatch.delitem(sys.modules, module_name, raising=False)

    importlib.import_module(module_name)

    assert calls == []

    if original_config is not None:
        sys.modules[module_name] = original_config


def test_explicit_configuration_loading_calls_load_dotenv_once(monkeypatch):
    import wiki_philosopher_bot.config as config

    calls = []
    monkeypatch.setattr(
        config,
        "load_dotenv",
        lambda: calls.append(True),
    )

    config.load_environment()

    assert calls == [True]


def test_wikimedia_user_agent_uses_optional_environment_contact(monkeypatch):
    import wiki_philosopher_bot.config as config

    monkeypatch.delenv("WIKIMEDIA_USER_AGENT_CONTACT", raising=False)
    assert config.get_wikimedia_user_agent() == "WikiScraperBot/3.0"

    monkeypatch.setenv("WIKIMEDIA_USER_AGENT_CONTACT", "project.example/contact")
    assert config.get_wikimedia_user_agent() == (
        "WikiScraperBot/3.0 (project.example/contact)"
    )


def test_report_media_group_settings_use_only_the_dedicated_report_chat(monkeypatch):
    import wiki_philosopher_bot.config as config

    monkeypatch.setenv("TELEGRAM_TOKEN", "token-value")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "normal-post-chat")
    monkeypatch.setenv("REPORT_TELEGRAM_CHAT_ID", "report-post-chat")

    url, chat_id = config.get_report_telegram_media_group_settings()

    assert url == "https://api.telegram.org/bottoken-value/sendMediaGroup"
    assert chat_id == "report-post-chat"
