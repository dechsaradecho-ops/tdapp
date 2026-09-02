"""Portfolio recommendation endpoint."""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.engine.portfolio_engine import PortfolioEngine
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.models.schemas import AssetOpportunity, PortfolioInput, PortfolioRecommendation

router = APIRouter()


@router.post("/recommend", response_model=PortfolioRecommendation)
async def recommend(payload: PortfolioInput, request: Request) -> PortfolioRecommendation:
    """Allocate capital across assets per risk profile + latest opportunity scores.

    Weights always sum to 100% (Cash absorbs the remainder).
    """
    db = request.app.state.db
    rows = db.select("market_analysis", limit=25)
    opportunities = [
        AssetOpportunity(
            asset=r["asset"],
            score=float(r["confidence"]),
            band=StrategyEngine.band_of(float(r["confidence"])),
            reasons=[],
        )
        for r in rows
    ] if rows else []

    if not opportunities:
        # No worker rows → live quotes (demo only as last resort)
        from app.integrations import quotes
        try:
            snaps = await quotes.fetch_all_snapshots(
                ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"])
            for asset, snap in snaps.items():
                ind = IndicatorSnapshot(**{**snap, "source": "live"})
                opp = StrategyEngine().opportunity_score(ind)
                opportunities.append(AssetOpportunity(
                    asset=asset, score=opp.score, band=opp.band, reasons=[]))
        except Exception:
            pass

    if not opportunities:
        from app.api.routes.market import DEMO
        from app.engine.strategy_engine import StrategyEngine
        opportunities = [StrategyEngine().opportunity_score(DEMO[a]) for a in DEMO]

    return PortfolioEngine().recommend(payload, opportunities)  # type: ignore[arg-type]
