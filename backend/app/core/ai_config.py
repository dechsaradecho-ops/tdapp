"""AI provider configuration loaded from ai.config.json (not env vars).

File location: backend/ai.config.json (gitignored — contains the secret key).
Format:
{
    "provider": "deepseek",            // "deepseek" | "glm"
    "api_key": "sk-...",               // secret key of the chosen provider
    "deepseek_base_url": "https://api.deepseek.com",
    "glm_base_url": "https://open.bigmodel.cn/api/paas/v4"
}
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_GLM_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"


class AIConfig:
    """Snapshot of ai.config.json."""

    def __init__(
        self,
        provider: str = "deepseek",
        api_key: str = "",
        deepseek_base_url: str = DEFAULT_DEEPSEEK_BASE_URL,
        glm_base_url: str = DEFAULT_GLM_BASE_URL,
    ) -> None:
        self.provider = provider
        self.api_key = api_key
        self.deepseek_base_url = deepseek_base_url
        self.glm_base_url = glm_base_url

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
        deepseek_base_url=str(data.get("deepseek_base_url", DEFAULT_DEEPSEEK_BASE_URL)),
        glm_base_url=str(data.get("glm_base_url", DEFAULT_GLM_BASE_URL)),
    )
