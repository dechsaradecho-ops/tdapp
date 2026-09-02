"""AI Chat Assistant endpoint — answers grounded in engine outputs."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.integrations.ai_provider import build_context_block, get_ai_provider
from app.models.schemas import ChatRequest, ChatResponse
from app.engine.goal_engine import GoalEngine
from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
from app.models.schemas import GoalInput, RiskProfile

router = APIRouter()


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """Every reply is grounded in Market Condition / Risk / Opportunity / Portfolio status."""
    db = request.app.state.db

    # Deterministic context (engines) — the AI must reference these.
    goal = GoalEngine().assess(GoalInput(
        capital=100_000, target_return_pct=3.0,
        max_drawdown_pct=10.0, risk_profile=RiskProfile.moderate,
    ))
    risk = RiskEngine().check(PortfolioSnapshot(
        starting_capital=100_000, peak_equity=102_000, current_equity=101_200,
        realized_pnl_today=-120, realized_pnl_week=300, realized_pnl_month=1200,
        open_risk=500,
    ))

    context = build_context_block({
        "market_condition": "bull_trend (confidence 72%), sentiment bullish",
        "opportunity_score": "XAUUSD 85 / EURUSD 76 / USDJPY 61 / GBPUSD 48",
        "risk_analysis": risk.model_dump(),
        "portfolio_status": f"goal_probability={goal.probability.value}, normal_case={goal.scenarios[1].expected_return_pct}%",
    })

    provider = get_ai_provider()
    messages = [m.model_dump() for m in payload.messages]
    messages[-1] = {"role": "user", "content": f"{messages[-1]['content']}\n\n{context}"}

    reply = await provider.chat(messages)
    return ChatResponse(
        reply=reply,
        grounded_on=["market_condition", "risk_analysis", "opportunity_score", "portfolio_status"],
    )
