"""Slash commands: deposit shortcuts, the FF Glory API commands and coupons."""

from __future__ import annotations

import logging
import re

from . import binance as binance_api
from . import bkash as bkash_api
from . import db
from . import keyboards as kb
from .basic import launch_basic
from .deposit_flows import _resolve_bkash
from .glory_api import GloryAPI, GloryAPIError
from .referral import redeem_coupon_code, show_coupons, show_referral
from .telegram import send_message
from .ui import (
    Ctx,
    _config_hint,
    _error_ref,
    _parse_amount,
    binance_settings,
    bkash_settings,
    ensure_user_key,
    glory_client,
    token_line,
)

logger = logging.getLogger("bot")

TRX_RE = re.compile(r"^[A-Z0-9]{6,20}$")
TXID_RE = re.compile(r"^[A-Za-z0-9:_-]{6,150}$")
COIN_RE = re.compile(r"^[A-Z0-9]{2,12}$")
CLAN_RE = re.compile(r"^[A-Za-z0-9]{4,20}$")


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
        credit_bdt = round(amount * rate, 2)
        tokens = ctx.token_cfg.tokens_for_bdt(credit_bdt)
        if not await db.credit_payment(pid, ctx.user_id, tokens):
            await send_message(ctx.chat_id, "This payment has already been processed.")
            return
        balance = await db.get_tokens(ctx.user_id)
        note = ""
        if declared is not None and abs(declared - amount) > 1e-8:
            note += f"\nYou expected {declared} {coin}; the deposit is {amount} {coin}."
        await send_message(
            ctx.chat_id,
            f"Binance deposit confirmed.\n"
            f"Received: {amount} {coin}\n"
            f"Credited: {token_line(tokens)}\n\n"
            f"New balance: {token_line(balance)}{note}",
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
    plan_cost = ctx.token_cfg.cost_for_plan("")
    await send_message(
        ctx.chat_id,
        "Available regions:\n"
        + "\n".join(lines)
        + f"\n\nDefault plan cost: {token_line(plan_cost)}",
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


async def cmd_referral(ctx: Ctx) -> None:
    await show_referral(ctx)


async def cmd_coupons(ctx: Ctx) -> None:
    await show_coupons(ctx)


async def cmd_redeem(ctx: Ctx) -> None:
    code = " ".join(ctx.args).strip()
    if not code:
        await send_message(
            ctx.chat_id,
            "Send the coupon code, for example /redeem REF-AB12CD.",
            kb.referral_menu(),
        )
        return
    await redeem_coupon_code(ctx, code)
