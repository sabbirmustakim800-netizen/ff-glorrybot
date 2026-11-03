"""Minimal async Telegram Bot API client.

Only the methods the bot needs: sendMessage, editMessageText,
answerCallbackQuery, sendPhoto/sendVideo/sendDocument.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .config import get_settings

logger = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
_client: httpx.AsyncClient | None = None

MEDIA_METHODS = {
    "photo": "sendPhoto",
    "video": "sendVideo",
    "document": "sendDocument",
}


def http_client() -> httpx.AsyncClient:
    """Shared client — reused across warm function invocations."""
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
    return _client


async def api_call(method: str, payload: dict[str, Any]) -> dict | None:
    """POST to the Bot API; returns the decoded body on success, else None."""
    settings = get_settings()
    if not settings.telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot call %s", method)
        return None
    url = f"{API_BASE}/bot{settings.telegram_token}/{method}"
    try:
        resp = await http_client().post(url, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200 or not (isinstance(data, dict) and data.get("ok")):
            logger.warning(
                "%s failed (HTTP %s): %s", method, resp.status_code, resp.text[:300]
            )
            return None
        return data
    except Exception as exc:  # network / timeout — never crash the update handler
        logger.warning("%s error: %s", method, exc)
        return None


async def send_message(
    chat_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await api_call("sendMessage", payload) is not None


async def edit_message(
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: dict | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text[:4096],
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    result = await api_call("editMessageText", payload)
    if result is None:
        # "message is not modified" is harmless — not worth retrying differently.
        logger.debug("editMessageText failed for chat=%s msg=%s", chat_id, message_id)
        return False
    return True


async def answer_callback(
    callback_id: str, text: str | None = None, alert: bool = False
) -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = text[:200]
    if alert:
        payload["show_alert"] = True
    await api_call("answerCallbackQuery", payload)


async def send_media(
    chat_id: int,
    kind: str,
    file_id: str,
    caption: str = "",
    reply_markup: dict | None = None,
) -> bool:
    method = MEDIA_METHODS.get(kind)
    if not method:
        return False
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "caption": (caption or "")[:1024],
    }
    payload[
        {"photo": "photo", "video": "video", "document": "document"}[kind]
    ] = file_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await api_call(method, payload) is not None
