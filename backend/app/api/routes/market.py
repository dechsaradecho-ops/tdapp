"""Market regime + opportunity score endpoints.

Data priority: worker-produced rows in market_analysis → live quotes
(Yahoo Finance) → deterministic demo snapshot. The demo layer only exists
so the dashboard works on a fresh install with no DB and no network.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.routes.settings import get_app_settings
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
from app.models.schemas import (
    AppSettings,
    AssetOpportunity,
    MarketRegime,
    MarketSummary,
)

router = APIRouter()

ASSETS = ["XAUUSD", "EURUSD", "USDJPY", "GBPUSD", "AUDUSD"]


def _market_assets(db) -> list[str]:
    """Dashboard universe = user's allowed_assets (fallback: hardcoded)."""
    try:
        assets = get_app_settings(db).effective_assets()
    except Exception:
        assets = []
    return assets or list(ASSETS)

# Demo snapshot (until Market Scanner persists real rows into market_analysis)
DEMO: dict[str, IndicatorSnapshot] = {
    "XAUUSD": IndicatorSnapshot("XAUUSD", 2400.0, 2395.0, 2370.0, 28.0, 1, 58.0, 120.0, 0.85, 18.0, 0.6, False),
    "EURUSD": IndicatorSnapshot("EURUSD", 1.0850, 1.0840, 1.0810, 22.0, 1, 52.0, 8.0, 0.45, 12.0, 0.1, False),
    "USDJPY": IndicatorSnapshot("USDJPY", 149.50, 149.30, 149.80, 18.0, -1, 47.0, -30.0, 0.55, 11.0, -0.2, False),
    "GBPUSD": IndicatorSnapshot("GBPUSD", 1.2650, 1.2640, 1.2660, 15.0, 0, 44.0, -5.0, 0.40, 10.0, 0.0, False),
    "AUDUSD": IndicatorSnapshot("AUDUSD", 0.6520, 0.6510, 0.6530, 17.0, -1, 42.0, -6.0, 0.50, 14.0, -0.1, False),
}

REGIME_EXPLANATION = {
    MarketRegime.strong_bull_trend: "EMA50 > EMA200 และ ADX ≥ 35 บนสินทรัพย์นำ — เทรนด์ขาขึ้นแข็งแรง ตามเทรนด์ได้แต่ระวังไล่ราคา",
    MarketRegime.bull_trend: "EMA50 > EMA200 และ ADX ≥ 25 บนสินทรัพย์หลัก ตลาดมีแนวโน้มขาขึ้นแต่ยังไม่ร้อนแรงระดับ Strong Bull",
    MarketRegime.sideway: "ADX ต่ำกว่า 25 — ตลาดไร้เทรนด์ รอ breakout หรือเทรดในกรอบ",
    MarketRegime.high_volatility: "ATR สูงเกิน 2.5% — ตลาดผันผวนหนัก ลดขนาดโพซิชันและกว้าง SL",
    MarketRegime.bear_trend: "EMA50 < EMA200 — ตลาดมีแนวโน้มขาลง เน้นฝั่งขายหรือรอจังหวะกลับตัว",
    MarketRegime.strong_bear_trend: "EMA50 < EMA200 และ ADX ≥ 35 — เทรนด์ขาลงแข็งแรง หลีกเลี่ยงฝั่งซื้อสวนเทรนด์",
    MarketRegime.news_driven_market: "มีข่าว impact สูงใกล้ตัว — ตลาดขับเคลื่อนด้วยข่าว รอให้ตลาดนิ่งก่อนเข้าเทรด",
}


