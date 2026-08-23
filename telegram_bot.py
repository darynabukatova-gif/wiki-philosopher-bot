import requests
from typing import Optional
from dataclasses import dataclass
from config import get_telegram_settings, REQUEST_TIMEOUT

@dataclass
class TelegramResult:
    ok: bool
    response_data: Optional[dict]
    error_reason: Optional[str]

def send_message(text, post=None):
    telegram_url, chat_id = get_telegram_settings()
    return send_message_to_chat(text, telegram_url, chat_id, post=post)


def send_message_to_chat(text, telegram_url, chat_id, post=None):
    """Send HTML text to one explicitly supplied Telegram chat destination."""
    if post is None:
        post = requests.post

    if not telegram_url or not chat_id:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="missing_configuration",
        )

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }

    try:
        response = post(
            telegram_url,
            data=payload,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.HTTPError:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="http_error",
        )
    except requests.RequestException:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="request_exception",
        )

    try:
        response_data = response.json()
    except ValueError:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="invalid_json",
        )

    if not isinstance(response_data, dict):
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="invalid_response",
        )

    if response_data.get("ok") is not True:
        return TelegramResult(
            ok=False,
            response_data=response_data,
            error_reason="telegram_error",
        )

    return TelegramResult(
        ok=True,
        response_data=response_data,
        error_reason=None,
    )
