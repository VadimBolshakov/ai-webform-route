import re
from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class Settings(BaseSettings):
    # Empty values in .env (e.g. TITLE_MODEL=) mean "use the default".
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: str
    fernet_key: str
    openrouter_api_key: str
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    login_path_token: str
    base_url: str
    session_ttl_days: int = 7
    default_monthly_limit_usd: Decimal = Decimal("5")
    chat_rate_per_min: int = 20
    context_token_budget: int = 24000
    max_output_tokens: int = 4096
    title_model: str = Field(default="openai/gpt-4o-mini", min_length=1)
    auto_allowed_models: list[str] = []
    max_upload_mb: int = 10
    enable_jobs: bool = True
    telegram_bot_token: str = ""
    telegram_owner_chat_id: int | None = None
    digest_timezone: str = "Europe/Moscow"
    digest_time: str = "09:00"

    @property
    def telegram_configured(self) -> bool:
        return bool(self.telegram_bot_token) and self.telegram_owner_chat_id is not None

    @field_validator("digest_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception:
            raise ValueError("DIGEST_TIMEZONE: unknown time zone") from None
        return value

    @field_validator("digest_time")
    @classmethod
    def _check_digest_time(cls, value: str) -> str:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("DIGEST_TIME: HH:MM (24-hour) is required")
        return value

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """Origin only: https (http only for localhost), no path, query or fragment."""
        parts = urlsplit(value)
        try:
            _ = parts.port  # port outside 0..65535 or not a number -> ValueError
        except ValueError:
            raise ValueError("BASE_URL: invalid port") from None
        if parts.username is not None or parts.password is not None:
            raise ValueError("BASE_URL: no username or password in the address")
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("BASE_URL: an absolute URL like https://host[:port] is required")
        if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
            raise ValueError("BASE_URL: http is allowed only for localhost")
        if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in value or "#" in value:
            raise ValueError("BASE_URL: no path, parameters or fragment")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