@router.get("/summary", response_model=MarketSummary)
async def market_summary(request: Request) -> MarketSummary:
    db = request.app.state.db
    engine = StrategyEngine()
    # One settings load drives the trading whitelist AND the confidence
    # gates echoed back to the frontend (per-symbol Confidence % badges).
    try:
        settings = get_app_settings(db)
    except Exception:
        settings = AppSettings()
    # Dashboard Confidence % covers the FULL priceable universe (user request
    # 2026-09-07: "ให้ประเมินตัวที่ไม่ได้อยู่ใน allowed_assets ด้วย") —
    # allowed_assets stays the trading whitelist, not the display universe.
    assets = list(quotes.SUPPORTED_ASSETS)

    opportunities: list[AssetOpportunity] = []
    # Header provenance: regime/sentiment per asset from the same source
    # that produced the score (scanner DB row → live snapshot → demo).
    regime_by_asset: dict[str, str] = {}
    sentiment_by_asset: dict[str, str] = {}

    # 1) Worker-produced analysis (persisted by the Market Scanner every 5 min)
    # One cycle writes one row per symbol (~28), so we need the NEWEST row per
    # asset — NOT 50 arbitrary rows. Without `order=` PostgREST returned an
    # arbitrary 50-row slice out of ~240k rows (audit 2026-09-11), so most of
    # the dashboard was silently rebuilt from live refetches below.
    try:
        rows = db.select("market_analysis", order="created_at", desc=True,
                         limit=4 * len(assets))
    except TypeError:  # very old fake select() without order/desc kwargs
        rows = db.select("market_analysis", limit=4 * len(assets))
    for row in rows:
        if row["asset"] not in {o.asset for o in opportunities}:
            # score_reasons (migration 026): full scoring breakdown from the
            # scanner — \n-joined component lines; falls back to the old 3-
            # reason explanation for rows written before the migration.
            raw_reasons = str(row.get("score_reasons") or "")
            reasons = ([s for s in raw_reasons.split("\n") if s.strip()]
                       if raw_reasons
                       else [row.get("explanation", "")])
            opportunities.append(AssetOpportunity(
                asset=row["asset"], score=float(row["confidence"]),
                band=StrategyEngine.band_of(float(row["confidence"])),
                reasons=reasons[:3],
                score_reasons=reasons,
            ))
            regime_by_asset[row["asset"]] = str(row.get("regime") or "")
            sentiment_by_asset[row["asset"]] = str(row.get("sentiment") or "")

    # 2) Partial fill: live-fetch ONLY symbols with no worker row yet (e.g.
    # the scanner hasn't cycled since SUPPORTED_ASSETS widened, or a pair is
    # missing) — never clobber fresh worker scores with a live refetch.
    have = {o.asset for o in opportunities}
    if set(assets) - have:
        try:
            snaps = await quotes.fetch_all_snapshots([a for a in assets if a not in have])
            for asset in assets:
                if asset in snaps and asset not in have:
                    ind = IndicatorSnapshot(**{**snaps[asset], "source": "live"})
                    regime_by_asset[asset] = regime_of(ind)
                    sentiment_by_asset[asset] = (
                        "bullish" if ind.ema_fast > ind.ema_slow else "bearish")
                    opp = engine.opportunity_score(ind)
                    opportunities.append(AssetOpportunity(
                        asset=asset, score=opp.score, band=opp.band,
                        reasons=opp.reasons[:3],
                        score_reasons=list(opp.reasons),
                    ))
        except Exception:
            pass  # network/quote failure → final fallback below

    # 3) Deterministic demo (fresh install, no DB, no network) — fills only
    # symbols still missing, so real data above is never overwritten.
    have = {o.asset for o in opportunities}
    demo_fill = [a for a in assets if a in DEMO and a not in have]
    if not opportunities:
        demo_fill = [a for a in ASSETS if a in DEMO]  # nothing at all → legacy 5
    for a in demo_fill:
        opportunities.append(engine.opportunity_score(DEMO[a]))
        if a not in regime_by_asset:
            regime_by_asset[a] = regime_of(DEMO[a])
            sentiment_by_asset[a] = (
                "bullish" if DEMO[a].ema_fast > DEMO[a].ema_slow else "bearish")

    # Header = the top-scoring asset's own regime/sentiment/score — the same
    # source that produced its Opportunity Score (DB row → live → demo).
    # The old code derived the header from a live snapshot that is EMPTY on
    # prod (DB covers all 28 pairs), so it always fell into the hardcoded
    # confidence=72.0 branch; the live branch used unclamped ADX*2 (can
    # exceed 100%) — a different metric from the % shown per symbol.
    ordered = sorted(opportunities, key=lambda o: -o.score)
    if ordered:
        top = ordered[0]
        try:
            regime = MarketRegime(regime_by_asset.get(top.asset, ""))
        except ValueError:
            regime = (MarketRegime.bull_trend
                      if top.score >= 61 else MarketRegime.sideway)
        raw_sent = str(sentiment_by_asset.get(top.asset, "") or "").lower()
        if raw_sent in ("bullish", "bearish", "neutral"):
            sentiment = raw_sent  # type: ignore[assignment]
        else:
            sentiment = ("bullish" if top.score >= 61
                         else "bearish" if top.score < 45 else "neutral")  # type: ignore[assignment]
        confidence = round(float(top.score), 1)
    else:
        top = engine.opportunity_score(DEMO["XAUUSD"])
        regime = MarketRegime.bull_trend if top.score >= 61 else MarketRegime.sideway
        confidence = round(float(top.score), 1)
        sentiment = "bullish" if top.score >= 61 else "bearish" if top.score < 45 else "neutral"  # type: ignore[assignment]

    return MarketSummary(
        regime=regime,
        confidence=confidence,
        explanation=REGIME_EXPLANATION.get(
            regime, "ตลาดไซด์เวย์ — ADX ต่ำกว่า 25, รอ breakout หรือเทรด range"),
        sentiment=sentiment,  # type: ignore[arg-type]
        opportunities=ordered,
        min_confidence=settings.min_confidence,
        min_confidence_gold=settings.min_confidence_gold,
    )


@router.get("/candles")
async def market_candles(asset: str, days: int = 60):
    """Daily OHLC candles for the position-chart popup (monitor row click).

    Reuses quotes.fetch_candles (Yahoo primary → Frankfurter/TwelveData
    fallback) so the popup shows the SAME price history the scanner trades
    on. days clamped to 30..120 (fetch_candles needs ≥30 bars for indicators).
    Fail-soft: feed failure → {"candles": [], "error": reason} with 200 so
    the popup still shows entry/SL/TP + position details without a chart.
    """
    import httpx

    a = str(asset or "").upper()
    n = max(30, min(int(days or 60), 120))
    if not quotes.is_supported_asset(a):
        return {"asset": a, "candles": [], "count": 0,
                "error": f"no feed mapping for {a}"}
    try:
        async with httpx.AsyncClient() as client:
            bars = await quotes.fetch_candles(a, client, days=n)
        return {"asset": a,
                "candles": [{"o": b.o, "h": b.h, "l": b.l, "c": b.c}
                            for b in bars],
                "count": len(bars), "error": ""}
    except Exception as exc:
        return {"asset": a, "candles": [], "count": 0,
                "error": f"{a}: {exc}"}
