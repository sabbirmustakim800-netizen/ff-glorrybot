"""Tests for the guided deposit flows.

No network and no MongoDB: ``handlers.db`` is replaced with an in-memory fake
and the Telegram transport is replaced with a list, so the exact text a user
would see can be asserted.

The last group is a secret-leak guard: users must never see provider payloads,
exception strings or configuration variable names.
"""

from __future__ import annotations

import asyncio
import math
import re
from dataclasses import replace
from datetime import datetime

import pytest

from app import binance as binance_api
from app import bkash as bkash_api
from app import deposit_flows, deposits, handlers, ui
from app import keyboards as kb
from app.binance import build_address_params, build_deposit_params
from app.bkash import BkashCheck
from app.config import Pricing, get_settings
from app.tokens import TokenConfig

USER_ID = 555
ADMIN_ID = 900
FIXED_PID = "a" * 24


# --------------------------------------------------------------------- fakes


class FakeDB:
    def __init__(self) -> None:
        self.states: dict[int, dict] = {}
        self.settings: dict = {}
        self.features: dict = {}
        self.token_settings: dict = {}
        self.payments: dict[str, dict] = {}
        # Token balances — the only currency the bot now moves.
        self.balances: dict[int, float] = {}
        self.orders: dict[str, dict] = {}
        self.referrals: dict[int, int] = {}
        self.referral_milestones: dict[int, int] = {}
        self.referral_pairs: set[tuple[int, int]] = set()
        self.coupons: dict[str, dict] = {}
        self.user_keys: dict[int, str] = {}
        self._counter = 0

    def _new_id(self) -> str:
        self._counter += 1
        return f"{self._counter:024x}"

    async def set_state(self, user_id: int, action: str, data: dict | None = None) -> None:
        self.states[user_id] = {"action": action, "data": dict(data or {})}

    async def get_state(self, user_id: int) -> dict | None:
        return self.states.get(user_id)

    async def clear_state(self, user_id: int) -> None:
        self.states.pop(user_id, None)

    async def claim_payment(
        self, kind: str, ref: str, telegram_id: int, meta: dict | None = None
    ) -> tuple[str, str | None]:
        for pid, payment in self.payments.items():
            if payment["kind"] == kind and payment["ref"] == ref:
                return "busy", pid
        pid = self._new_id()
        self.payments[pid] = {
            "_id": pid,
            "kind": kind,
            "ref": ref,
            "telegram_id": telegram_id,
            "status": "pending",
            "meta": dict(meta or {}),
            "details": None,
            "reason": None,
            "created_at": datetime(2026, 1, 1, 12, 0, 0),
        }
        return "claimed", pid

    async def finish_payment(
        self, pid: str, status: str, reason: str | None = None, details: dict | None = None
    ) -> None:
        payment = self.payments.get(pid)
        if payment and payment["status"] in ("pending", "needs_review"):
            payment["status"] = status
            payment["reason"] = reason
            payment["details"] = details

    async def credit_payment(self, pid: str, telegram_id: int, tokens: float) -> bool:
        payment = self.payments.get(pid)
        if not payment or payment["status"] not in ("pending", "needs_review"):
            return False
        payment["status"] = "verified"
        payment["credited_tokens"] = float(tokens)
        self.balances[telegram_id] = self.balances.get(telegram_id, 0.0) + float(tokens)
        return True

    async def get_tokens(self, telegram_id: int) -> float:
        return float(self.balances.get(telegram_id, 0.0))

    async def get_user_api_key(self, telegram_id: int) -> str:
        return str(self.user_keys.get(telegram_id) or "")

    async def set_user_api_key(self, telegram_id: int, api_key: str) -> None:
        self.user_keys[int(telegram_id)] = (api_key or "").strip()

    async def add_tokens(self, telegram_id: int, amount: float) -> float:
        self.balances[telegram_id] = self.balances.get(telegram_id, 0.0) + float(amount)
        return self.balances[telegram_id]

    async def hold_tokens(self, telegram_id: int, amount: float) -> bool:
        amount = float(amount)
        if amount <= 0:
            return True
        if self.balances.get(telegram_id, 0.0) < amount:
            return False
        self.balances[telegram_id] -= amount
        return True

    async def get_user(self, telegram_id: int) -> dict | None:
        return {
            "telegram_id": telegram_id,
            "tokens": self.balances.get(telegram_id, 0.0),
            "referrals": self.referrals.get(telegram_id, 0),
            "referral_milestones": self.referral_milestones.get(telegram_id, 0),
            "discount_percent": 0,
        }

    async def get_or_create_user(self, telegram_id: int, name: str) -> tuple[dict, bool]:
        self.balances.setdefault(telegram_id, 0.0)
        return ({"telegram_id": telegram_id, "tokens": self.balances[telegram_id]}, False)

    async def get_bot_settings(self) -> dict:
        return {
            "pricing": {},
            "api": {},
            "payment": dict(self.settings),
            "features": dict(self.features),
            "tokens": dict(self.token_settings),
        }

    async def set_token_settings(self, **values) -> dict:
        self.token_settings.update({k: v for k, v in values.items() if v is not None})
        return await self.get_bot_settings()

    async def set_plan_cost(self, plan_id: str, tokens: int) -> dict:
        plans = dict(self.token_settings.get("plan_costs") or {})
        plans[(plan_id or "").strip().lower()] = int(tokens)
        self.token_settings["plan_costs"] = plans
        return await self.get_bot_settings()

    async def reset_token_settings(self) -> dict:
        self.token_settings.clear()
        return await self.get_bot_settings()

    async def referral_stats(self, telegram_id: int) -> dict:
        return {
            "invites": self.referrals.get(telegram_id, 0),
            "milestones": self.referral_milestones.get(telegram_id, 0),
            "referred_by": None,
        }

    async def register_referral(
        self, referrer_id: int, referred_id: int, *, reward: int, goal: int
    ) -> dict:
        pair = (int(referrer_id), int(referred_id))
        if referrer_id == referred_id or pair in self.referral_pairs:
            return {
                "credited": False,
                "invites": self.referrals.get(referrer_id, 0),
                "milestones_due": 0,
            }
        self.referral_pairs.add(pair)
        self.referrals[referrer_id] = self.referrals.get(referrer_id, 0) + 1
        self.balances[referrer_id] = self.balances.get(referrer_id, 0.0) + float(reward)
        invites = self.referrals[referrer_id]
        reached = invites // max(1, goal)
        awarded = self.referral_milestones.get(referrer_id, 0)
        due = max(0, reached - awarded)
        if due:
            self.referral_milestones[referrer_id] = reached
        return {"credited": True, "invites": invites, "milestones_due": due}

    async def create_coupon(
        self, telegram_id: int, tokens: int, *, source: str = "referral", code: str = ""
    ) -> dict:
        self._counter += 1
        coupon = {
            "code": code or f"TEST-{self._counter:04X}",
            "telegram_id": int(telegram_id),
            "tokens": int(tokens),
            "source": source,
            "status": "active",
        }
        self.coupons[coupon["code"]] = coupon
        return coupon

    async def list_user_coupons(self, telegram_id: int, limit: int = 10) -> list[dict]:
        return [
            dict(coupon)
            for coupon in self.coupons.values()
            if coupon["telegram_id"] == int(telegram_id)
        ][:limit]

    async def redeem_coupon(self, code: str, telegram_id: int) -> dict:
        coupon = self.coupons.get((code or "").strip().upper())
        if not coupon:
            return {"ok": False, "tokens": 0, "reason": "unknown"}
        if coupon["telegram_id"] != int(telegram_id):
            return {"ok": False, "tokens": 0, "reason": "not_yours"}
        if coupon["status"] != "active":
            return {"ok": False, "tokens": 0, "reason": "used"}
        coupon["status"] = "redeemed"
        self.balances[telegram_id] = self.balances.get(telegram_id, 0.0) + coupon["tokens"]
        return {"ok": True, "tokens": coupon["tokens"], "reason": "redeemed"}

    async def get_payment(self, pid: str) -> dict | None:
        return self.payments.get(pid)

    async def get_balance(self, telegram_id: int) -> float:
        return float(self.balances.get(telegram_id, 0.0))

    async def set_payment_settings(self, **values) -> dict:
        self.settings.update({k: v for k, v in values.items() if v is not None})
        return dict(self.settings)

    async def set_feature(self, name: str, enabled: bool) -> dict:
        self.features[name] = bool(enabled)
        return {"features": dict(self.features)}

    async def list_payments(self, kind=None, status=None, limit=10):
        values = list(self.payments.values())
        if kind:
            values = [p for p in values if p["kind"] == kind]
        if status:
            values = [p for p in values if p["status"] == status]
        return values[:limit]


