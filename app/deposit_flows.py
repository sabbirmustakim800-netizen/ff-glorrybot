"""Guided deposit flows: bKash form and crypto sessions.

Both flows are button-driven; nothing here asks the user to type a command.
Amounts are always handled in Bangladeshi Taka and the exact text a user sees
is deliberately free of provider details.
"""

from __future__ import annotations

import logging
from datetime import datetime

from . import binance as binance_api
from . import bkash as bkash_api
from . import db
from . import deposits
from . import keyboards as kb
from .telegram import send_message
from .tokens import fmt_tokens
from .ui import (
    Ctx,
    _config_hint,
    _error_ref,
    _money,
    _short,
    bkash_number,
    bkash_settings,
    binance_settings,
    min_deposit,
    notify_admins,
    respond,
)

logger = logging.getLogger("bot")


# ------------------------------------------------------------------ deposits


async def show_deposit(ctx: Ctx) -> None:
    cfg = ctx.token_cfg
    text = (
        "Deposit / Top-up\n\n"
        "bKash — pay in Taka (৳) to our number, then confirm the TrxID here.\n"
        "Crypto — USDT TRC20/BEP20 with a dedicated address per deposit.\n\n"
        f"Rate: {cfg.rate_line()} — a ৳{min_deposit(ctx):g} deposit gives "
        f"{cfg.tokens_for_bdt(min_deposit(ctx)):,} tokens.\n\n"
        "Everything you buy afterwards is priced in tokens only.\n\n"
        "Pick a method:"
    )
    await respond(ctx, text, kb.deposit_menu())


def _amount_buttons(ctx: Ctx, prefix: str) -> dict:
    return kb.amount_choices(prefix, [100, 200, 500, 1000, 2000])


async def show_bkash_deposit(ctx: Ctx) -> None:
    number = bkash_number(ctx)
    support = f"\n\nSupport: {ctx.settings.support_contact}" if ctx.settings.support_contact else ""
    if not number:
        await respond(
            ctx,
            _config_hint(
                ctx,
                "The receiving bKash number is not set. Add it in Admin Panel -> "
                "Payment Settings, or set BKASH_RECEIVER_NUMBER.",
            ),
            kb.deposit_menu(),
        )
        return
    text = (
        "bKash Deposit\n\n"
        f"Send money (personal) to: {number}\n"
        f"Amounts are in Bangladeshi Taka (৳) only.\n"
        f"Minimum deposit: ৳{min_deposit(ctx):g}\n"
        f"Rate: {ctx.token_cfg.rate_line()} (tokens are credited, not cash)\n\n"
        "Tap Start Deposit and enter the number you paid from, the TrxID from "
        "the confirmation SMS and the amount. Tokens are credited after "
        "verification and each TrxID can only be used once."
        f"{support}"
    )
    await respond(ctx, text, kb.deposit_actions())


async def bkash_start(ctx: Ctx) -> None:
    if not bkash_number(ctx):
        await show_bkash_deposit(ctx)
        return
    await db.set_state(ctx.user_id, "bkash_sender", {})
    await respond(
        ctx,
        "bKash Deposit — step 1 of 3\n\n"
        "Send the bKash number you paid from, for example 01712345678.",
        kb.cancel_only("d:b:cancel"),
    )


async def bkash_quick_amount(ctx: Ctx, raw: str) -> None:
    state = await db.get_state(ctx.user_id)
    data = dict((state or {}).get("data") or {})
    if (state or {}).get("action") not in ("bkash_amount", "bkash_confirm") or not data.get("trx"):
        await show_bkash_deposit(ctx)
        return
    amount, error = deposits.parse_amount(raw, minimum=min_deposit(ctx))
    if error:
        await send_message(ctx.chat_id, error, _amount_buttons(ctx, "d:b:amt:"))
        return
    await _bkash_review(
        ctx, str(data.get("sender") or ""), str(data.get("trx") or ""), amount
    )


