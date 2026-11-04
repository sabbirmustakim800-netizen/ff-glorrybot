"""Tests for the token economy, referrals, coupons and the bottom keyboard.

The fakes live in ``tests/test_deposit_flows.py`` (an in-memory Mongo and a
list-backed Telegram transport); this module reuses them so every screen is
asserted on the exact text a user would see.
"""

from __future__ import annotations

import asyncio

from app import handlers
from app import keyboards as kb
from app.bkash import BkashCheck
from app.tokens import TokenConfig, fmt_tokens

from test_deposit_flows import (  # noqa: F401  (harness is a fixture)
    ADMIN_ID,
    USER_ID,
    _api,
    harness,
    make_ctx,
)

REFERRER_ID = 777


# --------------------------------------------------------------------- tokens


def test_tokens_format_without_a_currency_symbol() -> None:
    assert fmt_tokens(120) == "🪙 120"
    assert fmt_tokens(1234.0) == "🪙 1,234"
    assert "$" not in fmt_tokens(500)


def test_conversion_uses_the_admin_rate() -> None:
    cfg = TokenConfig(taka_per_token=2.0)
    assert cfg.tokens_for_bdt(500) == 250
    assert cfg.tokens_for_bdt(0) == 0
    assert cfg.tokens_for_bdt(-10) == 0
    assert TokenConfig(taka_per_token=0).tokens_for_bdt(100) == 100  # safe fallback
    assert TokenConfig(taka_per_token=1.5).rate_line() == "৳1.5 = 1 token"


def test_deposit_credits_tokens_at_the_configured_rate(harness) -> None:
    ctx = make_ctx(
        harness,
        api=_api(bkash_base_url="https://provider", bkash_api_key="k"),
        token_cfg=TokenConfig(taka_per_token=2.0),
    )
    harness.check = BkashCheck(
        state="verified", amount=500.0, sender="01712345678", sender_state="match"
    )
    asyncio.run(handlers.bkash_start(ctx))
    async def run() -> None:
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "01712345678")
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "BFD90JRLST")
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "500")

    asyncio.run(run())
    asyncio.run(handlers.bkash_submit(ctx))

    # ৳500 at ৳2 per token = 250 tokens, and the user is only told about tokens.
    assert harness.fake.balances[USER_ID] == 250
    assert "🪙 250" in harness.last()


def test_basic_plans_are_priced_in_tokens(harness) -> None:
    cfg = TokenConfig(basic_cost=80, plan_costs={"me": 150})
    ctx = make_ctx(harness, token_cfg=cfg)
    asyncio.run(handlers.show_basic(ctx))
    text = harness.last()
    assert "৳" not in text
    assert "Pick a plan" in text
    labels = [
        button["text"]
        for _, _, markup in harness.sent
        for row in (markup or {}).get("inline_keyboard", [])
        for button in row
    ]
    assert any("🪙 150 — Middle East" in label for label in labels)
    assert any("🪙 80 — Bangladesh" in label for label in labels)  # default cost
    assert "b:r:me" in harness.all_callbacks()


def test_user_screens_never_show_cash(harness) -> None:
    ctx = make_ctx(harness, token_cfg=TokenConfig(basic_cost=100, premium_cost=300))
    asyncio.run(handlers.show_main(ctx))
    assert "৳" not in harness.last() and "🪙" in harness.last()
    asyncio.run(handlers.show_premium(ctx))
    assert "৳" not in harness.last() and "🪙 300" in harness.last()


# ------------------------------------------------------------------ referrals


def test_referral_rewards_the_referrer_once(harness) -> None:
    cfg = TokenConfig(referral_reward=25, referral_goal=5)
    ctx = make_ctx(harness, token_cfg=cfg)
    ctx.args = [f"ref_{REFERRER_ID}"]

    async def first_join(telegram_id: int, name: str):
        return ({"telegram_id": telegram_id}, True)

    original = handlers.db.get_or_create_user
    handlers.db.get_or_create_user = first_join
    try:
        asyncio.run(handlers.cmd_start(ctx))
    finally:
        handlers.db.get_or_create_user = original

    assert harness.fake.balances[REFERRER_ID] == 25
    assert harness.fake.referrals[REFERRER_ID] == 1
    assert "🎁" in harness.last()  # the new user is told about the invite

    # A repeat /start with the same payload never pays twice.
    again = make_ctx(harness, token_cfg=cfg)
    again.args = [f"ref_{REFERRER_ID}"]
    handlers.db.get_or_create_user = first_join
    try:
        asyncio.run(handlers.cmd_start(again))
    finally:
        handlers.db.get_or_create_user = original
    assert harness.fake.balances[REFERRER_ID] == 25


def test_referral_goal_issues_exactly_one_coupon_per_milestone(harness) -> None:
    cfg = TokenConfig(referral_reward=10, referral_goal=2, referral_coupon_reward=100)
    ctx = make_ctx(harness, token_cfg=cfg)

    async def run() -> None:
        for new_user in (9001, 9002, 9003):
            await handlers.award_referral(ctx, REFERRER_ID, referred_id=new_user)

    asyncio.run(run())

    coupons = harness.fake.coupons.values()
    assert len(coupons) == 1  # milestone 1 at two invites, not yet at three
    coupon = next(iter(coupons))
    assert coupon["tokens"] == 100
    assert coupon["telegram_id"] == REFERRER_ID
    assert harness.fake.balances[REFERRER_ID] == 30  # three invites, no payout yet
    assert any("Referral goal reached" in text for text in harness.texts())


