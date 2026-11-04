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
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field, replace
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import httpx

from . import bkash as bkash_api
from . import binance as binance_api
from . import db
from . import deposits
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


from .deposit_flows import (
    _amount_buttons,
    _bkash_review,
    _crypto_create,
    _crypto_manual_txid,
    _resolve_bkash,
    bkash_quick_amount,
    bkash_start,
    bkash_submit,
    crypto_cancel,
    crypto_check,
    crypto_quick_amount,
    crypto_route,
    crypto_start,
    crypto_txid_prompt,
    show_bkash_deposit,
    show_crypto_deposit,
    show_deposit,
)
from .ui import (
    Ctx,
    _config_hint,
    _display_name,
    _error_ref,
    _help_text,
    _mask,
    _money,
    _parse_amount,
    _short,
    bkash_number,
    bkash_settings,
    binance_settings,
    ensure_user_key,
    glory_client,
    load_runtime,
    min_deposit,
    notify_admins,
    respond,
    user_api_enabled,
)


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
        api, pricing, payment, features = await load_runtime(settings)
        ctx = Ctx(
            chat_id=chat_id,
            user_id=user_id,
            name=_display_name(sender),
            args=[],
            settings=settings,
            client=http_client(),
            api=api,
            pricing=pricing,
            payment=payment,
            features=features,
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
        api, pricing, payment, features = await load_runtime(settings)
        ctx = Ctx(
            chat_id=chat_id,
            user_id=user_id,
            name=_display_name(sender),
            args=[],
            settings=settings,
            client=http_client(),
            api=api,
            pricing=pricing,
            payment=payment,
            features=features,
            is_admin=settings.is_admin(user_id),
            message_id=message.get("message_id"),
        )
        await route_callback(ctx, data)
    except Exception:
        logger.exception("callback failed: %s", data)
        await send_message(chat_id, "Something went wrong. Please try again.")


async def route_callback(ctx: Ctx, data: str) -> None:
    if data.startswith(("a:", "o:", "pay:")) and not ctx.is_admin:
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
    elif data == "d:b:start":
        await bkash_start(ctx)
    elif data == "d:b:go":
        await bkash_submit(ctx)
    elif data == "d:b:cancel":
        await db.clear_state(ctx.user_id)
        await show_deposit(ctx)
    elif data.startswith("d:b:amt:"):
        await bkash_quick_amount(ctx, data[8:])
    elif data == "d:crypto":
        await show_crypto_deposit(ctx)
    elif data.startswith("d:c:"):
        await crypto_route(ctx, data[4:])
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
    elif data == "a:pay":
        await admin_payment(ctx)
    elif data.startswith("a:pay:"):
        await admin_payment_action(ctx, data[6:])
    elif data == "a:api":
        await admin_api_menu(ctx)
    elif data == "a:api:userkeys":
        await admin_toggle_user_api(ctx)
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
    elif data.startswith("pay:v:"):
        await admin_payment_review(ctx, data[6:])
    elif data.startswith("pay:ok:"):
        await admin_payment_decide(ctx, data[7:], approve=True)
    elif data.startswith("pay:no:"):
        await admin_payment_decide(ctx, data[7:], approve=False)
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
            # Diagnostics stay in the log; users only see the default regions.
            logger.warning("live region list unavailable: %s", exc)
    text = (
        "Basic Services (Auto API)\n\n"
        f"Price per group launch: ৳{_money(price)}\n\n"
        "The API launches your group instantly and you are charged only after "
        "a successful launch."
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
            _config_hint(
                ctx,
                "Basic service is not configured: FFGLORY_API_KEY / "
                "FFGLORY_MASTER_KEY are missing. Set them in API Settings.",
            ),
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
        ref = _error_ref("launch", exc.status, exc.error)
        logger.warning("launch failed user=%s ref=%s: %s", ctx.user_id, ref, exc)
        await send_message(
            ctx.chat_id,
            "The launch could not be completed. You were not charged.\n"
            f"Reference: {ref}\nTry again in a minute or contact support.",
            kb.main_menu(ctx.is_admin),
        )
        return

    if resp.get("success") is False:
        ref = _error_ref("launch-rejected", resp.get("error"), resp.get("message"))
        logger.warning("launch rejected user=%s ref=%s payload=%s", ctx.user_id, ref, resp)
        await send_message(
            ctx.chat_id,
            "The provider rejected this launch. You were not charged.\n"
            f"Reference: {ref}\nCheck the clan ID and region, or contact support.",
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



# -------------------------------------------------------------------- account


async def show_account(ctx: Ctx) -> None:
    user = await db.get_user(ctx.user_id)
    balance = float((user or {}).get("balance") or 0.0)
    api_on = user_api_enabled(ctx)
    api_key = await ensure_user_key(ctx) if api_on else ""
    discount = int((user or {}).get("discount_percent") or 0)
    if not api_on:
        key_line = "API key: linking is turned off by the admin"
    else:
        key_line = f"API key: {'linked' if api_key else 'not linked'}"
    text = (
        "My Account\n\n"
        f"Name: {ctx.name}\n"
        f"Telegram ID: {ctx.user_id}\n"
        f"Balance: ৳{_money(balance)}\n"
        f"Discount: {discount}%\n"
        f"{key_line}"
    )
    await respond(ctx, text, kb.account_menu(bool(api_key), api_on))


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
    if not user_api_enabled(ctx):
        await respond(
            ctx,
            "Linking your own API key is currently turned off by the admin. "
            "Basic launches keep using the platform account.",
            kb.account_menu(False, False),
        )
        return
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


# --------------------------------------------------------------- payments admin


async def admin_payment(ctx: Ctx) -> None:
    bk = bkash_settings(ctx)
    bn = binance_settings(ctx)
    bkash_on = "on" if (bk.bkash_base_url and bk.bkash_api_key) else "off (manual review)"
    binance_on = (
        "on" if (bn.binance_api_key and bn.binance_api_secret) else "off (manual review)"
    )
    text = (
        "Payment settings\n\n"
        f"bKash receiving number: {bkash_number(ctx) or 'not set'}\n"
        f"Minimum deposit: ৳{min_deposit(ctx):g}\n"
        f"bKash auto-verification: {bkash_on}\n"
        f"Binance auto-verification: {binance_on}\n\n"
        "Users see the bKash number immediately after you save it — no redeploy."
    )
    await respond(ctx, text, kb.admin_payment_menu())


async def admin_payment_action(ctx: Ctx, action: str) -> None:
    if action == "bkash":
        await db.set_state(ctx.user_id, "admin_bkash_number", {})
        await respond(
            ctx,
            "Send the bKash number that should receive deposits (01XXXXXXXXX). "
            "It is saved instantly and shown to every user.",
            kb.cancel_only("a:pay"),
        )
        return
    if action == "min":
        await db.set_state(ctx.user_id, "admin_min_deposit", {})
        await respond(
            ctx,
            "Send the minimum deposit in BDT, for example 50.",
            kb.cancel_only("a:pay"),
        )
        return
    if action == "pending":
        await admin_payment_queue(ctx)
        return
    await admin_payment(ctx)


async def admin_payment_queue(ctx: Ctx) -> None:
    payments = await db.list_payments(status="needs_review", limit=10)
    if not payments:
        await respond(
            ctx, "No deposits are waiting for verification.", kb.admin_payment_menu()
        )
        return
    lines = [f"Deposits waiting for verification ({len(payments)}):", ""]
    rows: list[list[dict]] = []
    for payment in payments:
        meta = payment.get("meta") or {}
        details = payment.get("details") or {}
        kind = str(payment.get("kind") or "?")
        amount = details.get("amount_bdt") or meta.get("amount_bdt")
        when = payment.get("created_at")
        stamp = when.strftime("%m-%d %H:%M") if isinstance(when, datetime) else "-"
        short = str(payment.get("_id"))[-6:]
        if amount:
            shown = f"৳{_money(amount)}"
        else:
            shown = f"{meta.get('expected') or '?'} {meta.get('coin') or ''}".strip()
        lines.append(f"#{short}  {kind}  {shown}  {stamp}")
        rows.append([kb.button(f"Review #{short}", f"pay:v:{payment['_id']}")])
    rows.append([kb.button("Payment Settings", "a:pay")])
    await respond(ctx, "\n".join(lines), kb.keyboard(rows))


async def admin_payment_review(ctx: Ctx, pid: str) -> None:
    payment = await db.get_payment(pid)
    if not payment:
        await respond(ctx, "Payment not found.", kb.admin_payment_menu())
        return
    meta = payment.get("meta") or {}
    details = payment.get("details") or {}
    lines = [
        "Deposit review",
        "",
        f"Reference: #{pid[-6:]}",
        f"Kind: {payment.get('kind')}",
        f"Status: {payment.get('status')}",
        f"User: {payment.get('telegram_id')}",
    ]
    if meta.get("sender"):
        lines.append(f"Sender: {meta['sender']}")
    if payment.get("ref"):
        lines.append(f"Reference code: {payment['ref']}")
    txid = details.get("txid") or meta.get("txid")
    if txid:
        lines.append(f"Transaction: {txid}")
    if meta.get("expected"):
        lines.append(
            f"Expected: {meta.get('expected')} {meta.get('coin')} "
            f"({deposits.network_label(meta.get('network'))})"
        )
    amount_bdt = details.get("amount_bdt") or meta.get("amount_bdt")
    if amount_bdt:
        lines.append(f"Amount to credit: ৳{_money(amount_bdt)}")
    lines += ["", "Approve credits the balance once; decline closes the deposit."]
    await respond(ctx, "\n".join(lines), kb.payment_review(pid))


async def admin_payment_decide(ctx: Ctx, pid: str, *, approve: bool) -> None:
    payment = await db.get_payment(pid)
    if not payment:
        await respond(ctx, "Payment not found.", kb.admin_payment_menu())
        return
    telegram_id = int(payment.get("telegram_id") or 0)
    meta = payment.get("meta") or {}
    details = payment.get("details") or {}
    amount = details.get("amount_bdt") or meta.get("amount_bdt") or 0

    if not approve:
        await db.finish_payment(pid, "rejected", reason="admin_declined")
        if telegram_id:
            await send_message(
                telegram_id,
                "Your deposit could not be verified, so it was not credited. "
                "Contact support with your TrxID or transaction ID.",
                kb.main_menu(False),
            )
        await respond(
            ctx, f"Deposit #{pid[-6:]} declined.", kb.admin_payment_menu()
        )
        return

    if not amount or float(amount) <= 0:
        await respond(
            ctx,
            f"Deposit #{pid[-6:]} has no BDT amount recorded. Set BDT_RATES or "
            "credit it manually.",
            kb.admin_payment_menu(),
        )
        return
    if not await db.credit_payment(pid, telegram_id, float(amount)):
        await respond(ctx, "This deposit was already processed.", kb.admin_payment_menu())
        return
    balance = await db.get_balance(telegram_id)
    if telegram_id:
        await send_message(
            telegram_id,
            f"Deposit confirmed. ৳{_money(amount)} credited.\n\n"
            f"New balance: ৳{_money(balance)}",
            kb.main_menu(False),
        )
    await respond(
        ctx,
        f"Deposit #{pid[-6:]} credited: ৳{_money(amount)}.\n"
        f"User balance: ৳{_money(balance)}",
        kb.admin_payment_menu(),
    )


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
    user_on = user_api_enabled(ctx)
    lines += [
        "",
        f"User API keys (everyone): {'on' if user_on else 'off'}",
        "Tap a key to replace it from the panel. Values entered here override "
        "the environment without a redeploy.",
        "",
        "The toggle below decides whether users may link their own FF Glory "
        "key. When off, their key is not used and every launch runs on the "
        "platform account.",
    ]
    await respond(ctx, "\n".join(lines), kb.api_settings_menu(API_KEYS, user_on))


async def admin_toggle_user_api(ctx: Ctx) -> None:
    """Enable/disable the per-user API key feature for everyone."""
    current = user_api_enabled(ctx)
    try:
        doc = await db.set_feature("user_api_enabled", not current)
    except Exception:
        ref = _error_ref("user-api-toggle")
        logger.warning("could not save user api flag ref=%s", ref, exc_info=True)
        await respond(
            ctx,
            f"Could not save the setting right now. Reference: {ref}",
            kb.api_settings_menu(API_KEYS, current),
        )
        return
    ctx.features = dict(doc.get("features") or {})
    now_on = user_api_enabled(ctx)
    if now_on:
        text = (
            "User API keys are now on.\n\n"
            "Users can link their own FF Glory key from My Account and their "
            "basic launches will use it."
        )
    else:
        text = (
            "User API keys are now off.\n\n"
            "The link option is hidden for everyone, saved user keys are no "
            "longer used and basic launches run on the platform account."
        )
    await respond(ctx, text, kb.api_settings_menu(API_KEYS, now_on))


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

    if action == "bkash_sender":
        sender = bkash_api.normalize_bkash_number(text)
        if not sender:
            await send_message(
                ctx.chat_id,
                "That is not a valid bKash number. Send it like 01712345678.",
                kb.cancel_only("d:b:cancel"),
            )
            return
        await db.set_state(ctx.user_id, "bkash_trx", {"sender": sender})
        await send_message(
            ctx.chat_id,
            "bKash Deposit — step 2 of 3\n\n"
            "Send the TrxID from the bKash confirmation SMS, for example "
            "BFD90JRLST.",
            kb.cancel_only("d:b:cancel"),
        )
        return

    if action == "bkash_trx":
        trx = deposits.clean_trxid(text)
        if not trx:
            await send_message(
                ctx.chat_id,
                "That TrxID does not look right — it is 8-20 letters and numbers. "
                "Send it again.",
                kb.cancel_only("d:b:cancel"),
            )
            return
        data["trx"] = trx
        await db.set_state(ctx.user_id, "bkash_amount", data)
        await send_message(
            ctx.chat_id,
            "bKash Deposit — step 3 of 3\n\n"
            "Send the amount you paid in Taka (৳) only "
            f"(minimum ৳{min_deposit(ctx):g}).",
            _amount_buttons(ctx, "d:b:amt:"),
        )
        return

    if action == "bkash_amount":
        amount, error = deposits.parse_amount(text, minimum=min_deposit(ctx))
        if error:
            await send_message(
                ctx.chat_id, error, _amount_buttons(ctx, "d:b:amt:")
            )
            return
        await _bkash_review(
            ctx,
            str(data.get("sender") or ""),
            str(data.get("trx") or ""),
            amount,
        )
        return

    if action == "crypto_amount":
        net = deposits.normalize_network(str(data.get("network") or ""))
        amount, error = deposits.parse_amount(text, minimum=min_deposit(ctx))
        if error or not net:
            await send_message(
                ctx.chat_id,
                error or "Pick the network again.",
                _amount_buttons(ctx, f"d:c:amt:{net or 'trc20'}:"),
            )
            return
        await db.clear_state(ctx.user_id)
        await _crypto_create(ctx, str(data.get("coin") or "USDT"), net, amount)
        return

    if action == "crypto_txid":
        txid = deposits.clean_txid(text)
        if not txid:
            await send_message(
                ctx.chat_id,
                "That does not look like a transaction hash. Send the txId from "
                "your wallet or exchange.",
                kb.cancel_only(f"d:c:cancel:{data.get('pid') or ''}"),
            )
            return
        await _crypto_manual_txid(ctx, str(data.get("pid") or ""), txid)
        return

    if action == "user_key":
        if not user_api_enabled(ctx):
            await db.clear_state(ctx.user_id)
            await send_message(
                ctx.chat_id,
                "Linking your own API key is currently turned off by the admin.",
                kb.account_menu(False, False),
            )
            return
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

    if action == "admin_bkash_number":
        number = bkash_api.normalize_bkash_number(text)
        if not number:
            await send_message(
                ctx.chat_id,
                "That is not a valid bKash number. Send it like 01712345678.",
                kb.cancel_only("a:pay"),
            )
            return
        await db.set_payment_settings(bkash_number=number)
        ctx.payment = {**(ctx.payment or {}), "bkash_number": number}
        await db.clear_state(ctx.user_id)
        await send_message(
            ctx.chat_id,
            f"Saved. Users now see {number} for bKash deposits.",
            kb.admin_payment_menu(),
        )
        return

    if action == "admin_min_deposit":
        amount, error = deposits.parse_amount(text, minimum=1, maximum=deposits.MAX_DEPOSIT_BDT)
        if error:
            await send_message(ctx.chat_id, error, kb.cancel_only("a:pay"))
            return
        await db.set_payment_settings(min_deposit_bdt=amount)
        ctx.payment = {**(ctx.payment or {}), "min_deposit_bdt": amount}
        await db.clear_state(ctx.user_id)
        await send_message(
            ctx.chat_id,
            f"Saved. The minimum deposit is now ৳{amount:g}.",
            kb.admin_payment_menu(),
        )
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
            kb.api_settings_menu(API_KEYS, user_api_enabled(ctx)),
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


# -------------------------------------------------------------- deposit commands


async def cmd_bkash(ctx: Ctx) -> None:
    bk = bkash_settings(ctx)
    if not (bk.bkash_base_url and bk.bkash_api_key):
        await send_message(
            ctx.chat_id,
            _config_hint(
                ctx,
                "bKash verification is not configured: set BKASH_API_BASE_URL "
                "and BKASH_API_KEY in API Settings.",
            ),
        )
        return
    if len(ctx.args) < 2:
        await send_message(
            ctx.chat_id,
            "Usage: /bkash <TrxID> <sender_number> [amount]\n"
            "Example: /bkash BFD90JRLST 01712345678 500",
        )
        return
    trx = re.sub(r"[^A-Za-z0-9]", "", ctx.args[0]).upper()
    if not TRX_RE.match(trx):
        await send_message(ctx.chat_id, "Invalid TrxID. Example: /bkash BFD90JRLST 01712345678 500")
        return
    sender = bkash_api.normalize_bkash_number(ctx.args[1])
    if not sender:
        await send_message(
            ctx.chat_id,
            "Invalid sender number. Use 01XXXXXXXXX (or +8801XXXXXXXXX).",
        )
        return
    declared: float | None = None
    if len(ctx.args) >= 3:
        declared = _parse_amount(ctx.args[2])
        if declared is None:
            await send_message(ctx.chat_id, "Invalid amount. Example: /bkash BFD90JRLST 01712345678 500")
            return

    action, pid = await db.claim_payment(
        "bkash", trx, ctx.user_id, {"sender": sender, "amount": declared}
    )
    if action == "busy":
        await send_message(
            ctx.chat_id,
            "This TrxID is already being verified or has been used before. "
            "Each TrxID can only be used once.",
        )
        return

    await send_message(
        ctx.chat_id,
        "Verifying your bKash payment… this can take up to ~30 seconds.",
    )
    check = await bkash_api.verify_bkash_trxid(
        bk, ctx.client, trx_id=trx, sender=sender, amount=declared
    )
    await _resolve_bkash(ctx, pid, trx, declared, check)



async def cmd_binance(ctx: Ctx) -> None:
    bn = binance_settings(ctx)
    if not (bn.binance_api_key and bn.binance_api_secret):
        await send_message(
            ctx.chat_id,
            _config_hint(
                ctx,
                "Crypto verification is not configured: set BINANCE_API_KEY "
                "and BINANCE_API_SECRET in API Settings.",
            ),
        )
        return
    if len(ctx.args) < 2:
        await send_message(
            ctx.chat_id,
            "Usage: /binance <COIN> <txId> [amount]\n"
            "Example: /binance USDT 98A3EA560C6B3336D348B6C83F0F95ECE4F1F5919E94BD006E5BF3BF264FACFC 10",
        )
        return
    coin = ctx.args[0].strip().upper()
    if not COIN_RE.match(coin):
        await send_message(ctx.chat_id, "Invalid coin. Example: /binance USDT <txId> 10")
        return
    txid = ctx.args[1].strip()
    if not TXID_RE.match(txid):
        await send_message(ctx.chat_id, "Invalid transaction hash. Example: /binance USDT <txId> 10")
        return
    declared: float | None = None
    if len(ctx.args) >= 3:
        declared = _parse_amount(ctx.args[2])
        if declared is None:
            await send_message(ctx.chat_id, "Invalid amount. Example: /binance USDT <txId> 10")
            return

    action, pid = await db.claim_payment(
        "binance", txid.upper(), ctx.user_id, {"coin": coin, "amount": declared}
    )
    if action == "busy":
        await send_message(
            ctx.chat_id,
            "This transaction is already being verified or has been credited before.",
        )
        return

    await send_message(ctx.chat_id, "Checking Binance for your deposit…")
    try:
        records = await binance_api.fetch_deposits(
            ctx.client, bn, coin=coin, tx_id=txid
        )
    except binance_api.BinanceAPIError as exc:
        await db.finish_payment(
            pid,
            "rejected",
            reason="binance_error",
            details={"error": str(exc), "retryable": exc.retryable},
        )
        ref = _error_ref("binance-check", exc, exc.retryable)
        logger.warning("binance lookup failed user=%s ref=%s: %s", ctx.user_id, ref, exc)
        await send_message(
            ctx.chat_id,
            f"We could not check the blockchain right now. Reference: {ref}\n"
            "Please try again shortly.",
        )
        return

    state, record = binance_api.classify(records, txid)
    details: dict = {"state": state, "coin": coin, "record": record}

    if state == "credited":
        amount = binance_api.deposit_amount(record)
        rate = (ctx.api["rates"] or {}).get(coin)
        if amount is None or amount <= 0:
            await db.finish_payment(pid, "needs_review", reason="amount_missing", details=details)
            await send_message(
                ctx.chat_id,
                "Deposit found, but the amount could not be read. "
                "An admin will credit your account manually.",
            )
            return
        if rate is None:
            await db.finish_payment(pid, "needs_review", reason="no_rate", details=details)
            await send_message(
                ctx.chat_id,
                f"Binance deposit confirmed: {amount} {coin}\n"
                f"No BDT rate is configured for {coin}, so an admin will credit "
                "your account manually.",
            )
            return
        credit = round(amount * rate, 2)
        if not await db.credit_payment(pid, ctx.user_id, credit):
            await send_message(ctx.chat_id, "This payment has already been processed.")
            return
        balance = await db.get_balance(ctx.user_id)
        note = ""
        if declared is not None and abs(declared - amount) > 1e-8:
            note += f"\nYou expected {declared} {coin}; the deposit is {amount} {coin}."
        await send_message(
            ctx.chat_id,
            f"Binance deposit confirmed.\n"
            f"{amount} {coin} × {rate:g} = ৳{_money(credit)} credited.\n\n"
            f"New balance: ৳{_money(balance)}{note}",
            kb.main_menu(ctx.is_admin),
        )
        return

    await db.finish_payment(pid, "rejected", reason=state, details=details)
    if state == "pending":
        text = (
            "The deposit is still confirming on Binance. "
            "Try again in a few minutes."
        )
    elif state == "rejected":
        text = "Binance reports this deposit as rejected or invalid, so it cannot be credited."
    else:
        text = (
            f"No deposit with that transaction ID was found for {coin} yet. "
            "Deposits can take a few minutes — run /binance again shortly, and make "
            "sure the funds were sent to your Binance deposit address."
        )
    await send_message(ctx.chat_id, text)


# ----------------------------------------------------------------- ff commands


async def _ff_or_warn(ctx: Ctx) -> GloryAPI | None:
    await ensure_user_key(ctx)
    api = glory_client(ctx, platform=True)
    if not api.configured:
        await send_message(
            ctx.chat_id,
            _config_hint(
                ctx,
                "FFGlory API is not configured: set FFGLORY_API_KEY and "
                "FFGLORY_MASTER_KEY in API Settings.",
            ),
        )
        return None
    return api


async def _ff_failed(ctx: Ctx, what: str, exc: GloryAPIError) -> None:
    """Log the provider detail; show the user a clean message with a reference."""
    ref = _error_ref(what, exc.status, exc.error)
    logger.warning("%s failed user=%s ref=%s: %s", what, ctx.user_id, ref, exc)
    await send_message(
        ctx.chat_id,
        f"{what} could not be completed right now.\n"
        f"Reference: {ref}\nPlease try again shortly or contact support.",
    )


async def cmd_ffbalance(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    try:
        bal = await api.balance()
    except GloryAPIError as exc:
        await _ff_failed(ctx, "FFGlory balance check", exc)
        return
    await send_message(
        ctx.chat_id,
        f"FFGlory account: {bal.total:.0f} credits "
        f"({bal.basic:.0f} basic + {bal.premium:.0f} premium)",
    )


async def cmd_ffregions(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    try:
        pricing = await api.pricing()
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Region list", exc)
        return
    rows = pricing.get("regions") or []
    # Only user-facing fields — no raw provider payloads or internal pricing.
    lines = [
        f"  {row.get('region_name') or row.get('id')}"
        for row in rows
        if isinstance(row, dict) and (row.get("region_name") or row.get("id"))
    ]
    if not lines:
        await send_message(ctx.chat_id, "No regions are available right now.")
        return
    basic_price = ctx.pricing.final_price(ctx.pricing.basic_credit_bdt)
    await send_message(
        ctx.chat_id,
        "Available regions:\n"
        + "\n".join(lines)
        + f"\n\nPrice per launch: ৳{_money(basic_price)}",
    )


async def cmd_fflaunch(ctx: Ctx) -> None:
    """/fflaunch <region> <clan_id> — same flow as the Basic menu."""
    if len(ctx.args) < 2:
        await send_message(ctx.chat_id, "Usage: /fflaunch <region> <clan_id>")
        return
    region = ctx.args[0].strip().lower()
    clan_id = ctx.args[1].strip()
    if not CLAN_RE.match(clan_id):
        await send_message(ctx.chat_id, "clan_id must be 4-20 letters or numbers.")
        return
    await launch_basic(ctx, region, clan_id)


async def cmd_ffgroups(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    try:
        groups = await api.my_groups()
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Group list", exc)
        return
    if not groups:
        await send_message(ctx.chat_id, "No groups found yet.")
        return
    lines = [f"  {g.group_id[:8]}  {g.status:8s}  {g.region:4s}  {g.uptime}" for g in groups]
    await send_message(ctx.chat_id, "Your groups:\n" + "\n".join(lines))


async def cmd_ffglory(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    if not ctx.args:
        await send_message(ctx.chat_id, "Usage: /ffglory <group_id>")
        return
    group_id = ctx.args[0].strip()
    try:
        resp = await api.group_glory(group_id)
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Glory check", exc)
        return
    clanname = resp.get("clan_name") or resp.get("clan_id") or "-"
    note = ""
    if isinstance(resp.get("glory"), (int, float)):
        note = f"\nGlory: {float(resp['glory']):,.0f}"
    await send_message(ctx.chat_id, f"Group {group_id}: {clanname}{note}")


async def cmd_ffcoupons(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    try:
        coupons = await api.my_coupons()
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Coupon list", exc)
        return
    if not coupons:
        await send_message(ctx.chat_id, "No coupons created yet.")
        return
    lines = [
        f"  {c.code}  {c.status:8s}  basic={c.basic_credits}  premium={c.premium_credits}"
        for c in coupons
    ]
    await send_message(ctx.chat_id, "Your coupons:\n" + "\n".join(lines))


async def cmd_ffredeem(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    if not ctx.args:
        await send_message(ctx.chat_id, "Usage: /ffredeem <code>")
        return
    code = ctx.args[0].strip().upper().replace("-", "")
    try:
        resp = await api.redeem_coupon(code=code)
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Coupon redeem", exc)
        return
    # Whitelisted fields only, never the raw provider response.
    msg = "Coupon redeemed."
    basic = resp.get("basic_credits")
    premium = resp.get("premium_credits")
    if basic or premium:
        msg += f"\nBasic credits: {basic or 0}\nPremium credits: {premium or 0}"
    await send_message(ctx.chat_id, msg)


async def cmd_ffnotifs(ctx: Ctx) -> None:
    api = await _ff_or_warn(ctx)
    if api is None:
        return
    try:
        notifs = await api.notifications()
    except GloryAPIError as exc:
        await _ff_failed(ctx, "Notification check", exc)
        return
    if not notifs:
        await send_message(ctx.chat_id, "No pending notifications.")
        return
    lines = [
        f"  [{n.get('type', '?')}] {n.get('title')}: {n.get('message', '')}" for n in notifs
    ]
    await send_message(ctx.chat_id, "Notifications:\n" + "\n".join(lines))


COMMANDS = {
    "/start": cmd_start,
    "/menu": cmd_menu,
    "/help": cmd_help,
    "/balance": cmd_balance,
    "/admin": cmd_admin,
    "/bkash": cmd_bkash,
    "/binance": cmd_binance,
    "/ffbalance": cmd_ffbalance,
    "/ffregions": cmd_ffregions,
    "/fflaunch": cmd_fflaunch,
    "/ffgroups": cmd_ffgroups,
    "/ffglory": cmd_ffglory,
    "/ffcoupons": cmd_ffcoupons,
    "/ffredeem": cmd_ffredeem,
    "/ffnotifs": cmd_ffnotifs,
}