async def _bkash_review(ctx: Ctx, sender: str, trx: str, amount: float) -> None:
    await db.set_state(
        ctx.user_id,
        "bkash_confirm",
        {"sender": sender, "trx": trx, "amount": amount},
    )
    await respond(
        ctx,
        "bKash Deposit — confirm\n\n"
        f"Sender: {sender}\n"
        f"TrxID: {trx}\n"
        f"You pay: ৳{_money(amount)} BDT\n"
        f"You receive: {fmt_tokens(ctx.token_cfg.tokens_for_bdt(amount))} "
        f"({ctx.token_cfg.rate_line()})\n\n"
        "Check the details, then confirm. Tokens are credited after "
        "verification.",
        kb.bkash_confirm(),
    )


async def bkash_submit(ctx: Ctx) -> None:
    state = await db.get_state(ctx.user_id)
    data = dict((state or {}).get("data") or {})
    if (state or {}).get("action") != "bkash_confirm":
        await respond(
            ctx, "This deposit session expired. Please start again.", kb.deposit_actions()
        )
        return
    sender = str(data.get("sender") or "")
    trx = str(data.get("trx") or "")
    amount = data.get("amount")
    if not (sender and trx and amount):
        await db.clear_state(ctx.user_id)
        await respond(
            ctx, "This deposit session is incomplete. Please start again.", kb.deposit_actions()
        )
        return
    await db.clear_state(ctx.user_id)
    await _bkash_process(ctx, trx=trx, sender=sender, amount=float(amount))


async def _bkash_process(
    ctx: Ctx, *, trx: str, sender: str, amount: float | None
) -> None:
    action, pid = await db.claim_payment(
        "bkash",
        trx,
        ctx.user_id,
        {
            "sender": sender,
            "amount_bdt": amount,
            "tokens": ctx.token_cfg.tokens_for_bdt(amount or 0),
            "source": "guided_form",
        },
    )
    if action == "busy" or not pid:
        await send_message(
            ctx.chat_id,
            "This TrxID has already been submitted or verified. "
            "Each TrxID can only be used once.",
            kb.main_menu(ctx.is_admin),
        )
        return
    provider = bkash_settings(ctx)
    if not (provider.bkash_base_url and provider.bkash_api_key):
        await _bkash_manual_review(
            ctx, pid, trx, sender, amount, reason="provider_not_configured"
        )
        return
    await send_message(
        ctx.chat_id, "Checking your bKash payment… this can take up to ~30 seconds."
    )
    check = await bkash_api.verify_bkash_trxid(
        provider, ctx.client, trx_id=trx, sender=sender, amount=amount
    )
    await _resolve_bkash(ctx, pid, trx, amount, check)


async def _bkash_manual_review(
    ctx: Ctx, pid: str, trx: str, sender: str, amount: float | None, *, reason: str
) -> None:
    await db.finish_payment(
        pid,
        "needs_review",
        reason=reason,
        details={"sender": sender, "amount_bdt": amount, "source": "guided_form"},
    )
    await send_message(
        ctx.chat_id,
        "Deposit submitted for verification.\n\n"
        f"TrxID: {trx}\n"
        f"Sender: {sender}\n"
        f"Paid: ৳{_money(amount or 0)}\n"
        f"Tokens: {fmt_tokens(ctx.token_cfg.tokens_for_bdt(amount or 0))}\n"
        f"Reference: #{pid[-6:]}\n\n"
        "An admin will confirm it and your tokens will be credited here.",
        kb.main_menu(ctx.is_admin),
    )
    await notify_admins(
        "bKash deposit awaiting verification\n\n"
        f"User: {ctx.name} ({ctx.user_id})\n"
        f"TrxID: {trx}\n"
        f"Sender: {sender}\n"
        f"Amount: ৳{_money(amount or 0)}\n"
        f"Reference: #{pid[-6:]}",
        kb.payment_review(pid),
    )


async def show_crypto_deposit(ctx: Ctx) -> None:
    text = (
        "Crypto Deposit (USDT)\n\n"
        "Pick the network you will pay with. Every deposit gets its own address "
        "and an exact amount to send, so your payment is matched to your "
        "account automatically.\n"
        f"Rate: {ctx.token_cfg.rate_line()}\n\n"
        "Send only USDT on the selected network."
    )
    await respond(ctx, text, kb.crypto_networks())


