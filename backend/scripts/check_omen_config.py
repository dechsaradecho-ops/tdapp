"""Verify ai.config.json (omen-alpha) end-to-end: config parse + live chat + live stream.

Run:  d:/tdapp/.venv/Scripts/python.exe -X utf8 backend/scripts/check_omen_config.py
Writes results to backend/scripts/_omen_check.txt (Thai-safe output).
"""
import asyncio
import json
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.ai_config import get_ai_config  # noqa: E402
from app.integrations.ai_provider import get_ai_provider  # noqa: E402

OUT = Path(__file__).with_name("_omen_check.txt")
lines = []

c = get_ai_config()
lines.append(f"provider={c.provider} model={c.model}")
lines.append(f"base_url={c.base_url}")
assert c.base_url.endswith("/v1"), f"base_url must end with /v1, got {c.base_url}"
assert "/chat/completions" not in c.base_url, "base_url must NOT contain /chat/completions"
lines.append("config OK (base_url clean)")

provider = get_ai_provider()
lines.append(f"provider class={provider.__class__.__name__} name={provider.name}")

MSGS = [{"role": "user", "content": "ตอบสั้น ๆ 1 ประโยค: ทองวันนี้ควรระวังอะไร"}]

t0 = time.time()
reply = asyncio.run(provider.chat(MSGS))
lines.append(f"--- chat() {time.time()-t0:.1f}s ---")
lines.append(reply[:400])
assert reply.strip(), "chat() returned EMPTY"
assert not reply.startswith("[AI ERROR]"), f"chat() error: {reply[:200]}"

t0 = time.time()


async def _stream():
    acc = ""
    async for piece in provider.chat_stream(MSGS):
        acc += piece
    return acc


streamed = asyncio.run(_stream())
lines.append(f"--- chat_stream() {time.time()-t0:.1f}s ---")
lines.append(streamed[:400])
assert streamed.strip(), "chat_stream() returned EMPTY (no-reply bug still present)"

lines.append("ALL OK — omen-alpha chat + stream both return content")
OUT.write_text("\n".join(lines), encoding="utf-8")
print("DONE")
