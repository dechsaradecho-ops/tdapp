"""Worker #1 — Market Scanner (every 5 min).

Analyzes EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD: trend, volatility,
opportunity score → persists to market_analysis + signals when strong.

Data feed: live OHLCV from Yahoo Finance chart API (no key required).
Falls back to the random-walk demo feed when the live feed is unavailable.
"""
from __future__ import annotations

import logging
import random

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
from app.services.database import Database

log = logging.getLogger(__name__)

SCAN_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]


async def scan_once(db: Database) -> list[dict]:
    """One scan cycle. Live quotes when available, demo feed otherwise."""
    engine = StrategyEngine()
    results: list[dict] = []
    news_by_asset = _news_sentiment_by_asset(db)
    live_used, demo_used = 0, 0

    for asset in SCAN_ASSETS:
        ind = await _snapshot_for(asset, news_by_asset.get(asset, 0.0))
        if ind.source == "live":
            live_used += 1
        else:
            demo_used += 1
        opp = engine.opportunity_score(ind)
        row = {
            "asset": asset,
            "regime": regime_of(ind),
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

    log.info("Scan done: %d live, %d demo", live_used, demo_used)
    return results


async def _snapshot_for(asset: str, news_sentiment: float) -> IndicatorSnapshot:
    """Live snapshot from the quote feed; random-walk demo as fallback."""
    try:
        snaps = await quotes.fetch_all_snapshots([asset])
    except Exception as exc:  # defensive — never kill the scan
        log.warning("Quote fetch crashed for %s: %s — demo feed", asset, exc)
        snaps = {}

    snap = snaps.get(asset)
    if snap is None:
        return _random_walk_snapshot(asset, news_sentiment)

    snap["source"] = "live"
    snap["news_sentiment"] = news_sentiment
    snap["high_impact_event"] = False
    return IndicatorSnapshot(**snap)


def _news_sentiment_by_asset(db: Database) -> dict[str, float]:
    """Latest news_analysis sentiment per asset (0.0 when absent)."""
    rows = db.select("news_analysis", limit=20)
    by_asset: dict[str, float] = {}
    for r in rows:  # newest-first from db.select default ordering
        for asset in (r.get("affected_assets") or []):
            by_asset.setdefault(asset, float(r.get("sentiment") or 0.0))
    return by_asset


def _random_walk_snapshot(asset: str, news_sentiment: float = 0.0) -> IndicatorSnapshot:
    """Demo feed — fallback when the live data feed is unavailable."""
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
        news_sentiment=news_sentiment,
        high_impact_event=random.random() < 0.1,
    )
