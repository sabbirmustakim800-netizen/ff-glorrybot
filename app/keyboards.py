"""Inline keyboard builders.

Buttons carry a short label with one leading emoji so the menu feels alive;
callback data stays plain ASCII and short (Telegram caps it at 64 bytes).
"""

from __future__ import annotations

from typing import Any


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [row for row in rows if row]}


def remove() -> dict[str, bool]:
    return {"remove_keyboard": True}


BACK = "🔙 Back"
MAIN_MENU = "🏠 Main Menu"
ADMIN_PANEL = "🛡 Admin Panel"


# ------------------------------------------------------------- reply keyboard
#
# A permanent keyboard at the bottom of the chat, so nobody has to type a
# command. Labels are matched by ``app.handlers`` after stripping the emoji and
# lower-casing, so changing the emoji never breaks navigation.

REPLY_ROWS = [
    (("🎮 Basic Services", "basic"), ("💎 Premium", "premium")),
    (("💳 Deposit", "deposit"), ("👤 Account", "account")),
    (("🎁 Referral", "referral"), ("🎟 Coupons", "coupons")),
    (("🌍 Categories", "categories"), ("🔑 APIs", "apis")),
    (("🏠 Menu", "menu"), ("✖️ Cancel", "cancel")),
]


def reply_menu(is_admin: bool = False) -> dict[str, Any]:
    """Persistent bottom keyboard (ReplyKeyboardMarkup)."""
    rows: list[list[dict[str, str]]] = []
    for row in REPLY_ROWS:
        rows.append([{"text": label} for label, _ in row])
    if is_admin:
        rows.insert(2, [{"text": "🛡 Admin Panel"}])
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": True,
        "input_field_placeholder": "Pick a button below…",
    }


def reply_map(is_admin: bool = False) -> dict[str, str]:
    """Normalised reply label → action name."""
    mapping = {label: action for row in REPLY_ROWS for label, action in row}
    if is_admin:
        mapping["🛡 Admin Panel"] = "admin"
    return mapping


def main_menu(is_admin: bool = False) -> dict[str, Any]:
    rows = [
        [button("🎮 Basic Services (Auto API)", "m:basic")],
        [button("💎 Premium / Secret Bulk", "m:premium")],
        [button("💳 Deposit / Top-up (bKash / Crypto)", "m:deposit")],
        [button("👤 My Account / Tokens", "m:account")],
        [button("🎁 Referral Program", "m:referral")],
        [button("🎟 My Coupons", "m:coupons")],
        [button("⚙️ Settings / API Key", "m:settings")],
    ]
    if is_admin:
        rows.append([button(ADMIN_PANEL, "a:menu")])
    return keyboard(rows)


def back_to_main(is_admin: bool = False) -> dict[str, Any]:
    rows = [[button(MAIN_MENU, "m:main")]]
    if is_admin:
        rows.append([button(ADMIN_PANEL, "a:menu")])
    return keyboard(rows)


def admin_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("📦 Pending Premium Orders", "a:pending")],
            [button("🪙 Token Settings", "a:tk")],
            [button("📈 Profit Margin / Rates", "a:pricing")],
            [button("💳 Payment Settings", "a:pay")],
            [button("🔑 API Settings", "a:api")],
            [button("📊 System Stats", "a:stats")],
            [button("📣 Broadcast Message", "a:broadcast")],
            [button(MAIN_MENU, "m:main")],
        ]
    )


def token_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("💱 Taka → Token Rate", "a:tk:rate")],
            [button("🎮 Basic Plan Cost", "a:tk:basic")],
            [button("💎 Premium Cost", "a:tk:premium")],
            [button("📦 Per-Plan Costs", "a:tk:plans")],
            [button("🎁 Referral Reward", "a:tk:ref")],
            [button("🎯 Referral Goal", "a:tk:goal")],
            [button("🎟 Referral Coupon", "a:tk:coupon")],
            [button(ADMIN_PANEL, "a:menu")],
        ]
    )


