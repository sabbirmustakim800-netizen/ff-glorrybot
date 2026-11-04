"""Binance deposit APIs using a read-only API key.

Two endpoints are used:

  * ``GET /sapi/v1/capital/deposit/address`` — the account's deposit address
    for a coin + network, used to show a real receiving address per session;
  * ``GET /sapi/v1/capital/deposit/hisrec`` — Binance's own deposit ledger,
    filtered by coin / txId / time window to confirm the funds arrived.

Key facts (from the official Binance API docs):
  * signing = percent-encode the query string FIRST, then HMAC-SHA256 hex
    with the API secret (since 2026-01-15 encode-then-sign is mandatory);
  * auth header = X-MBX-APIKEY: <key>; only "Enable Reading" permission is
    needed — never enable withdrawals on this key, and IP-restrict it;
  * deposit status codes: 0 pending, 1 success, 2 rejected, 6 credited but
    withdrawal-locked, 7 wrong deposit, 8 awaiting user confirmation;
  * the endpoints cost 1 IP-weight — poll politely, not in a hot loop.
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
    tx_id: str | None = None,
    now_ms: int | None = None,
    start_ms: int | None = None,
    days: int = 30,
    recv_window: int = 10000,
) -> dict[str, Any]:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    days = max(1, min(days, 89))  # Binance allows at most a 90-day window
    start = now_ms - days * 86_400_000
    if start_ms is not None:
        # Never ask for more than the allowed window, even for old sessions.
        start = max(int(start_ms), now_ms - 89 * 86_400_000)
    params: dict[str, Any] = {"coin": coin}
    if tx_id:
        params["txId"] = tx_id
    params.update(
        {
            "startTime": start,
            "endTime": now_ms,
            "recvWindow": recv_window,
            "timestamp": now_ms,
        }
    )
    return params


def build_address_params(
    *,
    coin: str,
    network: str,
    now_ms: int | None = None,
    recv_window: int = 10000,
) -> dict[str, Any]:
    """Params for /sapi/v1/capital/deposit/address (network = Binance code)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return {
        "coin": coin,
        "network": network,
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


async def _signed_get(
    client: httpx.AsyncClient,
    settings: Settings,
    path: str,
    params: dict[str, Any],
    *,
    what: str,
) -> Any:
    query = signed_query(params, settings.binance_api_secret)
    url = f"{settings.binance_base_url}{path}?{query}"
    try:
        resp = await client.get(
            url,
            headers={"X-MBX-APIKEY": settings.binance_api_key},
            timeout=settings.binance_timeout,
        )
    except httpx.HTTPError as exc:
        raise BinanceAPIError(
            f"network error during {what}: {type(exc).__name__}", retryable=True
        ) from exc

    if resp.status_code != 200:
        message = _error_message(resp)
        retryable = resp.status_code in (429, 418) or resp.status_code >= 500
        logger.warning("Binance %s failed: %s", what, message)
        raise BinanceAPIError(message, retryable=retryable)

    try:
        return resp.json()
    except ValueError as exc:
        raise BinanceAPIError(
            f"Binance returned a non-JSON response for {what}", retryable=True
        ) from exc


async def fetch_deposits(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    coin: str,
    tx_id: str | None = None,
    start_ms: int | None = None,
) -> list[dict[str, Any]]:
    """Return deposit records for coin (optionally filtered by txId/time)."""
    params = build_deposit_params(
        coin=coin,
        tx_id=tx_id,
        start_ms=start_ms,
        recv_window=settings.binance_recv_window,
    )
    payload = await _signed_get(
        client, settings, "/sapi/v1/capital/deposit/hisrec", params, what="deposit lookup"
    )
    if not isinstance(payload, list):
        raise BinanceAPIError("unexpected Binance response shape", retryable=True)
    return payload


async def fetch_deposit_address(
    client: httpx.AsyncClient,
    settings: Settings,
    *,
    coin: str,
    network: str,
) -> dict[str, str] | None:
    """Return {"address", "tag", "url"} for a coin+network, or None.

    None means "Binance has no address for this pair" (or the key cannot read
    it) — callers fall back to the wallet address configured in the panel.
    """
    params = build_address_params(
        coin=coin, network=network, recv_window=settings.binance_recv_window
    )
    try:
        payload = await _signed_get(
            client,
            settings,
            "/sapi/v1/capital/deposit/address",
            params,
            what="deposit address lookup",
        )
    except BinanceAPIError as exc:
        logger.info("deposit address unavailable (%s/%s): %s", coin, network, exc)
        return None
    if not isinstance(payload, dict) or not payload.get("address"):
        return None
    return {
        "address": str(payload.get("address") or ""),
        "tag": str(payload.get("tag") or ""),
        "url": str(payload.get("url") or ""),
    }


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
