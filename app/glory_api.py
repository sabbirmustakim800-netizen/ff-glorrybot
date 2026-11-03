"""FF Glory Reseller API client (async httpx wrapper).

Every endpoint needs two credentials:

  * master key  -> ``x-api-key`` HTTP header  (same for everybody)
  * account key -> ``api_key`` query parameter (unique per account)

``GloryAPI`` takes both explicitly so the bot can call the API with the
platform account key (default) or with a customer's own key when they linked
one from *Settings / API Key*, and so an admin can override either value from
the admin panel without a redeploy.

Endpoints wired here (all JSON, all POST bodies JSON):

  GET  /api/auth/me                       account + credit balance
  GET  /api/pricing                       regions & credit prices (public)
  POST /api/client/auto-clan-group        launch a glory group (1 credit)
  GET  /api/client/my-groups              list groups
  POST /api/client/group-action           restart|stop|delete|get-glory|get-clan-info
  POST /api/client/create-coupon          create a coupon (charges credits)
  GET  /api/client/my-coupons             list coupons
  GET  /api/client/redeemed-coupons       redeemed coupons
  POST /api/client/redeem-coupon          redeem a code
  POST /api/client/cancel-coupon          cancel an active coupon
  GET  /api/client/transactions           purchase history
  GET  /api/client/group-history          group history
  GET  /api/client/glory-progression      glory timeline
  GET  /api/client/activity-log           activity log
  GET  /api/client/notifications          one-time notifications
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("glory_api")

DEFAULT_BASE_URL = "https://ffglory.pro"

_HTTP_CLIENT: httpx.AsyncClient | None = None


def http() -> httpx.AsyncClient:
    """Shared keep-alive client (safe across warm serverless invocations)."""
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None or _HTTP_CLIENT.is_closed:
        _HTTP_CLIENT = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _HTTP_CLIENT


class GloryAPIError(Exception):
    """FF Glory returned an error (or an unusable response)."""

    def __init__(self, status: int, error: str, details: Any = None) -> None:
        super().__init__(error or f"HTTP {status}")
        self.status = status
        self.error = error or f"HTTP {status}"
        self.details = details

    @property
    def message(self) -> str:
        return self.error


@dataclass(frozen=True)
class Balance:
    basic: float
    premium: float

    @property
    def total(self) -> float:
        return self.basic + self.premium


@dataclass(frozen=True)
class Region:
    id: str
    name: str
    tier: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class Group:
    group_id: str
    clan_id: str = ""
    region: str = ""
    status: str = "unknown"
    uptime: str = ""
    container_count: int = 1


@dataclass(frozen=True)
class Coupon:
    code: str
    basic_credits: int = 0
    premium_credits: int = 0
    status: str = "active"


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def extract_group_id(payload: Any) -> str | None:
    """Pull a group id out of the many shapes FF Glory can answer with."""
    if not isinstance(payload, dict):
        return None
    for container in (
        payload,
        payload.get("data"),
        payload.get("group"),
        payload.get("result"),
    ):
        if not isinstance(container, dict):
            continue
        for key in ("group_id", "groupId", "id"):
            value = container.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


class GloryAPI:
    """Thin async client for https://ffglory.pro/api/*."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        master_key: str = "",
        api_key: str = "",
    ) -> None:
        self.base = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.master_key = (master_key or "").strip()
        self.api_key = (api_key or "").strip()

    # ------------------------------------------------------------- plumbing
    @property
    def configured(self) -> bool:
        return bool(self.master_key and self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.master_key, "Accept": "application/json"}

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        if not self.configured:
            raise GloryAPIError(401, "FF Glory API key / master key is not configured")
        url = f"{self.base}{path if path.startswith('/') else '/' + path}"
        query = {"api_key": self.api_key}
        if params:
            query.update({k: v for k, v in params.items() if v is not None})
        try:
            resp = await http().request(
                method.upper(), url, params=query, json=json, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise GloryAPIError(408, f"FF Glory timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise GloryAPIError(502, f"FF Glory connection error: {exc}") from exc

        try:
            body = resp.json()
        except ValueError:
            body = None
        if resp.status_code != 200:
            error = ""
            if isinstance(body, dict):
                error = str(body.get("error") or body.get("message") or "")
            raise GloryAPIError(
                resp.status_code, error or f"HTTP {resp.status_code}", body
            )
        if body is None:
            raise GloryAPIError(500, "FF Glory returned a non-JSON response")
        return body

    # -------------------------------------------------------------- account
    async def me(self) -> dict:
        body = await self.request("GET", "/api/auth/me")
        if isinstance(body, dict) and isinstance(body.get("user"), dict):
            merged = dict(body["user"])
            for key in ("basic_credits", "premium_credits"):
                if key in body and key not in merged:
                    merged[key] = body[key]
            return merged
        return body if isinstance(body, dict) else {}

    async def balance(self) -> Balance:
        account = await self.me()
        return Balance(
            basic=_as_float(account.get("basic_credits")),
            premium=_as_float(account.get("premium_credits")),
        )

    async def signup(self, username: str, password: str) -> dict:
        body = await self.request(
            "POST", "/api/auth/signup", json={"username": username, "password": password}
        )
        return body if isinstance(body, dict) else {}

    # -------------------------------------------------------------- pricing
    async def pricing(self) -> dict:
        body = await self.request("GET", "/api/pricing")
        return body if isinstance(body, dict) else {}

    async def regions(self) -> list[Region]:
        body = await self.pricing()
        rows = body.get("regions") or []
        regions: list[Region] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = str(row.get("id") or row.get("code") or "").strip()
            if not rid:
                continue
            regions.append(
                Region(
                    id=rid,
                    name=str(row.get("region_name") or row.get("name") or rid),
                    tier=str(row.get("tier") or ""),
                    enabled=bool(row.get("enabled", True)),
                )
            )
        return regions

    # --------------------------------------------------------------- groups
    async def launch_group(self, region: str, clan_id: str) -> dict:
        body = await self.request(
            "POST",
            "/api/client/auto-clan-group",
            json={"region": region, "clan_id": clan_id},
        )
        return body if isinstance(body, dict) else {}

    async def my_groups(self) -> list[Group]:
        body = await self.request("GET", "/api/client/my-groups")
        rows = body if isinstance(body, list) else (body or {}).get("groups") or []
        groups: list[Group] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            groups.append(
                Group(
                    group_id=str(row.get("group_id") or row.get("id") or ""),
                    clan_id=str(row.get("clan_id") or ""),
                    region=str(row.get("region") or ""),
                    status=str(row.get("status") or "unknown"),
                    uptime=str(row.get("uptime") or ""),
                    container_count=_as_int(row.get("container_count"), 1) or 1,
                )
            )
        return groups

    async def group_action(self, group_id: str, action: str, **extra: Any) -> dict:
        payload: dict[str, Any] = {"action": action, "group_id": group_id}
        payload.update({k: v for k, v in extra.items() if v is not None})
        body = await self.request("POST", "/api/client/group-action", json=payload)
        return body if isinstance(body, dict) else {}

    async def group_glory(self, group_id: str) -> dict:
        return await self.group_action(group_id, "get-glory")

    async def group_clan_info(self, group_id: str) -> dict:
        return await self.group_action(group_id, "get-clan-info")

    async def restart_group(self, group_id: str) -> dict:
        return await self.group_action(group_id, "restart")

    async def stop_group(self, group_id: str) -> dict:
        return await self.group_action(group_id, "stop")

    async def delete_group(self, group_id: str) -> dict:
        return await self.group_action(group_id, "delete")

    # -------------------------------------------------------------- coupons
    async def create_coupon(self, basic: int, premium: int) -> Coupon:
        body = await self.request(
            "POST",
            "/api/client/create-coupon",
            json={"basic_credits": basic, "premium_credits": premium},
        )
        coupon = body.get("coupon") if isinstance(body.get("coupon"), dict) else body
        return Coupon(
            code=str(coupon.get("code") or ""),
            basic_credits=_as_int(coupon.get("basic_credits")),
            premium_credits=_as_int(coupon.get("premium_credits")),
            status=str(coupon.get("status") or "active"),
        )

    async def my_coupons(self) -> list[Coupon]:
        body = await self.request("GET", "/api/client/my-coupons")
        rows = body if isinstance(body, list) else (body or {}).get("coupons") or []
        coupons: list[Coupon] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            coupons.append(
                Coupon(
                    code=str(row.get("code") or ""),
                    basic_credits=_as_int(row.get("basic_credits")),
                    premium_credits=_as_int(row.get("premium_credits")),
                    status=str(row.get("status") or "active"),
                )
            )
        return coupons

    async def redeemed_coupons(self) -> list[dict]:
        body = await self.request("GET", "/api/client/redeemed-coupons")
        return body if isinstance(body, list) else (body or {}).get("coupons") or []

    async def redeem_coupon(self, code: str) -> dict:
        body = await self.request(
            "POST", "/api/client/redeem-coupon", json={"code": code}
        )
        return body if isinstance(body, dict) else {}

    async def cancel_coupon(self, code: str) -> dict:
        body = await self.request(
            "POST", "/api/client/cancel-coupon", json={"code": code}
        )
        return body if isinstance(body, dict) else {}

    # --------------------------------------------------------- transactions
    async def transactions(self) -> list[dict]:
        body = await self.request("GET", "/api/client/transactions")
        return body if isinstance(body, list) else (body or {}).get("transactions") or []

    async def group_history(self, group_id: str) -> list[dict]:
        body = await self.request(
            "GET", "/api/client/group-history", params={"group_id": group_id}
        )
        return body if isinstance(body, list) else (body or {}).get("history") or []

    async def glory_progression(self, group_id: str) -> dict:
        body = await self.request(
            "GET", "/api/client/glory-progression", params={"group_id": group_id}
        )
        return body if isinstance(body, dict) else {}

    async def activity_log(self) -> list[dict]:
        body = await self.request("GET", "/api/client/activity-log")
        return body if isinstance(body, list) else (body or {}).get("logs") or []

    async def notifications(self) -> list[dict]:
        body = await self.request("GET", "/api/client/notifications")
        return (
            body if isinstance(body, list) else (body or {}).get("notifications") or []
        )


# Backwards-compatible names (the old module was called ffglory).
FFGloryAPI = GloryAPI
FFGloryAPIError = GloryAPIError
FFGloryBalance = Balance
FFGloryGroup = Group
FFGloryCoupon = Coupon
