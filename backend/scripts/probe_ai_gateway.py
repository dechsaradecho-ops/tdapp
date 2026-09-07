r"""Probe AI gateway with the configured key — print status + response body.

Answers: (1) does the key work on the gateway in ai.config.json?
(2) what does the gateway's error body actually say? (the [AI ERROR] chat
message omits it, making 400s undiagnosable)

Run (cwd = backend so ai.config.json is found):
  Set-Location d:\tdapp\backend
  d:\tdapp\.venv\Scripts\python.exe -X utf8 scripts\probe_ai_gateway.py
Never prints the key itself.
"""
import json
import sys
from pathlib import Path

import httpx

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.ai_config import get_ai_config  # noqa: E402

c = get_ai_config()
print(f"provider(from file/inferred)={c.provider}")
print(f"base_url={c.base_url}")
print(f"model={c.model}")
print(f"key_loaded={'YES' if c.api_key else 'NO'} key_prefix={c.api_key[:4] + '...' if c.api_key else '-'}")

MSGS = [{"role": "user", "content": "reply with the single word: ok"}]


def probe(label: str, url: str, model: str) -> None:
    print(f"\n=== {label}: POST {url}/chat/completions model={model} ===")
    try:
        resp = httpx.post(
            f"{url}/chat/completions",
            headers={"Authorization": f"Bearer {c.api_key}"},
            json={
                "model": model,
                "messages": MSGS,
                "max_tokens": 32,
            },
            timeout=30.0,
        )
        print(f"status={resp.status_code}")
        body = resp.text
        # redact the key if the gateway echoes it
        body = body.replace(c.api_key, "<REDACTED>") if c.api_key else body
        print(f"body={body[:600]}")
    except httpx.RequestError as exc:
        print(f"REQUEST ERROR: {exc.__class__.__name__}: {exc}")


# 1) the gateway from ai.config.json with the configured model
probe("configured gateway", c.base_url, c.model)

# 2) same gateway, model "deepseek-chat" (in case the model name is the problem)
probe("configured gateway / deepseek-chat", c.base_url, "deepseek-chat")

# 3) api.deepseek.com with the same key (is this key a DeepSeek key?)
probe("api.deepseek.com / deepseek-chat", "https://api.deepseek.com", "deepseek-chat")
