"""Worker #1 — Market Scanner (every 5 min).

Analyzes EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD: trend, volatility,
opportunity score → persists to market_analysis + signals when strong.
"""
from __future__ import annotations

import asyncio
import logging
import random

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.services.database import Database

log = logging.getLogger(__name__)

SCAN_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]


async def scan_once(db: Database) -> list[dict]:
    """One scan cycle. Prices are demo/random-walk until a real data feed is wired."""
    engine = StrategyEngine()
    results: list[dict] = []

    for asset in SCAN_ASSETS:
        ind = _random_walk_snapshot(asset)
        opp = engine.opportunity_score(ind)
        row = {
            "asset": asset,
            "regime": _regime_of(ind),
            "sentiment": "bullish" if ind.ema_fast > ind.ema_slow else "bearish",
            "confidence": opp.score,
            "explanation": " | ".join(opp.reasons[:3]),
        }
        db.insert("market_analysis", row)
        results.append({"asset": asset, "opportunity": opp.model_dump(), "snapshot": vars(ind)})

        # Strong setups produce a signal (SEMI-AUTO approval flow)
        if opp.score >= 70:
            bullish = ind.ema_fast > ind.ema_slow
            proposal = engine.build_proposal(ind, opp, risk_per_trade_pct=0.5, regime_bullish=bullish)
            db.insert("signals", {
                "asset": asset, "direction": proposal.direction.lower(),
                "confidence": proposal.confidence, "opportunity_score": opp.score,
                "entry": proposal.entry, "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit, "expected_rr": proposal.expected_rr,
                "approval": "pending", "explanation": " | ".join(proposal.reason[:4]),
            })
    return results


def _random_walk_snapshot(asset: str) -> IndicatorSnapshot:
    """Deterministic-ish demo feed — replace with broker/quote API in production."""
    base = {"EURUSD": 1.085, "GBPUSD": 1.265, "USDJPY": 149.5, "AUDUSD": 0.652, "XAUUSD": 2400.0}[asset]
    drift = random.uniform(-0.3, 0.3)
    price = base * (1 + drift / 100)
    return IndicatorSnapshot(
        asset=asset, price=round(price, 5),
        ema_fast=price * (1 + drift / 500), ema_slow=price * (1 - drift / 800),
        adx=round(random.uniform(10, 40), 1),
        supertrend_dir=1 if drift > 0 else -1,
        rsi=round(random.uniform(35, 70), 1),
        macd_hist=round(random.uniform(-50, 80), 2),
        price_change_pct_20=drift,
        atr_pct=round(random.uniform(0.3, 1.6), 2),
        volatility_index=round(random.uniform(8, 25), 1),
        news_sentiment=round(random.uniform(-0.5, 0.8), 2),
        high_impact_event=random.random() < 0.1,
    )


def _regime_of(ind: IndicatorSnapshot) -> str:
    if ind.high_impact_event:
        return "news_driven_market"
    if ind.adx < 20:
        return "sideway"
    if ind.atr_pct > 2.5:
        return "high_volatility"
    if ind.ema_fast > ind.ema_slow:
        return "bull_trend" if ind.adx < 35 else "strong_bull_trend"
    return "bear_trend" if ind.adx < 35 else "strong_bear_trend"
