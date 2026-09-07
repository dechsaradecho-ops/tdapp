"""AI provider abstraction — DeepSeek or GLM, selected by ai.config.json.

All AI responsibilities (market/news/sentiment analysis, feasibility, explanations,
chat) route through this client. The AI must never guarantee profit; the system
prompt enforces the safety contract.
"""
from __future__ import annotations

import json
import uuid
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from app.core.ai_config import get_ai_config

# OpenCode Zen Go gateway REQUIRES a stable per-conversation session id in the
# `x-opencode-session` header (see https://opencode.ai/docs/go/#where-can-i-use-it).
# Without it the gateway answers 400 MissingSessionID even when the key is valid —
# the 2026-09-07 "[AI ERROR] ... HTTPStatusError" chat failure. One id per provider
# instance is enough: it is stable for the process lifetime (routing + prompt
# caching) and requests here are stateless completions, not a chat thread.
_OPENCODE_SESSION_ID = str(uuid.uuid4())


def _gateway_headers(api_key: str) -> dict[str, str]:
    """Headers for OpenAI-compatible gateway calls (opencode zen et al).

    - x-opencode-session: required by the Go gateway (400 MissingSessionID otherwise)
    - User-Agent: gateway asks clients to identify themselves rather than send
      a generic SDK user agent
    """
    return {
        "Authorization": f"Bearer {api_key}",
        "x-opencode-session": _OPENCODE_SESSION_ID,
        "User-Agent": "tdapp-backend/1.0",
    }


def _gateway_error_detail(exc: Exception) -> str:
    """Extract the gateway's error body from an HTTPStatusError so 400/401/429
    responses are diagnosable (the plain class name hides the real reason)."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            body = resp.json()
            err = body.get("error", body)
            if isinstance(err, dict):
                return str(err.get("type") or err.get("message") or body)[:200]
            return str(body)[:200]
        except Exception:  # noqa: BLE001 — non-JSON body
            return resp.text[:200]
    return ""

SYSTEM_PROMPT = """You are a professional AI Wealth & Trading Advisor for Forex, Gold (XAUUSD),
Crypto, Indices and CFDs. Your goal is to help users assess the FEASIBILITY of return targets
under risk constraints — never to promise profits.

Rules:
- Always explain your reasoning (numbered reasons), never answer with just BUY/SELL.
- Ground every answer in: Market Condition, Risk Analysis, Opportunity Score, Portfolio Status.
- Never guarantee profit, never fabricate returns, never suggest risk beyond the user's limits,
  never bypass risk management, never open orders beyond max drawdown.
- Answer in the user's language (Thai or English).
- Be CONCISE: lead with the direct answer, keep it under ~150 words, use short
  bullets. The reply streams to a chat widget — long essays delay the user.
- Start replying immediately; do not begin with preamble such as summarizing
  the system status before answering.
- When the user asks for a FULL analysis (e.g. "วิเคราะห์เต็ม", "extended analysis",
  "สรุปทั้งหมด"), structure the reply in these 11 sections in order:
  NEWS & CALENDAR / SESSION ANALYSIS / CORRELATION ANALYSIS / ORDER STRATEGY /
  EXECUTION PLAN / RISK OFFICER REVIEW / JOURNAL INSIGHT / BACKTEST RESULT /
  PAPER TRADING STATUS / KILL SWITCH STATUS / FINAL DECISION — one short line
  per section, ending with a clear FINAL DECISION (TRADE or WAIT + reason)."""


class AIProvider(ABC):
    name = "base"

    # set by subclasses in __init__ (used by the shared streaming impl)
    api_key: str = ""
    base_url: str = ""
    model: str = ""

    @abstractmethod
    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        """Return the assistant's reply text."""

    @staticmethod
    def _extract(data: dict) -> str:
        """message.content, falling back to reasoning_content (reasoning models
        can return an empty content with only reasoning — surface something)."""
        msg = data["choices"][0]["message"]
        content = (msg.get("content") or "").strip()
        if content:
            return content
        reasoning = (msg.get("reasoning_content") or "").strip()
        if reasoning:
            return (
                "[AI ERROR] โมเดลคิดเสร็จแต่ไม่ได้เขียนคำตอบ (reasoning กินโควตา "
                "max_tokens) — ส่วนสรุปจากการคิดล่าสุด:\n" + reasoning[-600:]
            )
        return ""

    async def chat_stream(self, messages: list[dict[str, str]],
                          temperature: float = 0.3) -> AsyncIterator[str]:
        """Yield reply chunks as they arrive (OpenAI-compatible SSE streaming).

        Lets the UI show the answer progressively instead of a frozen
        "AI กำลังคิด..." for the full generation time.
        On upstream failure yields a single chunk with the same error text
        that `chat` returns, so the UI contract stays identical.

        Reasoning-model guard (2026-09-07 "no reply" bug): reasoning models
        (omen-alpha ฯลฯ) emit delta.reasoning_content while thinking and may
        burn the whole max_tokens budget BEFORE any delta.content — the
        stream then ends empty and the UI shows "(no reply)". Two defenses:
        1) max_tokens 2048 gives the model headroom to finish thinking and
        still write the answer;
        2) if zero content chunks were emitted, fall back to the non-stream
        endpoint which assembles message.content server-side after reasoning.
        """
        emitted = False
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=_gateway_headers(self.api_key),
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        # 2048: reasoning models spend completion tokens thinking
                        # before the answer — 1024 can run out mid-reasoning
                        "max_tokens": 2048,
                        "stream": True,
                    },
                ) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0].get("delta", {})
                        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                            continue
                        piece = delta.get("content")
                        if piece:
                            emitted = True
                            yield piece
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            detail = _gateway_error_detail(exc)
            yield (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}"
                f"{' — ' + detail if detail else ''}). "
                "ตรวจสอบว่า AI_API_KEY ตรงกับ provider/url ใน ai.config.json หรือไม่ "
                "(provider ปัจจุบันดูได้ที่ GET /health) — key ต้องเป็นของ gateway "
                "ตาม url ใน ai.config.json เท่านั้น"
            )
            return
        if not emitted:
            # stream ended with zero content (reasoning ate the budget, or the
            # gateway dropped the text) — one retry via the non-stream endpoint
            reply = await self.chat(messages, temperature)
            yield reply or (
                "[AI ERROR] โมเดลไม่ส่งข้อความกลับ (empty completion) — ลองถามใหม่อีกครั้ง"
            )


