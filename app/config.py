"""Application settings, read from environment variables (and .env locally)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None or val == "":
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def normalize_db_url(url: str) -> str:
    """Render/Heroku style postgres:// URLs -> SQLAlchemy psycopg3 driver URL."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


@dataclass
class Settings:
    database_url: str = field(default_factory=lambda: normalize_db_url(
        os.getenv("DATABASE_URL", "sqlite:///./journal.db")))
    app_password: str = field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    secret_key: str = field(default_factory=lambda: os.getenv("SECRET_KEY", ""))
    cookie_secure: bool = field(default_factory=lambda: _bool("COOKIE_SECURE", False))
    display_tz: str = field(default_factory=lambda: os.getenv("DISPLAY_TZ", "America/New_York"))
    tos_timezone: str = field(default_factory=lambda: os.getenv("TOS_TIMEZONE", "America/New_York"))
    base_url: str = field(default_factory=lambda: os.getenv("BASE_URL", os.getenv("RENDER_EXTERNAL_URL", "http://127.0.0.1:8000")))

    # Schwab Trader API data source (optional; disabled unless all three are set)
    schwab_app_key: str = field(default_factory=lambda: os.getenv("SCHWAB_APP_KEY", ""))
    schwab_app_secret: str = field(default_factory=lambda: os.getenv("SCHWAB_APP_SECRET", ""))
    schwab_callback_url: str = field(default_factory=lambda: os.getenv("SCHWAB_CALLBACK_URL", ""))
    schwab_max_lookback_days: int = field(default_factory=lambda: _int("SCHWAB_MAX_LOOKBACK_DAYS", 1825))
    schwab_chunk_days: int = field(default_factory=lambda: _int("SCHWAB_CHUNK_DAYS", 90))
    sync_overlap_days: int = field(default_factory=lambda: _int("SYNC_OVERLAP_DAYS", 3))
    token_encryption_key: str = field(default_factory=lambda: os.getenv("TOKEN_ENCRYPTION_KEY", ""))

    # SnapTrade data source (Personal API key; disabled unless both are set)
    snaptrade_client_id: str = field(default_factory=lambda: os.getenv("SNAPTRADE_CLIENT_ID", ""))
    snaptrade_consumer_key: str = field(default_factory=lambda: os.getenv("SNAPTRADE_CONSUMER_KEY", ""))

    # Price data for trade charts (optional)
    price_provider: str = field(default_factory=lambda: os.getenv("PRICE_PROVIDER", "auto").lower())
    polygon_api_key: str = field(default_factory=lambda: os.getenv("POLYGON_API_KEY", ""))

    # Reminder email hook (stub; only sends if SMTP_* configured)
    reminders_enabled: bool = field(default_factory=lambda: _bool("REMINDER_EMAIL_ENABLED", False))
    reminder_email_to: str = field(default_factory=lambda: os.getenv("REMINDER_EMAIL_TO", ""))
    smtp_host: str = field(default_factory=lambda: os.getenv("SMTP_HOST", ""))
    smtp_port: int = field(default_factory=lambda: _int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: os.getenv("SMTP_USER", ""))
    smtp_password: str = field(default_factory=lambda: os.getenv("SMTP_PASSWORD", ""))
    smtp_from: str = field(default_factory=lambda: os.getenv("SMTP_FROM", ""))
    import_reminder_days: int = field(default_factory=lambda: _int("IMPORT_REMINDER_DAYS", 3))

    def __post_init__(self) -> None:
        if not self.secret_key:
            # Dev fallback: sessions are invalidated on restart. Always set SECRET_KEY in production.
            self.secret_key = secrets.token_urlsafe(32)
            self.secret_key_is_ephemeral = True
        else:
            self.secret_key_is_ephemeral = False

    @property
    def snaptrade_configured(self) -> bool:
        return bool(self.snaptrade_client_id and self.snaptrade_consumer_key)

    @property
    def schwab_configured(self) -> bool:
        return bool(self.schwab_app_key and self.schwab_app_secret and self.schwab_callback_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
