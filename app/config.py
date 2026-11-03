"""Environment-driven configuration.

All secrets live in environment variables (see .env.example) — nothing is
hardcoded in source. Values are loaded once and cached per process.
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
    # --- MongoDB ---
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

    @property
    def bkash_configured(self) -> bool:
        return bool(self.bkash_base_url and self.bkash_api_key)

    @property
    def binance_configured(self) -> bool:
        return bool(self.binance_api_key and self.binance_api_secret)

    def rate_for(self, coin: str) -> float | None:
        return self.bdt_rates.get((coin or "").strip().upper())


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
    )