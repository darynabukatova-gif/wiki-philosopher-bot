import requests
import pytest
import wiki_philosopher_bot.telegram_bot as telegram_bot

class FakeTelegramResponse:
    def __init__(self, payload=None, http_error=None, json_error=None):
        self.payload = payload if payload is not None else {}
        self.http_error = http_error
        self.json_error = json_error

    def raise_for_status(self):
        if self.http_error is not None:
            raise self.http_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


@pytest.fixture(autouse=True)
def configured_telegram_settings(monkeypatch):
    monkeypatch.setattr(
        telegram_bot,
        "get_telegram_settings",
        lambda: ("https://api.telegram.org/botTEST/sendMessage", "chat-id"),
    )

def test_send_message_returns_success_result():
    calls = []

    def fake_post(url, data, timeout):
        calls.append(
            {
                "url": url,
                "data": data,
                "timeout": timeout,
            }
        )
        return FakeTelegramResponse(payload={"ok": True, "result": {"message_id": 42}})

    result = telegram_bot.send_message(
        "Hello",
        post=fake_post,
    )

    assert result.ok is True
    assert result.response_data == {"ok": True, "result": {"message_id": 42}}
    assert result.error_reason is None
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_CONFIRMED_SUCCESS
    assert result.message_id == 42
    assert len(calls) == 1

def test_send_message_returns_http_failure_result():
    def fake_post(url, data, timeout):
        return FakeTelegramResponse(
            http_error=requests.HTTPError("bad request")
        )

    result = telegram_bot.send_message(
        "Hello",
        post=fake_post,
    )

    assert result.ok is False
    assert result.response_data is None
    assert result.error_reason == "http_error"
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_AMBIGUOUS

def test_send_message_returns_request_failure_result():
    def fake_post(url, data, timeout):
        raise requests.Timeout("timeout")

    result = telegram_bot.send_message(
        "Hello",
        post=fake_post,
    )

    assert result.ok is False
    assert result.error_reason == "request_exception"
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_AMBIGUOUS

def test_send_message_returns_invalid_json_result():
    def fake_post(url, data, timeout):
        return FakeTelegramResponse(
            json_error=ValueError("invalid JSON")
        )

    result = telegram_bot.send_message(
        "Hello",
        post=fake_post,
    )

    assert result.ok is False
    assert result.error_reason == "invalid_json"
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_AMBIGUOUS

def test_send_message_returns_telegram_failure_result():
    def fake_post(url, data, timeout):
        return FakeTelegramResponse(
            payload={
                "ok": False,
                "description": "Bad Request",
            }
        )

    result = telegram_bot.send_message(
        "Hello",
        post=fake_post,
    )

    assert result.ok is False
    assert result.error_reason == "telegram_error"
    assert result.response_data == {
        "ok": False,
        "description": "Bad Request",
    }
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_DEFINITE_REJECTION


def test_send_message_returns_failure_on_non_object_json():
    for payload in ([], "ok", 123, None):
        response = FakeTelegramResponse()
        response.payload = payload

        result = telegram_bot.send_message(
            "Hello",
            post=lambda url, data, timeout, response=response: response,
        )

        assert result.ok is False
        assert result.response_data is None
        assert result.error_reason == "invalid_response"
        assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_AMBIGUOUS


def test_send_message_does_not_swallow_unexpected_programming_exception():
    def fake_post(url, data, timeout):
        raise RuntimeError("bug")

    try:
        telegram_bot.send_message("Hello", post=fake_post)
    except RuntimeError as error:
        assert str(error) == "bug"
    else:
        raise AssertionError("unexpected programming exception was swallowed")


def test_send_message_returns_failure_when_telegram_configuration_missing(
    monkeypatch,
):
    for settings in ((None, "chat-id"), ("https://example.invalid", None)):
        monkeypatch.setattr(
            telegram_bot,
            "get_telegram_settings",
            lambda settings=settings: settings,
        )

        result = telegram_bot.send_message(
            "Hello",
            post=lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("missing configuration must not make a request")
            ),
        )

        assert result.ok is False
        assert result.response_data is None
        assert result.error_reason == "missing_configuration"
        assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_DEFINITE_FAILURE


def test_ok_response_without_positive_message_id_is_not_confirmed_delivery():
    result = telegram_bot.send_message(
        "Hello",
        post=lambda *args, **kwargs: FakeTelegramResponse(payload={"ok": True}),
    )

    assert result.ok is True
    assert result.outcome == telegram_bot.TELEGRAM_OUTCOME_AMBIGUOUS
    assert result.message_id is None
