"""FastAPI app: Telegram webhook + payment verification flows.

The webhook runs the whole flow inline before answering Telegram. On Vercel's
Python runtime post-response work is not guaranteed (waitUntil/after are
Node/Edge only), so a BackgroundTask would be killed right after the 200 and
no reply would ever reach the user. Everything stays inside maxDuration.

Commands:
  /start                     register / show balance
  /help                      usage
  /balance                   current balance
  /bkash <TrxID> <sender> [amount]        verify a bKash payment
  /binance <COIN> <txId> [amount]         verify a Binance deposit
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

import httpx

from . import bkash as bkash_api
from . import binance as binance_api
from . import db
from .config import Settings, get_settings
from .telegram import http_client, send_message

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("bot")

app = FastAPI(title="FF Glory Bot", version="1.0.0")

TRX_RE = re.compile(r"^[A-Z0-9]{6,20}$")
TXID_RE = re.compile(r"^[A-Za-z0-9:_-]{6,150}$")
COIN_RE = re.compile(r"^[A-Z0-9]{2,12}$")

BKASH_USAGE = (
    "Usage: /bkash <TrxID> <sender_number> [amount]\n"
    "Example: /bkash BFD90JRLST 01712345678 500"
)
BINANCE_USAGE = (
    "Usage: /binance <COIN> <txId> [amount]\n"
    "Example: /binance USDT 98A3EA560C6B3336D348B6C83F0F95ECE4F1F5919E94BD006E5BF3BF264FACFC 10"
)
HELP_TEXT = (
    "FF Glory Bot — commands\n\n"
    "/balance — show your current balance\n\n"
    "/bkash <TrxID> <sender_number> [amount]\n"
    "Verify a bKash payment.\n"
    "Example: /bkash BFD90JRLST 01712345678 500\n\n"
    "/binance <COIN> <txId> [amount]\n"
    "Verify a Binance crypto deposit.\n"
    "Example: /binance USDT 98A3EA560C6B3336... 10\n\n"
    "Verified payments are credited to your balance automatically."
)


@dataclass
class Ctx:
    chat_id: int
    user_id: int
    name: str
    args: list[str]
    settings: Settings
    client: httpx.AsyncClient


def _parse_amount(raw: str) -> float | None:
    try:
        value = float(str(raw).replace(",", "").replace("৳", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _money(value: float) -> str:
    return f"{value:.2f}"


# --------------------------------------------------------------------- HTTP endpoints


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


# --------------------------------------------------------------------------- updates


async def process_update(data: dict) -> None:
    try:
        message = data.get("message") or data.get("edited_message")
        if not isinstance(message, dict):
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = sender.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        parts = text.split()
        command = parts[0].split("@")[0].lower()
        handler = COMMANDS.get(command)
        if handler is None:
            await send_message(chat_id, "Unknown command. Send /help to see what I can do.")
            return
        ctx = Ctx(
            chat_id=chat_id,
            user_id=user_id,
            name=str(sender.get("first_name") or sender.get("username") or "User"),
            args=parts[1:],
            settings=get_settings(),
            client=http_client(),
        )
        await handler(ctx)
    except Exception:
        logger.exception("failed to process update")


# ------------------------------------------------------------------------- commands


async def cmd_start(ctx: Ctx) -> None:
    user, created = await db.get_or_create_user(ctx.user_id, ctx.name)
    if created:
        await send_message(
            ctx.chat_id,
            f"Welcome {ctx.name}! Your account has been created successfully.\n\n"
            "Send /help to see how to top up your balance.",
        )
        return
    balance = float(user.get("balance", 0.0) or 0.0)
    discount = int(user.get("discount_percent", 0) or 0)
    await send_message(
        ctx.chat_id,
        f"Welcome back, {ctx.name}!\nBalance: {_money(balance)} BDT\nDiscount: {discount}%",
    )


async def cmd_help(ctx: Ctx) -> None:
    await send_message(ctx.chat_id, HELP_TEXT)


async def cmd_balance(ctx: Ctx) -> None:
    balance = await db.get_balance(ctx.user_id)
    await send_message(ctx.chat_id, f"Your balance: ৳{_money(balance)}")


async def cmd_bkash(ctx: Ctx) -> None:
    s = ctx.settings
    if not s.bkash_configured:
        await send_message(
            ctx.chat_id,
            "bKash verification is not configured yet "
            "(set BKASH_API_BASE_URL and BKASH_API_KEY).",
        )
        return
    if len(ctx.args) < 2:
        await send_message(ctx.chat_id, BKASH_USAGE)
        return
    trx = re.sub(r"[^A-Za-z0-9]", "", ctx.args[0]).upper()
    if not TRX_RE.match(trx):
        await send_message(ctx.chat_id, f"Invalid TrxID.\n{BKASH_USAGE}")
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
            await send_message(ctx.chat_id, f"Invalid amount.\n{BKASH_USAGE}")
            return

    action, pid = await db.claim_payment(
        "bkash", trx, ctx.user_id, {"sender": sender, "amount": declared}
    )
    if action == "busy":
        await send_message(
            ctx.chat_id,
            "⏳ This TrxID is already being verified or has been used before. "
            "Each TrxID can only be used once.",
        )
        return

    await send_message(
        ctx.chat_id,
        "⏳ Verifying your bKash payment… this can take up to ~30 seconds.",
    )
    check = await bkash_api.verify_bkash_trxid(
        s, ctx.client, trx_id=trx, sender=sender, amount=declared
    )
    await _resolve_bkash(ctx, pid, trx, declared, check)


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
                "❌ The sender number does not match this TrxID. "
                "The payment must be sent from the number you submitted.",
            )
            return
        amount = check.amount
        if amount is None or amount <= 0:
            await db.finish_payment(
                pid, "needs_review", reason="amount_not_returned", details=details
            )
            await send_message(
                ctx.chat_id,
                f"✅ TrxID {trx} verified, but the provider did not return the amount. "
                "An admin will credit your account manually.",
            )
            return
        if not await db.credit_payment(pid, ctx.user_id, amount):
            await send_message(ctx.chat_id, "This payment has already been processed.")
            return
        balance = await db.get_balance(ctx.user_id)
        note = ""
        if declared is not None and abs(declared - amount) > 0.01:
            note += f"\nYou declared ৳{_money(declared)}; verified amount is ৳{_money(amount)}."
        if check.sender_state != "match":
            note += "\nNote: the provider did not return a sender number to cross-check."
        await send_message(
            ctx.chat_id,
            f"✅ bKash payment verified!\n"
            f"TrxID: {trx}\n"
            f"Sender: {check.sender or 'n/a'}\n"
            f"Amount: ৳{_money(amount)}\n\n"
            f"New balance: ৳{_money(balance)}{note}",
        )
        return

    await db.finish_payment(pid, "rejected", reason=check.state, details=details)
    if check.state == "pending":
        text = "⏳ The transaction is still pending at bKash. Wait a minute and run the command again."
    elif check.state == "not_found":
        text = (
            "❌ We could not find that TrxID yet. Check the TrxID and try again in a "
            "minute (bKash SMS forwarding can be slow)."
        )
    elif check.state == "failed":
        text = f"❌ Verification failed: {check.message or 'transaction is not valid.'}"
    elif check.state == "error":
        text = f"⚠️ Verification service error: {check.message}\nContact support if this keeps happening."
    else:
        text = (
            "⚠️ The verification service is unreachable right now. "
            "Please try again in a minute."
        )
    await send_message(ctx.chat_id, text)


async def cmd_binance(ctx: Ctx) -> None:
    s = ctx.settings
    if not s.binance_configured:
        await send_message(
            ctx.chat_id,
            "Binance verification is not configured yet "
            "(set BINANCE_API_KEY and BINANCE_API_SECRET).",
        )
        return
    if len(ctx.args) < 2:
        await send_message(ctx.chat_id, BINANCE_USAGE)
        return
    coin = ctx.args[0].strip().upper()
    if not COIN_RE.match(coin):
        await send_message(ctx.chat_id, f"Invalid coin.\n{BINANCE_USAGE}")
        return
    txid = ctx.args[1].strip()
    if not TXID_RE.match(txid):
        await send_message(ctx.chat_id, f"Invalid transaction hash.\n{BINANCE_USAGE}")
        return
    declared: float | None = None
    if len(ctx.args) >= 3:
        declared = _parse_amount(ctx.args[2])
        if declared is None:
            await send_message(ctx.chat_id, f"Invalid amount.\n{BINANCE_USAGE}")
            return

    action, pid = await db.claim_payment(
        "binance", txid.upper(), ctx.user_id, {"coin": coin, "amount": declared}
    )
    if action == "busy":
        await send_message(
            ctx.chat_id,
            "⏳ This transaction is already being verified or has been credited before.",
        )
        return

    await send_message(ctx.chat_id, "⏳ Checking Binance for your deposit…")
    try:
        records = await binance_api.fetch_deposits(
            ctx.client, s, coin=coin, tx_id=txid
        )
    except binance_api.BinanceAPIError as exc:
        await db.finish_payment(
            pid,
            "rejected",
            reason="binance_error",
            details={"error": str(exc), "retryable": exc.retryable},
        )
        await send_message(ctx.chat_id, f"⚠️ Binance check failed: {exc}\nPlease try again shortly.")
        return

    state, record = binance_api.classify(records, txid)
    details: dict = {"state": state, "coin": coin, "record": record}

    if state == "credited":
        amount = binance_api.deposit_amount(record)
        rate = s.rate_for(coin)
        if amount is None or amount <= 0:
            await db.finish_payment(
                pid, "needs_review", reason="amount_missing", details=details
            )
            await send_message(
                ctx.chat_id,
                f"✅ Deposit found, but the amount could not be read. "
                "An admin will credit your account manually.",
            )
            return
        if rate is None:
            await db.finish_payment(pid, "needs_review", reason="no_rate", details=details)
            await send_message(
                ctx.chat_id,
                f"✅ Binance deposit confirmed: {amount} {coin}\n"
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
            f"✅ Binance deposit confirmed!\n"
            f"{amount} {coin} × {rate:g} = ৳{_money(credit)} credited.\n\n"
            f"New balance: ৳{_money(balance)}{note}",
        )
        return

    await db.finish_payment(pid, "rejected", reason=state, details=details)
    if state == "pending":
        status = (record or {}).get("status")
        text = (
            f"⏳ The deposit is still confirming on Binance (status: {status}). "
            "Run /binance again in a few minutes."
        )
    elif state == "rejected":
        text = "❌ Binance reports this deposit as rejected or invalid, so it cannot be credited."
    else:
        text = (
            f"❌ No deposit with that transaction ID was found for {coin} yet. "
            "Deposits can take a few minutes — run /binance again shortly, and make "
            "sure the funds were sent to your Binance deposit address."
        )
    await send_message(ctx.chat_id, text)


COMMANDS = {
    "/start": cmd_start,
    "/help": cmd_help,
    "/balance": cmd_balance,
    "/bkash": cmd_bkash,
    "/binance": cmd_binance,
}