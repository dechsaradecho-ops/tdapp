"""Market regime + opportunity score endpoints.

Data priority: worker-produced rows in market_analysis → live quotes
(Yahoo Finance) → deterministic demo snapshot. The demo layer only exists
so the dashboard works on a fresh install with no DB and no network.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
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

    opportunities: list[AssetOpportunity] = []
    snapshot_of: dict[str, IndicatorSnapshot] = {}

    # 1) Worker-produced analysis (persisted by the Market Scanner every 5 min)
    rows = db.select("market_analysis", limit=25)
    for row in rows:
        if row["asset"] not in {o.asset for o in opportunities}:
            opportunities.append(AssetOpportunity(
                asset=row["asset"], score=float(row["confidence"]),
                band=StrategyEngine.band_of(float(row["confidence"])),
                reasons=[row.get("explanation", "")],
            ))

    # 2) No worker rows → fetch live quotes right now (no DB needed)
    if not opportunities:
        try:
            snaps = await quotes.fetch_all_snapshots(ASSETS)
            for asset in ASSETS:
                if asset in snaps:
                    ind = IndicatorSnapshot(**{**snaps[asset], "source": "live"})
                    snapshot_of[asset] = ind
                    opp = engine.opportunity_score(ind)
                    opportunities.append(AssetOpportunity(
                        asset=asset, score=opp.score, band=opp.band,
                        reasons=opp.reasons[:3],
                    ))
        except Exception:
            pass  # network/quote failure → final fallback below

    # 3) Deterministic demo (fresh install, no DB, no network)
    if not opportunities:
        opportunities = [engine.opportunity_score(DEMO[a]) for a in ASSETS]

    top_snapshot = snapshot_of.get(opportunities[0].asset) if opportunities else None
    if top_snapshot is None:
        # best-effort: any live snapshot we fetched
        top_snapshot = next(iter(snapshot_of.values()), None)

    if top_snapshot is not None:
        regime_str = regime_of(top_snapshot)
        try:
            regime = MarketRegime(regime_str)
        except ValueError:
            regime = MarketRegime.sideway
        confidence = round(top_snapshot.adx * 2.0, 1)
        if top_snapshot.ema_fast > top_snapshot.ema_slow:
            sentiment = "bullish"
        elif top_snapshot.adx < 20:
            sentiment = "neutral"
        else:
            sentiment = "bearish"
    else:
        top = opportunities[0] if opportunities else engine.opportunity_score(DEMO["XAUUSD"])
        regime = MarketRegime.bull_trend if top.score >= 61 else MarketRegime.sideway
        confidence = 72.0
        sentiment = "bullish" if top.score >= 61 else "bearish" if top.score < 45 else "neutral"

    return MarketSummary(
        regime=regime,
        confidence=confidence,
        explanation=REGIME_EXPLANATION.get(
            regime, "ตลาดไซด์เวย์ — ADX ต่ำกว่า 25, รอ breakout หรือเทรด range"),
        sentiment=sentiment,
        opportunities=sorted(opportunities, key=lambda o: -o.score),
    )