class Harness:
    def __init__(self, fake: FakeDB) -> None:
        self.fake = fake
        self.sent: list[tuple[int, str, dict | None]] = []
        self.admin: list[tuple[str, dict | None]] = []
        self.records: list[dict] = []
        self.address = {"address": "TXyzDepositAddress", "tag": "", "url": ""}
        self.check: BkashCheck | None = None

    # --- assertions helpers ---
    def texts(self, chat_id: int | None = None) -> list[str]:
        return [text for cid, text, _ in self.sent if chat_id is None or cid == chat_id]

    def last(self) -> str:
        return self.sent[-1][1]

    def all_callbacks(self) -> list[str]:
        data: list[str] = []
        for _, _, markup in self.sent:
            for row in (markup or {}).get("inline_keyboard", []):
                data += [button["callback_data"] for button in row]
        for _, markup in self.admin:
            for row in (markup or {}).get("inline_keyboard", []):
                data += [button["callback_data"] for button in row]
        return data


def _api(**overrides) -> dict:
    base = {
        "rates": {"USDT": 110.0},
        "ffglory_api_key": "",
        "ffglory_master_key": "",
        "ffglory_base_url": "https://ffglory.pro",
        "usdt_trc20": "TPanelTronAddress",
        "usdt_bep20": "0xPanelBscAddress",
        "bkash_base_url": "",
        "bkash_api_key": "",
        "binance_api_key": "",
        "binance_api_secret": "",
    }
    base.update(overrides)
    return base


