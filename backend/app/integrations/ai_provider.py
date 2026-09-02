"""AI provider abstraction — DeepSeek or GLM, selected by AI_PROVIDER env.

All AI responsibilities (market/news/sentiment analysis, feasibility, explanations,
chat) route through this client. The AI must never guarantee profit; the system
prompt enforces the safety contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import get_settings

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
        s = get_settings()
        self.api_key = s.deepseek_api_key
        self.base_url = s.deepseek_base_url.rstrip("/")
        self.model = "deepseek-chat"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
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


class GLMProvider(AIProvider):
    name = "glm"

    def __init__(self) -> None:
        s = get_settings()
        self.api_key = s.glm_api_key
        self.base_url = s.glm_base_url.rstrip("/")
        self.model = "glm-4-flash"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
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


class StubProvider(AIProvider):
    """Offline fallback so the platform runs without API keys."""

    name = "stub"

    async def chat(self, messages: list[dict[str, str]], temperature: float = 0.3) -> str:
        last = messages[-1]["content"] if messages else ""
        return (
            "[STUB AI] ยังไม่ได้ตั้งค่า AI provider (DEEPSEEK_API_KEY / GLM_API_KEY). "
            f"คำถามล่าสุด: “{last[:120]}”. ระบบวิเคราะห์เชิงกล (Goal/Risk/Opportunity engines) "
            "ยังทำงานปกติโดยไม่ต้องพึ่ง AI ภายนอก."
        )


def get_ai_provider() -> AIProvider:
    s = get_settings()
    if s.ai_provider == "glm" and s.glm_api_key:
        return GLMProvider()
    if s.ai_provider == "deepseek" and s.deepseek_api_key:
        return DeepSeekProvider()
    return StubProvider()


def build_context_block(context: dict[str, Any]) -> str:
    """Serialize engine outputs as grounded context for the AI."""
    lines = ["--- GROUNDED CONTEXT (from deterministic engines) ---"]
    for key, value in context.items():
        lines.append(f"{key}: {value}")
    lines.append("--- END CONTEXT ---")
    return "\n".join(lines)
