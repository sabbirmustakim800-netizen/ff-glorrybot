"""MongoDB (async, Motor) access layer.

Owns:
  * a lazily-created client + collection handles (works both under uvicorn on a
    VPS and inside a Vercel Python function, which does not guarantee lifespan
    startup events),
  * index creation (unique payment refs are the anti-double-credit guard),
  * user / payment state transitions that must be race-safe.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from .config import get_settings

logger = logging.getLogger("db")

# A payment stuck in "pending" (e.g. process died mid-verification) can be
# re-claimed by the user after this many minutes.
PENDING_TAKEOVER_MINUTES = 10

_client: AsyncIOMotorClient | None = None
_users: AsyncIOMotorCollection | None = None
_payments: AsyncIOMotorCollection | None = None
_ready = False
_lock = asyncio.Lock()


def utcnow() -> datetime:
    """Naive UTC datetime — matches how Motor/Pymongo round-trips datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def collections() -> tuple[AsyncIOMotorCollection, AsyncIOMotorCollection]:
    """Return (users, payments), connecting and ensuring indexes on first use."""
    global _client, _users, _payments, _ready
    if _ready:
        assert _users is not None and _payments is not None
        return _users, _payments
    async with _lock:
        if not _ready:
            settings = get_settings()
            if not settings.mongo_uri:
                raise RuntimeError("MONGO_URI is not set")
            _client = AsyncIOMotorClient(
                settings.mongo_uri,
                serverSelectionTimeoutMS=8000,
            )
            db = _client[settings.mongo_db]
            _users = db["users"]
            _payments = db["payments"]
            # Unique (kind, ref) — no two submissions of the same TrxID/txId.
            await _payments.create_index([("kind", 1), ("ref", 1)], unique=True)
            await _payments.create_index([("telegram_id", 1), ("created_at", -1)])
            await _users.create_index("telegram_id", unique=True)
            _ready = True
            logger.info("MongoDB connected, indexes ensured")
    assert _users is not None and _payments is not None
    return _users, _payments


# --------------------------------------------------------------------------- users


async def get_or_create_user(telegram_id: int, name: str) -> tuple[dict, bool]:
    """Return (user, created)."""
    users, _ = await collections()
    now = utcnow()
    existing = await users.find_one({"telegram_id": telegram_id})
    if existing:
        await users.update_one(
            {"_id": existing["_id"]}, {"$set": {"name": name, "last_seen": now}}
        )
        return existing, False
    doc = {
        "telegram_id": telegram_id,
        "name": name,
        "balance": 0.0,
        "discount_percent": 0,
        "created_at": now,
        "last_seen": now,
    }
    try:
        await users.insert_one(doc)
        return doc, True
    except DuplicateKeyError:  # concurrent /start race
        fresh = await users.find_one({"telegram_id": telegram_id})
        assert fresh is not None
        return fresh, False


async def get_balance(telegram_id: int) -> float:
    users, _ = await collections()
    user = await users.find_one({"telegram_id": telegram_id})
    if not user:
        return 0.0
    try:
        return float(user.get("balance", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------------ payments


def _object_id(pid: str) -> ObjectId:
    try:
        return ObjectId(pid)
    except (InvalidId, TypeError):
        raise ValueError(f"invalid payment id: {pid!r}")


async def claim_payment(
    kind: str, ref: str, telegram_id: int, meta: dict | None = None
) -> tuple[str, str | None]:
    """Register a new verification attempt.

    Returns (action, payment_id) where action is:
      * "claimed" — this call owns the verification and must finish it,
      * "busy"    — an identical payment is already verified / under way.
    Rejected payments (failed checks) and stale pendings may be re-claimed so
    transient upstream failures never lock a user out.
    """
    _, payments = await collections()
    now = utcnow()
    meta = meta or {}

    existing = await payments.find_one({"kind": kind, "ref": ref})
    if existing:
        status = existing.get("status")
        created = existing.get("created_at") or now
        fresh_pending = status == "pending" and (now - created) < timedelta(
            minutes=PENDING_TAKEOVER_MINUTES
        )
        if status in ("verified", "needs_review") or fresh_pending:
            return "busy", str(existing["_id"])
        # rejected (or stale pending) → re-verify from scratch
        result = await payments.update_one(
            {"_id": existing["_id"], "status": {"$ne": "verified"}},
            {
                "$set": {
                    "status": "pending",
                    "telegram_id": telegram_id,
                    "meta": meta,
                    "reason": None,
                    "details": None,
                    "updated_at": now,
                }
            },
        )
        if result.modified_count == 1:
            return "claimed", str(existing["_id"])
        return "busy", str(existing["_id"])

    doc = {
        "kind": kind,
        "ref": ref,
        "telegram_id": telegram_id,
        "status": "pending",
        "meta": meta,
        "reason": None,
        "details": None,
        "created_at": now,
        "updated_at": now,
    }
    try:
        result = await payments.insert_one(doc)
        return "claimed", str(result.inserted_id)
    except DuplicateKeyError:
        return "busy", None


async def credit_payment(pid: str, telegram_id: int, amount_bdt: float) -> bool:
    """Atomically move pending → verified and bump the user's balance once."""
    users, payments = await collections()
    now = utcnow()
    result = await payments.update_one(
        {"_id": _object_id(pid), "status": "pending"},
        {
            "$set": {
                "status": "verified",
                "credited_amount": float(amount_bdt),
                "verified_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count != 1:
        return False  # someone else already finished this payment
    await users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"balance": float(amount_bdt)}},
        upsert=True,
    )
    return True


async def finish_payment(
    pid: str,
    status: str,
    reason: str | None = None,
    details: dict | None = None,
) -> None:
    """Set a terminal status (rejected / needs_review) with diagnostics."""
    _, payments = await collections()
    now = utcnow()
    await payments.update_one(
        {"_id": _object_id(pid), "status": {"$in": ["pending", "needs_review"]}},
        {
            "$set": {
                "status": status,
                "reason": reason,
                "details": details,
                "updated_at": now,
            }
        },
    )