async def crypto_route(ctx: Ctx, rest: str) -> None:
    parts = [part for part in rest.split(":") if part]
    head = parts[0] if parts else ""
    if head == "usdt" and len(parts) >= 2:
        await crypto_start(ctx, "USDT", parts[1])
    elif head == "amt" and len(parts) >= 3:
        await crypto_quick_amount(ctx, parts[1], parts[2])
    elif head == "check" and len(parts) >= 2:
        await crypto_check(ctx, parts[1])
    elif head == "tx" and len(parts) >= 2:
        await crypto_txid_prompt(ctx, parts[1])
    elif head == "cancel" and len(parts) >= 2:
        await crypto_cancel(ctx, parts[1])
    else:
        await show_crypto_deposit(ctx)


async def crypto_start(ctx: Ctx, coin: str, network: str) -> None:
    net = deposits.normalize_network(network)
    if str(coin or "").upper() != "USDT" or not net:
        await show_crypto_deposit(ctx)
        return
    static_address = ctx.api["usdt_trc20"] if net == "trc20" else ctx.api["usdt_bep20"]
    provider = binance_settings(ctx)
    automatic = bool(provider.binance_api_key and provider.binance_api_secret)
    if not (static_address or automatic):
        await respond(
            ctx,
            _config_hint(
                ctx,
                "Crypto deposits are not configured: set BINANCE_API_KEY / "
                "BINANCE_API_SECRET or the wallet addresses in API Settings.",
            ),
            kb.crypto_networks(),
        )
        return
    await db.set_state(ctx.user_id, "crypto_amount", {"coin": "USDT", "network": net})
    await respond(
        ctx,
        f"Crypto Deposit — {deposits.network_label(net)}\n\n"
        f"Send the amount in BDT you want to deposit (minimum ৳{min_deposit(ctx):g}). "
        f"It is credited as tokens at {ctx.token_cfg.rate_line()}, and we show "
        "you the exact amount and address to send.",
        _amount_buttons(ctx, f"d:c:amt:{net}:"),
    )


async def crypto_quick_amount(ctx: Ctx, network: str, raw: str) -> None:
    net = deposits.normalize_network(network)
    state = await db.get_state(ctx.user_id)
    data = dict((state or {}).get("data") or {})
    if not net or (state or {}).get("action") != "crypto_amount":
        await show_crypto_deposit(ctx)
        return
    amount, error = deposits.parse_amount(raw, minimum=min_deposit(ctx))
    if error:
        await send_message(ctx.chat_id, error, _amount_buttons(ctx, f"d:c:amt:{net}:"))
        return
    await db.clear_state(ctx.user_id)
    await _crypto_create(ctx, str(data.get("coin") or "USDT"), net, amount)


async def _crypto_create(ctx: Ctx, coin: str, net: str, amount_bdt: float) -> None:
    provider = binance_settings(ctx)
    automatic = bool(provider.binance_api_key and provider.binance_api_secret)
    rate = (ctx.api.get("rates") or {}).get(coin)
    if not rate or float(rate) <= 0:
        await send_message(
            ctx.chat_id,
            _config_hint(
                ctx,
                f"No BDT rate is configured for {coin}. Add it in API Settings "
                "(BDT_RATES, for example USDT=110).",
            ),
            kb.crypto_networks(),
        )
        return

    address, tag, source = "", "", "panel"
    if automatic:
        info = await binance_api.fetch_deposit_address(
            ctx.client, provider, coin=coin, network=deposits.BINANCE_NETWORKS[net]
        )
        if info:
            address, tag, source = info["address"], info.get("tag") or "", "binance"
    if not address:
        address = ctx.api["usdt_trc20"] if net == "trc20" else ctx.api["usdt_bep20"]
    if not address:
        await send_message(
            ctx.chat_id,
            "Crypto deposits are temporarily unavailable. Please contact support.",
        )
        return

    coin_amount = round(float(amount_bdt) / float(rate), 2)
    expected = deposits.reference_amount(coin_amount)
    tokens = ctx.token_cfg.tokens_for_bdt(amount_bdt)
    meta = {
        "coin": coin,
        "network": net,
        "address": address,
        "tag": tag,
        "expected": expected,
        "requested_bdt": round(float(amount_bdt), 2),
        "tokens": tokens,
        "rate": float(rate),
        "automatic": automatic,
        "address_source": source,
    }
    action, pid = await db.claim_payment(
        "crypto_session", deposits.session_code(), ctx.user_id, meta
    )
    if action != "claimed" or not pid:
        await send_message(
            ctx.chat_id, "Could not open a deposit session. Please try again."
        )
        return

    lines = [
        "Crypto Deposit — send exactly this amount",
        "",
        f"Amount: {expected:g} {coin}",
        f"Network: {deposits.network_label(net)}",
        f"Address: {address}",
        f"You receive: {fmt_tokens(tokens)}",
    ]
    if tag:
        lines.append(f"Memo/Tag: {tag}")
    lines += [
        "",
        f"Expires in {deposits.SESSION_MINUTES} minutes.",
        "",
        "The exact amount (including the decimals) identifies your deposit.",
    ]
    lines.append(
        "Tap the button below after sending and we check Binance automatically."
        if automatic
        else "After sending, tap the button and submit your transaction ID for admin verification."
    )
    await send_message(
        ctx.chat_id, "\n".join(lines), kb.crypto_session(pid, automatic=automatic)
    )


