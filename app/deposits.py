"""Guided deposit rules: bKash form validation and crypto payment sessions.

Everything in this module is pure logic — no Telegram, no Mongo, no HTTP — so
the money-facing rules (amount bounds, TrxID shape, session matching) are unit
testable and shared by both the guided form and the legacy commands.

Crypto sessions
---------------
Networks such as TRC20 have no memo/tag field, so a plain "send to this address"
instruction cannot tell two users' transfers apart. Each session therefore gets
a **unique fractional reference amount** (500.00 → 500.37): the user sends
exactly that, and the incoming deposit is matched on coin + address + amount.
That keeps concurrent deposits unambiguous without asking users for anything.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone

MIN_DEPOSIT_BDT = 50.0
MAX_DEPOSIT_BDT = 200_000.0

# A bKash TrxID is 10 characters, but some providers expose 8-20.
TRX_RE = re.compile(r"^[A-Z0-9]{6,20}$")
COIN_RE = re.compile(r"^[A-Z0-9]{2,12}$")
TXID_RE = re.compile(r"^[A-Za-z0-9:_-]{6,150}$")

SESSION_MINUTES = 60
SESSION_CODE_LENGTH = 8
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I lookalikes

NETWORK_LABELS = {
    "trc20": "USDT TRC20 (Tron)",
    "bep20": "USDT BEP20 (BNB Smart Chain)",
}
# Binance's own network identifiers for the same chains.
BINANCE_NETWORKS = {"trc20": "TRX", "bep20": "BSC"}
_NETWORK_ALIASES = {
    "trc20": "trc20",
    "trx": "trc20",
    "tron": "trc20",
    "bep20": "bep20",
    "bsc": "bep20",
    "bep-20": "bep20",
    "bnb": "bep20",
}


def normalize_network(raw: str | None) -> str | None:
    """Map user/Binance spellings onto our two supported networks."""
    return _NETWORK_ALIASES.get(str(raw or "").strip().lower())


def network_label(network: str | None) -> str:
    return NETWORK_LABELS.get(normalize_network(network) or "", "Crypto")


def clean_trxid(raw: str | None) -> str | None:
    """Strip separators/case and return a valid TrxID, or None."""
    value = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    return value if TRX_RE.match(value) else None


def clean_txid(raw: str | None) -> str | None:
    value = str(raw or "").strip()
    return value if TXID_RE.match(value) else None


def parse_amount(
    raw: str | float | None,
    *,
    minimum: float = MIN_DEPOSIT_BDT,
    maximum: float = MAX_DEPOSIT_BDT,
) -> tuple[float | None, str | None]:
    """Return (amount, error_message). Never raises on user input.

    Deposits are always priced in Bangladeshi Taka, so dollar/crypto amounts are
    rejected instead of being silently converted.
    """
    text = str(raw if raw is not None else "").strip()
    lowered = text.lower()
    if any(mark in lowered for mark in ("$", "usd", "usdt", "dollar", "cent")):
        return None, "Enter the amount in Taka only (৳), for example 500."
    text = lowered.replace(",", "").replace("৳", "").replace("tk", "").replace("bdt", "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None, "Send the amount as a number, for example 500."
    if value <= 0:
        return None, "The amount must be greater than zero."
    if value < minimum:
        return None, f"The minimum deposit is ৳{minimum:g}."
    if value > maximum:
        return None, f"The maximum deposit is ৳{maximum:g}. Contact support for more."
    return round(value, 2), None


def reference_amount(base: float, *, fraction: int | None = None) -> float:
    """Add a unique 0.01-0.99 suffix so a memo-less deposit can be identified.

    ``fraction`` is injectable for tests; production uses a CSPRNG.
    """
    value = round(float(base), 2)
    part = fraction if fraction is not None else secrets.randbelow(99) + 1
    part = max(1, min(99, int(part)))
    return round(value + part / 100.0, 2)


def amount_matches(expected: float, actual: float | str, *, tolerance: float = 1e-6) -> bool:
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def address_equal(left: str | None, right: str | None, network: str | None) -> bool:
    """Tron addresses are base58 (case-sensitive); EVM hex is not."""
    a, b = str(left or "").strip(), str(right or "").strip()
    if not (a and b):
        return False
    network = normalize_network(network)
    return a == b if network == "trc20" else a.lower() == b.lower()


def session_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(SESSION_CODE_LENGTH))


def session_expiry(minutes: int = SESSION_MINUTES, *, now: datetime | None = None) -> datetime:
    start = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return start + timedelta(minutes=minutes)


def match_deposit(
    records: list[dict] | None,
    *,
    coin: str,
    network: str | None,
    address: str | None,
    expected: float,
    tag: str | None = None,
) -> tuple[str, dict | None]:
    """Find the deposit that belongs to this session.

    Returns (state, record) with state in credited | pending | rejected | not_found.
    An unknown/absent status never counts as credited.
    """
    best_state: str | None = None
    best_record: dict | None = None
    for record in records or []:
        if not isinstance(record, dict):
            continue
        record_coin = str(record.get("coin") or coin).strip().upper()
        if record_coin != str(coin or "").strip().upper():
            continue
        if not amount_matches(expected, record.get("amount")):
            continue
        record_address = record.get("address")
        if address and record_address and not address_equal(address, str(record_address), network):
            continue
        record_tag = record.get("addressTag")
        if tag and record_tag and str(record_tag).strip() != tag:
            continue
        try:
            status = int(record.get("status"))
        except (TypeError, ValueError):
            status = None
        if status == 1:
            return "credited", record
        if status in (2, 7):
            best_state, best_record = "rejected", record
            continue
        if best_state is None:
            best_state, best_record = "pending", record
    if best_state is None:
        return "not_found", None
    return best_state, best_record
