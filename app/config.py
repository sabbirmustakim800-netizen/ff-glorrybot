"""Environment-driven configuration.

All secrets live in environment variables (see .env.example) — nothing is
hardcoded in source. Values are loaded once and cached per process.

Admin credentials (``ADMIN_IDS``) are always environment-only: an admin can
change prices, keys and wallets from the bot, but cannot grant themselves
admin rights.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache

try:  # optional: local .env loading (not needed on Vercel/VPS where env is set externally)
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def parse_admin_ids(raw: str) -> tuple[int, ...]:
    """Parse ADMIN_IDS — "123,456" or "123 456" or "[123,456]".

    Invalid entries are ignored; duplicates are dropped while keeping order.
    """
    text = (raw or "").strip().strip("[]")
    if not text:
        return ()
    ids: list[int] = []
    for chunk in text.replace(";", ",").replace(" ", ",").split(","):
        chunk = chunk.strip().strip("'\"")
        if not chunk:
            continue
        try:
            value = int(chunk)
        except ValueError:
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    return tuple(ids)


def parse_rates(raw: str) -> dict[str, float]:
    """Parse BDT_RATES — JSON ({"USDT": 110}) or a list (USDT=110, BTC=6500000)."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    rates: dict[str, float] = {}
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(data, dict):
            return {}
        for coin, value in data.items():
            try:
                rates[str(coin).strip().upper()] = float(value)
            except (TypeError, ValueError):
                continue
        return rates
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sep = "=" if "=" in chunk else (":" if ":" in chunk else None)
        if not sep:
            continue
        coin, _, value = chunk.partition(sep)
        try:
            rates[coin.strip().upper()] = float(value.strip())
        except ValueError:
            continue
    return rates