async def crypto_check(ctx: Ctx, pid: str) -> None:
    payment = await db.get_payment(pid)
    if not payment or payment.get("kind") != "crypto_session":
        await respond(ctx, "This deposit session was not found.", kb.deposit_menu())
        return
    if int(payment.get("telegram_id") or 0) != ctx.user_id:
        await send_message(ctx.chat_id, "This deposit session belongs to another account.")
        return
    status = str(payment.get("status") or "")
    if status == "verified":
        await respond(ctx, "This deposit is already credited.", kb.main_menu(ctx.is_admin))
        return
    if status not in ("pending", "needs_review"):
        await respond(
            ctx, "This deposit session is closed. Start a new deposit.", kb.deposit_menu()
        )
        return
    meta = dict(payment.get("meta") or {})
    provider = binance_settings(ctx)
    if not (provider.binance_api_key and provider.binance_api_secret):
        await crypto_txid_prompt(ctx, pid)
        return

    created = payment.get("created_at")
    start_ms = None
    if isinstance(created, datetime):
        start_ms = int((created - datetime(1970, 1, 1)).total_seconds() * 1000) - 300_000
    await respond(
        ctx,
        f"Checking Binance for {meta.get('expected')} {meta.get('coin')}…",
    )
    try:
        records = await binance_api.fetch_deposits(
            ctx.client,
            provider,
            coin=str(meta.get("coin") or "USDT"),
            start_ms=start_ms,
        )
    except binance_api.BinanceAPIError as exc:
        ref = _error_ref("crypto-check", exc)
        logger.warning("crypto check failed pid=%s ref=%s: %s", pid, ref, exc)
        await send_message(
            ctx.chat_id,
            f"We could not reach the crypto service right now. Reference: {ref}\n"
            "Try again in a minute.",
            kb.crypto_session(pid, automatic=True),
        )
        return

    state, record = deposits.match_deposit(
        records,
        coin=str(meta.get("coin") or "USDT"),
        network=str(meta.get("network") or ""),
        address=str(meta.get("address") or ""),
        expected=float(meta.get("expected") or 0),
        tag=str(meta.get("tag") or ""),
    )
    if state == "credited" and record:
        await _crypto_credit(ctx, pid, meta, record)
        return
    if state == "pending":
        await send_message(
            ctx.chat_id,
            "Binance sees your deposit but it is still confirming. "
            "Check again in a few minutes.",
            kb.crypto_session(pid, automatic=True),
        )
        return
    if state == "rejected":
        await send_message(
            ctx.chat_id,
            "Binance reports this deposit as rejected or invalid, so it cannot "
            "be credited. Contact support with your transaction ID.",
            kb.main_menu(ctx.is_admin),
        )
        return
    await send_message(
        ctx.chat_id,
        "No matching deposit yet.\n\n"
        f"We look for exactly {meta.get('expected')} {meta.get('coin')} sent to "
        f"{meta.get('address')}. If you sent a different amount, contact "
        "support with the transaction ID.",
        kb.crypto_session(pid, automatic=True),
    )