def make_ctx(
    harness: Harness,
    *,
    user_id: int = USER_ID,
    is_admin: bool = False,
    api: dict | None = None,
    payment: dict | None = None,
    features: dict | None = None,
    token_cfg: TokenConfig | None = None,
) -> handlers.Ctx:
    settings = replace(
        get_settings(),
        admin_ids=(USER_ID, ADMIN_ID),
        bkash_receiver_number="01712345678",
        bot_username="ffglory_test_bot",
    )
    return handlers.Ctx(
        chat_id=user_id,
        user_id=user_id,
        name="Tester",
        args=[],
        settings=settings,
        client=None,  # type: ignore[arg-type]  # transport is faked below
        api=api or _api(),
        pricing=Pricing(),
        payment=payment if payment is not None else dict(harness.fake.settings),
        features=dict(features or {}),
        token_cfg=token_cfg or TokenConfig(),
        is_admin=is_admin,
    )


@pytest.fixture
def harness(monkeypatch) -> Harness:
    fake = FakeDB()
    h = Harness(fake)

    for name in (
        "set_state",
        "get_state",
        "clear_state",
        "claim_payment",
        "finish_payment",
        "credit_payment",
        "get_payment",
        "get_tokens",
        "add_tokens",
        "hold_tokens",
        "get_user",
        "get_or_create_user",
        "get_user_api_key",
        "set_user_api_key",
        "referral_stats",
        "register_referral",
        "create_coupon",
        "list_user_coupons",
        "redeem_coupon",
        "set_payment_settings",
        "set_feature",
        "set_token_settings",
        "set_plan_cost",
        "reset_token_settings",
        "get_bot_settings",
        "list_payments",
    ):
        monkeypatch.setattr(handlers.db, name, getattr(fake, name))

    async def send_message(chat_id: int, text: str, markup: dict | None = None) -> bool:
        h.sent.append((chat_id, text, markup))
        return True

    async def edit_message(
        chat_id: int, message_id: int, text: str, markup: dict | None = None
    ) -> bool:
        h.sent.append((chat_id, text, markup))
        return True

    async def answer_callback(callback_id: str, text: str | None = None) -> bool:
        return True

    async def notify_admins(text: str, markup: dict | None = None) -> int:
        h.admin.append((text, markup))
        return 1

    async def send_media(
        chat_id: int, kind: str, file_id: str, caption: str = "", markup: dict | None = None
    ) -> bool:
        h.sent.append((chat_id, f"[{kind}] {caption}", markup))
        return True

    async def fetch_deposit_address(client, settings, *, coin, network):
        return h.address

    async def fetch_deposits(client, settings, *, coin, tx_id=None, start_ms=None):
        return h.records

    async def verify_bkash_trxid(settings, client, *, trx_id, sender, amount):
        assert h.check is not None, "test must set harness.check"
        return h.check

    monkeypatch.setattr(handlers, "send_message", send_message)
    if hasattr(handlers, "answer_callback"):
        monkeypatch.setattr(handlers, "answer_callback", answer_callback)
    if hasattr(handlers, "notify_admins"):
        monkeypatch.setattr(handlers, "notify_admins", notify_admins)
    # Every module binds the transport itself, so patch each one it imported.
    from app import admin_panel, basic, commands, premium, referral, state_machine

    for module in (
        handlers,
        basic,
        premium,
        referral,
        commands,
        admin_panel,
        state_machine,
        deposit_flows,
        ui,
    ):
        if hasattr(module, "send_message"):
            monkeypatch.setattr(module, "send_message", send_message)
        if hasattr(module, "edit_message"):
            monkeypatch.setattr(module, "edit_message", edit_message)
        if hasattr(module, "notify_admins"):
            monkeypatch.setattr(module, "notify_admins", notify_admins)
    monkeypatch.setattr(state_machine, "send_media", send_media)
    monkeypatch.setattr(binance_api, "fetch_deposit_address", fetch_deposit_address)
    monkeypatch.setattr(binance_api, "fetch_deposits", fetch_deposits)
    monkeypatch.setattr(bkash_api, "verify_bkash_trxid", verify_bkash_trxid)
    return h


