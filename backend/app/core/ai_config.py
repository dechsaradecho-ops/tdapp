"""AI provider configuration loaded from ai.config.json (not env vars).

File location: backend/ai.config.json (gitignored — contains the secret key).
Format:
{
    "provider": "deepseek",            // "deepseek" | "glm"
    "api_key": "sk-...",               // secret key of the chosen provider
    "url": ""                          // optional — defaults per provider if empty
}
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}


class AIConfig:
    """Snapshot of ai.config.json."""

    def __init__(self, provider: str = "deepseek", api_key: str = "", url: str = "") -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = (url or DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["deepseek"])).rstrip("/")

    @property
    def is_configured(self) -> bool:
        return self.api_key != ""


def _config_path() -> Path:
    """ai.config.json lives next to the backend root (cwd when running uvicorn)."""
    env_path = os.environ.get("AI_CONFIG_PATH")
    if env_path:
        return Path(env_path)
    return Path.cwd() / "ai.config.json"


@lru_cache
def get_ai_config() -> AIConfig:
    """Load and cache ai.config.json. Missing file → unconfigured (stub provider)."""
    path = _config_path()
    if not path.is_file():
        return AIConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return AIConfig()
    return AIConfig(
        provider=str(data.get("provider", "deepseek")).lower(),
        api_key=str(data.get("api_key", "")),
        url=str(data.get("url", "")),
    )