def plan_costs_menu(plans: list[tuple[str, str]], current: dict[str, int]) -> dict[str, Any]:
    rows = [
        [button(f"🪙 {current.get(code, 0):,} — {label}", f"a:tk:plan:{code}")]
        for code, label in plans
    ]
    rows.append([button("♻️ Reset To Defaults", "a:tk:reset")])
    rows.append([button(ADMIN_PANEL, "a:tk")])
    return keyboard(rows)


def admin_pricing_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("📈 Set Margin Percent", "a:pr:margin")],
            [button("➕ Set Fixed Markup", "a:pr:fixed")],
            [button("🟢 Set Basic Base Price", "a:pr:basic")],
            [button("💎 Set Premium Base Price", "a:pr:premium")],
            [button("♻️ Reset To Defaults", "a:pr:reset")],
            [button(ADMIN_PANEL, "a:menu")],
        ]
    )


def api_settings_menu(keys: list[str], user_api_enabled: bool = True) -> dict[str, Any]:
    rows = [[button(f"🔑 {key}", f"a:api:set:{key}")] for key in keys]
    toggle = "🟢 User API Keys: On" if user_api_enabled else "🔴 User API Keys: Off"
    rows.append([button(toggle, "a:api:userkeys")])
    rows.append([button("🧪 API Console", "a:console")])
    rows.append([button(ADMIN_PANEL, "a:menu")])
    return keyboard(rows)


def console_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("👤 Account (me)", "a:c:me"), button("📈 Pricing", "a:c:pricing")],
            [button("👥 My Groups", "a:c:groups"), button("🎟 Coupons", "a:c:coupons")],
            [button("🎟 Redeemed Coupons", "a:c:redeemed")],
            [button("🧾 Transactions", "a:c:tx"), button("📜 Activity Log", "a:c:log")],
            [button("🔔 Notifications", "a:c:notifs")],
            [button("🔑 API Settings", "a:api")],
        ]
    )


def deposit_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("💚 bKash (BDT)", "d:bkash")],
            [button("🪙 Crypto / USDT", "d:crypto")],
            [button(BACK, "m:main")],
        ]
    )


def deposit_actions() -> dict[str, Any]:
    return keyboard(
        [
            [button("💚 Start bKash Deposit", "d:b:start")],
            [button("🪙 Crypto / USDT", "d:crypto")],
            [button(BACK, "m:deposit")],
        ]
    )


def amount_choices(prefix: str, values: list[int]) -> dict[str, Any]:
    """Two quick-amount buttons per row; prefix + value stays under 64 bytes."""
    rows = []
    for index in range(0, len(values), 2):
        pair = values[index : index + 2]
        rows.append([button(f"৳{value}", f"{prefix}{value}") for value in pair])
    rows.append([button(BACK, "m:deposit")])
    return keyboard(rows)


def bkash_confirm() -> dict[str, Any]:
    return keyboard(
        [
            [button("✅ Confirm & Verify", "d:b:go")],
            [button("✖️ Cancel", "d:b:cancel")],
        ]
    )


def crypto_networks() -> dict[str, Any]:
    return keyboard(
        [
            [button("🪙 USDT TRC20 (Tron)", "d:c:usdt:trc20")],
            [button("🪙 USDT BEP20 (BNB Chain)", "d:c:usdt:bep20")],
            [button(BACK, "m:deposit")],
        ]
    )


def crypto_session(pid: str, *, automatic: bool) -> dict[str, Any]:
    action = f"d:c:check:{pid}" if automatic else f"d:c:tx:{pid}"
    label = "✅ I have paid — check now" if automatic else "🧾 Submit Transaction ID"
    return keyboard(
        [
            [button(label, action)],
            [button("✖️ Cancel Deposit", f"d:c:cancel:{pid}")],
            [button(BACK, "m:deposit")],
        ]
    )


def payment_review(pid: str) -> dict[str, Any]:
    return keyboard(
        [
            [button("✅ Credit Balance", f"pay:ok:{pid}"), button("⛔ Decline", f"pay:no:{pid}")],
            [button(ADMIN_PANEL, "a:menu")],
        ]
    )