def complete_bkash_form(h: Harness, ctx: handlers.Ctx, *, amount: str = "500") -> None:
    asyncio.run(handlers.bkash_start(ctx))
    asyncio.run(
        handlers.handle_state_input(ctx, asyncio.run(handlers.db.get_state(USER_ID)), "01712345678")
    )
    asyncio.run(
        handlers.handle_state_input(ctx, asyncio.run(handlers.db.get_state(USER_ID)), "BFD90JRLST")
    )
    asyncio.run(handlers.handle_state_input(ctx, asyncio.run(handlers.db.get_state(USER_ID)), amount))
    asyncio.run(handlers.bkash_submit(ctx))


# --------------------------------------------------------------- bKash form


def test_guided_bkash_form_submits_for_review(harness):
    ctx = make_ctx(harness)
    asyncio.run(handlers.bkash_start(ctx))
    assert harness.fake.states[USER_ID]["action"] == "bkash_sender"
    assert "01712345678" in harness.last()

    asyncio.run(
        handlers.handle_state_input(
            ctx, harness.fake.states[USER_ID], "01712345678"
        )
    )
    assert harness.fake.states[USER_ID]["action"] == "bkash_trx"

    asyncio.run(
        handlers.handle_state_input(ctx, harness.fake.states[USER_ID], "BFD90JRLST")
    )
    assert harness.fake.states[USER_ID]["action"] == "bkash_amount"

    asyncio.run(handlers.handle_state_input(ctx, harness.fake.states[USER_ID], "500"))
    assert harness.fake.states[USER_ID]["action"] == "bkash_confirm"
    assert "BFD90JRLST" in harness.last()

    asyncio.run(handlers.bkash_submit(ctx))
    payment = next(iter(harness.fake.payments.values()))
    assert payment["kind"] == "bkash"
    assert payment["status"] == "needs_review"  # provider not configured
    assert payment["meta"]["sender"] == "01712345678"
    assert payment["details"]["amount_bdt"] == 500.0
    # User gets a clean confirmation with a reference, admin gets the review buttons.
    assert "Reference" in harness.last()
    assert harness.admin and "BFD90JRLST" in harness.admin[0][0]
    assert any("pay:ok:" in data for data in harness.all_callbacks())