class DeepSeekProvider(AIProvider):
    name = "deepseek"

    def __init__(self) -> None:
        c = get_ai_config()
        self.api_key = c.api_key
        self.base_url = c.base_url
        self.model = c.model

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        try:
            # read=120: long Thai answers from flash models can exceed 60s
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=_gateway_headers(self.api_key),
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        "max_tokens": 2048,
                    },
                )
                resp.raise_for_status()
                return self._extract(resp.json())
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            detail = _gateway_error_detail(exc)
            return (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}"
                f"{' — ' + detail if detail else ''}). "
                "ตรวจสอบว่า AI_API_KEY ตรงกับ provider ใน ai.config.json หรือไม่ "
                "(key ของ DeepSeek ขึ้นต้น sk- / key ของ GLM เป็นรูปแบบ id.secret)"
            )


class GLMProvider(AIProvider):
    name = "glm"

    def __init__(self) -> None:
        c = get_ai_config()
        self.api_key = c.api_key
        self.base_url = c.base_url
        self.model = c.model

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        try:
            # read=120: long Thai answers from flash models can exceed 60s
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers=_gateway_headers(self.api_key),
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        "max_tokens": 2048,
                    },
                )
                resp.raise_for_status()
                return self._extract(resp.json())
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            detail = _gateway_error_detail(exc)
            return (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}"
                f"{' — ' + detail if detail else ''}). "
                "ตรวจสอบว่า AI_API_KEY ตรงกับ provider ใน ai.config.json หรือไม่ "
                "(key ของ DeepSeek ขึ้นต้น sk- / key ของ GLM เป็นรูปแบบ id.secret)"
            )


class StubProvider(AIProvider):
    """Offline fallback so the platform runs without API keys."""

    name = "stub"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        last = messages[-1]["content"] if messages else ""
        return (
            "[STUB AI] ยังไม่ได้ตั้งค่า AI_API_KEY. "
            f"คำถามล่าสุด: “{last[:120]}”. ระบบวิเคราะห์เชิงกล (Goal/Risk/Opportunity engines) "
            "ยังทำงานปกติโดยไม่ต้องพึ่ง AI ภายนอก."
        )

    async def chat_stream(self, messages: list[dict[str, str]],
                          temperature: float = 0.3) -> AsyncIterator[str]:
        yield await self.chat(messages, temperature)


def get_ai_provider() -> AIProvider:
    c = get_ai_config()
    if not c.is_configured:
        return StubProvider()
    if c.provider in ("glm", "qwen"):
        # qwen via OpenAI-compatible gateways (e.g. opencode zen) — same wire format
        return GLMProvider()
    return DeepSeekProvider()


def build_context_block(context: dict[str, Any]) -> str:
    """Serialize engine outputs as grounded context for the AI."""
    lines = ["--- GROUNDED CONTEXT (from deterministic engines) ---"]
    for key, value in context.items():
        lines.append(f"{key}: {value}")
    lines.append("--- END CONTEXT ---")
    return "\n".join(lines)