def admin_payment_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("📱 Set bKash Number", "a:pay:bkash")],
            [button("💵 Set Minimum Deposit", "a:pay:min")],
            [button("🧾 Review Deposits", "a:pay:pending")],
            [button(ADMIN_PANEL, "a:menu")],
        ]
    )


def account_menu(has_key: bool, api_enabled: bool = True) -> dict[str, Any]:
    rows = [[button("🧾 Order History", "ac:history")]]
    rows.append([button("🎁 Referral Program", "m:referral")])
    rows.append([button("🎟 My Coupons", "m:coupons")])
    if api_enabled:
        rows.append([button("🔑 Remove API Key" if has_key else "➕ Set My API Key", "ac:key")])
    rows.append([button(BACK, "m:main")])
    return keyboard(rows)


def premium_intro() -> dict[str, Any]:
    return keyboard(
        [
            [button("🚀 Start Premium Order", "p:start")],
            [button(BACK, "m:main")],
        ]
    )


def premium_confirm() -> dict[str, Any]:
    return keyboard(
        [
            [button("✅ Confirm Order", "p:confirm")],
            [button("✖️ Cancel", "p:cancel")],
        ]
    )


def order_actions(order_id: str) -> dict[str, Any]:
    return keyboard(
        [
            [
                button("✅ Approve & Complete", f"o:ok:{order_id}"),
                button("↩️ Reject & Refund", f"o:no:{order_id}"),
            ],
            [button("📎 Attach Proof", f"o:pf:{order_id}")],
        ]
    )


def order_review(order_id: str) -> dict[str, Any]:
    return keyboard(
        [
            [
                button("✅ Approve & Complete", f"o:ok:{order_id}"),
                button("↩️ Reject & Refund", f"o:no:{order_id}"),
            ],
            [button("📎 Attach Proof", f"o:pf:{order_id}")],
            [button("📦 Pending Orders", "a:pending")],
        ]
    )


def region_menu(regions: list[tuple[str, str]]) -> dict[str, Any]:
    rows = [[button(f"🌍 {label}", f"b:r:{code}")] for code, label in regions]
    rows.append([button(BACK, "m:basic")])
    return keyboard(rows)


def plan_menu(plans: list[tuple[str, str, int]]) -> dict[str, Any]:
    """One button per API plan: ``🪙 120 — Middle East`` (tokens only)."""
    rows = [
        [button(f"🪙 {cost:,} — {label}", f"b:r:{code}")] for code, label, cost in plans
    ]
    rows.append([button(BACK, "m:main")])
    return keyboard(rows)


def insufficient_tokens() -> dict[str, Any]:
    return keyboard(
        [
            [button("💳 Deposit / Top-up", "m:deposit")],
            [button(MAIN_MENU, "m:main")],
        ]
    )


# Backwards-compatible alias (older call sites / tests).
insufficient_balance = insufficient_tokens


def referral_menu(coupon_ready: bool = False) -> dict[str, Any]:
    rows = [[button("📤 Share My Link", "rf:link")]]
    if coupon_ready:
        rows.append([button("🎟 Claim Referral Coupon", "rf:coupons")])
    rows.append([button("🎟 My Coupons", "rf:coupons")])
    rows.append([button(MAIN_MENU, "m:main")])
    return keyboard(rows)


def coupon_menu(coupons: list[dict]) -> dict[str, Any]:
    rows: list[list[dict[str, str]]] = []
    for coupon in coupons[:8]:
        code = str(coupon.get("code") or "")
        active = str(coupon.get("status") or "") == "active"
        label = (
            f"✅ Redeem {code} (🪙 {int(coupon.get('tokens') or 0):,})"
            if active
            else f"✔️ {code} used"
        )
        rows.append([button(label[:40], f"cp:{code}" if active else "rf:coupons")])
    rows.append([button(BACK, "m:main")])
    return keyboard(rows)


def cancel_only(target: str = "a:menu") -> dict[str, Any]:
    return keyboard([[button("✖️ Cancel", target)]])