async def _crypto_credit(ctx: Ctx, pid: str, meta: dict, record: dict) -> None:
    coin = str(meta.get("coin") or "USDT")
    txid = str(record.get("txId") or "").strip()
    amount = binance_api.deposit_amount(record) or 0.0
    rate = float(meta.get("rate") or 0)
    if not txid:
        await db.finish_payment(
            pid, "needs_review", reason="txid_missing", details={"record": record}
        )
        await notify_admins(
            "Crypto deposit matched but has no transaction id.\n\n"
            f"User: {ctx.name} ({ctx.user_id})\nAmount: {amount} {coin}\n"
            f"Reference: #{pid[-6:]}",
            kb.payment_review(pid),
        )
        await send_message(
            ctx.chat_id,
            "Your deposit was found but needs manual review. "
            f"Reference: #{pid[-6:]}",
        )
        return

    action, bpid = await db.claim_payment(
        "binance",
        txid.upper(),
        ctx.user_id,
        {"coin": coin, "amount": amount, "session": pid},
    )
    if action == "busy" or not bpid:
        await db.finish_payment(pid, "verified", reason="duplicate_txid")
        await send_message(
            ctx.chat_id,
            "This transaction has already been credited.",
            kb.main_menu(ctx.is_admin),
        )
        return

    credit_bdt = round(amount * rate, 2)
    # The deposit buys tokens; the Taka value only exists in the ledger.
    tokens = ctx.token_cfg.tokens_for_bdt(meta.get("requested_bdt") or credit_bdt)
    if not await db.credit_payment(bpid, ctx.user_id, tokens):
        await db.finish_payment(pid, "needs_review", reason="credit_race")
        await send_message(
            ctx.chat_id,
            "This payment is already being processed. "
            f"Reference: #{pid[-6:]}",
        )
        return
    await db.finish_payment(
        pid,
        "verified",
        reason="session_matched",
        details={
            "txid": txid,
            "amount": amount,
            "credit_bdt": credit_bdt,
            "credited_tokens": tokens,
        },
    )
    balance = await db.get_tokens(ctx.user_id)
    await send_message(
        ctx.chat_id,
        "Crypto deposit confirmed.\n\n"
        f"Received: {amount:g} {coin}\n"
        f"Credited: {fmt_tokens(tokens)}\n\n"
        f"New balance: {fmt_tokens(balance)}",
        kb.main_menu(ctx.is_admin),
    )


async def crypto_txid_prompt(ctx: Ctx, pid: str) -> None:
    payment = await db.get_payment(pid)
    if (
        not payment
        or payment.get("kind") != "crypto_session"
        or int(payment.get("telegram_id") or 0) != ctx.user_id
        or str(payment.get("status")) not in ("pending", "needs_review")
    ):
        await respond(ctx, "This deposit session is closed.", kb.deposit_menu())
        return
    await db.set_state(ctx.user_id, "crypto_txid", {"pid": pid})
    await respond(
        ctx,
        "Send the transaction ID (txId) of your deposit. An admin verifies it "
        "and credits your balance.",
        kb.cancel_only(f"d:c:cancel:{pid}"),
    )


async def crypto_cancel(ctx: Ctx, pid: str) -> None:
    payment = await db.get_payment(pid)
    if (
        payment
        and int(payment.get("telegram_id") or 0) == ctx.user_id
        and str(payment.get("status")) in ("pending", "needs_review")
    ):
        await db.finish_payment(pid, "rejected", reason="user_cancelled")
    await db.clear_state(ctx.user_id)
    await show_deposit(ctx)


async def _crypto_manual_txid(ctx: Ctx, pid: str, txid: str) -> None:
    payment = await db.get_payment(pid)
    if not payment or int(payment.get("telegram_id") or 0) != ctx.user_id:
        await db.clear_state(ctx.user_id)
        await respond(ctx, "This deposit session was not found.", kb.deposit_menu())
        return
    if str(payment.get("status")) not in ("pending", "needs_review"):
        await db.clear_state(ctx.user_id)
        await respond(ctx, "This deposit session is closed.", kb.deposit_menu())
        return
    meta = dict(payment.get("meta") or {})
    await db.finish_payment(
        pid,
        "needs_review",
        reason="manual_txid",
        details={
            "txid": txid,
            "coin": meta.get("coin"),
            "network": meta.get("network"),
            "expected": meta.get("expected"),
            "amount_bdt": meta.get("requested_bdt"),
        },
    )
    await db.clear_state(ctx.user_id)
    await send_message(
        ctx.chat_id,
        "Deposit submitted for verification.\n\n"
        f"Transaction: {txid}\n"
        f"Amount: {meta.get('expected')} {meta.get('coin')}\n"
        f"Tokens: {fmt_tokens(meta.get('tokens') or 0)}\n"
        f"Reference: #{pid[-6:]}\n\n"
        "An admin will confirm it and your tokens will be credited here.",
        kb.main_menu(ctx.is_admin),
    )
    await notify_admins(
        "Crypto deposit awaiting verification\n\n"
        f"User: {ctx.name} ({ctx.user_id})\n"
        f"Network: {deposits.network_label(meta.get('network'))}\n"
        f"Expected: {meta.get('expected')} {meta.get('coin')}\n"
        f"Taka: ৳{_money(meta.get('requested_bdt') or 0)} → "
        f"{fmt_tokens(meta.get('tokens') or 0)}\n"
        f"Transaction: {txid}\n"
        f"Reference: #{pid[-6:]}",
        kb.payment_review(pid),
    )