def test_bkash_form_rejects_bad_input_without_losing_progress(harness):
    ctx = make_ctx(harness)

    async def scenario():
        await handlers.bkash_start(ctx)
        state = await handlers.db.get_state(USER_ID)
        await handlers.handle_state_input(ctx, state, "12345")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_sender"

        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "01812345678")
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "**bad**")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_trx"

        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "BFD90JRLST")
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "10")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_amount"

    asyncio.run(scenario())
    assert any("minimum deposit" in text.lower() for text in harness.texts())
    assert not harness.fake.payments  # nothing submitted


def test_bkash_deposit_is_taka_only(harness):
    """bKash screens and amount input must stay in Taka — no dollars."""
    ctx = make_ctx(harness)

    async def scenario():
        await handlers.show_bkash_deposit(ctx)
        await handlers.bkash_start(ctx)
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "01712345678")
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "BFD90JRLST")
        # A dollar/crypto amount is refused and the session keeps its progress.
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "$5")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_amount"
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "5 USDT")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_amount"
        await handlers.handle_state_input(ctx, await handlers.db.get_state(USER_ID), "500")
        assert (await handlers.db.get_state(USER_ID))["action"] == "bkash_confirm"

    asyncio.run(scenario())

    for text in harness.texts():
        assert "$" not in text, text
        assert "USDT" not in text and "USD" not in text, text
    assert any("Taka only" in text for text in harness.texts())
    assert "৳500.00 BDT" in harness.last()
    # Quick-amount buttons are labelled in Taka too.
    labels = [
        button["text"]
        for _, _, markup in harness.sent
        for row in (markup or {}).get("inline_keyboard", [])
        for button in row
        if button.get("callback_data", "").startswith("d:b:amt:")
    ]
    assert labels and all(label.startswith("৳") for label in labels)


def test_bkash_provider_error_never_leaks_details(harness):
    ctx = make_ctx(harness, api=_api(bkash_base_url="https://provider", bkash_api_key="k"))
    harness.check = BkashCheck(
        state="error",
        message="upstream https://provider failed: token=abcdef SECRET",
        http_status=502,
    )
    complete_bkash_form(harness, ctx)

    joined = "\n".join(harness.texts())
    assert "SECRET" not in joined
    assert "provider failed" not in joined
    assert "ERR-" in joined


def test_bkash_verified_payment_credits_balance(harness):
    ctx = make_ctx(harness, api=_api(bkash_base_url="https://provider", bkash_api_key="k"))
    harness.check = BkashCheck(
        state="verified", amount=500.0, sender="01712345678", sender_state="match"
    )
    complete_bkash_form(harness, ctx)

    assert harness.fake.balances[USER_ID] == 500.0
    payment = next(iter(harness.fake.payments.values()))
    assert payment["status"] == "verified"


# ------------------------------------------------------------ crypto sessions


def test_crypto_session_uses_dynamic_address_and_exact_amount(harness):
    ctx = make_ctx(
        harness,
        api=_api(binance_api_key="k", binance_api_secret="s"),
    )
    asyncio.run(handlers.crypto_start(ctx, "USDT", "trc20"))
    assert harness.fake.states[USER_ID]["action"] == "crypto_amount"

    asyncio.run(handlers.crypto_quick_amount(ctx, "trc20", "500"))
    session = next(iter(harness.fake.payments.values()))
    assert session["kind"] == "crypto_session"
    assert session["meta"]["address"] == "TXyzDepositAddress"
    expected = session["meta"]["expected"]
    assert 4.56 <= expected <= 5.54  # 500 / 110 plus the unique decimal suffix
    assert round(expected, 2) == expected
    assert "TXyzDepositAddress" in harness.last()
    assert f"{expected:g} USDT" in harness.last()
    assert all(len(data.encode()) <= 64 for data in harness.all_callbacks())


