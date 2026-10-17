from decimal import Decimal
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

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
    title_model: str
    auto_allowed_models: list[str] = []
    max_upload_mb: int = 10
    enable_jobs: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
