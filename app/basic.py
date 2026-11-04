"""Basic-service flow: the plan list comes from the FF Glory API.

Plans are priced in **tokens** only — the admin sets a default cost plus
per-plan overrides (Admin Panel → 🪙 Token Settings), so a new plan from the
API is sellable immediately.
"""

from __future__ import annotations

import logging

from . import db
from . import keyboards as kb
from .glory_api import GloryAPIError, extract_group_id
from .telegram import send_message
from .ui import (
    Ctx,
    _config_hint,
    _error_ref,
    _short,
    ensure_user_key,
    glory_client,
    notify_admins,
    respond,
    token_balance_of,
    token_line,
)

logger = logging.getLogger("bot")

DEFAULT_REGIONS = [("me", "Middle East (me)"), ("bd", "Bangladesh (bd)")]


async def plan_catalog(ctx: Ctx) -> list[tuple[str, str]]:
    """(plan id, label) as the FF Glory API reports them, with a safe fallback."""
    await ensure_user_key(ctx)
    api = glory_client(ctx)
    plans = list(DEFAULT_REGIONS)
    if api.configured:
        try:
            fetched = await api.regions()
            live = [(r.id, r.name) for r in fetched if r.enabled]
            if live:
                plans = live
        except GloryAPIError as exc:
            # Diagnostics stay in the log; users only see the fallback plans.
            logger.warning("live plan list unavailable: %s", exc)
    return plans


async def show_basic(ctx: Ctx) -> None:
    plans = await plan_catalog(ctx)
    priced = [(code, label, ctx.token_cfg.cost_for_plan(code)) for code, label in plans]
    balance = await token_balance_of(ctx)
    text = (
        "Basic Services (Auto API)\n\n"
        f"Your balance: {token_line(balance)}\n\n"
        "Every plan below is priced in tokens. The API launches your group "
        "instantly and tokens are only taken after a successful launch.\n\n"
        "Pick a plan:"
    )
    await respond(ctx, text, kb.plan_menu(priced))


async def prompt_clan_id(ctx: Ctx, region: str) -> None:
    region = (region or "me").strip().lower()
    price = ctx.token_cfg.cost_for_plan(region)
    balance = await token_balance_of(ctx)
    if balance < price:
        await respond(
            ctx,
            "Not enough tokens for this plan.\n\n"
            f"Plan cost: {token_line(price)}\n"
            f"Balance: {token_line(balance)}\n\n"
            "Top up to continue.",
            kb.insufficient_tokens(),
        )
        return
    await db.set_state(ctx.user_id, "basic_clan", {"region": region})
    await respond(
        ctx,
        f"Plan: {region}\n"
        f"Cost: {token_line(price)}\n"
        f"Balance: {token_line(balance)}\n\n"
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

    price = ctx.token_cfg.cost_for_plan(region)
    balance = await token_balance_of(ctx)
    if balance < price:
        await send_message(
            ctx.chat_id,
            f"Not enough tokens. Plan: {token_line(price)}, "
            f"balance: {token_line(balance)}.",
            kb.insufficient_tokens(),
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
    if not await db.hold_tokens(ctx.user_id, price):
        logger.error(
            "token balance changed during launch: user=%s price=%s", ctx.user_id, price
        )
        await send_message(
            ctx.chat_id,
            "The group was launched, but the tokens could not be taken "
            "(the balance changed during checkout). Support will reconcile the order.",
        )
        await notify_admins(
            f"Charge failed after a successful launch.\n"
            f"User: {ctx.name} ({ctx.user_id})\nClan: {clan_id}\nRegion: {region}\n"
            f"Group: {group_id or 'n/a'}\nCost: {price:,} tokens"
        )
        return

    order_id = await db.create_order(
        telegram_id=ctx.user_id,
        name=ctx.name,
        tier="basic",
        service="glory_group",
        base_price=price,
        final_price=price,
        status="completed",
        region=region,
        clan_id=clan_id,
        group_id=group_id,
        meta={"plan": region, "cost_tokens": price},
    )
    new_balance = await db.get_tokens(ctx.user_id)
    lines = [
        "Group launched successfully.",
        "",
        f"Clan: {clan_id}",
        f"Plan: {region}",
        f"Group ID: {group_id or 'n/a'}",
        f"Charged: {token_line(price)}",
        f"Balance: {token_line(new_balance)}",
        f"Order: #{_short(order_id)}",
    ]
    await send_message(ctx.chat_id, "\n".join(lines), kb.main_menu(ctx.is_admin))
