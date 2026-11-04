"""The admin panel: orders, payments, API settings and the token economy."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from . import db
from . import deposits
from . import keyboards as kb
from .basic import plan_catalog
from .glory_api import GloryAPIError
from .premium import _order_admin_text
from .telegram import send_message
from .tokens import TokenConfig
from .ui import (
    ADMIN_DENIED,
    Ctx,
    _error_ref,
    _mask,
    _money,
    _short,
    binance_settings,
    bkash_number,
    bkash_settings,
    ensure_user_key,
    get_settings,
    glory_client,
    min_deposit,
    respond,
    token_line,
    user_api_enabled,
)

logger = logging.getLogger("bot")

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


def _parse_whole(raw: str) -> int | None:
    """Parse a whole number of tokens (``0`` is valid)."""
    text = str(raw or "").replace(",", "").replace("🪙", "").strip()
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


async def cmd_admin(ctx: Ctx) -> None:
    if not ctx.is_admin:
        await send_message(ctx.chat_id, ADMIN_DENIED)
        return
    await show_admin_menu(ctx)


async def show_admin_menu(ctx: Ctx) -> None:
    cfg = ctx.token_cfg
    text = (
        "Admin panel\n\n"
        f"Admins: {len(ctx.settings.admin_ids)}\n"
        f"Token rate: {cfg.rate_line()}\n"
        f"Basic plan cost: {token_line(cfg.basic_cost)}\n"
        f"Premium order cost: {token_line(cfg.premium_cost)}\n"
        f"Referral: {token_line(cfg.referral_reward)} per invite, "
        f"goal {cfg.referral_goal} → {token_line(cfg.referral_coupon_reward)} coupon"
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
            f"{token_line(order.get('final_price') or 0)}"
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
            f"Charged: {token_line(price)}\n\nThank you!",
            kb.main_menu(False),
        )
        await respond(
            ctx,
            f"Order #{_short(order)} approved and completed. The user has been notified.",
            kb.admin_menu(),
        )
    else:
        new_balance = await db.add_tokens(order["telegram_id"], price)
        await db.update_order(order_id, status="rejected", decided_by=ctx.user_id)
        await send_message(
            order["telegram_id"],
            f"Premium order #{_short(order)} was rejected.\n"
            f"Refunded: {token_line(price)}\n"
            f"Balance: {token_line(new_balance)}\n\n"
            "Contact support if you have questions.",
            kb.main_menu(False),
        )
        await respond(
            ctx,
            f"Order #{_short(order)} rejected and {token_line(price)} refunded.",
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
    tokens = meta.get("tokens") or ctx.token_cfg.tokens_for_bdt(amount_bdt or 0)
    if amount_bdt:
        lines.append(f"Paid: ৳{_money(amount_bdt)} → {token_line(tokens)}")
    elif tokens:
        lines.append(f"Tokens to credit: {token_line(tokens)}")
    lines += ["", "Approve credits the tokens once; decline closes the deposit."]
    await respond(ctx, "\n".join(lines), kb.payment_review(pid))


async def admin_payment_decide(ctx: Ctx, pid: str, *, approve: bool) -> None:
    payment = await db.get_payment(pid)
    if not payment:
        await respond(ctx, "Payment not found.", kb.admin_payment_menu())
        return
    telegram_id = int(payment.get("telegram_id") or 0)
    meta = payment.get("meta") or {}
    details = payment.get("details") or {}
    amount_bdt = details.get("amount_bdt") or meta.get("amount_bdt") or 0
    tokens = int(meta.get("tokens") or ctx.token_cfg.tokens_for_bdt(amount_bdt or 0))

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

    if tokens <= 0:
        await respond(
            ctx,
            f"Deposit #{pid[-6:]} has no Taka amount recorded, so no tokens can be "
            "worked out. Set the deposit amount or the token rate first.",
            kb.admin_payment_menu(),
        )
        return
    if not await db.credit_payment(pid, telegram_id, tokens):
        await respond(ctx, "This deposit was already processed.", kb.admin_payment_menu())
        return
    balance = await db.get_tokens(telegram_id)
    if telegram_id:
        await send_message(
            telegram_id,
            f"Deposit confirmed. {token_line(tokens)} credited.\n\n"
            f"New balance: {token_line(balance)}",
            kb.main_menu(False),
        )
    await respond(
        ctx,
        f"Deposit #{pid[-6:]} credited: {token_line(tokens)}\n"
        f"User balance: {token_line(balance)}",
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
        f"Tokens spent (completed orders): {token_line(stats['revenue_bdt'])}\n"
        f"Tokens sold via deposits: {token_line(stats.get('credited_tokens', 0))}\n"
        f"Tokens in wallets: {token_line(stats.get('tokens_in_wallets', 0))}"
    )
    await respond(ctx, text, kb.admin_menu())


async def admin_token_menu(ctx: Ctx) -> None:
    cfg = ctx.token_cfg
    plans = await plan_catalog(ctx)
    overrides = len(cfg.plan_costs)
    text = (
        "🪙 Token Settings\n\n"
        f"Conversion: {cfg.rate_line()}\n"
        f"Basic plan cost: {token_line(cfg.basic_cost)}\n"
        f"Premium order cost: {token_line(cfg.premium_cost)}\n"
        f"Per-plan overrides: {overrides} of {len(plans)} plans\n\n"
        f"Referral reward: {token_line(cfg.referral_reward)} per invite\n"
        f"Referral goal: {cfg.referral_goal} invites → "
        f"{token_line(cfg.referral_coupon_reward)} coupon\n\n"
        "Users only ever see tokens; Taka appears on the deposit form alone."
    )
    await respond(ctx, text, kb.token_menu())


TOKEN_PROMPTS = {
    "rate": (
        "taka_per_token",
        "Send how many Taka buy one token (e.g. 1.5).",
    ),
    "basic": ("basic_cost", "Send the default token cost of a basic plan (e.g. 100)."),
    "premium": ("premium_cost", "Send the token cost of a premium order (e.g. 300)."),
    "ref": ("referral_reward", "Send the tokens paid per successful invite (e.g. 20)."),
    "goal": ("referral_goal", "Send how many invites earn one coupon (e.g. 5)."),
    "coupon": (
        "referral_coupon_reward",
        "Send the token value of a referral coupon (e.g. 100).",
    ),
}


async def admin_token_action(ctx: Ctx, key: str) -> None:
    key = (key or "").strip().lower()
    if key == "plans":
        plans = await plan_catalog(ctx)
        rows = [(code, label) for code, label in plans]
        current = {
            code: ctx.token_cfg.cost_for_plan(code) for code, _ in plans
        }
        lines = [
            "📦 Per-plan token costs",
            "",
            "Tap a plan and send its token cost. Plans come from the FF Glory API.",
        ]
        await respond(ctx, "\n".join(lines), kb.plan_costs_menu(rows, current))
        return
    if key == "reset":
        await db.reset_token_settings()
        settings = get_settings()
        ctx.token_cfg = TokenConfig.from_doc(await db.get_bot_settings(), settings)
        await respond(
            ctx,
            "Token settings reset to their environment defaults.",
            kb.token_menu(),
        )
        return
    if key.startswith("plan:"):
        plan_id = key[5:]
        await db.set_state(ctx.user_id, "admin_plan_cost", {"plan_id": plan_id})
        await respond(
            ctx,
            f"Send the token cost for plan “{plan_id}” "
            f"(currently {token_line(ctx.token_cfg.cost_for_plan(plan_id))}).",
            kb.cancel_only("a:tk:plans"),
        )
        return
    entry = TOKEN_PROMPTS.get(key)
    if not entry:
        await admin_token_menu(ctx)
        return
    field, question = entry
    await db.set_state(ctx.user_id, "admin_token_value", {"field": field})
    await respond(ctx, question, kb.cancel_only("a:tk"))


async def admin_broadcast_prompt(ctx: Ctx) -> None:
    await db.set_state(ctx.user_id, "admin_broadcast", {})
    await respond(
        ctx,
        "Send the broadcast message as one message. It goes to every user "
        "who started the bot. Send /admin to cancel.",
        kb.cancel_only("a:menu"),
    )
