"""AI explanation endpoint — generate narrative for a trade/signal."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.integrations.ai_provider import build_context_block, get_ai_provider

router = APIRouter()


class ExplainRequest(BaseModel):
    asset: str
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    opportunity_score: float
    regime: str = "bull_trend"


@router.post("/explain")
async def explain(payload: ExplainRequest, request: Request) -> dict:
    provider = get_ai_provider()
    context = build_context_block(payload.model_dump())
    reply = await provider.chat([{
        "role": "user",
        "content": (
            f"Explain this {payload.direction} {payload.asset} proposal with 3-5 numbered reasons, "
            f"referencing trend/momentum/volatility/news. End with a one-line risk caveat.\n{context}"
        ),
    }])
    return {"explanation": reply}
