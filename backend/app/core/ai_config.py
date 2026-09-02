"""AI provider configuration — non-secret settings in ai.config.json, key in env var.

- File: backend/ai.config.json (NON-SECRET — safe to commit, no key inside):
    {
        "provider": "glm",                       // "deepseek" | "glm"
        "url": "https://opencode.ai/zen/go/v1",  // optional — defaults per provider
        "model": "glm-5.3-flash"                 // optional — defaults per provider
    }
- Secret key: env var AI_API_KEY (never stored in files to prevent leaks).
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from app.core.config import get_settings

DEFAULT_BASE_URLS = {
    "deepseek": "https://api.deepseek.com",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
}

DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "glm": "glm-5.3-flash",
}


class AIConfig:
    """Merged AI config: JSON (provider/url/model) + env var (api_key)."""

    def __init__(self, provider: str = "deepseek", api_key: str = "", url: str = "", model: str = "") -> None:
        self.provider = provider
        self.api_key = api_key
        self.base_url = (url or DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["deepseek"])).rstrip("/")
        self.model = model or DEFAULT_MODELS.get(provider, DEFAULT_MODELS["deepseek"])

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
    """Merge ai.config.json (provider/url/model) with env var AI_API_KEY (secret).

    If the file is missing or incomplete, infer the provider from the key's shape:
    DeepSeek keys start with "sk-", anything else is treated as GLM.
    """
    path = _config_path()
    provider, url, model = "", "", ""
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            provider = str(data.get("provider", "")).lower()
            url = str(data.get("url", ""))
            model = str(data.get("model", ""))
        except (json.JSONDecodeError, OSError):
            pass
    # Secret key ALWAYS from env var (supports .env locally via pydantic-settings)
    api_key = get_settings().ai_api_key
    if provider not in DEFAULT_BASE_URLS:
        provider = "deepseek" if api_key.startswith("sk-") else "glm"
    return AIConfig(provider=provider, api_key=api_key, url=url, model=model)
