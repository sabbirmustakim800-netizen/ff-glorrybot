"""FastAPI app: the Telegram webhook and the user-facing screens.

The webhook runs the whole flow inline before answering Telegram. On Vercel's
Python runtime post-response work is not guaranteed (waitUntil/after are
Node/Edge only), so a BackgroundTask would be killed right after the 200 and
no reply would ever reach the user. Everything stays inside maxDuration.

Modules
-------
* this file           — webhook, routing, main menu, account, order history
* ``app/basic.py``    — plans from the FF Glory API, priced in tokens
* ``app/premium.py``  — manual premium orders with tokens held
* ``app/referral.py`` — invite links, token rewards and coupons
* ``app/commands.py`` — /bkash, /binance and the /ff* commands
* ``app/admin_panel.py``  — orders, payments, API settings, token economy
* ``app/state_machine.py`` — every wizard answer (text input)
* ``app/tokens.py``   — the token currency and its admin settings

Commands:
  /start /menu               main menu (inline keyboards + bottom keyboard)
  /help                      usage
  /balance                   current token balance
  /referral /coupons /redeem your invite link and token coupons
  /admin                     admin panel (admins only)
  /bkash <TrxID> <sender> [amount]        verify a bKash payment
  /binance <COIN> <txId> [amount]         verify a Binance deposit
  /ffbalance /ffregions /fflaunch /ffgroups /ffglory /ffcoupons /ffredeem /ffnotifs
"""

from __future__ import annotations

