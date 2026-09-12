"""AI provider configuration — non-secret settings in ai.config.json, key in env var.

- File: backend/ai.config.json (NON-SECRET — safe to commit, no key inside):
    {
        "provider": "glm",                       // "deepseek" | "glm"
        "url": "https://opencode.ai/zen/go/v1",  // optional — defaults per provider
        "model": "glm-5.3-flash"                 // optional — defaults per provider
    }
- Secret key: env var AI_API_KEY (never stored in files to prevent leaks).

Runtime overrides (2026-09-12): the Settings page ("AI Chat" panel) can set
model name + base URL per account without touching the file/deploy — they live
in the trading_settings row (ai_model / ai_base_url) and are pushed into this
module by the settings router (and re-loaded from DB at startup). Precedence:

    Settings page (DB)  >  ai.config.json  >  per-provider defaults

An empty string in the DB means "not overridden — use the file/default value".
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
    "qwen": "https://opencode.ai/zen/go/v1",
}

DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "glm": "glm-5.3-flash",
    "qwen": "qwen3.8-flash",
}


class AIConfig:
    """Merged AI config: JSON (provider/url/model) + env var (api_key)."""

    def __init__(self, provider: str = "deepseek", api_key: str = "", url: str = "", model: str = "") -> None:
        self.provider = provider
        self.api_key = api_key
        # base_url must NOT include /chat/completions — ai_provider appends it.
        # Tolerate users pasting the full endpoint (2026-09-07 config mistake
        # that produced .../chat/completions/chat/completions → 404 → no reply).
        self.base_url = (
            (url or DEFAULT_BASE_URLS.get(provider, DEFAULT_BASE_URLS["deepseek"]))
            .rstrip("/")
            .removesuffix("/chat/completions")
        )
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


# ---- Settings-page overrides (DB-stored, non-secret) -----------------------
# Module-level instead of a function argument because get_ai_provider() is
# called from many places (chat, webhook, workers, /health). Set once per
# process — by the settings router on save, and from the DB at startup.
# Empty string = not overridden → value from ai.config.json / provider default.
_OVERRIDES: dict[str, str] = {"url": "", "model": ""}


def set_ai_overrides(url: str = "", model: str = "") -> None:
    """Install the DB-stored overrides and drop the cached AIConfig.

    Called by the settings router (save/reset) and by app startup, so the next
    get_ai_config()/get_ai_provider() reads the new value without a restart.
    """
    global _OVERRIDES
    new = {"url": (url or "").strip(), "model": (model or "").strip()}
    if new == _OVERRIDES:
        return
    _OVERRIDES = new
    get_ai_config.cache_clear()


def get_ai_overrides() -> dict[str, str]:
    """Current overrides ("" = falling back to ai.config.json/defaults)."""
    return dict(_OVERRIDES)


def clear_ai_overrides() -> None:
    """Drop overrides — used by /api/settings/reset."""
    set_ai_overrides()


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
    # Settings-page overrides win over the file (empty = keep the file value)
    url = _OVERRIDES.get("url") or url
    model = _OVERRIDES.get("model") or model
    # Secret key ALWAYS from env var (supports .env locally via pydantic-settings)
    api_key = get_settings().ai_api_key
    if provider not in DEFAULT_BASE_URLS:
        provider = "deepseek" if api_key.startswith("sk-") else "glm"
    return AIConfig(provider=provider, api_key=api_key, url=url, model=model)


def describe_ai_config() -> dict[str, str]:
    """Non-secret snapshot of the EFFECTIVE AI config (for /health + Settings UI).

    Includes where each value came from (`*_source`) so a wrong-looking model
    name can be traced to the Settings page vs ai.config.json vs the built-in
    default without reading env vars or the file by hand.
    """
    c = get_ai_config()
    ov = get_ai_overrides()
    return {
        "provider": c.provider,
        "model": c.model,
        "base_url": c.base_url,
        "model_source": "settings" if ov["model"] else "config-file",
        "url_source": "settings" if ov["url"] else "config-file",
        "configured": "yes" if c.is_configured else "no",
    }
