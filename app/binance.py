"""Binance deposit verification using a read-only API key.

Flow: signed GET /sapi/v1/capital/deposit/hisrec filtered by coin + txId.
The response is Binance's own deposit ledger, so a match means the funds
really arrived on *your* account.

Key facts (from the official Binance API docs):
  * signing = percent-encode the query string FIRST, then HMAC-SHA256 hex
    with the API secret (since 2026-01-15 encode-then-sign is mandatory);
  * auth header = X-MBX-APIKEY: <key>; only "Enable Reading" permission is
    needed — never enable withdrawals on this key, and IP-restrict it;
  * deposit status codes: 0 pending, 1 success, 2 rejected, 6 credited but
    withdrawal-locked, 7 wrong deposit, 8 awaiting user confirmation;
  * the endpoint costs 1 IP-weight — poll politely, not in a hot loop.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import Settings

logger = logging.getLogger("binance")

CREDITED_STATUSES = {1}          # funds credited — safe to top up balance
PENDING_STATUSES = {0, 6, 8}     # not final yet → ask the user to retry later
REJECTED_STATUSES = {2, 7}       # rejected / wrong deposit → never credit


class BinanceAPIError(Exception):
    """Binance returned an error or an unusable response."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


_CODE_HINTS = {
    -1021: "timestamp outside recvWindow — sync the server clock (NTP)",
    -1022: "signature rejected — check BINANCE_API_SECRET",
    -1100: "illegal characters in a parameter",
    -1121: "invalid coin",
    -1127: "lookup interval too large (max 90 days)",
}


def signed_query(params: dict[str, Any], secret: str) -> str:
    """Percent-encode params, then append the HMAC-SHA256 signature."""
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return f"{query}&signature={signature}"


def build_deposit_params(
    *,
    coin: str,
    tx_id: str,
    now_ms: int | None = None,
    days: int = 30,
    recv_window: int = 10000,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    days = max(1, min(days, 89))  # Binance allows at most a 90-day window
    return {
        "coin": coin,
        "txId": tx_id,
        "startTime": now_ms - days * 86_400_000,
        "endTime": now_ms,
        "recvWindow": recv_window,
        "timestamp": now_ms,
    }


def _error_message(resp: httpx.Response) -> str:
    code, msg = None, ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            code = body.get("code")
            msg = str(body.get("msg") or "")
    except ValueError:
        pass
    if resp.status_code == 429:
        return "Binance rate limit reached (429) — retry in a minute"
    if resp.status_code == 418:
        return "Binance rate-limit IP ban (418) — back off and check rate limits"
    if isinstance(code, int) and code in _CODE_HINTS:
        return f"{_CODE_HINTS[code]} ({code})"
    return msg or f"Binance error HTTP {resp.status_code}" + (f" ({code})" if code else "")


async def fetch_deposits(
    client: httpx.AsyncClient, settings: Settings, *, coin: str, tx_id: str
) -> list[dict[str, Any]]:
    """Return deposit records matching txId for coin (possibly empty)."""
    params = build_deposit_params(
        coin=coin, tx_id=tx_id, recv_window=settings.binance_recv_window
    )
    query = signed_query(params, settings.binance_api_secret)
    url = f"{settings.binance_base_url}/sapi/v1/capital/deposit/hisrec?{query}"
    try:
        resp = await client.get(
            url,
            headers={"X-MBX-APIKEY": settings.binance_api_key},
            timeout=settings.binance_timeout,
        )
    except httpx.HTTPError as exc:
        raise BinanceAPIError(f"network error: {type(exc).__name__}", retryable=True) from exc

    if resp.status_code != 200:
        message = _error_message(resp)
        retryable = resp.status_code in (429, 418) or resp.status_code >= 500
        logger.warning("Binance deposit lookup failed: %s", message)
        raise BinanceAPIError(message, retryable=retryable)

    try:
        payload = resp.json()
    except ValueError as exc:
        raise BinanceAPIError("Binance returned a non-JSON response", retryable=True) from exc
    if not isinstance(payload, list):
        raise BinanceAPIError("unexpected Binance response shape", retryable=True)
    return payload


def classify(records: list[dict[str, Any]] | None, tx_id: str) -> tuple[str, dict | None]:
    """Match txId (case-insensitive) and map Binance status → our state.

    Returns (state, record) with state in credited | pending | rejected | not_found.
    """
    target = (tx_id or "").strip().lower()
    match: dict | None = None
    for record in records or []:
        if str(record.get("txId") or "").strip().lower() == target:
            match = record
            break
    if match is None:
        return "not_found", None
    try:
        status = int(match.get("status"))
    except (TypeError, ValueError):
        return "pending", match  # unknown code → do not credit, do not reject
    if status in CREDITED_STATUSES:
        return "credited", match
    if status in REJECTED_STATUSES:
        return "rejected", match
    return "pending", match


def deposit_amount(record: dict[str, Any] | None) -> float | None:
    if not record:
        return None
    try:
        return float(record.get("amount"))
    except (TypeError, ValueError):
        return None