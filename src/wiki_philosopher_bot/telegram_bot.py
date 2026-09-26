import json
import requests
from typing import List, Optional
from dataclasses import dataclass
from wiki_philosopher_bot.config import get_telegram_settings, REQUEST_TIMEOUT


TELEGRAM_OUTCOME_CONFIRMED_SUCCESS = "confirmed_success"
TELEGRAM_OUTCOME_DEFINITE_REJECTION = "definite_rejection"
TELEGRAM_OUTCOME_DEFINITE_FAILURE = "definite_failure"
TELEGRAM_OUTCOME_AMBIGUOUS = "ambiguous"

@dataclass
class TelegramResult:
    ok: bool
    response_data: Optional[dict]
    error_reason: Optional[str]
    outcome: str = TELEGRAM_OUTCOME_AMBIGUOUS
    message_id: Optional[int] = None


@dataclass
class TelegramMediaGroupResult:
    ok: bool
    response_data: Optional[dict]
    error_reason: Optional[str]
    outcome: str = TELEGRAM_OUTCOME_AMBIGUOUS
    message_ids: Optional[List[int]] = None

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
            outcome=TELEGRAM_OUTCOME_DEFINITE_FAILURE,
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
            outcome=TELEGRAM_OUTCOME_AMBIGUOUS,
        )
    except requests.RequestException:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="request_exception",
            outcome=TELEGRAM_OUTCOME_AMBIGUOUS,
        )

    try:
        response_data = response.json()
    except ValueError:
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="invalid_json",
            outcome=TELEGRAM_OUTCOME_AMBIGUOUS,
        )

    if not isinstance(response_data, dict):
        return TelegramResult(
            ok=False,
            response_data=None,
            error_reason="invalid_response",
            outcome=TELEGRAM_OUTCOME_AMBIGUOUS,
        )

    if response_data.get("ok") is not True:
        return TelegramResult(
            ok=False,
            response_data=response_data,
            error_reason="telegram_error",
            outcome=TELEGRAM_OUTCOME_DEFINITE_REJECTION,
        )

    result = response_data.get("result")
    message_id = result.get("message_id") if isinstance(result, dict) else None
    if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
        return TelegramResult(
            ok=True,
            response_data=response_data,
            error_reason=None,
            outcome=TELEGRAM_OUTCOME_AMBIGUOUS,
        )

    return TelegramResult(
        ok=True,
        response_data=response_data,
        error_reason=None,
        outcome=TELEGRAM_OUTCOME_CONFIRMED_SUCCESS,
        message_id=message_id,
    )


def send_media_group_to_chat(media, caption_html, telegram_url, chat_id, post=None):
    """Send one exact photo media group in one HTTP request."""
    if post is None:
        post = requests.post
    if not telegram_url or not chat_id:
        return TelegramMediaGroupResult(False, None, "missing_configuration", TELEGRAM_OUTCOME_DEFINITE_FAILURE)
    descriptions = []
    files = {}
    for index, item in enumerate(media, 1):
        attachment = "media{}".format(index)
        description = {"type": "photo", "media": "attach://{}".format(attachment)}
        if index == 1:
            description.update({"caption": caption_html, "parse_mode": "HTML"})
        descriptions.append(description)
        files[attachment] = (item["filename"], item["bytes"], "image/png")
    try:
        response = post(telegram_url, data={"chat_id": chat_id, "media": json.dumps(descriptions, ensure_ascii=False)}, files=files, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
    except requests.HTTPError:
        return TelegramMediaGroupResult(False, None, "http_error", TELEGRAM_OUTCOME_AMBIGUOUS)
    except requests.RequestException:
        return TelegramMediaGroupResult(False, None, "request_exception", TELEGRAM_OUTCOME_AMBIGUOUS)
    try:
        payload = response.json()
    except ValueError:
        return TelegramMediaGroupResult(False, None, "invalid_json", TELEGRAM_OUTCOME_AMBIGUOUS)
    if not isinstance(payload, dict):
        return TelegramMediaGroupResult(False, None, "invalid_response", TELEGRAM_OUTCOME_AMBIGUOUS)
    if payload.get("ok") is not True:
        return TelegramMediaGroupResult(False, payload, "telegram_error", TELEGRAM_OUTCOME_DEFINITE_REJECTION)
    result = payload.get("result")
    ids = [row.get("message_id") for row in result] if isinstance(result, list) and all(isinstance(row, dict) for row in result) else []
    valid = len(ids) == len(media) and all(isinstance(value, int) and not isinstance(value, bool) and value > 0 for value in ids)
    if not valid:
        return TelegramMediaGroupResult(True, payload, "invalid_response", TELEGRAM_OUTCOME_AMBIGUOUS)
    return TelegramMediaGroupResult(True, payload, None, TELEGRAM_OUTCOME_CONFIRMED_SUCCESS, ids)