def test_crypto_check_credits_once(harness):
    ctx = make_ctx(harness, api=_api(binance_api_key="k", binance_api_secret="s"))
    asyncio.run(handlers.crypto_start(ctx, "USDT", "trc20"))
    asyncio.run(handlers.crypto_quick_amount(ctx, "trc20", "500"))
    pid = next(iter(harness.fake.payments))
    expected = harness.fake.payments[pid]["meta"]["expected"]

    harness.records = [
        {
            "coin": "USDT",
            "amount": str(expected),
            "address": "TXyzDepositAddress",
            "status": 1,
            "txId": "TXID123456",
        }
    ]
    asyncio.run(handlers.crypto_check(ctx, pid))
    # The user asked for a ৳500 deposit, so they get 500 tokens at 1:1 — the
    # unique reference amount (the extra fraction) is only there for matching.
    credit = math.floor(500.0)
    assert harness.fake.balances[USER_ID] == pytest.approx(credit)
    assert harness.fake.payments[pid]["status"] == "verified"

    asyncio.run(handlers.crypto_check(ctx, pid))  # duplicate tap
    assert harness.fake.balances[USER_ID] == pytest.approx(credit)
    assert "already credited" in harness.last().lower()


def test_crypto_session_not_matched_on_wrong_amount(harness):
    ctx = make_ctx(harness, api=_api(binance_api_key="k", binance_api_secret="s"))
    asyncio.run(handlers.crypto_start(ctx, "USDT", "trc20"))
    asyncio.run(handlers.crypto_quick_amount(ctx, "trc20", "500"))
    pid = next(iter(harness.fake.payments))
    harness.records = [
        {"coin": "USDT", "amount": "4.55", "address": "TXyzDepositAddress", "status": 1, "txId": "X1"}
    ]
    asyncio.run(handlers.crypto_check(ctx, pid))
    assert USER_ID not in harness.fake.balances
    assert "no matching deposit" in harness.last().lower()


def test_crypto_without_binance_asks_for_txid(harness):
    ctx = make_ctx(harness)  # no Binance keys → panel address, manual verification
    asyncio.run(handlers.crypto_start(ctx, "USDT", "trc20"))
    asyncio.run(handlers.crypto_quick_amount(ctx, "trc20", "500"))
    pid = next(iter(harness.fake.payments))
    asyncio.run(handlers.crypto_check(ctx, pid))
    assert harness.fake.states[USER_ID]["action"] == "crypto_txid"

    asyncio.run(
        handlers.handle_state_input(ctx, harness.fake.states[USER_ID], "ABC123TXID")
    )
    assert harness.fake.payments[pid]["status"] == "needs_review"
    assert harness.admin and "ABC123TXID" in harness.admin[-1][0]


# ------------------------------------------------------------------ admin panel


def test_admin_sets_bkash_number_and_user_sees_it(harness):
    admin_ctx = make_ctx(harness, user_id=ADMIN_ID, is_admin=True)
    asyncio.run(handlers.admin_payment(ctx=admin_ctx))
    assert "01712345678" in harness.last()

    async def set_number():
        await handlers.admin_payment_action(admin_ctx, "bkash")
        state = await handlers.db.get_state(ADMIN_ID)
        assert state["action"] == "admin_bkash_number"
        await handlers.handle_state_input(admin_ctx, state, "01812345678")

    asyncio.run(set_number())
    assert harness.fake.settings["bkash_number"] == "01812345678"

    user_ctx = make_ctx(harness, payment=dict(harness.fake.settings))
    asyncio.run(handlers.show_bkash_deposit(user_ctx))
    assert "01812345678" in harness.last()


