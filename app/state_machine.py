"""Text-input state machine: every wizard answer lands here."""

from __future__ import annotations

import asyncio
import logging
import re

from . import bkash as bkash_api
from . import db
from . import deposits
from . import keyboards as kb
from .admin_panel import API_KEYS, _parse_whole, admin_token_menu
from .basic import launch_basic
from .commands import CLAN_RE
from .config import parse_rates
from .deposit_flows import _amount_buttons, _bkash_review, _crypto_create, _crypto_manual_txid
from .premium import premium_details
from .telegram import send_media, send_message
from .tokens import TokenConfig
from .ui import (
    ADMIN_DENIED,
    Ctx,
    _mask,
    _parse_amount,
    _short,
    get_settings,
    min_deposit,
    token_line,
    user_api_enabled,
)

logger = logging.getLogger("bot")


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

    if action in ("admin_token_value", "admin_plan_cost"):
        field_name = str(data.get("field") or "")
        plan_id = str(data.get("plan_id") or "")
        whole = _parse_whole(text)
        if action == "admin_plan_cost":
            if whole is None or whole < 0 or not plan_id:
                await send_message(
                    ctx.chat_id,
                    "Send a whole number of tokens (0 or more).",
                    kb.cancel_only("a:tk:plans"),
                )
                return
            await db.set_plan_cost(plan_id, whole)
        elif field_name == "taka_per_token":
            value = _parse_amount(text)
            if value is None or value <= 0:
                await send_message(
                    ctx.chat_id,
                    "Send how many Taka buy one token (e.g. 1.5).",
                    kb.cancel_only("a:tk"),
                )
                return
            await db.set_token_settings(taka_per_token=round(value, 4))
        elif field_name == "referral_goal":
            if whole is None or not (1 <= whole <= 1000):
                await send_message(
                    ctx.chat_id,
                    "Send the number of invites per coupon (1-1000).",
                    kb.cancel_only("a:tk"),
                )
                return
            await db.set_token_settings(referral_goal=whole)
        elif field_name:
            if whole is None or whole < 0:
                await send_message(
                    ctx.chat_id,
                    "Send a whole number of tokens (0 or more).",
                    kb.cancel_only("a:tk"),
                )
                return
            await db.set_token_settings(**{field_name: whole})
        else:
            await db.clear_state(ctx.user_id)
            await admin_token_menu(ctx)
            return
        ctx.token_cfg = TokenConfig.from_doc(await db.get_bot_settings(), get_settings())
        await db.clear_state(ctx.user_id)
        cfg = ctx.token_cfg
        saved = (
            f"Cost for plan “{plan_id}” = {token_line(cfg.cost_for_plan(plan_id))}"
            if plan_id
            else "Token settings saved"
        )
        await send_message(
            ctx.chat_id,
            f"{saved}.\n\n"
            f"Rate: {cfg.rate_line()}\n"
            f"Basic: {token_line(cfg.basic_cost)} · Premium: {token_line(cfg.premium_cost)}\n"
            f"Referral: {token_line(cfg.referral_reward)} per invite, "
            f"goal {cfg.referral_goal} → {token_line(cfg.referral_coupon_reward)} coupon",
            kb.token_menu(),
        )
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
