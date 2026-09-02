"""AI provider abstraction — DeepSeek or GLM, selected by ai.config.json.

All AI responsibilities (market/news/sentiment analysis, feasibility, explanations,
chat) route through this client. The AI must never guarantee profit; the system
prompt enforces the safety contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

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
- Answer in the user's language (Thai or English)."""


class AIProvider(ABC):
    name = "base"

    @abstractmethod
    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        """Return the assistant's reply text."""


class DeepSeekProvider(AIProvider):
    name = "deepseek"

    def __init__(self) -> None:
        c = get_ai_config()
        self.api_key = c.api_key
        self.base_url = c.base_url
        self.model = "deepseek-chat"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
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
        self.model = "glm-4-flash"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={
                        "model": self.model,
                        "messages": [{"role": "system", "content": SYSTEM_PROMPT}] + messages,
                        "temperature": temperature,
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


def get_ai_provider() -> AIProvider:
    c = get_ai_config()
    if not c.is_configured:
        return StubProvider()
    if c.provider == "glm":
        return GLMProvider()
    return DeepSeekProvider()


def build_context_block(context: dict[str, Any]) -> str:
    """Serialize engine outputs as grounded context for the AI."""
    lines = ["--- GROUNDED CONTEXT (from deterministic engines) ---"]
    for key, value in context.items():
        lines.append(f"{key}: {value}")
    lines.append("--- END CONTEXT ---")
    return "\n".join(lines)
