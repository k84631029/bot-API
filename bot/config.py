"""Settings loaded from environment variables.

Secrets (tokens, keys, owner IDs) live only in the environment, never in the
database or the repository.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _decimal(name: str, default: str) -> Decimal:
    raw = os.environ.get(name, default).strip()
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _owner_ids(raw: str) -> frozenset[int]:
    ids = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.lstrip("-").isdigit():
            raise ConfigError(f"OWNER_IDS contains a non-numeric value: {part!r}")
        ids.add(int(part))
    if not ids:
        raise ConfigError("OWNER_IDS must contain at least one Telegram user ID")
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    bot_token: str
    g4s_api_token: str
    owner_ids: frozenset[int]
    encryption_keys: str
    hmac_key: str

    g4s_base_url: str = "https://api.game4station.com"
    # Optional HTTP(S) proxy with a static IP, used only for game4station calls
    # (for IP allow-listing, error 123).
    g4s_proxy_url: str | None = None

    webhook_base_url: str | None = None
    webhook_secret: str | None = None
    port: int = 8080

    google_project_id: str | None = None
    firebase_credentials_json: str | None = None

    default_margin_percent: Decimal = Decimal("10")
    currency_symbol: str = "$"
    low_provider_balance: Decimal = Decimal("20")
    log_retention_days: int = 180
    support_contact: str = ""

    # Anti-spam: per-user limits.
    actions_per_minute: int = 30
    orders_per_minute: int = 5

    order_poll_seconds: int = 60
    balance_check_seconds: int = 600

    @property
    def use_webhook(self) -> bool:
        return bool(self.webhook_base_url)


def load_settings() -> Settings:
    webhook_base = (
        os.environ.get("WEBHOOK_BASE_URL", "").strip()
        # Render sets this automatically for web services.
        or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        or None
    )
    webhook_secret = os.environ.get("WEBHOOK_SECRET", "").strip() or None
    if webhook_base and not webhook_secret:
        raise ConfigError("WEBHOOK_SECRET is required when running with a webhook")
    if webhook_secret and not all(c.isalnum() or c in "_-" for c in webhook_secret):
        raise ConfigError("WEBHOOK_SECRET may contain only A-Z, a-z, 0-9, _ and -")

    margin = _decimal("DEFAULT_MARGIN_PERCENT", "10")
    if margin < 0:
        raise ConfigError("DEFAULT_MARGIN_PERCENT cannot be negative")

    return Settings(
        bot_token=_required("BOT_TOKEN"),
        g4s_api_token=_required("G4S_API_TOKEN"),
        owner_ids=_owner_ids(_required("OWNER_IDS")),
        encryption_keys=_required("ENCRYPTION_KEYS"),
        hmac_key=_required("HMAC_KEY"),
        g4s_base_url=os.environ.get("G4S_BASE_URL", "https://api.game4station.com").rstrip("/"),
        g4s_proxy_url=os.environ.get("G4S_PROXY_URL", "").strip() or None,
        webhook_base_url=webhook_base.rstrip("/") if webhook_base else None,
        webhook_secret=webhook_secret,
        port=_int("PORT", 8080),
        google_project_id=os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip() or None,
        firebase_credentials_json=os.environ.get("FIREBASE_CREDENTIALS_JSON", "").strip() or None,
        default_margin_percent=margin,
        currency_symbol=os.environ.get("CURRENCY_SYMBOL", "$"),
        low_provider_balance=_decimal("LOW_PROVIDER_BALANCE", "20"),
        log_retention_days=_int("LOG_RETENTION_DAYS", 180),
        support_contact=os.environ.get("SUPPORT_CONTACT", "").strip(),
        actions_per_minute=_int("ACTIONS_PER_MINUTE", 30),
        orders_per_minute=_int("ORDERS_PER_MINUTE", 5),
        order_poll_seconds=_int("ORDER_POLL_SECONDS", 60),
        balance_check_seconds=_int("BALANCE_CHECK_SECONDS", 600),
    )
