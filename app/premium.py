"""Premium / secret bulk: manual approval with tokens held up front."""

from __future__ import annotations

from . import db
from . import keyboards as kb
from .telegram import send_message
from .ui import Ctx, _short, notify_admins, respond, token_balance_of, token_line


def _order_admin_text(order: dict) -> str:
    lines = [
        "Premium order — approval needed",
        "",
        f"Order: #{_short(order)}",
        f"User: {order.get('name') or '?'} (id {order.get('telegram_id')})",
        f"Tokens held: {token_line(order.get('final_price') or 0)}",
        f"Plan cost: {token_line(order.get('base_price') or 0)}",
    ]
    if order.get("region"):
        lines.append(f"Region: {order['region']}")
    if order.get("clan_id"):
        lines.append(f"Clan: {order['clan_id']}")
    lines += ["", "Details from the user:", str(order.get("user_input") or "-")]
    return "\n".join(lines)


async def show_premium(ctx: Ctx) -> None:
    price = ctx.token_cfg.premium_cost
    balance = await token_balance_of(ctx)
    text = (
        "Premium / Secret Bulk\n\n"
        "For bulk orders, personal glory pushes and anything the automatic API "
        "cannot handle. An admin handles these manually.\n\n"
        f"Price: {token_line(price)} per order\n"
        f"Your balance: {token_line(balance)}\n\n"
        "Tokens are held when you order and refunded automatically if the "
        "admin rejects it.\n\n"
        "You send the details once; the admin team receives them and completes "
        "the order."
    )
    await respond(ctx, text, kb.premium_intro())


async def premium_start(ctx: Ctx) -> None:
    price = ctx.token_cfg.premium_cost
    balance = await token_balance_of(ctx)
    if balance < price:
        await respond(
            ctx,
            "Not enough tokens.\n\n"
            f"Premium price: {token_line(price)}\n"
            f"Balance: {token_line(balance)}\n\n"
            "Top up to continue.",
            kb.insufficient_tokens(),
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
    price = ctx.token_cfg.premium_cost
    await db.set_state(ctx.user_id, "premium_confirm", {"details": text[:1500]})
    await respond(
        ctx,
        f"Order summary\n\n{text[:1500]}\n\n"
        f"Price: {token_line(price)}\n"
        "These tokens are held now and refunded if the order is rejected.",
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
    price = ctx.token_cfg.premium_cost
    if not await db.hold_tokens(ctx.user_id, price):
        await db.clear_state(ctx.user_id)
        await respond(
            ctx,
            "Not enough tokens — the order was not placed.",
            kb.insufficient_tokens(),
        )
        return
    order_id = await db.create_order(
        telegram_id=ctx.user_id,
        name=ctx.name,
        tier="premium",
        service="premium_bulk",
        base_price=price,
        final_price=price,
        status="pending",
        user_input=details,
        meta={"cost_tokens": price},
    )
    await db.clear_state(ctx.user_id)
    balance = await db.get_tokens(ctx.user_id)
    await respond(
        ctx,
        f"Premium order submitted.\n\nOrder: #{_short(order_id)}\n"
        f"Tokens held: {token_line(price)}\n"
        f"Balance: {token_line(balance)}\n\n"
        "An admin will review it and you will be notified here.",
        kb.main_menu(ctx.is_admin),
    )
    order = await db.get_order(order_id)
    if order:
        await notify_admins(_order_admin_text(order), kb.order_actions(order_id))
