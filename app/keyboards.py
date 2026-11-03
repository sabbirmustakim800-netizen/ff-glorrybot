"""Inline keyboard builders.

Buttons carry plain text only — no emoji, per the product spec. Callback data
stays short (Telegram caps it at 64 bytes).
"""

from __future__ import annotations

from typing import Any


def button(text: str, data: str) -> dict[str, str]:
    return {"text": text, "callback_data": data}


def keyboard(rows: list[list[dict[str, str]]]) -> dict[str, Any]:
    return {"inline_keyboard": [row for row in rows if row]}


def remove() -> dict[str, bool]:
    return {"remove_keyboard": True}


BACK = "Back"
MAIN_MENU = "Main Menu"


def main_menu(is_admin: bool = False) -> dict[str, Any]:
    rows = [
        [button("Basic Services (Auto API)", "m:basic")],
        [button("Premium / Secret Bulk", "m:premium")],
        [button("Deposit / Top-up (bKash / Crypto)", "m:deposit")],
        [button("My Account / Balance", "m:account")],
        [button("Settings / API Key", "m:settings")],
    ]
    if is_admin:
        rows.append([button("Admin Panel", "a:menu")])
    return keyboard(rows)


def back_to_main(is_admin: bool = False) -> dict[str, Any]:
    rows = [[button(MAIN_MENU, "m:main")]]
    if is_admin:
        rows.append([button("Admin Panel", "a:menu")])
    return keyboard(rows)


def admin_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("Pending Premium Orders", "a:pending")],
            [button("Profit Margin / Rates", "a:pricing")],
            [button("API Settings", "a:api")],
            [button("System Stats", "a:stats")],
            [button("Broadcast Message", "a:broadcast")],
            [button(MAIN_MENU, "m:main")],
        ]
    )


def admin_pricing_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("Set Margin Percent", "a:pr:margin")],
            [button("Set Fixed Markup", "a:pr:fixed")],
            [button("Set Basic Base Price", "a:pr:basic")],
            [button("Set Premium Base Price", "a:pr:premium")],
            [button("Reset To Defaults", "a:pr:reset")],
            [button("Admin Panel", "a:menu")],
        ]
    )


def api_settings_menu(keys: list[str]) -> dict[str, Any]:
    rows = [[button(key, f"a:api:set:{key}")] for key in keys]
    rows.append([button("API Console", "a:console")])
    rows.append([button("Admin Panel", "a:menu")])
    return keyboard(rows)


def console_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("Account (me)", "a:c:me"), button("Pricing", "a:c:pricing")],
            [button("My Groups", "a:c:groups"), button("Coupons", "a:c:coupons")],
            [button("Redeemed Coupons", "a:c:redeemed")],
            [button("Transactions", "a:c:tx"), button("Activity Log", "a:c:log")],
            [button("Notifications", "a:c:notifs")],
            [button("API Settings", "a:api")],
        ]
    )


def deposit_menu() -> dict[str, Any]:
    return keyboard(
        [
            [button("bKash (BDT)", "d:bkash")],
            [button("Crypto / USDT", "d:crypto")],
            [button(BACK, "m:main")],
        ]
    )


def deposit_actions() -> dict[str, Any]:
    return keyboard(
        [
            [button("I sent the money - verify", "d:bkash:how")],
            [button("Crypto wallets", "d:crypto")],
            [button(BACK, "m:deposit")],
        ]
    )


def account_menu(has_key: bool) -> dict[str, Any]:
    rows = [
        [button("Order History", "ac:history")],
        [button("Remove API Key" if has_key else "Set My API Key", "ac:key")],
        [button(BACK, "m:main")],
    ]
    return keyboard(rows)


def premium_intro() -> dict[str, Any]:
    return keyboard(
        [
            [button("Start Premium Order", "p:start")],
            [button(BACK, "m:main")],
        ]
    )


def premium_confirm() -> dict[str, Any]:
    return keyboard(
        [
            [button("Confirm Order", "p:confirm")],
            [button("Cancel", "p:cancel")],
        ]
    )


def order_actions(order_id: str) -> dict[str, Any]:
    return keyboard(
        [
            [
                button("Approve & Complete", f"o:ok:{order_id}"),
                button("Reject & Refund", f"o:no:{order_id}"),
            ],
            [button("Attach Proof", f"o:pf:{order_id}")],
        ]
    )


def order_review(order_id: str) -> dict[str, Any]:
    return keyboard(
        [
            [
                button("Approve & Complete", f"o:ok:{order_id}"),
                button("Reject & Refund", f"o:no:{order_id}"),
            ],
            [button("Attach Proof", f"o:pf:{order_id}")],
            [button("Pending Orders", "a:pending")],
        ]
    )


def region_menu(regions: list[tuple[str, str]]) -> dict[str, Any]:
    rows = [[button(label, f"b:r:{code}")] for code, label in regions]
    rows.append([button(BACK, "m:basic")])
    return keyboard(rows)


def insufficient_balance() -> dict[str, Any]:
    return keyboard(
        [
            [button("Deposit / Top-up", "m:deposit")],
            [button(MAIN_MENU, "m:main")],
        ]
    )


def cancel_only(target: str = "a:menu") -> dict[str, Any]:
    return keyboard([[button("Cancel", target)]])