def test_coupon_can_only_be_redeemed_once_and_by_its_owner(harness) -> None:
    ctx = make_ctx(harness)
    coupon = asyncio.run(harness.fake.create_coupon(USER_ID, 75))
    code = coupon["code"]

    asyncio.run(handlers.redeem_coupon_code(ctx, code))
    assert harness.fake.balances[USER_ID] == 75
    assert "🪙 75" in harness.last()

    asyncio.run(handlers.redeem_coupon_code(ctx, code))
    assert harness.fake.balances[USER_ID] == 75
    assert "already been used" in harness.last()

    other = make_ctx(harness, user_id=ADMIN_ID)
    second = asyncio.run(harness.fake.create_coupon(USER_ID, 40))
    asyncio.run(handlers.redeem_coupon_code(other, second["code"]))
    assert harness.fake.balances.get(ADMIN_ID, 0) == 0
    assert "another account" in harness.last()


def test_referral_screen_shows_link_goal_and_reward(harness) -> None:
    cfg = TokenConfig(referral_reward=15, referral_goal=3, referral_coupon_reward=90)
    ctx = make_ctx(harness, token_cfg=cfg)
    asyncio.run(handlers.show_referral(ctx))
    text = harness.last()
    assert f"https://t.me/ffglory_test_bot?start=ref_{USER_ID}" in text
    assert "🪙 15" in text and "🪙 90" in text
    assert "0/3" in text


# ------------------------------------------------------------ admin token panel


def test_admin_sets_the_conversion_rate(harness) -> None:
    ctx = make_ctx(harness, is_admin=True)
    asyncio.run(handlers.admin_token_action(ctx, "rate"))
    state = asyncio.run(handlers.db.get_state(USER_ID))
    assert state["action"] == "admin_token_value"
    asyncio.run(handlers.handle_state_input(ctx, state, "2.5"))
    assert harness.fake.token_settings["taka_per_token"] == 2.5
    assert ctx.token_cfg.taka_per_token == 2.5
    assert "৳2.5 = 1 token" in harness.last()


def test_admin_sets_a_plan_token_cost(harness) -> None:
    ctx = make_ctx(harness, is_admin=True)
    asyncio.run(handlers.admin_token_action(ctx, "plan:me"))
    state = asyncio.run(handlers.db.get_state(USER_ID))
    assert state["action"] == "admin_plan_cost"
    asyncio.run(handlers.handle_state_input(ctx, state, "175"))
    assert harness.fake.token_settings["plan_costs"] == {"me": 175}
    assert ctx.token_cfg.cost_for_plan("me") == 175
    assert ctx.token_cfg.cost_for_plan("bd") == ctx.token_cfg.basic_cost


def test_admin_rejects_bad_token_input(harness) -> None:
    ctx = make_ctx(harness, is_admin=True)
    asyncio.run(handlers.admin_token_action(ctx, "goal"))
    state = asyncio.run(handlers.db.get_state(USER_ID))
    asyncio.run(handlers.handle_state_input(ctx, state, "0"))
    assert harness.fake.token_settings == {}
    assert "1-1000" in harness.last()


# ---------------------------------------------------------- bottom keyboard


def test_reply_keyboard_is_persistent_and_complete() -> None:
    markup = kb.reply_menu(False)
    assert markup["resize_keyboard"] is True
    assert markup["is_persistent"] is True
    labels = [button["text"] for row in markup["keyboard"] for button in row]
    assert labels == [
        "🎮 Basic Services",
        "💎 Premium",
        "💳 Deposit",
        "👤 Account",
        "🎁 Referral",
        "🎟 Coupons",
        "🌍 Categories",
        "🔑 APIs",
        "🏠 Menu",
        "✖️ Cancel",
    ]
    admin_labels = [b["text"] for row in kb.reply_menu(True)["keyboard"] for b in row]
    assert "🛡 Admin Panel" in admin_labels


def test_reply_labels_route_with_or_without_the_emoji() -> None:
    assert handlers.reply_action("🎟 Coupons") == "coupons"
    assert handlers.reply_action("coupons") == "coupons"
    assert handlers.reply_action("  💳 DEPOSIT ") == "deposit"
    assert handlers.reply_action("🛡 Admin Panel") == "admin"
    assert handlers.reply_action("BFD90JRLST") == ""


def test_reply_buttons_open_the_right_screen(harness) -> None:
    ctx = make_ctx(harness)
    asyncio.run(handlers.handle_reply_action(ctx, "referral"))
    assert "Referral Program" in harness.last()
    asyncio.run(handlers.handle_reply_action(ctx, "coupons"))
    assert "no coupons yet" in harness.last()
    asyncio.run(handlers.handle_reply_action(ctx, "categories"))
    assert "Basic Services" in harness.last()


def test_only_admin_can_open_the_admin_button(harness) -> None:
    ctx = make_ctx(harness)
    asyncio.run(handlers.handle_reply_action(ctx, "admin"))
    assert "Access denied" in harness.last()
