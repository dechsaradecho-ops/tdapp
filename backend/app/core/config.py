"""Application settings loaded from environment variables (.env supported)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_env: str = "development"
    log_level: str = "INFO"
    frontend_origin: str = "http://localhost:3000"

    # Supabase — new naming (publishable/secret, see Supabase dashboard)
    supabase_url: str = ""
    supabase_publishable_key: str = ""   # SUPABASE_PUBLISHABLE_KEY (replaces anon)
    supabase_secret_key: str = ""        # SUPABASE_SECRET_KEY (replaces service_role)
    supabase_jwks_url: str = ""          # SUPABASE_JWKS_URL (for JWT verification)
    # Legacy naming — still accepted for backward compatibility
    supabase_service_key: str = ""
    supabase_anon_key: str = ""

    @property
    def effective_service_key(self) -> str:
        """Backend/server key: SECRET_KEY preferred, falls back to legacy service_role."""
        return self.supabase_secret_key or self.supabase_service_key

    @property
    def effective_anon_key(self) -> str:
        """Public key: PUBLISHABLE_KEY preferred, falls back to legacy anon."""
        return self.supabase_publishable_key or self.supabase_anon_key

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Background workers — set ENABLE_WORKERS=1 on exactly ONE service
    # (e.g. the tdapp-api web service) to run the APScheduler in-process.
    enable_workers: bool = False

    # AI — secret key via env var AI_API_KEY (ป้องกันรั่วไหล)
    # provider + url อยู่ในไฟล์ ai.config.json (ไม่ใช่ความลับ)
    ai_api_key: str = ""

    # LINE
    line_channel_access_token: str = ""
    line_channel_secret: str = ""

    # Supabase table names for worker pipelines (worker #2 output, AI daily report log)
    news_analysis_table: str = "news_analysis"
    ai_daily_report_table: str = "ai_daily_report"

    # Risk defaults (percent)
    default_risk_per_trade: float = 0.5
    default_max_daily_loss: float = 2.0
    default_max_weekly_loss: float = 5.0
    default_max_monthly_loss: float = 8.0
    default_max_drawdown: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
