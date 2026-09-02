"""Application settings loaded from environment variables (.env supported)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    frontend_origin: str = "http://localhost:3000"

    # Supabase
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_anon_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # AI provider: deepseek | glm
    ai_provider: str = "deepseek"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    glm_api_key: str = ""
    glm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"

    # LINE
    line_channel_access_token: str = ""
    line_channel_secret: str = ""

    # Risk defaults (percent)
    default_risk_per_trade: float = 0.5
    default_max_daily_loss: float = 2.0
    default_max_weekly_loss: float = 5.0
    default_max_monthly_loss: float = 8.0
    default_max_drawdown: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
