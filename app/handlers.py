"""FastAPI app: Telegram webhook, dual-tier ordering and the admin panel.

The webhook runs the whole flow inline before answering Telegram. On Vercel's
Python runtime post-response work is not guaranteed (waitUntil/after are
Node/Edge only), so a BackgroundTask would be killed right after the 200 and
no reply would ever reach the user. Everything stays inside maxDuration.

Architecture
------------
* **Basic tier** — automated ffglory.pro calls (`app/glory_api.py`) with the
  master key header + an account key. The price is checked against the user's
  balance, the API result is checked, and only a successful launch is charged.
* **Premium tier** — manual admin approval. The price is held (debited) when
  the order is placed, every admin gets a notification with inline
  approve/reject buttons, and the money is refunded on rejection.
* **Admin panel** — `/admin` only, authenticated from ``ADMIN_IDS``. Admins set
  markup and base prices, plug/replace API credentials, browse the FF Glory
  API, review orders, attach proof media and broadcast.

Commands:
  /start /menu               main menu (inline keyboards)
  /help                      usage
  /balance                   current balance
  /admin                     admin panel (admins only)
  /bkash <TrxID> <sender> [amount]        verify a bKash payment
  /binance <COIN> <txId> [amount]         verify a Binance deposit
  /ffbalance /ffregions /fflaunch /ffgroups /ffglory /ffcoupons /ffredeem /ffnotifs
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, replace

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import httpx

from . import bkash as bkash_api
from . import binance as binance_api
from . import db
from . import keyboards as kb
from .config import Pricing, Settings, get_settings, parse_rates
from .glory_api import GloryAPI, GloryAPIError, extract_group_id
from .telegram import (
    answer_callback,
    edit_message,
    http_client,
    send_media,
    send_message,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

app = FastAPI(title="FF Glory Bot", version="2.0.0")

TRX_RE = re.compile(r"^[A-Z0-9]{6,20}$")
TXID_RE = re.compile(r"^[A-Za-z0-9:_-]{6,150}$")
COIN_RE = re.compile(r"^[A-Z0-9]{2,12}$")
CLAN_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")

DEFAULT_REGIONS = [("me", "Middle East (me)"), ("bd", "Bangladesh (bd)")]

API_FIELDS = {
    "FFGLORY_API_KEY": "ffglory_api_key",
    "FFGLORY_MASTER_KEY": "ffglory_master_key",
    "FFGLORY_BASE_URL": "ffglory_base_url",
    "BKASH_API_BASE_URL": "bkash_base_url",
    "BKASH_API_KEY": "bkash_api_key",
    "BINANCE_API_KEY": "binance_api_key",
    "BINANCE_API_SECRET": "binance_api_secret",
    "BDT_RATES": "rates",
    "USDT_TRC20": "usdt_trc20",
    "USDT_BEP20": "usdt_bep20",
}
API_KEYS = list(API_FIELDS)

ADMIN_DENIED = "Access denied. This command and panel are for administrators only."


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
    is_admin: bool = False
    message_id: int | None = None
    user_api_key: str = ""
    key_loaded: bool = False


async def load_runtime(settings: Settings) -> tuple[dict, Pricing]:
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
    return api, Pricing.from_doc(doc, settings)


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


# --------------------------------------------------------------------- HTTP routes


@app.get("/")
async def health() -> dict:
    return {"ok": True, "service": "ff-glory-bot"}


@app.post("/")
async def webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    secret = settings.webhook_secret
    if secret:
        supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            logger.warning("webhook call rejected: bad secret token")
            return JSONResponse({"ok": False}, status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "error": "invalid payload"}, status_code=400)
    await process_update(data)
    return JSONResponse({"ok": True})


# ------------------------------------------------------------------ update router


def _media_of(message: dict) -> dict | None:
    if isinstance(message.get("photo"), list) and message["photo"]:
        return {"kind": "photo", "file_id": message["photo"][-1].get("file_id")}
    if isinstance(message.get("video"), dict):
        return {"kind": "video", "file_id": message["video"].get("file_id")}
    if isinstance(message.get("document"), dict):
        return {
            "kind": "document",
            "file_id": message["document"].get("file_id"),
            "name": message["document"].get("file_name"),
        }
    return None


async def process_update(data: dict) -> None:
    try:
        callback = data.get("callback_query")
        if isinstance(callback, dict):
            await handle_callback(callback)
            return
        message = data.get("message") or data.get("edited_message")
        if not isinstance(message, dict):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return

        settings = get_settings()
        api, pricing = await load_runtime(settings)
        ctx = Ctx(
            chat_id=chat_id,
            user_id=user_id,
            name=_display_name(sender),
            args=[],
            settings=settings,
            client=http_client(),
            api=api,
            pricing=pricing,
            is_admin=settings.is_admin(user_id),
            message_id=message.get("message_id"),
        )

        media = _media_of(message)
        if media:
            await handle_media(ctx, media)
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        if text.startswith("/"):
            parts = text.split()
            command = parts[0].split("@")[0].lower()
            ctx.args = parts[1:]
            if command in ("/start", "/menu"):
                await db.clear_state(user_id)
            handler = COMMANDS.get(command)
            if handler is None:
                await send_message(
                    chat_id, "Unknown command. Send /help to see what I can do."
                )
                return
            await handler(ctx)
            return

        state = await db.get_state(user_id)
        if state:
            await handle_state_input(ctx, state, text)
            return
        await send_message(
            chat_id, "Use the menu below to get started.", kb.main_menu(ctx.is_admin)
        )
    except Exception:
        logger.exception("failed to process update")


async def handle_callback(callback: dict) -> None:
    settings = get_settings()
    callback_id = str(callback.get("id") or "")
    data = str(callback.get("data") or "")
    sender = callback.get("from") or {}
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    user_id = sender.get("id")
    await answer_callback(callback_id)
    if not isinstance(chat_id, int) or not isinstance(user_id, int):
        return
    try:
        api, pricing = await load_runtime(settings)
        ctx = Ctx(
            chat_id=chat_id,
            user_id=user_id,
            name=_display_name(sender),
            args=[],
            settings=settings,
            client=http_client(),
            api=api,
            pricing=pricing,
            is_admin=settings.is_admin(user_id),
            message_id=message.get("message_id"),
        )
        await route_callback(ctx, data)
    except Exception:
        logger.exception("callback failed: %s", data)
        await send_message(chat_id, "Something went wrong. Please try again.")


async def route_callback(ctx: Ctx, data: str) -> None:
    if data.startswith(("a:", "o:")) and not ctx.is_admin:
        await send_message(ctx.chat_id, ADMIN_DENIED)
        return

    if data in ("m:main", "menu"):
        await show_main(ctx)
    elif data == "m:basic":
        await show_basic(ctx)
    elif data.startswith("b:r:"):
        await prompt_clan_id(ctx, data[4:])
    elif data == "m:premium":
        await show_premium(ctx)
    elif data == "p:start":
        await premium_start(ctx)
    elif data == "p:confirm":
        await premium_confirm(ctx)
    elif data == "p:cancel":
        await db.clear_state(ctx.user_id)
        await show_main(ctx)
    elif data == "m:deposit":
        await show_deposit(ctx)
    elif data == "d:bkash":
        await show_bkash_deposit(ctx)
    elif data == "d:bkash:how":
        await show_bkash_deposit(ctx)
    elif data == "d:crypto":
        await show_crypto_deposit(ctx)
    elif data == "m:account":
        await show_account(ctx)
    elif data == "ac:history":
        await show_history(ctx)
    elif data == "ac:key":
        await account_key_action(ctx)
    elif data == "m:settings":
        await show_settings(ctx)
    elif data == "a:menu":
        await show_admin_menu(ctx)
    elif data == "a:pending":
        await admin_pending(ctx)
    elif data == "a:pricing":
        await admin_pricing(ctx)
    elif data.startswith("a:pr:"):
        await admin_pricing_action(ctx, data[5:])
    elif data == "a:api":
        await admin_api_menu(ctx)
    elif data.startswith("a:api:set:"):
        await admin_api_prompt(ctx, data[10:])
    elif data == "a:console":
        await respond(ctx, "FF Glory API console — pick a call:", kb.console_menu())
    elif data.startswith("a:c:"):
        await admin_console_call(ctx, data[4:])
    elif data == "a:stats":
        await admin_stats(ctx)
    elif data == "a:broadcast":
        await admin_broadcast_prompt(ctx)
    elif data.startswith("o:v:"):
        await admin_order_review(ctx, data[4:])
    elif data.startswith("o:ok:"):
        await admin_decide(ctx, data[5:], approve=True)
    elif data.startswith("o:no:"):
        await admin_decide(ctx, data[5:], approve=False)
    elif data.startswith("o:pf:"):
        await admin_proof_prompt(ctx, data[5:])
    else:
        await show_main(ctx)


# --------------------------------------------------------------------- main menu


async def show_main(ctx: Ctx) -> None:
    balance = await db.get_balance(ctx.user_id)
    text = (
        f"FF Glory Bot\n\n"
        f"Balance: ৳{_money(balance)}\n\n"
        "Choose an option below."
    )
    await respond(ctx, text, kb.main_menu(ctx.is_admin))


async def cmd_start(ctx: Ctx) -> None:
    _, created = await db.get_or_create_user(ctx.user_id, ctx.name)
    balance = await db.get_balance(ctx.user_id)
    greeting = "Welcome" if created else "Welcome back"
    text = (
        f"{greeting} {ctx.name}!\n\n"
        f"Balance: ৳{_money(balance)}\n\n"
        "Choose an option below."
    )
    await send_message(ctx.chat_id, text, kb.main_menu(ctx.is_admin))


async def cmd_menu(ctx: Ctx) -> None:
    await show_main(ctx)


async def cmd_help(ctx: Ctx) -> None:
    await send_message(ctx.chat_id, _help_text(ctx.is_admin))


async def cmd_balance(ctx: Ctx) -> None:
    balance = await db.get_balance(ctx.user_id)
    await send_message(
        ctx.chat_id,
        f"Your balance: ৳{_money(balance)}",
        kb.back_to_main(ctx.is_admin),
    )


# ---------------------------------------------------------------- basic services


async def show_basic(ctx: Ctx) -> None:
    await ensure_user_key(ctx)
    api = glory_client(ctx)
    price = ctx.pricing.final_price(ctx.pricing.basic_credit_bdt)
    regions = list(DEFAULT_REGIONS)
    note = ""
    if api.configured:
        try:
            fetched = await api.regions()
            live = [(r.id, f"{r.name} ({r.id})") for r in fetched if r.enabled]
            if live:
                regions = live
        except GloryAPIError as exc:
            note = f"\n\nNote: live region list unavailable ({exc.status})."
    text = (
        "Basic Services (Auto API)\n\n"
        f"Price per group launch: ৳{_money(price)}\n"
        f"Base ৳{_money(ctx.pricing.basic_credit_bdt)} + markup "
        f"৳{_money(ctx.pricing.markup_amount(ctx.pricing.basic_credit_bdt))}\n\n"
        "The API launches your group instantly. You are charged only after a "
        "successful launch."
        f"{note}\n\nPick a region:"
    )
    await respond(ctx, text, kb.region_menu(regions))


async def prompt_clan_id(ctx: Ctx, region: str) -> None:
    region = (region or "me").strip().lower()
    price = ctx.pricing.final_price(ctx.pricing.basic_credit_bdt)
    balance = await db.get_balance(ctx.user_id)
    if balance < price:
        await respond(
            ctx,
            f"Insufficient balance.\n\nPrice: ৳{_money(price)}\n"
            f"Balance: ৳{_money(balance)}\n\nTop up to continue.",
            kb.insufficient_balance(),
        )
        return
    await db.set_state(ctx.user_id, "basic_clan", {"region": region})
    await respond(
        ctx,
        f"Region: {region}\nPrice: ৳{_money(price)}\n\n"
        "Send the clan ID for this glory launch:",
        kb.cancel_only("m:main"),
    )


async def launch_basic(ctx: Ctx, region: str, clan_id: str) -> None:
    await ensure_user_key(ctx)
    api = glory_client(ctx)
    if not api.configured:
        await send_message(
            ctx.chat_id,
            "Basic service is not configured yet: the FF Glory master key or "
            "account key is missing. Set them from the admin panel or contact support.",
        )
        return

    price = ctx.pricing.final_price(ctx.pricing.basic_credit_bdt)
    balance = await db.get_balance(ctx.user_id)
    if balance < price:
        await send_message(
            ctx.chat_id,
            f"Insufficient balance. Price: ৳{_money(price)}, balance: ৳{_money(balance)}.",
            kb.insufficient_balance(),
        )
        return

    await send_message(ctx.chat_id, "Launching your group, please wait...")
    try:
        resp = await api.launch_group(region=region, clan_id=clan_id)
    except GloryAPIError as exc:
        await send_message(
            ctx.chat_id,
            f"Launch failed ({exc.status}): {exc.error}\n"
            "You were not charged. Try again or contact support.",
            kb.main_menu(ctx.is_admin),
        )
        return

    if resp.get("success") is False:
        message = resp.get("error") or resp.get("message") or "the API rejected the request"
        await send_message(
            ctx.chat_id,
            f"Launch failed: {message}\nYou were not charged.",
            kb.main_menu(ctx.is_admin),
        )
        return

    group_id = extract_group_id(resp) or ""
    if not await db.hold_balance(ctx.user_id, price):
        logger.error(
            "balance changed during launch: user=%s price=%s", ctx.user_id, price
        )
        await send_message(
            ctx.chat_id,
            "The group was launched, but your balance could not be charged "
            "(it changed during checkout). Support will reconcile the order.",
        )
        await notify_admins(
            f"Charge failed after a successful launch.\n"
            f"User: {ctx.name} ({ctx.user_id})\nClan: {clan_id}\nRegion: {region}\n"
            f"Group: {group_id or 'n/a'}\nPrice: ৳{_money(price)}"
        )
        return

    order_id = await db.create_order(
        telegram_id=ctx.user_id,
        name=ctx.name,
        tier="basic",
        service="glory_group",
        base_price=ctx.pricing.basic_credit_bdt,
        final_price=price,
        status="completed",
        region=region,
        clan_id=clan_id,
        group_id=group_id,
    )
    new_balance = await db.get_balance(ctx.user_id)
    lines = [
        "Group launched successfully.",
        "",
        f"Clan: {clan_id}",
        f"Region: {region}",
        f"Group ID: {group_id or 'n/a'}",
        f"Charged: ৳{_money(price)}",
        f"Balance: ৳{_money(new_balance)}",
        f"Order: #{_short(order_id)}",
    ]
    await send_message(ctx.chat_id, "\n".join(lines), kb.main_menu(ctx.is_admin))


def _order_admin_text(order: dict) -> str:
    lines = [
        "Premium order — approval needed",
        "",
        f"Order: #{_short(order)}",
        f"User: {order.get('name') or '?'} (id {order.get('telegram_id')})",
        f"Price held: ৳{_money(order.get('final_price'))}",
        f"Base: ৳{_money(order.get('base_price'))}",
    ]
    if order.get("region"):
        lines.append(f"Region: {order['region']}")
    if order.get("clan_id"):
        lines.append(f"Clan: {order['clan_id']}")
    lines += ["", "Details from the user:", str(order.get("user_input") or "-")]
    return "\n".join(lines)


async def show_premium(ctx: Ctx) -> None:
    price = ctx.pricing.final_price(ctx.pricing.premium_credit_bdt)
    text = (
        "Premium / Secret Bulk\n\n"
        "For bulk orders, personal glory pushes and anything the automatic API "
        "cannot handle. An admin handles these manually.\n\n"
        f"Price: ৳{_money(price)} per order\n"
        "The amount is held when you order and refunded automatically if the "
        "admin rejects it.\n\n"
        "You send the details once; the admin team receives them and completes "
        "the order."
    )
    await respond(ctx, text, kb.premium_intro())


async def premium_start(ctx: Ctx) -> None:
    price = ctx.pricing.final_price(ctx.pricing.premium_credit_bdt)
    balance = await db.get_balance(ctx.user_id)
    if balance < price:
        await respond(
            ctx,
            f"Insufficient balance.\n\nPremium price: ৳{_money(price)}\n"
            f"Balance: ৳{_money(balance)}\n\nTop up to continue.",
            kb.insufficient_balance(),
        )
        return
    await db.set_state(ctx.user_id, "premium_info", {})
    await respond(
        ctx,
        "Send everything the admin needs in ONE message:\n\n"
        "- clan ID and region\n"
        "- quantity / duration\n"
        "- any extra instructions\n\n"
        "This message goes to the admins only and is never shown to other users.",
        kb.cancel_only("m:main"),
    )


async def premium_details(ctx: Ctx, text: str) -> None:
    if len(text.strip()) < 5:
        await send_message(
            ctx.chat_id, "Please send a bit more detail for the admin team."
        )
        return
    price = ctx.pricing.final_price(ctx.pricing.premium_credit_bdt)
    await db.set_state(ctx.user_id, "premium_confirm", {"details": text[:1500]})
    await respond(
        ctx,
        f"Order summary\n\n{text[:1500]}\n\n"
        f"Price: ৳{_money(price)}\n"
        "This amount is held now and refunded if the order is rejected.",
        kb.premium_confirm(),
    )


async def premium_confirm(ctx: Ctx) -> None:
    state = await db.get_state(ctx.user_id)
    if not state or state.get("action") != "premium_confirm":
        await respond(
            ctx,
            "This order session expired. Start a new premium order.",
            kb.back_to_main(ctx.is_admin),
        )
        return
    details = str((state.get("data") or {}).get("details") or "")
    price = ctx.pricing.final_price(ctx.pricing.premium_credit_bdt)
    if not await db.hold_balance(ctx.user_id, price):
        await db.clear_state(ctx.user_id)
        await respond(
            ctx,
            "Insufficient balance — the order was not placed.",
            kb.insufficient_balance(),
        )
        return
    order_id = await db.create_order(
        telegram_id=ctx.user_id,
        name=ctx.name,
        tier="premium",
        service="premium_bulk",
        base_price=ctx.pricing.premium_credit_bdt,
        final_price=price,
        status="pending",
        user_input=details,
    )
    await db.clear_state(ctx.user_id)
    balance = await db.get_balance(ctx.user_id)
    await respond(
        ctx,
        f"Premium order submitted.\n\nOrder: #{_short(order_id)}\n"
        f"Price held: ৳{_money(price)}\nBalance: ৳{_money(balance)}\n\n"
        "An admin will review it and you will be notified here.",
        kb.main_menu(ctx.is_admin),
    )
    order = await db.get_order(order_id)
    if order:
        await notify_admins(_order_admin_text(order), kb.order_actions(order_id))


# ------------------------------------------------------------------ deposits


async def show_deposit(ctx: Ctx) -> None:
    text = (
        "Deposit / Top-up\n\n"
        "bKash — send money, then verify the TrxID in the bot.\n"
        "Crypto — USDT TRC20/BEP20, verified automatically from Binance.\n\n"
        "Pick a method:"
    )
    await respond(ctx, text, kb.deposit_menu())


async def show_bkash_deposit(ctx: Ctx) -> None:
    support = f"\n\nSupport: {ctx.settings.support_contact}" if ctx.settings.support_contact else ""
    text = (
        "bKash deposit\n\n"
        "1. Send money (personal) to the bKash number you were given.\n"
        "2. Keep the TrxID from the confirmation SMS.\n"
        "3. Verify it here:\n\n"
        "/bkash <TrxID> <sender_number> [amount]\n"
        "Example: /bkash BFD90JRLST 01712345678 500\n\n"
        "Your balance is credited automatically after verification. Each TrxID "
        "can be used once."
        f"{support}"
    )
    await respond(ctx, text, kb.deposit_actions())


async def show_crypto_deposit(ctx: Ctx) -> None:
    trc20 = ctx.api["usdt_trc20"] or "ask support"
    bep20 = ctx.api["usdt_bep20"] or "ask support"
    text = (
        "Crypto deposit (USDT)\n\n"
        f"TRC20 (Tron): {trc20}\n"
        f"BEP20 (BNB Chain): {bep20}\n\n"
        "Send only USDT to these addresses. After sending, verify the "
        "transaction hash here:\n\n"
        "/binance <COIN> <txId> [amount]\n"
        "Example: /binance USDT 98A3EA560C6B3336... 10\n\n"
        "Your balance is credited after Binance confirms the deposit."
    )
    await respond(ctx, text, kb.deposit_actions())


# -------------------------------------------------------------------- account


async def show_account(ctx: Ctx) -> None:
    user = await db.get_user(ctx.user_id)
    balance = float((user or {}).get("balance") or 0.0)
    api_key = await ensure_user_key(ctx)
    discount = int((user or {}).get("discount_percent") or 0)
    text = (
        "My Account\n\n"
        f"Name: {ctx.name}\n"
        f"Telegram ID: {ctx.user_id}\n"
        f"Balance: ৳{_money(balance)}\n"
        f"Discount: {discount}%\n"
        f"API key: {'linked' if api_key else 'not linked'}"
    )
    await respond(ctx, text, kb.account_menu(bool(api_key)))


async def show_history(ctx: Ctx) -> None:
    orders = await db.user_orders(ctx.user_id, 10)
    if not orders:
        await respond(ctx, "No orders yet.", kb.account_menu(False))
        return
    lines = ["Your recent orders:", ""]
    for order in orders:
        created = order.get("created_at")
        when = created.strftime("%Y-%m-%d %H:%M") if created else "-"
        lines.append(
            f"#{_short(order)}  {when}  {order.get('tier', '?')}  "
            f"৳{_money(order.get('final_price'))}  {order.get('status', '?')}"
        )
    await respond(ctx, "\n".join(lines), kb.account_menu(False))


async def account_key_action(ctx: Ctx) -> None:
    key = await ensure_user_key(ctx)
    if key:
        await db.set_user_api_key(ctx.user_id, "")
        ctx.user_api_key = ""
        await respond(
            ctx,
            "Your FF Glory API key has been removed. Basic launches will use the "
            "platform account.",
            kb.account_menu(False),
        )
        return
    await db.set_state(ctx.user_id, "user_key", {})
    await respond(
        ctx,
        "Send your FF Glory API key in one message.\n\n"
        "It is stored against your account and used only for your own launches. "
        "Send /start to cancel.",
        kb.cancel_only("m:settings"),
    )


async def show_settings(ctx: Ctx) -> None:
    key = await ensure_user_key(ctx)
    status = f"linked ({_mask(key)})" if key else "not linked (using platform account)"
    text = (
        "Settings / API Key\n\n"
        f"FF Glory API key: {status}\n\n"
        "Link your own FF Glory account key to run basic launches on your own "
        "credits. Leave it unlinked to use the platform account."
    )
    await respond(ctx, text, kb.account_menu(bool(key)))


# ------------------------------------------------------------------ admin panel


async def cmd_admin(ctx: Ctx) -> None:
    if not ctx.is_admin:
        await send_message(ctx.chat_id, ADMIN_DENIED)
        return
    await show_admin_menu(ctx)


async def show_admin_menu(ctx: Ctx) -> None:
    text = (
        "Admin panel\n\n"
        f"Admins: {len(ctx.settings.admin_ids)}\n"
        f"Markup: {ctx.pricing.margin_percent:g}% + ৳{_money(ctx.pricing.fixed_markup_bdt)}\n"
        f"Basic price: ৳{_money(ctx.pricing.final_price(ctx.pricing.basic_credit_bdt))}\n"
        f"Premium price: ৳{_money(ctx.pricing.final_price(ctx.pricing.premium_credit_bdt))}"
    )
    await respond(ctx, text, kb.admin_menu())


async def admin_pending(ctx: Ctx) -> None:
    orders = await db.list_orders(status="pending", limit=10)
    if not orders:
        await respond(ctx, "No pending premium orders.", kb.admin_menu())
        return
    lines = [f"Pending premium orders ({len(orders)}):", ""]
    for order in orders:
        created = order.get("created_at")
        when = created.strftime("%m-%d %H:%M") if created else "-"
        lines.append(
            f"#{_short(order)}  {when}  {order.get('name') or '?'}  "
            f"৳{_money(order.get('final_price'))}"
        )
    rows = [
        [kb.button(f"Review #{_short(o)}", f"o:v:{o['_id']}")] for o in orders
    ]
    rows.append([kb.button("Admin Panel", "a:menu")])
    await respond(ctx, "\n".join(lines), kb.keyboard(rows))


async def admin_order_review(ctx: Ctx, order_id: str) -> None:
    order = await db.get_order(order_id)
    if not order:
        await respond(ctx, "Order not found.", kb.admin_menu())
        return
    text = _order_admin_text(order) + f"\n\nStatus: {order.get('status')}"
    markup = (
        kb.order_review(order_id)
        if order.get("status") == "pending"
        else kb.admin_menu()
    )
    await respond(ctx, text, markup)


async def admin_decide(ctx: Ctx, order_id: str, *, approve: bool) -> None:
    order = await db.get_order(order_id)
    if not order:
        await respond(ctx, "Order not found.", kb.admin_menu())
        return
    if order.get("status") != "pending":
        await respond(
            ctx,
            f"Order #{_short(order)} is already {order.get('status')}.",
            kb.admin_menu(),
        )
        return
    price = float(order.get("final_price") or 0.0)
    if approve:
        await db.update_order(
            order_id,
            status="completed",
            decided_by=ctx.user_id,
        )
        await send_message(
            order["telegram_id"],
            f"Premium order #{_short(order)} is approved and completed.\n"
            f"Charged: ৳{_money(price)}\n\nThank you!",
            kb.main_menu(False),
        )
        await respond(
            ctx,
            f"Order #{_short(order)} approved and completed. The user has been notified.",
            kb.admin_menu(),
        )
    else:
        new_balance = await db.add_balance(order["telegram_id"], price)
        await db.update_order(order_id, status="rejected", decided_by=ctx.user_id)
        await send_message(
            order["telegram_id"],
            f"Premium order #{_short(order)} was rejected.\n"
            f"Refunded: ৳{_money(price)}\nBalance: ৳{_money(new_balance)}\n\n"
            "Contact support if you have questions.",
            kb.main_menu(False),
        )
        await respond(
            ctx,
            f"Order #{_short(order)} rejected and ৳{_money(price)} refunded.",
            kb.admin_menu(),
        )


async def admin_proof_prompt(ctx: Ctx, order_id: str) -> None:
    order = await db.get_order(order_id)
    if not order:
        await respond(ctx, "Order not found.", kb.admin_menu())
        return
    await db.set_state(ctx.user_id, "admin_proof", {"order_id": order_id})
    await respond(
        ctx,
        f"Send the proof for order #{_short(order)} as a photo, video or file.\n"
        "It is forwarded to the user only.",
        kb.cancel_only("a:menu"),
    )


async def admin_pricing(ctx: Ctx) -> None:
    price = ctx.pricing
    text = (
        "Profit Margin / Rates\n\n"
        f"Margin: {price.margin_percent:g}%\n"
        f"Fixed markup: ৳{_money(price.fixed_markup_bdt)}\n"
        f"Basic base: ৳{_money(price.basic_credit_bdt)} → "
        f"৳{_money(price.final_price(price.basic_credit_bdt))}\n"
        f"Premium base: ৳{_money(price.premium_credit_bdt)} → "
        f"৳{_money(price.final_price(price.premium_credit_bdt))}\n\n"
        "final = base + base × margin/100 + fixed markup"
    )
    await respond(ctx, text, kb.admin_pricing_menu())


async def admin_pricing_action(ctx: Ctx, key: str) -> None:
    prompts = {
        "margin": ("admin_margin", "Send the new margin percentage (e.g. 25):"),
        "fixed": ("admin_fixed", "Send the fixed markup in BDT (e.g. 20):"),
        "basic": ("admin_base_basic", "Send the new basic base price in BDT (e.g. 120):"),
        "premium": ("admin_base_premium", "Send the new premium base price in BDT (e.g. 350):"),
    }
    if key == "reset":
        await db.update_bot_settings(
            "pricing",
            {
                "margin_percent": "",
                "fixed_markup_bdt": "",
                "basic_credit_bdt": "",
                "premium_credit_bdt": "",
            },
        )
        settings = get_settings()
        fresh = Pricing.from_doc({}, settings)
        await respond(
            ctx,
            f"Pricing reset to defaults: margin {fresh.margin_percent:g}%, "
            f"basic ৳{_money(fresh.basic_credit_bdt)}, "
            f"premium ৳{_money(fresh.premium_credit_bdt)}.",
            kb.admin_pricing_menu(),
        )
        return
    if key not in prompts:
        await admin_pricing(ctx)
        return
    action, question = prompts[key]
    await db.set_state(ctx.user_id, action, {})
    await respond(ctx, question, kb.cancel_only("a:pricing"))


async def admin_api_menu(ctx: Ctx) -> None:
    overrides = ctx.api.get("overrides") or {}
    lines = ["API Settings", ""]
    for env_key, field in API_FIELDS.items():
        value = ctx.api.get(field)
        if field == "rates":
            shown = ", ".join(f"{k}={v:g}" for k, v in (value or {}).items()) or "not set"
        else:
            shown = _mask(str(value or ""))
        source = "panel" if str(overrides.get(env_key) or "").strip() else "env"
        lines.append(f"{env_key}: {shown} [{source}]")
    lines += [
        "",
        "Tap a key to replace it from the panel. Values entered here override "
        "the environment without a redeploy.",
    ]
    await respond(ctx, "\n".join(lines), kb.api_settings_menu(API_KEYS))


async def admin_api_prompt(ctx: Ctx, key: str) -> None:
    key = key.strip().upper()
    if key not in API_FIELDS:
        await admin_api_menu(ctx)
        return
    hints = {
        "BDT_RATES": "USDT=110,BTC=6500000",
        "FFGLORY_BASE_URL": "https://ffglory.pro",
        "USDT_TRC20": "T... wallet address",
        "USDT_BEP20": "0x... wallet address",
    }
    hint = hints.get(key, "the new value")
    await db.set_state(ctx.user_id, "admin_api_value", {"key": key})
    await respond(
        ctx,
        f"Send the new value for {key} ({hint}).\n\n"
        "Send a single dash (-) to clear the override and fall back to the "
        "environment value.",
        kb.cancel_only("a:api"),
    )


async def admin_console_call(ctx: Ctx, name: str) -> None:
    await ensure_user_key(ctx)
    api = glory_client(ctx, platform=True)
    if not api.configured:
        await respond(
            ctx,
            "FF Glory is not configured. Set FFGLORY_API_KEY and "
            "FFGLORY_MASTER_KEY in API Settings first.",
            kb.console_menu(),
        )
        return
    try:
        if name == "me":
            payload = await api.me()
        elif name == "pricing":
            payload = await api.pricing()
        elif name == "groups":
            payload = [g.__dict__ for g in await api.my_groups()]
        elif name == "coupons":
            payload = [c.__dict__ for c in await api.my_coupons()]
        elif name == "redeemed":
            payload = await api.redeemed_coupons()
        elif name == "tx":
            payload = await api.transactions()
        elif name == "log":
            payload = await api.activity_log()
        elif name == "notifs":
            payload = await api.notifications()
        else:
            await respond(ctx, "Unknown console call.", kb.console_menu())
            return
    except GloryAPIError as exc:
        await respond(
            ctx,
            f"FF Glory error ({exc.status}): {exc.error}",
            kb.console_menu(),
        )
        return
    body = json.dumps(payload, indent=1, default=str)
    await respond(ctx, f"{name}:\n\n{body[:3500]}", kb.console_menu())


async def admin_stats(ctx: Ctx) -> None:
    stats = await db.system_stats()
    text = (
        "System stats\n\n"
        f"Users: {stats['users']}\n"
        f"Pending premium orders: {stats['pending_orders']}\n"
        f"Completed orders: {stats['completed_orders']}\n"
        f"Rejected orders: {stats['rejected_orders']}\n"
        f"Verified payments: {stats['verified_payments']}\n"
        f"Revenue (completed orders): ৳{_money(stats['revenue_bdt'])}\n"
        f"Deposits credited: ৳{_money(stats['credited_bdt'])}"
    )
    await respond(ctx, text, kb.admin_menu())


async def admin_broadcast_prompt(ctx: Ctx) -> None:
    await db.set_state(ctx.user_id, "admin_broadcast", {})
    await respond(
        ctx,
        "Send the broadcast message as one message. It goes to every user "
        "who started the bot. Send /admin to cancel.",
        kb.cancel_only("a:menu"),
    )


# --------------------------------------------------------------- state machine


async def handle_state_input(ctx: Ctx, state: dict, text: str) -> None:
    action = str(state.get("action") or "")
    data = state.get("data") or {}

    if action == "basic_clan":
        clan_id = re.sub(r"\s+", "", text)
        if not CLAN_RE.match(clan_id):
            await send_message(
                ctx.chat_id,
                "Clan ID must be 4-20 letters or numbers. Try again.",
            )
            return
        await db.clear_state(ctx.user_id)
        await launch_basic(ctx, str(data.get("region") or "me"), clan_id)
        return

    if action == "premium_info":
        await premium_details(ctx, text)
        return

    if action == "premium_confirm":
        await send_message(
            ctx.chat_id,
            "Use the Confirm Order or Cancel button above.",
            kb.premium_confirm(),
        )
        return

    if action == "user_key":
        key = text.strip()
        if not re.fullmatch(r"[A-Za-z0-9_\-]{10,120}", key):
            await send_message(
                ctx.chat_id, "That does not look like an API key. Send it again."
            )
            return
        await db.set_user_api_key(ctx.user_id, key)
        ctx.user_api_key = key
        ctx.key_loaded = True
        await db.clear_state(ctx.user_id)
        await send_message(
            ctx.chat_id,
            f"API key linked ({_mask(key)}). Basic launches will use your account.",
            kb.account_menu(True),
        )
        return

    if action.startswith("admin_") and not ctx.is_admin:
        await db.clear_state(ctx.user_id)
        await send_message(ctx.chat_id, ADMIN_DENIED)
        return

    if action in ("admin_margin", "admin_fixed", "admin_base_basic", "admin_base_premium"):
        value = _parse_amount(text)
        if value is None:
            await send_message(ctx.chat_id, "Send a positive number.")
            return
        field = {
            "admin_margin": "margin_percent",
            "admin_fixed": "fixed_markup_bdt",
            "admin_base_basic": "basic_credit_bdt",
            "admin_base_premium": "premium_credit_bdt",
        }[action]
        if field == "margin_percent" and value > 1000:
            await send_message(ctx.chat_id, "Margin must be 1000% or less.")
            return
        await db.set_pricing(**{field: value})
        await db.clear_state(ctx.user_id)
        settings = get_settings()
        doc = await db.get_bot_settings()
        fresh = Pricing.from_doc(doc, settings)
        await send_message(
            ctx.chat_id,
            f"Saved {field} = {value:g}.\n"
            f"Basic: ৳{_money(fresh.final_price(fresh.basic_credit_bdt))}, "
            f"Premium: ৳{_money(fresh.final_price(fresh.premium_credit_bdt))}.",
            kb.admin_pricing_menu(),
        )
        return

    if action == "admin_api_value":
        key = str(data.get("key") or "").upper()
        value = "" if text.strip() in ("-", "clear", "") else text.strip()
        if key == "BDT_RATES" and value and not parse_rates(value):
            await send_message(
                ctx.chat_id, "Send rates like USDT=110,BTC=6500000 or as JSON."
            )
            return
        await db.set_api_value(key, value)
        await db.clear_state(ctx.user_id)
        await send_message(
            ctx.chat_id,
            f"Saved {key} ({'cleared, using env' if not value else 'override active'}).",
            kb.api_settings_menu(API_KEYS),
        )
        return

    if action == "admin_broadcast":
        recipients = await db.all_user_ids()
        await db.clear_state(ctx.user_id)
        sent = 0
        failed = 0
        for chat_id in recipients:
            if await send_message(chat_id, text[:4096]):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
        await send_message(
            ctx.chat_id,
            f"Broadcast finished.\nSent: {sent}\nFailed: {failed}",
            kb.admin_menu(),
        )
        return

    if action == "admin_proof":
        await send_message(
            ctx.chat_id,
            "Send the proof as a photo, video or document, or press Cancel.",
            kb.cancel_only("a:menu"),
        )
        return

    await db.clear_state(ctx.user_id)
    await send_message(ctx.chat_id, "Session cleared.", kb.main_menu(ctx.is_admin))


async def handle_media(ctx: Ctx, media: dict) -> None:
    state = await db.get_state(ctx.user_id)
    if ctx.is_admin and state and state.get("action") == "admin_proof":
        order_id = str((state.get("data") or {}).get("order_id") or "")
        order = await db.get_order(order_id)
        if not order:
            await db.clear_state(ctx.user_id)
            await send_message(ctx.chat_id, "Order not found.", kb.admin_menu())
            return
        file_id = media.get("file_id")
        if not file_id:
            await send_message(ctx.chat_id, "Could not read that file. Try again.")
            return
        await db.update_order(
            order_id, proof={"kind": media["kind"], "file_id": file_id}
        )
        await db.clear_state(ctx.user_id)
        delivered = await send_media(
            int(order["telegram_id"]),
            media["kind"],
            file_id,
            caption=f"Proof for your order #{_short(order)}.",
        )
        await send_message(
            ctx.chat_id,
            "Proof sent to the user." if delivered else "Could not deliver the proof.",
            kb.admin_menu(),
        )
        return
    if state:
        await db.clear_state(ctx.user_id)
    await send_message(
        ctx.chat_id,
        "I only accept text here. Send /start to open the menu.",
        kb.main_menu(ctx.is_admin),
    )