async def _resolve_bkash(
    ctx: Ctx, pid: str, trx: str, declared: float | None, check: bkash_api.BkashCheck
) -> None:
    details: dict = {
        "state": check.state,
        "message": check.message,
        "http_status": check.http_status,
        "attempts": check.attempts,
        "provider_sender": check.sender,
        "provider_amount": check.amount,
        "provider_time": check.timestamp.isoformat() if check.timestamp else None,
    }
    if isinstance(check.raw, (dict, list)):
        details["raw"] = check.raw

    if check.state == "verified":
        if check.sender_state == "mismatch":
            await db.finish_payment(pid, "rejected", reason="sender_mismatch", details=details)
            await send_message(
                ctx.chat_id,
                "The sender number does not match this TrxID. "
                "The payment must be sent from the number you submitted.",
            )
            return
        amount = check.amount
        tokens = ctx.token_cfg.tokens_for_bdt(amount or 0)
        if amount is None or amount <= 0:
            await db.finish_payment(
                pid, "needs_review", reason="amount_not_returned", details=details
            )
            await notify_admins(
                "bKash payment needs a manual amount\n\n"
                f"User: {ctx.name} ({ctx.user_id})\nTrxID: {trx}\n"
                f"Declared: ৳{_money(declared or 0)}\nReference: #{pid[-6:]}",
                kb.payment_review(pid),
            )
            await send_message(
                ctx.chat_id,
                "Your TrxID was verified, but the amount needs a manual check. "
                f"Reference: #{pid[-6:]} — an admin will credit your account.",
            )
            return
        if not await db.credit_payment(pid, ctx.user_id, tokens):
            await send_message(ctx.chat_id, "This payment has already been processed.")
            return
        balance = await db.get_tokens(ctx.user_id)
        note = ""
        if declared is not None and abs(declared - amount) > 0.01:
            note += f"\nYou declared ৳{_money(declared)}; verified amount is ৳{_money(amount)}."
        if check.sender_state != "match":
            note += "\nNote: the provider did not return a sender number to cross-check."
        await send_message(
            ctx.chat_id,
            f"bKash payment verified.\n"
            f"TrxID: {trx}\n"
            f"Sender: {check.sender or 'n/a'}\n"
            f"Paid: ৳{_money(amount)}\n"
            f"Credited: {fmt_tokens(tokens)}\n\n"
            f"New balance: {fmt_tokens(balance)}{note}",
            kb.main_menu(ctx.is_admin),
        )
        return

    await db.finish_payment(pid, "rejected", reason=check.state, details=details)
    if check.state == "pending":
        text = "The transaction is still pending at bKash. Wait a minute and run the command again."
    elif check.state == "not_found":
        text = (
            "We could not find that TrxID yet. Check the TrxID and try again in a "
            "minute (bKash SMS forwarding can be slow)."
        )
    elif check.state == "failed":
        text = (
            "We could not validate this payment. Check the TrxID and amount, "
            "or contact support."
        )
    elif check.state == "error":
        ref = _error_ref("bkash-check", check.message, check.http_status)
        logger.warning(
            "bKash verification error user=%s ref=%s: %s",
            ctx.user_id,
            ref,
            check.message,
        )
        text = (
            "We could not complete the verification right now.\n"
            f"Reference: {ref}\nContact support if this keeps happening."
        )
    else:
        text = "The verification service is unreachable right now. Please try again in a minute."
    await send_message(ctx.chat_id, text)