@dataclass(frozen=True)
class Settings:
    # --- Telegram ---
    telegram_token: str
    webhook_secret: str
    # --- Mongo ---
    mongo_uri: str
    mongo_db: str
    # --- bKash TrxID verification provider ---
    bkash_base_url: str
    bkash_api_key: str
    bkash_verify_path: str
    bkash_confirm_path: str
    bkash_auth_header: str
    bkash_auth_scheme: str
    bkash_payload_mode: str
    bkash_field_trx: str
    bkash_field_sender: str
    bkash_field_amount: str
    bkash_timeout: float
    bkash_attempts: int
    bkash_retry_delay: float
    bkash_max_age_hours: float
    # --- Binance (read-only API key) ---
    binance_api_key: str
    binance_api_secret: str
    binance_base_url: str
    binance_timeout: float
    binance_recv_window: int
    # --- Credits ---
    bdt_rates: dict[str, float] = field(default_factory=dict)
    # --- FF Glory Reseller API ---
    ffglory_base_url: str = field(default_factory=lambda: _env("FFGLORY_BASE_URL"))
    ffglory_api_key: str = field(default_factory=lambda: _env("FFGLORY_API_KEY"))
    ffglory_master_key: str = field(default_factory=lambda: _env("FFGLORY_MASTER_KEY"))
    # --- Admin panel (environment only) ---
    admin_ids: tuple[int, ...] = field(
        default_factory=lambda: parse_admin_ids(_env("ADMIN_IDS"))
    )
    # --- Pricing defaults (overridable from the admin panel / Mongo) ---
    profit_margin_percent: float = field(
        default_factory=lambda: _float("PROFIT_MARGIN_PERCENT", 25.0)
    )
    fixed_markup_bdt: float = field(default_factory=lambda: _float("FIXED_MARKUP_BDT", 0.0))
    basic_credit_bdt: float = field(default_factory=lambda: _float("BASIC_CREDIT_BDT", 120.0))
    premium_credit_bdt: float = field(
        default_factory=lambda: _float("PREMIUM_CREDIT_BDT", 350.0)
    )
    usd_bdt_rate: float = field(default_factory=lambda: _float("USD_BDT_RATE", 122.0))
    # --- Crypto deposit wallets ---
    usdt_trc20_address: str = field(default_factory=lambda: _env("USDT_TRC20_ADDRESS"))
    usdt_bep20_address: str = field(default_factory=lambda: _env("USDT_BEP20_ADDRESS"))
    support_contact: str = field(default_factory=lambda: _env("SUPPORT_CONTACT"))
    # --- Deposits (the admin panel can override the bKash number at runtime) ---
    bkash_receiver_number: str = field(
        default_factory=lambda: _env("BKASH_RECEIVER_NUMBER")
    )
    min_deposit_bdt: float = field(default_factory=lambda: _float("MIN_DEPOSIT_BDT", 50.0))
    # --- Tokens (the only currency users see; the admin panel wins) ---
    bot_username: str = field(default_factory=lambda: _env("BOT_USERNAME"))
    taka_per_token: float = field(default_factory=lambda: _float("TAKA_PER_TOKEN", 1.0))
    basic_token_cost: int = field(default_factory=lambda: _int("BASIC_TOKEN_COST", 100))
    premium_token_cost: int = field(
        default_factory=lambda: _int("PREMIUM_TOKEN_COST", 300)
    )
    referral_reward_tokens: int = field(
        default_factory=lambda: _int("REFERRAL_REWARD_TOKENS", 20)
    )
    referral_goal: int = field(default_factory=lambda: _int("REFERRAL_GOAL", 5))
    referral_coupon_tokens: int = field(
        default_factory=lambda: _int("REFERRAL_COUPON_TOKENS", 100)
    )

    @property
    def bkash_configured(self) -> bool:
        return bool(self.bkash_base_url and self.bkash_api_key)

    @property
    def binance_configured(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    @property
    def ffglory_configured(self) -> bool:
        return bool(self.ffglory_api_key and self.ffglory_master_key)

    def is_admin(self, user_id: int | None) -> bool:
        return bool(user_id) and int(user_id) in self.admin_ids

    def rate_for(self, coin: str) -> float | None:
        return self.bdt_rates.get((coin or "").strip().upper())


@dataclass(frozen=True)
class Pricing:
    """Runtime pricing: base API cost + admin markup.

    ``final_price = base + (base * margin/100) + fixed_markup`` — the formula
    the spec asks for, with a fixed-markup term for flat fees.
    """

    margin_percent: float = 25.0
    fixed_markup_bdt: float = 0.0
    basic_credit_bdt: float = 120.0
    premium_credit_bdt: float = 350.0

    @classmethod
    def from_doc(cls, doc: dict | None, settings: Settings) -> "Pricing":
        section = (doc or {}).get("pricing") or {}

        def num(key: str, default: float) -> float:
            value = section.get(key)
            if value is None or value == "":
                return float(default)
            try:
                return float(value)
            except (TypeError, ValueError):
                return float(default)

        return cls(
            margin_percent=max(0.0, num("margin_percent", settings.profit_margin_percent)),
            fixed_markup_bdt=max(0.0, num("fixed_markup_bdt", settings.fixed_markup_bdt)),
            basic_credit_bdt=max(0.0, num("basic_credit_bdt", settings.basic_credit_bdt)),
            premium_credit_bdt=max(0.0, num("premium_credit_bdt", settings.premium_credit_bdt)),
        )

    def final_price(self, base: float) -> float:
        base = max(0.0, float(base or 0.0))
        return round(base + base * (self.margin_percent / 100.0) + self.fixed_markup_bdt, 2)

    def markup_amount(self, base: float) -> float:
        return round(self.final_price(base) - max(0.0, float(base or 0.0)), 2)

    def as_dict(self) -> dict:
        return {
            "margin_percent": self.margin_percent,
            "fixed_markup_bdt": self.fixed_markup_bdt,
            "basic_credit_bdt": self.basic_credit_bdt,
            "premium_credit_bdt": self.premium_credit_bdt,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        # Telegram
        telegram_token=_env("TELEGRAM_BOT_TOKEN"),
        webhook_secret=_env("TELEGRAM_WEBHOOK_SECRET"),
        # MongoDB
        mongo_uri=_env("MONGO_URI"),
        mongo_db=_env("MONGO_DB", "ff_glory_bot"),
        # bKash provider
        bkash_base_url=_env("BKASH_API_BASE_URL"),
        bkash_api_key=_env("BKASH_API_KEY"),
        bkash_verify_path=_env("BKASH_VERIFY_PATH", "/verify"),
        bkash_confirm_path=_env("BKASH_CONFIRM_PATH"),
        bkash_auth_header=_env("BKASH_AUTH_HEADER", "Authorization"),
        bkash_auth_scheme=_env("BKASH_AUTH_SCHEME", "Bearer"),
        bkash_payload_mode=_env("BKASH_PAYLOAD_MODE", "json").lower(),
        bkash_field_trx=_env("BKASH_FIELD_TRX", "trx_id"),
        bkash_field_sender=_env("BKASH_FIELD_SENDER", "mobile_number"),
        bkash_field_amount=_env("BKASH_FIELD_AMOUNT", "amount"),
        bkash_timeout=_float("BKASH_TIMEOUT", 12.0),
        bkash_attempts=max(1, _int("BKASH_ATTEMPTS", 3)),
        bkash_retry_delay=_float("BKASH_RETRY_DELAY", 4.0),
        bkash_max_age_hours=_float("BKASH_MAX_AGE_HOURS", 48.0),
        # Binance
        binance_api_key=_env("BINANCE_API_KEY"),
        binance_api_secret=_env("BINANCE_API_SECRET"),
        binance_base_url=_env("BINANCE_BASE_URL", "https://api.binance.com").rstrip("/"),
        binance_timeout=_float("BINANCE_TIMEOUT", 12.0),
        binance_recv_window=_int("BINANCE_RECV_WINDOW", 10000),
        # Credits
        bdt_rates=parse_rates(_env("BDT_RATES")),
        ffglory_base_url=_env("FFGLORY_BASE_URL", "https://ffglory.pro"),
        ffglory_api_key=_env("FFGLORY_API_KEY", ""),
        ffglory_master_key=_env("FFGLORY_MASTER_KEY", ""),
        # Admin panel
        admin_ids=parse_admin_ids(_env("ADMIN_IDS")),
        profit_margin_percent=_float("PROFIT_MARGIN_PERCENT", 25.0),
        fixed_markup_bdt=_float("FIXED_MARKUP_BDT", 0.0),
        basic_credit_bdt=_float("BASIC_CREDIT_BDT", 120.0),
        premium_credit_bdt=_float("PREMIUM_CREDIT_BDT", 350.0),
        usd_bdt_rate=_float("USD_BDT_RATE", 122.0),
        usdt_trc20_address=_env("USDT_TRC20_ADDRESS"),
        usdt_bep20_address=_env("USDT_BEP20_ADDRESS"),
        support_contact=_env("SUPPORT_CONTACT"),
        bkash_receiver_number=_env("BKASH_RECEIVER_NUMBER"),
        min_deposit_bdt=_float("MIN_DEPOSIT_BDT", 50.0),
        # Tokens
        bot_username=_env("BOT_USERNAME"),
        taka_per_token=_float("TAKA_PER_TOKEN", 1.0),
        basic_token_cost=_int("BASIC_TOKEN_COST", 100),
        premium_token_cost=_int("PREMIUM_TOKEN_COST", 300),
        referral_reward_tokens=_int("REFERRAL_REWARD_TOKENS", 20),
        referral_goal=_int("REFERRAL_GOAL", 5),
        referral_coupon_tokens=_int("REFERRAL_COUPON_TOKENS", 100),
    )
