"""Shared Telegram UI primitives.

``Ctx`` carries the per-update context (chat, user, settings, API overrides and
pricing) and the helpers here are used by every handler module: text
formatting, user-facing error references, admin notifications and the settings
accessors that merge the admin panel overrides with the environment.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace

import httpx

from . import db
from . import deposits
from .config import Pricing, Settings, get_settings, parse_rates
from .glory_api import GloryAPI
from .telegram import edit_message, send_message

logger = logging.getLogger("bot")


# ------------------------------------------------------------------------ helpers


def _display_name(sender: dict) -> str:
    return str(sender.get("first_name") or sender.get("username") or "User")


def _parse_amount(raw: str) -> float | None:
    try:
        value = float(str(raw).replace(",", "").replace("৳", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _money(value: float) -> str:
    return f"{float(value or 0):.2f}"


def _short(order: dict | str) -> str:
    raw = order.get("_id") if isinstance(order, dict) else order
    return str(raw or "")[-6:]


def _mask(value: str) -> str:
    text = str(value or "")
    if not text:
        return "not set"
    if len(text) <= 8:
        return "set"
    return f"{text[:4]}...{text[-3:]}"


def _error_ref(*parts) -> str:
    """Short, non-revealing reference shown to users; the detail stays in logs."""
    digest = hashlib.sha1("|".join(str(part) for part in parts).encode()).hexdigest()
    return f"ERR-{digest[:6].upper()}"


def _config_hint(ctx: "Ctx", text: str) -> str:
    """Technical configuration hints are for admins only."""
    return text if ctx.is_admin else "This service is temporarily unavailable. Please contact support."


def _help_text(is_admin: bool) -> str:
    text = (
        "FF Glory Bot — commands\n\n"
        "/start — main menu\n"
        "/balance — show your current balance\n\n"
        "Deposits:\n"
        "/bkash <TrxID> <sender_number> [amount]\n"
        "  Example: /bkash BFD90JRLST 01712345678 500\n"
        "/binance <COIN> <txId> [amount]\n"
        "  Example: /binance USDT 98A3EA560C6B3336... 10\n\n"
        "FF Glory:\n"
        "/ffbalance — account credits\n"
        "/ffregions — regions & prices\n"
        "/fflaunch <region> <clan_id> — launch a glory group\n"
        "/ffgroups — your glory groups\n"
        "/ffglory <group_id> — current glory of a group\n"
        "/ffcoupons — your coupons\n"
        "/ffredeem <code> — redeem a coupon code\n"
        "/ffnotifs — pending notifications"
    )
    if is_admin:
        text += "\n\nAdmin:\n/admin — control panel"
    return text


@dataclass
class Ctx:
    chat_id: int
    user_id: int
    name: str
    args: list[str]
    settings: Settings
    client: httpx.AsyncClient
    api: dict
    pricing: Pricing
    payment: dict = field(default_factory=dict)
    is_admin: bool = False
    message_id: int | None = None
    user_api_key: str = ""
    key_loaded: bool = False


def bkash_number(ctx: Ctx) -> str:
    """Receiving bKash number: admin-panel value first, environment second."""
    stored = str((ctx.payment or {}).get("bkash_number") or "").strip()
    if stored:
        return stored
    return str(ctx.settings.bkash_receiver_number or "").strip()


def min_deposit(ctx: Ctx) -> float:
    stored = (ctx.payment or {}).get("min_deposit_bdt")
    try:
        value = float(stored) if stored not in (None, "") else float(ctx.settings.min_deposit_bdt)
    except (TypeError, ValueError):
        value = deposits.MIN_DEPOSIT_BDT
    return value if value > 0 else deposits.MIN_DEPOSIT_BDT


async def load_runtime(settings: Settings) -> tuple[dict, Pricing, dict]:
    """Merge admin panel overrides (Mongo) over environment settings."""
    doc: dict = {}
    try:
        doc = await db.get_bot_settings()
    except Exception:  # DB down must not crash a menu render
        logger.warning("bot settings unavailable; falling back to env", exc_info=True)
    overrides = doc.get("api") or {}

    def pick(name: str, env_value: str) -> str:
        value = str(overrides.get(name) or "").strip()
        return value or env_value

    rates = parse_rates(str(overrides.get("BDT_RATES") or "")) or dict(settings.bdt_rates)
    api = {
        "ffglory_api_key": pick("FFGLORY_API_KEY", settings.ffglory_api_key),
        "ffglory_master_key": pick("FFGLORY_MASTER_KEY", settings.ffglory_master_key),
        "ffglory_base_url": pick(
            "FFGLORY_BASE_URL", settings.ffglory_base_url or "https://ffglory.pro"
        ),
        "bkash_base_url": pick("BKASH_API_BASE_URL", settings.bkash_base_url),
        "bkash_api_key": pick("BKASH_API_KEY", settings.bkash_api_key),
        "binance_api_key": pick("BINANCE_API_KEY", settings.binance_api_key),
        "binance_api_secret": pick("BINANCE_API_SECRET", settings.binance_api_secret),
        "usdt_trc20": pick("USDT_TRC20", settings.usdt_trc20_address),
        "usdt_bep20": pick("USDT_BEP20", settings.usdt_bep20_address),
        "rates": rates,
        "overrides": overrides,
    }
    return api, Pricing.from_doc(doc, settings), dict(doc.get("payment") or {})


async def ensure_user_key(ctx: Ctx) -> str:
    if not ctx.key_loaded:
        try:
            ctx.user_api_key = await db.get_user_api_key(ctx.user_id)
        except Exception:
            logger.warning("could not load user api key", exc_info=True)
            ctx.user_api_key = ""
        ctx.key_loaded = True
    return ctx.user_api_key


def glory_client(ctx: Ctx, *, platform: bool = False) -> GloryAPI:
    """Client bound to the user's own key, or the platform key."""
    key = ctx.api["ffglory_api_key"]
    if not platform and ctx.user_api_key:
        key = ctx.user_api_key
    return GloryAPI(
        base_url=ctx.api["ffglory_base_url"],
        master_key=ctx.api["ffglory_master_key"],
        api_key=key,
    )


def bkash_settings(ctx: Ctx) -> Settings:
    return replace(
        ctx.settings,
        bkash_base_url=ctx.api["bkash_base_url"],
        bkash_api_key=ctx.api["bkash_api_key"],
    )


def binance_settings(ctx: Ctx) -> Settings:
    return replace(
        ctx.settings,
        binance_api_key=ctx.api["binance_api_key"],
        binance_api_secret=ctx.api["binance_api_secret"],
    )


async def respond(ctx: Ctx, text: str, markup: dict | None = None) -> None:
    """Edit the callback's message when possible, else send a new one."""
    if ctx.message_id:
        if await edit_message(ctx.chat_id, ctx.message_id, text, markup):
            return
    await send_message(ctx.chat_id, text, markup)


async def notify_admins(text: str, markup: dict | None = None) -> int:
    settings = get_settings()
    sent = 0
    for admin_id in settings.admin_ids:
        if await send_message(admin_id, text, markup):
            sent += 1
    return sent
