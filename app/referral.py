"""Referral program and token coupons.

Every user gets a personal deep link; a new user arriving through it pays the
referrer the admin-set token reward exactly once, and every completed goal
milestone issues one single-use coupon.
"""

from __future__ import annotations

import re

from . import db
from . import keyboards as kb
from .telegram import bot_username, send_message
from .ui import Ctx, respond, token_line


async def referral_link(ctx: Ctx) -> str:
    """Deep link that greets the referrer when a new user joins through it."""
    username = (ctx.settings.bot_username or "").lstrip("@").strip()
    if not username and ctx.settings.telegram_token:
        username = await bot_username()
    if not username:
        return ""
    return f"https://t.me/{username}?start=ref_{ctx.user_id}"


def referral_code_from(payload: str) -> int:
    """``ref_123`` / ``ref123`` / ``123`` → 123 (0 when malformed)."""
    digits = re.sub(r"[^0-9]", "", str(payload or ""))
    try:
        value = int(digits)
    except ValueError:
        return 0
    return value if value > 0 else 0


async def award_referral(ctx: Ctx, referrer_id: int, *, referred_id: int) -> None:
    """Pay the referrer once and issue the goal coupon when a milestone lands."""
    cfg = ctx.token_cfg
    outcome = await db.register_referral(
        referrer_id,
        referred_id,
        reward=cfg.referral_reward,
        goal=cfg.referral_goal,
    )
    if not outcome.get("credited"):
        return
    invites = int(outcome.get("invites") or 0)
    await send_message(
        int(referrer_id),
        "🎉 Someone joined with your referral link.\n\n"
        f"Reward: {token_line(cfg.referral_reward)}\n"
        f"Total invites: {invites}",
        kb.main_menu(False),
    )
    for _ in range(int(outcome.get("milestones_due") or 0)):
        coupon = await db.create_coupon(
            int(referrer_id), cfg.referral_coupon_reward, source="referral_goal"
        )
        await send_message(
            int(referrer_id),
            "🎟 Referral goal reached!\n\n"
            f"Invites: {invites}\n"
            f"Coupon: {coupon['code']} worth {token_line(coupon['tokens'])}\n\n"
            "Open Coupons to claim it.",
            kb.referral_menu(coupon_ready=True),
        )


async def show_referral(ctx: Ctx) -> None:
    cfg = ctx.token_cfg
    stats = await db.referral_stats(ctx.user_id)
    invites = int(stats["invites"])
    progress, goal = cfg.goal_progress(invites)
    link = await referral_link(ctx)
    coupons = await db.list_user_coupons(ctx.user_id)
    active = [c for c in coupons if str(c.get("status")) == "active"]
    lines = [
        "🎁 Referral Program",
        "",
        f"Reward per invite: {token_line(cfg.referral_reward)}",
        f"Goal: {goal} invites → a {token_line(cfg.referral_coupon_reward)} coupon",
        f"Invites so far: {invites} ({progress}/{goal} to the next coupon)",
        f"Coupons waiting: {len(active)}",
        "",
        "Your link:",
        link or "Not configured yet — ask the admin to set BOT_USERNAME.",
        "",
        "Share it: every new user who joins through it pays you tokens once.",
    ]
    await respond(ctx, "\n".join(lines), kb.referral_menu(bool(active)))


async def show_coupons(ctx: Ctx) -> None:
    coupons = await db.list_user_coupons(ctx.user_id)
    if not coupons:
        await respond(
            ctx,
            "🎟 You have no coupons yet.\n\n"
            "Reach a referral goal to earn one.",
            kb.referral_menu(),
        )
        return
    lines = ["🎟 Your coupons", ""]
    for coupon in coupons[:8]:
        status = str(coupon.get("status") or "?")
        lines.append(
            f"{coupon.get('code')} — {token_line(coupon.get('tokens') or 0)} [{status}]"
        )
    lines += ["", "Tap a coupon to add its tokens to your balance."]
    await respond(ctx, "\n".join(lines), kb.coupon_menu(coupons))


async def redeem_coupon_code(ctx: Ctx, code: str) -> None:
    result = await db.redeem_coupon(code, ctx.user_id)
    if result.get("ok"):
        balance = await db.get_tokens(ctx.user_id)
        await respond(
            ctx,
            f"✅ Coupon {code.strip().upper()} redeemed.\n\n"
            f"Credited: {token_line(result.get('tokens') or 0)}\n"
            f"New balance: {token_line(balance)}",
            kb.main_menu(ctx.is_admin),
        )
        return
    reason = result.get("reason")
    message = {
        "used": "This coupon has already been used.",
        "not_yours": "This coupon belongs to another account.",
        "unknown": "That coupon code was not found.",
        "empty": "Send a coupon code, for example /redeem REF-AB12CD.",
    }.get(str(reason), "This coupon could not be redeemed.")
    await respond(ctx, message, kb.referral_menu())