import hmac
import logging
import os
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import db
from . import keyboards as kb
from .config import get_settings
from .deposit_flows import (  # noqa: F401  (re-exported for routes/tests)
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
from .telegram import answer_callback, http_client, send_message
from .ui import (
    ADMIN_DENIED,
    Ctx,
    _display_name,
    _help_text,
    _mask,
    _short,
    attach_reply_keyboard,
    ensure_user_key,
    load_runtime,
    respond,
    token_amount,
    token_balance_of,
    token_line,
    user_api_enabled,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

app = FastAPI(title="FF Glory Bot", version="2.0.0")


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

REPLY_LABELS = {}

for _label, _action in kb.reply_map(True).items():
    REPLY_LABELS[_label.lower()] = _action
    # The emoji is decoration: also match the bare words ("basic services").
    REPLY_LABELS[re.sub(r"^[^0-9a-z]+", "", _label.lower()).strip()] = _action


def reply_action(text: str) -> str:
    """Which permanent-keyboard action a message text means, if any."""
    raw = (text or "").strip().lower()
    if not raw:
        return ""
    if raw in REPLY_LABELS:
        return REPLY_LABELS[raw]
    return REPLY_LABELS.get(re.sub(r"^[^0-9a-z]+", "", raw).strip(), "")


async def handle_reply_action(ctx: Ctx, action: str) -> None:
    """Route a bottom-keyboard tap to the same screen the inline menu opens."""
    if action == "basic" or action == "categories":
        await show_basic(ctx)
    elif action == "premium":
        await show_premium(ctx)
    elif action == "deposit":
        await show_deposit(ctx)
    elif action == "account":
        await show_account(ctx)
    elif action == "referral":
        await show_referral(ctx)
    elif action == "coupons":
        await show_coupons(ctx)
    elif action == "apis":
        await show_settings(ctx)
    elif action == "admin":
        if ctx.is_admin:
            await show_admin_menu(ctx)
        else:
            await send_message(ctx.chat_id, ADMIN_DENIED)
    else:  # "menu" / "cancel"
        await attach_reply_keyboard(ctx)
        await show_main(ctx)


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
        api, pricing, payment, features, token_cfg = await load_runtime(settings)
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
            token_cfg=token_cfg,
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

        action = reply_action(text)
        if action:
            await db.clear_state(user_id)
            await handle_reply_action(ctx, action)
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
        api, pricing, payment, features, token_cfg = await load_runtime(settings)
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
            token_cfg=token_cfg,
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
    elif data == "m:referral" or data == "rf:link":
        await show_referral(ctx)
    elif data in ("m:coupons", "rf:coupons"):
        await show_coupons(ctx)
    elif data.startswith("cp:"):
        await redeem_coupon_code(ctx, data[3:])
    elif data == "ac:history":
        await show_history(ctx)
    elif data == "ac:key":
        await account_key_action(ctx)
    elif data == "m:settings":
        await show_settings(ctx)
    elif data == "a:menu":
        await show_admin_menu(ctx)
    elif data == "a:tk":
        await admin_token_menu(ctx)
    elif data.startswith("a:tk:"):
        await admin_token_action(ctx, data[5:])
    elif data == "a:pending":
        await admin_pending(ctx)
    elif data == "a:pricing":
        await admin_token_menu(ctx)
    elif data.startswith("a:pr:"):
        await admin_token_action(ctx, data[5:])
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


async def show_main(ctx: Ctx) -> None:
    balance = await token_balance_of(ctx)
    text = (
        "FF Glory Bot\n\n"
        f"Balance: {token_line(balance)}\n\n"
        "Choose an option below, or use the buttons at the bottom of the chat."
    )
    await respond(ctx, text, kb.main_menu(ctx.is_admin))


async def cmd_start(ctx: Ctx) -> None:
    user, created = await db.get_or_create_user(ctx.user_id, ctx.name)
    payload = ctx.args[0] if ctx.args else ""
    joined_note = ""
    if created and payload:
        referrer = referral_code_from(payload)
        if referrer and referrer != ctx.user_id:
            exists = await db.get_user(referrer)
            if exists:
                before = int((await db.referral_stats(referrer)).get("invites") or 0)
                await award_referral(ctx, referrer, referred_id=ctx.user_id)
                after = int((await db.referral_stats(referrer)).get("invites") or 0)
                if after > before:
                    joined_note = "\n🎁 You joined through an invite link. Welcome!\n"
    balance = await db.get_tokens(ctx.user_id)
    greeting = "Welcome" if created else "Welcome back"
    text = (
        f"{greeting} {ctx.name}!\n\n"
        f"Balance: {token_line(balance)}\n"
        f"{joined_note}\n"
        "Choose an option below, or use the buttons at the bottom of the chat."
    )
    await attach_reply_keyboard(ctx)
    await send_message(ctx.chat_id, text, kb.main_menu(ctx.is_admin))


async def cmd_menu(ctx: Ctx) -> None:
    await show_main(ctx)


async def cmd_help(ctx: Ctx) -> None:
    await send_message(ctx.chat_id, _help_text(ctx.is_admin))


async def cmd_balance(ctx: Ctx) -> None:
    balance = await db.get_tokens(ctx.user_id)
    await send_message(
        ctx.chat_id,
        f"Your balance: {token_line(balance)}\n"
        f"Deposit rate: {ctx.token_cfg.rate_line()}",
        kb.back_to_main(ctx.is_admin),
    )


async def show_account(ctx: Ctx) -> None:
    user = await db.get_user(ctx.user_id)
    balance = float((user or {}).get("tokens") or 0.0)
    invites = int((user or {}).get("referrals") or 0)
    api_on = user_api_enabled(ctx)
    api_key = await ensure_user_key(ctx) if api_on else ""
    discount = int((user or {}).get("discount_percent") or 0)
    if not api_on:
        key_line = "API key: linking is turned off by the admin"
    else:
        key_line = f"API key: {'linked' if api_key else 'not linked'}"
    progress, goal = ctx.token_cfg.goal_progress(invites)
    text = (
        "My Account\n\n"
        f"Name: {ctx.name}\n"
        f"Telegram ID: {ctx.user_id}\n"
        f"Balance: {token_line(balance)}\n"
        f"Discount: {discount}%\n"
        f"Referrals: {invites} ({progress}/{goal} to the next coupon)\n"
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
            f"🪙 {token_amount(order.get('final_price') or 0)}  {order.get('status', '?')}"
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

# ------------------------------------------------------------------ sub-modules
#
# The handlers were split by concern to keep this file readable. Everything is
# re-exported here, so ``app.handlers.<name>`` keeps working (routes, tests).

from .admin_panel import (  # noqa: E402,F401
    admin_api_menu,
    admin_api_prompt,
    admin_broadcast_prompt,
    admin_console_call,
    admin_decide,
    admin_order_review,
    admin_payment,
    admin_payment_action,
    admin_payment_decide,
    admin_payment_queue,
    admin_payment_review,
    admin_pending,
    admin_proof_prompt,
    admin_stats,
    admin_token_action,
    admin_token_menu,
    admin_toggle_user_api,
    cmd_admin,
    show_admin_menu,
)
from .basic import (  # noqa: E402,F401
    plan_catalog,
    prompt_clan_id,
    show_basic,
    launch_basic,
)
from .commands import (  # noqa: E402,F401
    cmd_binance,
    cmd_bkash,
    cmd_coupons,
    cmd_ffbalance,
    cmd_ffcoupons,
    cmd_ffglory,
    cmd_ffgroups,
    cmd_fflaunch,
    cmd_ffnotifs,
    cmd_ffredeem,
    cmd_ffregions,
    cmd_redeem,
    cmd_referral,
)
from .premium import (  # noqa: E402,F401
    premium_confirm,
    premium_details,
    premium_start,
    show_premium,
)
from .referral import (  # noqa: E402,F401
    award_referral,
    redeem_coupon_code,
    referral_code_from,
    referral_link,
    show_coupons,
    show_referral,
)
from .state_machine import handle_media, handle_state_input  # noqa: E402,F401

COMMANDS = {
    "/start": cmd_start,
    "/menu": cmd_menu,
    "/help": cmd_help,
    "/balance": cmd_balance,
    "/referral": cmd_referral,
    "/coupons": cmd_coupons,
    "/redeem": cmd_redeem,
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