def test_admin_credit_button_credits_review_payment(harness):
    ctx = make_ctx(harness)
    complete_bkash_form(harness, ctx)
    pid = next(iter(harness.fake.payments))

    admin_ctx = make_ctx(harness, user_id=ADMIN_ID, is_admin=True)
    asyncio.run(handlers.admin_payment_review(admin_ctx, pid))
    assert "500.00" in harness.last()

    asyncio.run(handlers.admin_payment_decide(admin_ctx, pid, approve=True))
    assert harness.fake.balances[USER_ID] == 500.0
    assert harness.fake.payments[pid]["status"] == "verified"


def test_admin_decline_notifies_user(harness):
    ctx = make_ctx(harness)
    complete_bkash_form(harness, ctx)
    pid = next(iter(harness.fake.payments))
    admin_ctx = make_ctx(harness, user_id=ADMIN_ID, is_admin=True)
    asyncio.run(handlers.admin_payment_decide(admin_ctx, pid, approve=False))
    assert harness.fake.payments[pid]["status"] == "rejected"
    assert harness.fake.balances.get(USER_ID, 0.0) == 0.0
    assert any("could not be verified" in text for text in harness.texts(USER_ID))


# ---------------------------------------------------------------------- logic


def test_deposit_input_helpers():
    assert deposits.clean_trxid("bfd-90 jrlst") == "BFD90JRLST"
    assert deposits.clean_trxid("abc") is None
    assert deposits.parse_amount("1,500") == (1500.0, None)
    assert deposits.parse_amount("৳500") == (500.0, None)
    assert deposits.parse_amount("0")[1] is not None
    assert deposits.parse_amount("10")[1] is not None  # below the minimum
    assert deposits.parse_amount("abc")[1] is not None
    assert deposits.reference_amount(10.0, fraction=37) == 10.37
    assert deposits.reference_amount(10.0, fraction=0) == 10.01
    assert deposits.reference_amount(10.0, fraction=999) == 10.99
    assert deposits.amount_matches(10.37, "10.37")
    assert not deposits.amount_matches(10.37, "10.38")


def test_match_deposit_requires_address_and_exact_amount():
    record = {
        "coin": "USDT",
        "amount": "10.37",
        "address": "TXyz",
        "status": 1,
        "txId": "abc",
    }
    assert deposits.match_deposit(
        [record], coin="USDT", network="trc20", address="TXyz", expected=10.37
    )[0] == "credited"
    # Tron addresses are case-sensitive; a different case is a different address.
    assert deposits.match_deposit(
        [record], coin="USDT", network="trc20", address="txyz", expected=10.37
    )[0] == "not_found"
    assert deposits.match_deposit(
        [record], coin="USDT", network="trc20", address="TXyz", expected=10.38
    )[0] == "not_found"
    assert deposits.match_deposit(
        [{**record, "status": 2}], coin="USDT", network="trc20", address="TXyz", expected=10.37
    )[0] == "rejected"
    # BEP20 addresses are hex → case-insensitive.
    assert deposits.match_deposit(
        [{**record, "address": "0xAbC"}],
        coin="USDT",
        network="bep20",
        address="0xabc",
        expected=10.37,
    )[0] == "credited"


def test_binance_address_params_and_window():
    params = build_address_params(coin="USDT", network="TRX", now_ms=1_700_000_000_000)
    assert params["coin"] == "USDT"
    assert params["network"] == "TRX"
    deposit_params = build_deposit_params(coin="USDT", tx_id="abc", now_ms=1_700_000_000_000)
    assert deposit_params["txId"] == "abc"
    recent = build_deposit_params(
        coin="USDT", start_ms=1_700_000_000_000 - 3_600_000, now_ms=1_700_000_000_000
    )
    assert "txId" not in recent
    assert recent["endTime"] - recent["startTime"] >= 3_600_000


