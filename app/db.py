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
_orders: AsyncIOMotorCollection | None = None
_settings: AsyncIOMotorCollection | None = None
_states: AsyncIOMotorCollection | None = None
_referrals: AsyncIOMotorCollection | None = None
_coupons: AsyncIOMotorCollection | None = None
_ready = False
_lock = asyncio.Lock()

SETTINGS_DOC_ID = "bot"
SETTINGS_SECTIONS = ("pricing", "api", "payment", "features", "tokens")


def utcnow() -> datetime:
    """Naive UTC datetime — matches how Motor/Pymongo round-trips datetimes."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def collections() -> tuple[AsyncIOMotorCollection, AsyncIOMotorCollection]:
    """Return (users, payments), connecting and ensuring indexes on first use."""
    global _client, _users, _payments, _orders, _settings, _states
    global _referrals, _coupons, _ready
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
            _orders = db["orders"]
            _settings = db["settings"]
            _states = db["states"]
            _referrals = db["referrals"]
            _coupons = db["coupons"]
            # Unique (kind, ref) — no two submissions of the same TrxID/txId.
            await _payments.create_index([("kind", 1), ("ref", 1)], unique=True)
            await _payments.create_index([("telegram_id", 1), ("created_at", -1)])
            await _users.create_index("telegram_id", unique=True)
            await _orders.create_index([("telegram_id", 1), ("created_at", -1)])
            await _orders.create_index([("status", 1), ("created_at", -1)])
            await _states.create_index("updated_at", expireAfterSeconds=86_400)
            # One reward per invited person, forever — the unique pair is the guard.
            await _referrals.create_index(
                [("referrer_id", 1), ("referred_id", 1)], unique=True
            )
            await _referrals.create_index([("referrer_id", 1), ("created_at", -1)])
            await _coupons.create_index("code", unique=True)
            await _coupons.create_index([("telegram_id", 1), ("created_at", -1)])
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
        # Tokens are the only currency a user sees; ``balance`` is legacy.
        "tokens": 0.0,
        "balance": 0.0,
        "discount_percent": 0,
        "referred_by": None,
        "referrals": 0,
        "referral_milestones": 0,
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


async def get_tokens(telegram_id: int) -> float:
    """The user's token balance — the only currency shown to users."""
    users, _ = await collections()
    user = await users.find_one({"telegram_id": telegram_id})
    if not user:
        return 0.0
    try:
        return float(user.get("tokens", user.get("balance", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# Legacy name kept for older call sites; it now means tokens.
get_balance = get_tokens


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


async def credit_payment(pid: str, telegram_id: int, tokens: float) -> bool:
    """Atomically move pending → verified and add the tokens once.

    The amount is stored as ``credited_tokens`` so the deposit ledger keeps the
    Taka the user actually paid (``details``) next to what it bought.
    """
    users, payments = await collections()
    now = utcnow()
    result = await payments.update_one(
        # needs_review payments are credited by an admin decision, so they are
        # allowed here too — the status guard still makes double-credit impossible.
        {"_id": _object_id(pid), "status": {"$in": ["pending", "needs_review"]}},
        {
            "$set": {
                "status": "verified",
                "credited_tokens": float(tokens),
                "verified_at": now,
                "updated_at": now,
            }
        },
    )
    if result.modified_count != 1:
        return False  # someone else already finished this payment
    await users.update_one(
        {"telegram_id": telegram_id},
        {"$inc": {"tokens": float(tokens)}},
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

# ------------------------------------------------------------------ bot settings

async def _settings_collection() -> AsyncIOMotorCollection:
    await collections()
    assert _settings is not None
    return _settings


async def _orders_collection() -> AsyncIOMotorCollection:
    await collections()
    assert _orders is not None
    return _orders


async def get_bot_settings() -> dict:
    """Return {"pricing": {...}, "api": {...}} overrides stored by admins."""
    coll = await _settings_collection()
    doc = await coll.find_one({"_id": SETTINGS_DOC_ID}) or {}
    return {section: dict(doc.get(section) or {}) for section in SETTINGS_SECTIONS}


async def update_bot_settings(section: str, values: dict) -> dict:
    """Set one or more keys inside a settings section (dot-path upsert)."""
    if section not in SETTINGS_SECTIONS:
        raise ValueError(f"unknown settings section: {section}")
    coll = await _settings_collection()
    patch = {f"{section}.{key}": value for key, value in values.items()}
    if patch:
        patch["updated_at"] = utcnow()
        await coll.update_one({"_id": SETTINGS_DOC_ID}, {"$set": patch}, upsert=True)
    return await get_bot_settings()


async def set_pricing(**values: float) -> dict:
    parsed: dict[str, float] = {}
    for key, value in values.items():
        try:
            parsed[key] = max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return await update_bot_settings("pricing", parsed)


async def set_api_value(key: str, value: str) -> dict:
    """Store (or clear, with an empty value) an API credential override."""
    return await update_bot_settings(
        "api", {key.strip().upper(): (value or "").strip()}
    )


async def set_payment_settings(**values) -> dict:
    """Persist deposit settings (e.g. the receiving bKash number)."""
    clean = {key: value for key, value in values.items() if value is not None}
    return await update_bot_settings("payment", clean)


async def set_feature(name: str, enabled: bool) -> dict:
    """Toggle a global feature flag controlled from the admin panel."""
    key = (name or "").strip()
    if not key:
        raise ValueError("feature name is required")
    return await update_bot_settings("features", {key: bool(enabled)})


async def set_token_settings(**values) -> dict:
    """Persist token economy values (rate, costs, referral rewards)."""
    clean = {key: value for key, value in values.items() if value is not None}
    if not clean:
        return await get_bot_settings()
    return await update_bot_settings("tokens", clean)


async def set_plan_cost(plan_id: str, tokens: int) -> dict:
    """Store the token cost of one API plan (read-modify-write the map)."""
    key = (plan_id or "").strip().lower()
    if not key:
        raise ValueError("plan id is required")
    doc = await get_bot_settings()
    costs = dict((doc.get("tokens") or {}).get("plan_costs") or {})
    costs[key] = max(0, int(tokens))
    return await update_bot_settings("tokens", {"plan_costs": costs})


async def reset_token_settings() -> dict:
    """Drop every stored token override so the env defaults apply again."""
    coll = await _settings_collection()
    await coll.update_one(
        {"_id": SETTINGS_DOC_ID},
        {"$unset": {"tokens": ""}, "$set": {"updated_at": utcnow()}},
    )
    return await get_bot_settings()


async def clear_plan_cost(plan_id: str) -> dict:
    key = (plan_id or "").strip().lower()
    doc = await get_bot_settings()
    costs = dict((doc.get("tokens") or {}).get("plan_costs") or {})
    costs.pop(key, None)
    return await update_bot_settings("tokens", {"plan_costs": costs})


# ---------------------------------------------------------------- payment lookup


async def get_payment(pid: str) -> dict | None:
    """Fetch one payment/session document by id."""
    _, payments = await collections()
    try:
        return await payments.find_one({"_id": _object_id(pid)})
    except ValueError:
        return None


async def list_payments(
    *, kind: str | None = None, status: str | None = None, limit: int = 10
) -> list[dict]:
    _, payments = await collections()
    query: dict = {}
    if kind:
        query["kind"] = kind
    if status:
        query["status"] = status
    cursor = payments.find(query).sort("created_at", -1).limit(max(1, limit))
    return [doc async for doc in cursor]


# ----------------------------------------------------------------------- orders

async def create_order(
    *,
    telegram_id: int,
    name: str,
    tier: str,
    service: str,
    base_price: float,
    final_price: float,
    status: str = "pending",
    region: str = "",
    clan_id: str = "",
    group_id: str = "",
    user_input: str = "",
    meta: dict | None = None,
) -> str:
    """Insert an order and return its id as a string."""
    orders = await _orders_collection()
    now = utcnow()
    doc = {
        "telegram_id": telegram_id,
        "name": name,
        "tier": tier,
        "service": service,
        "region": region,
        "clan_id": clan_id,
        "group_id": group_id,
        "base_price": float(base_price),
        "final_price": float(final_price),
        "status": status,
        "user_input": user_input,
        "meta": meta or {},
        "proof": None,
        "decided_by": None,
        "created_at": now,
        "updated_at": now,
    }
    result = await orders.insert_one(doc)
    return str(result.inserted_id)


async def get_order(order_id: str) -> dict | None:
    orders = await _orders_collection()
    try:
        return await orders.find_one({"_id": _object_id(order_id)})
    except ValueError:
        return None


async def update_order(order_id: str, **fields) -> bool:
    orders = await _orders_collection()
    fields["updated_at"] = utcnow()
    try:
        result = await orders.update_one({"_id": _object_id(order_id)}, {"$set": fields})
    except ValueError:
        return False
    return result.matched_count == 1


async def list_orders(status: str | None = None, limit: int = 10) -> list[dict]:
    orders = await _orders_collection()
    query = {"status": status} if status else {}
    cursor = orders.find(query).sort("created_at", -1).limit(max(1, limit))
    return [doc async for doc in cursor]


async def user_orders(telegram_id: int, limit: int = 10) -> list[dict]:
    orders = await _orders_collection()
    cursor = (
        orders.find({"telegram_id": telegram_id})
        .sort("created_at", -1)
        .limit(max(1, limit))
    )
    return [doc async for doc in cursor]


# ---------------------------------------------------------------------- tokens


async def add_tokens(telegram_id: int, amount: float) -> float:
    """Credit (or with a negative amount, debit) and return the new balance."""
    users, _ = await collections()
    result = await users.find_one_and_update(
        {"telegram_id": telegram_id},
        {"$inc": {"tokens": float(amount)}, "$set": {"last_seen": utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    return float((result or {}).get("tokens") or 0.0)


async def hold_tokens(telegram_id: int, amount: float) -> bool:
    """Atomically debit ``amount`` only if the tokens cover it (funds hold)."""
    users, _ = await collections()
    amount = float(amount)
    if amount <= 0:
        return True
    result = await users.find_one_and_update(
        {"telegram_id": telegram_id, "tokens": {"$gte": amount}},
        {"$inc": {"tokens": -amount}, "$set": {"last_seen": utcnow()}},
        return_document=ReturnDocument.AFTER,
    )
    return result is not None


# Legacy names: the balance was renamed to tokens, the semantics are identical.
add_balance = add_tokens
hold_balance = hold_tokens


# ------------------------------------------------------------------- referrals


async def _referrals_collection() -> AsyncIOMotorCollection:
    await collections()
    assert _referrals is not None
    return _referrals


async def _coupons_collection() -> AsyncIOMotorCollection:
    await collections()
    assert _coupons is not None
    return _coupons


async def referral_stats(telegram_id: int) -> dict:
    """Invite count and milestone progress for the referral screen."""
    users, _ = await collections()
    user = await users.find_one({"telegram_id": telegram_id}) or {}
    try:
        invites = int(user.get("referrals") or 0)
    except (TypeError, ValueError):
        invites = 0
    try:
        milestones = int(user.get("referral_milestones") or 0)
    except (TypeError, ValueError):
        milestones = 0
    return {
        "invites": max(0, invites),
        "milestones": max(0, milestones),
        "referred_by": user.get("referred_by"),
    }


async def register_referral(
    referrer_id: int,
    referred_id: int,
    *,
    reward: int,
    goal: int,
) -> dict:
    """Record an invite once and pay the referrer.

    Returns ``{"credited": bool, "invites": int, "milestones_due": int}``.
    The unique (referrer_id, referred_id) index makes a repeated ``/start``
    payload — or a second person forwarding the same link — unable to pay twice.
    """
    users, _ = await collections()
    now = utcnow()
    if referrer_id == referred_id or referrer_id <= 0 or referred_id <= 0:
        return {"credited": False, "invites": 0, "milestones_due": 0}
    try:
        await _referrals_collection().insert_one(
            {
                "referrer_id": int(referrer_id),
                "referred_id": int(referred_id),
                "reward": int(reward),
                "created_at": now,
            }
        )
    except DuplicateKeyError:
        existing = await users.find_one({"telegram_id": referrer_id}) or {}
        return {
            "credited": False,
            "invites": int(existing.get("referrals") or 0),
            "milestones_due": 0,
        }

    fresh = await users.find_one_and_update(
        {"telegram_id": int(referrer_id)},
        {
            "$inc": {"tokens": float(reward), "referrals": 1},
            "$set": {"last_seen": now},
        },
        upsert=True,
        return_document=ReturnDocument.AFTER,
    ) or {}
    await users.update_one(
        {"telegram_id": int(referred_id)}, {"$set": {"referred_by": int(referrer_id)}}
    )

    invites = int(fresh.get("referrals") or 0)
    awarded = int(fresh.get("referral_milestones") or 0)
    reached = invites // max(1, int(goal))
    due = max(0, reached - awarded)
    if due:
        await users.update_one(
            {"telegram_id": int(referrer_id)}, {"$set": {"referral_milestones": reached}}
        )
    return {"credited": True, "invites": invites, "milestones_due": due}


# --------------------------------------------------------------------- coupons


async def create_coupon(
    telegram_id: int,
    tokens: int,
    *,
    source: str = "referral",
    code: str = "",
) -> dict:
    """Issue a single-use token coupon owned by one user."""
    import secrets

    coll = await _coupons_collection()
    now = utcnow()
    for _ in range(5):
        candidate = (code or f"REF-{secrets.token_hex(3).upper()}").strip().upper()
        doc = {
            "code": candidate,
            "telegram_id": int(telegram_id),
            "tokens": max(0, int(tokens)),
            "source": source,
            "status": "active",
            "created_at": now,
            "redeemed_at": None,
        }
        try:
            await coll.insert_one(doc)
            return doc
        except DuplicateKeyError:
            code = ""  # regenerate another code
            continue
    raise RuntimeError("could not allocate a unique coupon code")


async def list_user_coupons(telegram_id: int, limit: int = 10) -> list[dict]:
    coll = await _coupons_collection()
    cursor = (
        coll.find({"telegram_id": int(telegram_id)})
        .sort("created_at", -1)
        .limit(max(1, int(limit)))
    )
    return [doc async for doc in cursor]


async def get_coupon(code: str) -> dict | None:
    coll = await _coupons_collection()
    return await coll.find_one({"code": (code or "").strip().upper()})


async def redeem_coupon(code: str, telegram_id: int) -> dict:
    """Mark a coupon redeemed and pay it out once.

    Returns ``{"ok": bool, "tokens": int, "reason": str}``.
    """
    coll = await _coupons_collection()
    normalized = (code or "").strip().upper()
    if not normalized:
        return {"ok": False, "tokens": 0, "reason": "empty"}
    coupon = await coll.find_one({"code": normalized})
    if not coupon:
        return {"ok": False, "tokens": 0, "reason": "unknown"}
    if int(coupon.get("telegram_id") or 0) != int(telegram_id):
        return {"ok": False, "tokens": 0, "reason": "not_yours"}
    now = utcnow()
    result = await coll.update_one(
        {"_id": coupon["_id"], "status": "active"},
        {"$set": {"status": "redeemed", "redeemed_at": now}},
    )
    if result.modified_count != 1:
        return {"ok": False, "tokens": 0, "reason": "used"}
    tokens = int(coupon.get("tokens") or 0)
    await add_tokens(telegram_id, tokens)
    return {"ok": True, "tokens": tokens, "reason": "redeemed"}


# ----------------------------------------------------------------- user api key

async def set_user_api_key(telegram_id: int, api_key: str) -> None:
    users, _ = await collections()
    await users.update_one(
        {"telegram_id": telegram_id},
        {"$set": {"api_key": (api_key or "").strip(), "last_seen": utcnow()}},
        upsert=True,
    )


async def get_user_api_key(telegram_id: int) -> str:
    users, _ = await collections()
    user = await users.find_one({"telegram_id": telegram_id})
    return str((user or {}).get("api_key") or "")


async def get_user(telegram_id: int) -> dict | None:
    users, _ = await collections()
    return await users.find_one({"telegram_id": telegram_id})


async def all_user_ids() -> list[int]:
    users, _ = await collections()
    ids: list[int] = []
    async for doc in users.find({}, {"telegram_id": 1}):
        value = doc.get("telegram_id")
        if isinstance(value, int):
            ids.append(value)
    return ids


# ------------------------------------------------------------------ wizard state

async def set_state(user_id: int, action: str, data: dict | None = None) -> None:
    states = await _states_collection()
    await states.update_one(
        {"_id": user_id},
        {"$set": {"action": action, "data": data or {}, "updated_at": utcnow()}},
        upsert=True,
    )


async def get_state(user_id: int) -> dict | None:
    states = await _states_collection()
    return await states.find_one({"_id": user_id})


async def clear_state(user_id: int) -> None:
    states = await _states_collection()
    await states.delete_one({"_id": user_id})


async def _states_collection() -> AsyncIOMotorCollection:
    await collections()
    assert _states is not None
    return _states


# ------------------------------------------------------------------------ stats

async def system_stats() -> dict:
    users, payments = await collections()
    orders = await _orders_collection()
    total_users = await users.count_documents({})
    pending = await orders.count_documents({"status": "pending"})
    completed = await orders.count_documents({"status": {"$in": ["completed", "approved"]}})
    rejected = await orders.count_documents({"status": "rejected"})
    verified = await payments.count_documents({"status": "verified"})
    revenue = 0.0
    async for doc in orders.find({"status": {"$in": ["completed", "approved"]}}):
        revenue += float(doc.get("final_price") or 0.0)
    credited_tokens = 0.0
    async for doc in payments.find({"status": "verified"}):
        credited_tokens += float(doc.get("credited_tokens") or 0.0)
    tokens_held = 0.0
    async for doc in users.find({}, {"tokens": 1}):
        tokens_held += float(doc.get("tokens") or 0.0)
    return {
        "users": total_users,
        "pending_orders": pending,
        "completed_orders": completed,
        "rejected_orders": rejected,
        "verified_payments": verified,
        "revenue_bdt": round(revenue, 2),
        "credited_tokens": round(credited_tokens, 2),
        "tokens_in_wallets": round(tokens_held, 2),
    }
