"""AI provider abstraction — DeepSeek or GLM, selected by ai.config.json.

All AI responsibilities (market/news/sentiment analysis, feasibility, explanations,
chat) route through this client. The AI must never guarantee profit; the system
prompt enforces the safety contract.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

import httpx

from app.core.ai_config import get_ai_config

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

    async def chat_stream(self, messages: list[dict[str, str]],
                          temperature: float = 0.3) -> AsyncIterator[str]:
        """Yield reply chunks as they arrive (OpenAI-compatible SSE streaming).

        Lets the UI show the answer progressively instead of a frozen
        "AI กำลังคิด..." for the full generation time.
        On upstream failure yields a single chunk with the same error text
        that `chat` returns, so the UI contract stays identical.
        """
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        "max_tokens": 1024,
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
                            yield piece
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            yield (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}). "
                "ตรวจสอบว่า AI_API_KEY ตรงกับ provider ใน ai.config.json หรือไม่ "
                "(key ของ DeepSeek ขึ้นต้น sk- / key ของ GLM เป็นรูปแบบ id.secret)"
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
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        "max_tokens": 1024,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}). "
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
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
                        "max_tokens": 1024,
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            return (
                f"[AI ERROR] เรียก {self.name} API ไม่สำเร็จ ({exc.__class__.__name__}). "
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