def test_every_callback_fits_telegram_limit():
    markups = [
        kb.main_menu(True),
        kb.admin_menu(),
        kb.admin_payment_menu(),
        kb.deposit_menu(),
        kb.deposit_actions(),
        kb.amount_choices("d:b:amt:", [100, 200, 500, 1000, 2000]),
        kb.amount_choices("d:c:amt:trc20:", [100, 200]),
        kb.bkash_confirm(),
        kb.crypto_networks(),
        kb.crypto_session(FIXED_PID, automatic=True),
        kb.crypto_session(FIXED_PID, automatic=False),
        kb.payment_review(FIXED_PID),
        kb.order_actions(FIXED_PID),
        kb.cancel_only(f"d:c:cancel:{FIXED_PID}"),
    ]
    for markup in markups:
        for row in markup["inline_keyboard"]:
            for button in row:
                assert len(button["callback_data"].encode()) <= 64, button
                assert button["text"].strip()


def test_action_buttons_start_with_an_emoji():
    """Every action button leads with one emoji, then plain English."""
    emoji = re.compile(
        "^[\U0001F300-\U0001FAFF\u2190-\u21FF\u2600-\u27BF\u2B00-\u2BFF]"
    )
    markups = [
        kb.main_menu(True),
        kb.back_to_main(True),
        kb.admin_menu(),
        kb.admin_pricing_menu(),
        kb.api_settings_menu(["FFGLORY_API_KEY"]),
        kb.console_menu(),
        kb.deposit_menu(),
        kb.deposit_actions(),
        kb.bkash_confirm(),
        kb.crypto_networks(),
        kb.crypto_session(FIXED_PID, automatic=True),
        kb.crypto_session(FIXED_PID, automatic=False),
        kb.payment_review(FIXED_PID),
        kb.admin_payment_menu(),
        kb.account_menu(False),
        kb.account_menu(True),
        kb.premium_intro(),
        kb.premium_confirm(),
        kb.order_actions(FIXED_PID),
        kb.order_review(FIXED_PID),
        kb.region_menu([("bd", "Bangladesh (bd)")]),
        kb.insufficient_balance(),
        kb.cancel_only("m:main"),
    ]
    for markup in markups:
        for row in markup["inline_keyboard"]:
            for button in row:
                assert emoji.match(button["text"]), button["text"]
                assert len(button["text"]) <= 40, button["text"]


def test_amount_buttons_stay_clean_taka_labels():
    markup = kb.amount_choices("d:b:amt:", [100, 500])
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
    assert labels[:2] == ["৳100", "৳500"]


def test_user_api_switch_hides_linking():
    on = [b["callback_data"] for row in kb.account_menu(False, True)["inline_keyboard"] for b in row]
    off = [b["callback_data"] for row in kb.account_menu(False, False)["inline_keyboard"] for b in row]
    assert "ac:key" in on
    assert "ac:key" not in off


def test_user_api_switch_blocks_linking(harness: Harness) -> None:
    ctx = make_ctx(harness, features={"user_api_enabled": False})
    assert ui.user_api_enabled(ctx) is False
    assert asyncio.run(ui.ensure_user_key(ctx)) == ""
    asyncio.run(handlers.account_key_action(ctx))
    assert "turned off by the admin" in harness.last()
    assert asyncio.run(handlers.db.get_state(USER_ID)) in (None, {})


def test_user_api_switch_defaults_to_on(harness: Harness) -> None:
    ctx = make_ctx(harness)
    assert ui.user_api_enabled(ctx) is True
    assert ui.user_api_enabled(make_ctx(harness, features={"user_api_enabled": True})) is True


def test_admin_toggle_flips_the_user_api_switch(harness: Harness) -> None:
    ctx = make_ctx(harness, is_admin=True)
    asyncio.run(handlers.admin_toggle_user_api(ctx))
    assert harness.fake.features == {"user_api_enabled": False}
    assert ctx.features == {"user_api_enabled": False}
    assert "now off" in harness.last()
    assert "a:api:userkeys" in harness.all_callbacks()

    asyncio.run(handlers.admin_toggle_user_api(ctx))
    assert harness.fake.features == {"user_api_enabled": True}
    assert "now on" in harness.last()


def test_admin_api_menu_shows_the_switch(harness: Harness) -> None:
    ctx = make_ctx(harness, is_admin=True)
    asyncio.run(handlers.admin_api_menu(ctx))
    assert "User API keys (everyone): on" in harness.last()
    assert "a:api:userkeys" in harness.all_callbacks()
