"""Minimal async Telegram Bot API client (sendMessage only)."""

from __future__ import annotations

import logging

import httpx

from .config import get_settings

logger = logging.getLogger("telegram")

API_BASE = "https://api.telegram.org"
_client: httpx.AsyncClient | None = None


def http_client() -> httpx.AsyncClient:
    """Shared client — reused across warm function invocations."""
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0))
    return _client


async def send_message(chat_id: int, text: str) -> bool:
    settings = get_settings()
    if not settings.telegram_token:
        logger.error("TELEGRAM_BOT_TOKEN is not set; cannot reply to %s", chat_id)
        return False
    url = f"{API_BASE}/bot{settings.telegram_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text[:4096]}
    try:
        resp = await http_client().post(url, json=payload)
        try:
            data = resp.json()
        except ValueError:
            data = None
        if resp.status_code != 200 or not (isinstance(data, dict) and data.get("ok")):
            logger.warning(
                "sendMessage failed (HTTP %s): %s", resp.status_code, resp.text[:300]
            )
            return False
        return True
    except Exception as exc:  # network / timeout — never crash the update handler
        logger.warning("sendMessage error: %s", exc)
        return False