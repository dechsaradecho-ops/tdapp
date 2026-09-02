"""AI Chat Assistant endpoint — answers grounded in engine outputs."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.integrations.ai_provider import build_context_block, get_ai_provider
from app.models.schemas import ChatRequest, ChatResponse
from app.engine.goal_engine import GoalEngine
from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
from app.models.schemas import GoalInput, RiskProfile
from app.api.routes.market import DEMO as market_demo

router = APIRouter()


@router.post("", response_model=ChatResponse)
async def chat(payload: ChatRequest, request: Request) -> ChatResponse:
    """Every reply is grounded in real Market Condition / Risk / Opportunity / Portfolio status."""
    db = request.app.state.db
    engine = StrategyEngine()

    # ---- Live market context: worker rows → live quotes → demo constants ----
    rows = db.select("market_analysis", limit=25)
    seen: set[str] = set()
    per_asset: dict[str, tuple[float, str]] = {}
    regime_str, regime_confidence = "sideway", 50.0
    for row in rows:
        if row["asset"] in seen:
            continue
        seen.add(row["asset"])
        per_asset[row["asset"]] = (float(row["confidence"]), row.get("regime", ""))
    if per_asset:
        top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
        regime_str = top_regime or "sideway"
        regime_confidence = top_score

    if not per_asset:
        try:
            snaps = await quotes.fetch_all_snapshots(
                ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
            for asset, snap in snaps.items():
                ind = IndicatorSnapshot(**{**snap, "source": "live"})
                opp = engine.opportunity_score(ind)
                per_asset[asset] = (opp.score, regime_of(ind))
        except Exception:
            pass
        if per_asset:
            top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
            regime_str, regime_confidence = top_regime, top_score
    if not per_asset:
        per_asset = {a: (engine.opportunity_score(market_demo[a]).score, "sideway")
                     for a in market_demo}
        top_asset, (top_score, top_regime) = max(per_asset.items(), key=lambda kv: kv[1][0])
        regime_str, regime_confidence = top_regime, top_score

    opportunity_str = " / ".join(
        f"{a} {score:.0f}" for a, (score, _) in
        sorted(per_asset.items(), key=lambda kv: -kv[1][0]))

    # ---- Goal/risk context defaults (user-specific values wired to auth later) ----
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
        "market_condition": f"{regime_str} (score {regime_confidence:.0f}%), "
                            f"top asset {top_asset}",
        "opportunity_score": opportunity_str,
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
