"""The token economy — the only currency users ever see.

Everything a user is charged for (a basic plan, a premium order) is priced in
**tokens**. Taka appears in exactly one place: the deposit form, where the user
pays and the admin-configured conversion rate turns the paid amount into tokens.

Admins own every number here through the ``tokens`` settings section:

    tokens.taka_per_token        ৳ that buys one token (e.g. 1.5)
    tokens.basic_cost            default token cost of a basic plan
    tokens.premium_cost          token cost of a premium order
    tokens.plan_costs            per-plan overrides {plan_id: tokens}
    tokens.referral_reward       tokens a referrer earns per joined invite
    tokens.referral_goal         invites needed for one referral coupon
    tokens.referral_coupon_reward tokens the referral coupon is worth
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Settings, get_settings

DEFAULT_TAKA_PER_TOKEN = 1.0
DEFAULT_BASIC_COST = 100
DEFAULT_PREMIUM_COST = 300
DEFAULT_REFERRAL_REWARD = 20
DEFAULT_REFERRAL_GOAL = 5
DEFAULT_REFERRAL_COUPON_REWARD = 100

# Guard rails so a typo cannot hand out (or charge) absurd amounts.
MAX_TAKA_PER_TOKEN = 100_000.0
MAX_SERVICE_COST = 1_000_000
MAX_REFERRAL_GOAL = 1_000


def fmt_tokens(value: float | int) -> str:
    """User-facing token amount: ``🪙 120`` (never a currency symbol)."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if abs(amount - round(amount)) < 0.005:
        return f"🪙 {int(round(amount)):,}"
    return f"🪙 {amount:,.2f}"


def token_balance(value: float | int) -> str:
    """Compact amount without the emoji, for tables and one-line summaries."""
    try:
        amount = float(value or 0)
    except (TypeError, ValueError):
        amount = 0.0
    if abs(amount - round(amount)) < 0.005:
        return f"{int(round(amount)):,}"
    return f"{amount:,.2f}"


@dataclass(frozen=True)
class TokenConfig:
    taka_per_token: float = DEFAULT_TAKA_PER_TOKEN
    basic_cost: int = DEFAULT_BASIC_COST
    premium_cost: int = DEFAULT_PREMIUM_COST
    plan_costs: dict[str, int] = field(default_factory=dict)
    referral_reward: int = DEFAULT_REFERRAL_REWARD
    referral_goal: int = DEFAULT_REFERRAL_GOAL
    referral_coupon_reward: int = DEFAULT_REFERRAL_COUPON_REWARD

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_doc(cls, doc: dict | None, settings: Settings | None = None) -> "TokenConfig":
        settings = settings or get_settings()
        section = (doc or {}).get("tokens") or {}

        def number(key: str, default: float, *, minimum: float = 0.0) -> float:
            value = section.get(key)
            if value in (None, ""):
                return float(default)
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                return float(default)
            return max(minimum, parsed)

        plan_costs: dict[str, int] = {}
        raw_plans = section.get("plan_costs")
        if isinstance(raw_plans, dict):
            for key, value in raw_plans.items():
                plan_id = str(key or "").strip().lower()
                if not plan_id:
                    continue
                try:
                    plan_costs[plan_id] = min(MAX_SERVICE_COST, max(0, int(float(value))))
                except (TypeError, ValueError):
                    continue

        return cls(
            taka_per_token=min(
                MAX_TAKA_PER_TOKEN,
                number("taka_per_token", settings.taka_per_token, minimum=0.01),
            ),
            basic_cost=min(
                MAX_SERVICE_COST, int(number("basic_cost", settings.basic_token_cost))
            ),
            premium_cost=min(
                MAX_SERVICE_COST, int(number("premium_cost", settings.premium_token_cost))
            ),
            plan_costs=plan_costs,
            referral_reward=min(
                MAX_SERVICE_COST,
                int(number("referral_reward", settings.referral_reward_tokens)),
            ),
            referral_goal=max(
                1,
                min(
                    MAX_REFERRAL_GOAL,
                    int(number("referral_goal", settings.referral_goal, minimum=1)),
                ),
            ),
            referral_coupon_reward=min(
                MAX_SERVICE_COST,
                int(
                    number(
                        "referral_coupon_reward", settings.referral_coupon_tokens
                    )
                ),
            ),
        )

    # ------------------------------------------------------------- conversion
    def tokens_for_bdt(self, amount_bdt: float) -> int:
        """Convert a paid Taka amount into tokens (rounded down, never negative)."""
        try:
            paid = float(amount_bdt or 0.0)
        except (TypeError, ValueError):
            paid = 0.0
        rate = self.taka_per_token if self.taka_per_token > 0 else DEFAULT_TAKA_PER_TOKEN
        if paid <= 0:
            return 0
        return max(0, int(math.floor(paid / rate)))

    def taka_for_tokens(self, tokens: float) -> float:
        """Internal only — what a token amount cost at the current rate."""
        rate = self.taka_per_token if self.taka_per_token > 0 else DEFAULT_TAKA_PER_TOKEN
        return round(float(tokens or 0.0) * rate, 2)

    def rate_line(self) -> str:
        return f"৳{self.taka_per_token:g} = 1 token"

    # ------------------------------------------------------------------ plans
    def cost_for_plan(self, plan_id: str) -> int:
        key = str(plan_id or "").strip().lower()
        if key and key in self.plan_costs:
            return int(self.plan_costs[key])
        return int(self.basic_cost)

    def has_plan_override(self, plan_id: str) -> bool:
        return str(plan_id or "").strip().lower() in self.plan_costs

    # -------------------------------------------------------------- referrals
    def goal_progress(self, invites: int) -> tuple[int, int]:
        """Return (invites into the current goal, goal)."""
        goal = max(1, int(self.referral_goal))
        return int(max(0, invites)) % goal, goal

    def milestones_reached(self, invites: int) -> int:
        goal = max(1, int(self.referral_goal))
        return int(max(0, invites)) // goal

    def as_dict(self) -> dict:
        return {
            "taka_per_token": self.taka_per_token,
            "basic_cost": self.basic_cost,
            "premium_cost": self.premium_cost,
            "plan_costs": dict(self.plan_costs),
            "referral_reward": self.referral_reward,
            "referral_goal": self.referral_goal,
            "referral_coupon_reward": self.referral_coupon_reward,
        }
