"""bKash TrxID + sender-number verification.

Why an adapter? bKash's official merchant/checkout API is merchant-initiated:
it can only query payments *you* created (by paymentID), or search TrxIDs
belonging to your own merchant account. There is no official endpoint that
looks up an arbitrary "Send Money" TrxID a customer pastes into the bot, so
real-world bots verify against a TrxID-check service (SMS-relay style or a
reseller API). Providers differ in field names and response envelopes, so:

  * request field names / auth header / payload mode are configurable,
  * the parser below understands the common envelopes (success/valid booleans,
    status words, legacy trxStatus codes, bKash-style statusCode 0000).

Safety rules enforced here (never blind-trust a provider reply):
  1. "not found" is retried — SMS forwarding has a 5-20 s latency, so a miss
     usually just means "not synced yet";
  2. the sender number must match (exact or against a masked value like
     017****5678);
  3. the TrxID must be recent (BKASH_MAX_AGE_HOURS) so an old transaction
     cannot be replayed;
  4. anything ambiguous maps to a non-"verified" state — no auto-credit.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .config import Settings

logger = logging.getLogger("bkash")

BD_MOBILE_RE = re.compile(r"^01[3-9]\d{8}$")

# Legacy dreamwave-era trxStatus codes (still emitted by many wrappers).
_TRXSTATUS_MAP = {
    "0000": "verified",
    "0010": "pending",
    "0011": "pending",
    "0100": "failed",      # reversed
    "0111": "failed",      # failed
    "1001": "failed",      # invalid MSISDN
    "1002": "not_found",   # TrxID does not exist
    "1003": "error",       # bad credentials
    "1004": "not_found",   # TrxID not linked to this account
    "9999": "unavailable",  # system error
}

_STATUS_WORDS = {
    "verified": {
        "verified", "success", "succeeded", "completed", "complete",
        "confirmed", "ok", "unused", "valid", "trx_success", "payment_success",
    },
    "pending": {
        "pending", "processing", "in_progress", "in-progress", "waiting",
        "initiated", "not_processed",
    },
    "failed": {
        "failed", "failure", "reversed", "cancelled", "canceled", "rejected",
        "invalid", "error", "expired",
    },
    "not_found": {
        "not_found", "notfound", "not found", "no_data", "not-available",
        "does_not_exist",
    },
}


@dataclass
class BkashCheck:
    """Result of a verification attempt.

    States:
      verified    — TrxID confirmed (and sender/freshness checks passed)
      pending     — transaction exists but is not final yet → retry
      not_found   — no such TrxID (yet) → retry (SMS latency)
      failed      — definitive business failure (bad number, reversed, old…)
      error       — provider rejected our request/config → do not retry
      unavailable — network/5xx/rate-limit → retry
    """

    state: str
    message: str = ""
    amount: float | None = None
    sender: str | None = None
    sender_state: str = "unavailable"  # match | mismatch | unavailable
    timestamp: datetime | None = None
    http_status: int = 0
    attempts: int = 0
    raw: Any = None

    @property
    def retryable(self) -> bool:
        return self.state in ("pending", "not_found", "unavailable")


# --------------------------------------------------------------------- input helpers


def normalize_bkash_number(raw: str) -> str | None:
    """01712345678 / +8801712345678 / 8801712345678 → 01712345678, else None."""
    digits = re.sub(r"\D", "", raw or "")
    if digits.startswith("880"):
        digits = "0" + digits[3:]
    elif len(digits) == 10 and digits.startswith("1"):
        digits = "0" + digits
    return digits if BD_MOBILE_RE.match(digits) else None


def sender_check(claimed: str, actual: str | None) -> str:
    """Compare the user's number with the one the provider reports.

    Returns "match" | "mismatch" | "unavailable" (provider returned nothing).
    Handles masked values such as 017****5678.
    """
    if actual is None or not str(actual).strip():
        return "unavailable"
    claimed_n = normalize_bkash_number(claimed)
    if not claimed_n:
        return "mismatch"
    pattern = str(actual).strip()
    if pattern.startswith("+880"):
        pattern = "0" + pattern[4:]
    elif pattern.startswith("880"):
        pattern = "0" + pattern[3:]
    if pattern == claimed_n:
        return "match"
    if any(ch in pattern for ch in "*#xX"):
        parts: list[str] = []
        for ch in pattern:
            if ch.isdigit():
                parts.append(ch)
            elif ch in "*#xX":
                parts.append(r"\d")
            else:
                parts.append(re.escape(ch))
        return "match" if re.fullmatch("".join(parts), claimed_n) else "mismatch"
    if re.sub(r"\D", "", pattern) == claimed_n:
        return "match"
    return "mismatch"


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+0000"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_tx_time(value: Any) -> datetime | None:
    """Best-effort parse of a provider timestamp → naive UTC datetime.

    Supports epoch s/ms, ISO-8601 (incl. bKash's "... GMT+0600" suffix),
    DD/MM/YYYY HH:MM[:SS], YYYY-MM-DD HH:MM:SS. Returns None when unsure —
    a missing timestamp never blocks a verification, it only disables the
    freshness check.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        num = float(value)
    elif isinstance(value, str) and value.strip().isdigit():
        num = float(value.strip())
    else:
        num = None
    if num is not None:
        if num > 1e12:  # milliseconds
            num /= 1000.0
        if num < 1e9:  # not a plausible epoch
            return None
        try:
            return datetime.fromtimestamp(num, tz=timezone.utc).replace(tzinfo=None)
        except (OverflowError, OSError, ValueError):
            return None

    s = str(value).strip()
    m = re.match(r"^(.*?)\s*GMT\s*([+-]\d{2}:?\d{2})$", s, flags=re.IGNORECASE)
    if m:
        head, offset = m.groups()
        parsed = _iso(f"{head.strip()}{offset.replace(':', '')}")
        if parsed:
            return parsed
    for fmt in (
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %I:%M:%S %p",
        "%d-%m-%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d %b %Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    parsed = _iso(s)
    if parsed and parsed > _utcnow() + timedelta(hours=1):
        return None  # implausible future date → treat as unparseable
    return parsed


def enforce_freshness(check: BkashCheck, max_age_hours: float) -> BkashCheck:
    """Reject transactions older than max_age_hours (replay protection)."""
    if check.timestamp is None:
        return check
    age = _utcnow() - check.timestamp
    if age > timedelta(hours=max_age_hours):
        return replace(
            check,
            state="failed",
            message=(
                f"transaction is {age} old (limit {max_age_hours:.0f}h); "
                "old TrxIDs cannot be reused"
            ),
        )
    if age < timedelta(minutes=-5):
        return replace(check, state="failed", message="transaction timestamp is in the future")
    return check


# ---------------------------------------------------------------------- response parse


def _pick(payload: Any, keys: tuple[str, ...]) -> Any:
    """Find the first non-empty value among keys, searching common envelopes."""
    if not isinstance(payload, dict):
        return None
    containers: list[Any] = [
        payload,
        payload.get("data"),
        payload.get("transaction"),
        payload.get("result"),
        payload.get("payment"),
    ]
    for container in list(containers):
        if isinstance(container, dict):
            containers.append(container.get("data"))
            containers.append(container.get("transaction"))
    for container in containers:
        if isinstance(container, dict):
            for key in keys:
                value = container.get(key)
                if value not in (None, ""):
                    return value
    return None


def _state_from_message(message: str, default: str) -> str:
    msg = (message or "").lower()
    if any(w in msg for w in ("not found", "not exist", "no transaction", "no data", "didn't find")):
        return "not_found"
    if any(w in msg for w in ("already used", "already used", "duplicate", "consumed")):
        return "failed"
    if any(w in msg for w in ("mismatch", "does not match", "doesn't match")):
        return "failed"
    if any(w in msg for w in ("pending", "processing", "not yet")):
        return "pending"
    if any(w in msg for w in ("invalid key", "unauthorized", "forbidden", "credit", "quota", "plan")):
        return "error"
    if any(w in msg for w in ("success", "verified", "valid", "complete")):
        return "verified"
    return default


def _state_from_payload(payload: Any, message: str) -> str:
    if isinstance(payload, list):
        payload = payload[0] if payload else None
    if not isinstance(payload, dict):
        return _state_from_message(message, default="error")

    # 1. boolean envelopes: {"success": true} / {"valid": false}
    for key in ("success", "valid", "ok"):
        value = payload.get(key)
        if isinstance(value, bool):
            if value:
                return "verified"
            return _state_from_message(message, default="failed")

    # 2. legacy trxStatus codes
    code = payload.get("trxStatus", payload.get("trx_status"))
    if code is not None and str(code).strip() in _TRXSTATUS_MAP:
        return _TRXSTATUS_MAP[str(code).strip()]

    # 3. plain status word (or boolean-ish string)
    status = payload.get("status")
    if status is not None and not isinstance(status, (dict, list)):
        if isinstance(status, bool):
            return "verified" if status else _state_from_message(message, default="failed")
        word = str(status).strip().lower()
        for state_name, words in _STATUS_WORDS.items():
            if word in words:
                if state_name == "failed" and _state_from_message(message, "x") == "not_found":
                    return "not_found"
                return state_name
        if word.isdigit():
            if word in ("1", "200", "201", "2000"):
                return "verified"
            if word in ("0", "404"):
                return _state_from_message(message, default="pending")

    # 4. statusCode: bKash official "0000" = success
    status_code = payload.get("statusCode", payload.get("status_code"))
    if status_code is not None and str(status_code).strip() != "":
        sc = str(status_code).strip()
        if sc == "0000":
            return "verified"
        if sc in ("200", "201"):
            return "verified"
        return _state_from_message(message, default="failed")

    # 5. message heuristics as a last resort
    return _state_from_message(message, default="error")


def parse_bkash_response(http_status: int, payload: Any) -> BkashCheck:
    message = str(
        _pick(
            payload,
            ("message", "msg", "statusMessage", "status_message", "error", "detail", "reason"),
        )
        or ""
    )
    amount = _to_float(
        _pick(
            payload,
            (
                "amount", "payment_amount", "paidAmount", "paid_amount",
                "transaction_amount", "total_amount", "value",
            ),
        )
    )
    sender = _pick(
        payload,
        (
            "sender", "sender_number", "senderNumber", "senderPhone", "sender_phone",
            "payer_mobile_no", "payerMSISDN", "debitMSISDN", "customerMsisdn",
            "customer_msisdn", "mobile_number", "mobileNumber", "from",
        ),
    )
    timestamp = parse_tx_time(
        _pick(
            payload,
            (
                "timestamp", "time", "trxTimestamp", "trx_timestamp", "transaction_time",
                "payment_time", "dateTime", "date_time", "insertTime", "created_at",
                "datetime", "date",
            ),
        )
    )

    if http_status in (401, 403):
        state = "error"
        message = message or "provider rejected the API key (check BKASH_API_KEY)"
    elif http_status == 402:
        state = "error"
        message = message or "provider reports insufficient credits (402)"
    elif http_status == 429:
        state = "unavailable"
        message = message or "provider rate limit (429)"
    elif http_status >= 500:
        state = "unavailable"
        message = message or f"provider server error ({http_status})"
    elif http_status == 404:
        state = "not_found"
        message = message or "transaction not found"
    else:
        state = _state_from_payload(payload, message)
        if state == "error" and not message:
            message = "unrecognized provider response — check BKASH_* settings"

    return BkashCheck(
        state=state,
        message=message,
        amount=amount,
        sender=str(sender) if sender not in (None, "") else None,
        timestamp=timestamp,
        http_status=http_status,
        raw=payload,
    )


# ------------------------------------------------------------------------- requests


def build_request(
    settings: Settings, *, trx_id: str, sender: str, amount: float | None = None
) -> tuple[str, dict[str, str], dict[str, Any]]:
    base = settings.bkash_base_url.rstrip("/")
    path = settings.bkash_verify_path or "/verify"
    if not path.startswith("/"):
        path = "/" + path
    fields: dict[str, Any] = {
        settings.bkash_field_trx: trx_id,
        settings.bkash_field_sender: sender,
    }
    if amount is not None:
        fields[settings.bkash_field_amount] = amount
    headers = {"Accept": "application/json"}
    if settings.bkash_api_key:
        header = settings.bkash_auth_header or "Authorization"
        scheme = settings.bkash_auth_scheme
        headers[header] = f"{scheme} {settings.bkash_api_key}".strip()
    if settings.bkash_payload_mode == "form":
        return base + path, headers, {"data": {k: str(v) for k, v in fields.items()}}
    return base + path, headers, {"json": fields}


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


async def verify_bkash_trxid(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    trx_id: str,
    sender: str,
    amount: float | None = None,
) -> BkashCheck:
    """Verify a bKash TrxID + sender number, retrying transient misses.

    Applies the sender match and freshness rules before returning a
    "verified" result — callers only need to look at `state`.
    """
    url, headers, kwargs = build_request(settings, trx_id=trx_id, sender=sender, amount=amount)
    check = BkashCheck(state="unavailable", message="verification not attempted")

    for attempt in range(1, settings.bkash_attempts + 1):
        try:
            resp = await client.post(
                url, headers=headers, timeout=settings.bkash_timeout, **kwargs
            )
            payload = _safe_json(resp)
            check = parse_bkash_response(resp.status_code, payload)
            if check.state == "error":
                logger.warning(
                    "bKash provider error (HTTP %s): %s | payload=%.400s",
                    resp.status_code, check.message, payload,
                )
        except httpx.HTTPError as exc:
            logger.warning("bKash provider request failed: %s", exc)
            check = BkashCheck(state="unavailable", message=f"network error: {type(exc).__name__}")
        check.attempts = attempt
        if not check.retryable:
            break
        if attempt < settings.bkash_attempts and settings.bkash_retry_delay > 0:
            await asyncio.sleep(settings.bkash_retry_delay)

    if check.state == "verified":
        outcome = sender_check(sender, check.sender)
        check.sender_state = outcome
        if outcome == "mismatch":
            check = replace(
                check,
                state="failed",
                sender_state=outcome,
                message=f"sender number does not match (provider reports {check.sender})",
            )
        elif settings.bkash_max_age_hours > 0:
            check = enforce_freshness(check, settings.bkash_max_age_hours)
    return check