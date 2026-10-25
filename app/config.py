from decimal import Decimal
from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


class Settings(BaseSettings):
    # Пустые значения в .env (например, TITLE_MODEL=) означают «по умолчанию».
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

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        """Только origin: https (http — лишь для localhost), без пути, query и фрагмента."""
        parts = urlsplit(value)
        try:
            _ = parts.port  # порт вне 0..65535 или не число -> ValueError
        except ValueError:
            raise ValueError("BASE_URL: некорректный порт") from None
        if parts.username is not None or parts.password is not None:
            raise ValueError("BASE_URL: без логина и пароля в адресе")
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise ValueError("BASE_URL: нужен абсолютный URL вида https://host[:port]")
        if parts.scheme == "http" and parts.hostname not in LOCAL_HOSTS:
            raise ValueError("BASE_URL: http допустим только для localhost")
        if parts.path not in ("", "/") or parts.query or parts.fragment or "?" in value or "#" in value:
            raise ValueError("BASE_URL: без пути, параметров и фрагмента")
        return value.rstrip("/")


@lru_cache
def get_settings() -> Settings:
    return Settings()
