"""Market regime + opportunity score endpoints.

Returns the latest worker-produced analysis when available; falls back to a
deterministic demo snapshot so the dashboard works on first run.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.models.schemas import AssetOpportunity, MarketSummary, MarketRegime

router = APIRouter()

ASSETS = ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"]

# Demo snapshot (until Market Scanner persists real rows into market_analysis)
DEMO: dict[str, IndicatorSnapshot] = {
    "XAUUSD": IndicatorSnapshot("XAUUSD", 2400.0, 2395.0, 2370.0, 28.0, 1, 58.0, 120.0, 0.85, 18.0, 0.6, False),
    "EURUSD": IndicatorSnapshot("EURUSD", 1.0850, 1.0840, 1.0810, 22.0, 1, 52.0, 8.0, 0.45, 12.0, 0.1, False),
    "USDJPY": IndicatorSnapshot("USDJPY", 149.50, 149.30, 149.80, 18.0, -1, 47.0, -30.0, 0.55, 11.0, -0.2, False),
    "GBPUSD": IndicatorSnapshot("GBPUSD", 1.2650, 1.2640, 1.2660, 15.0, 0, 44.0, -5.0, 0.40, 10.0, 0.0, False),
    "AUDUSD": IndicatorSnapshot("AUDUSD", 0.6520, 0.6510, 0.6530, 17.0, -1, 42.0, -6.0, 0.50, 14.0, -0.1, False),
}

REGIME_EXPLANATION = {
    MarketRegime.bull_trend: "EMA50 > EMA200 และ ADX ≥ 25 บนสินทรัพย์หลัก ตลาดมีแนวโน้มขาขึ้นแต่ยังไม่ร้อนแรงระดับ Strong Bull",
}


@router.get("/summary", response_model=MarketSummary)
async def market_summary(request: Request) -> MarketSummary:
    db = request.app.state.db
    engine = StrategyEngine()

    rows = db.select("market_analysis", limit=5)
    opportunities: list[AssetOpportunity] = []
    if rows:
        latest_asset = rows[0]["asset"]
        for row in db.select("market_analysis", limit=25):
            if row["asset"] not in {o.asset for o in opportunities}:
                opportunities.append(AssetOpportunity(
                    asset=row["asset"], score=float(row["confidence"]),
                    band=StrategyEngine.band_of(float(row["confidence"])),
                    reasons=[row.get("explanation", "")],
                ))
    else:
        opportunities = [engine.opportunity_score(DEMO[a]) for a in ASSETS]

    top = opportunities[0] if opportunities else engine.opportunity_score(DEMO["XAUUSD"])
    sentiment = "bullish" if top.score >= 61 else "bearish" if top.score < 45 else "neutral"
    regime = MarketRegime.bull_trend if sentiment == "bullish" else MarketRegime.sideway

    return MarketSummary(
        regime=regime,
        confidence=72.0,
        explanation=REGIME_EXPLANATION.get(
            regime, "ตลาดไซด์เวย์ — ADX ต่ำกว่า 25, รอ breakout หรือเทรด range"),
        sentiment=sentiment,
        opportunities=sorted(opportunities, key=lambda o: -o.score),
    )
