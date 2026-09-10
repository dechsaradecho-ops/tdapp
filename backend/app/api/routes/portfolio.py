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
    # limit=50: one scanner cycle writes ~28 rows (full universe), so the
    # old limit=25 truncated the tail of each cycle. Dedupe to latest row
    # per asset and carry the scoring breakdown (score_reasons) so the
    # allocation rationale is verifiable — same pattern as market summary.
    rows = db.select("market_analysis", limit=50)
    opportunities: list[AssetOpportunity] = []
    if rows:
        seen: set[str] = set()
        for r in rows:
            asset = str(r.get("asset") or "").upper()
            if not asset or asset in seen:
                continue
            seen.add(asset)
            try:
                score = float(r["confidence"])
            except (KeyError, TypeError, ValueError):
                continue
            raw_reasons = str(r.get("score_reasons") or "")
            reasons = ([s for s in raw_reasons.split("\n") if s.strip()]
                       if raw_reasons
                       else [str(r.get("explanation") or "")])
            reasons = [s for s in reasons if s]
            opportunities.append(AssetOpportunity(
                asset=asset,
                score=score,
                band=StrategyEngine.band_of(score),
                reasons=reasons[:3] or ["คะแนนจาก Market Scanner"],
                score_reasons=reasons,
            ))

    if not opportunities:
        # No worker rows → live quotes over the tradable whitelist
        # (demo only as last resort). Carry the full scoring breakdown
        # so the allocation rationale stays verifiable on every tier.
        from app.integrations import quotes
        try:
            from app.api.routes.market import _market_assets
            snaps = await quotes.fetch_all_snapshots(
                _market_assets(db))
            for asset, snap in snaps.items():
                ind = IndicatorSnapshot(**{**snap, "source": "live"})
                opp = StrategyEngine().opportunity_score(ind)
                opportunities.append(AssetOpportunity(
                    asset=asset, score=opp.score, band=opp.band,
                    reasons=opp.reasons[:3],
                    score_reasons=list(opp.reasons)))
        except Exception:
            pass

    if not opportunities:
        from app.api.routes.market import DEMO
        opportunities = [StrategyEngine().opportunity_score(DEMO[a]) for a in DEMO]

    return PortfolioEngine().recommend(payload, opportunities)  # type: ignore[arg-type]
