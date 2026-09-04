"""Worker #1 — Market Scanner (every 5 min).

Analyzes EURUSD, GBPUSD, USDJPY, AUDUSD, XAUUSD: trend, volatility,
opportunity score → persists to market_analysis + signals when strong.

Data feed: live OHLCV from Yahoo Finance chart API (no key required).
Falls back to the random-walk demo feed when the live feed is unavailable.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone

from app.api.routes.settings import get_app_settings
from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine, regime_of
from app.integrations import quotes
from app.models.schemas import (
    GOLD_ASSET,
    FrequencyEngine,
    TradeLimits,
    effective_min_confidence,
)
from app.services import signal_log
from app.services.database import Database

log = logging.getLogger(__name__)

SCAN_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]


async def scan_once(db: Database) -> list[dict]:
    """One scan cycle. Live quotes when available, demo feed otherwise."""
    engine = StrategyEngine()
    results: list[dict] = []
    news_by_asset = _news_sentiment_by_asset(db)
    live_used, demo_used = 0, 0
    # Settings loaded once per cycle — the per-asset Min Confidence (gold)
    # gate below needs them BEFORE the emit block.
    settings = get_app_settings(db)

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
        # Signal quality filter: confidence < min_confidence => NO TRADE
        # (gold uses its own Min Confidence (gold) threshold).
        min_conf = effective_min_confidence(settings, asset)
        if opp.score >= min_conf:
            # Market-closed guard — FX/gold trade Sun 21:00 UTC → Fri 21:00
            # UTC. Emitting signals into a closed market would pin entries at
            # Friday's close for the whole weekend (the "ราคาเก่า" complaint).
            if _market_closed():
                results[-1]["market_closed"] = True
                continue

            # Pending-dedup guard — a strong regime persists for hours, so
            # without this the scanner re-emits the SAME setup every cycle
            # (~4 min) and the queue floods with identical cards. Skip only
            # while a pending signal for the asset is still awaiting action.
            # NOTE: an OPEN position does NOT suppress signals — the user
            # wants the page to keep generating all day; the auto-trader's
            # open-position gate is what prevents duplicate orders.
            pending_assets = {
                str(r.get("asset") or "").upper()
                for r in db.select("signals", filters={"approval": "pending"},
                                   limit=200)
            }
            if asset in pending_assets:
                log.info("Signal for %s skipped: pending signal already "
                         "awaiting action for this asset", asset)
                results[-1]["dedup_skipped"] = True
                continue

            # Frequency guard — count today's emitted signals before adding another.
            # Limits come from the user's saved settings (Settings page) — NOT the
            # hardcoded moderate profile. Bug (2026-09-04): user raised max_trades_daily
            # to 20 but the scanner kept throttling at the profile default 6/day.
            today = datetime.now(timezone.utc).date().isoformat()
            todays = db.select("signals", limit=200)
            today_count = len([r for r in todays if str(r.get("created_at", ""))[:10] == today])
            freq = FrequencyEngine(
                settings.risk_profile,
                limits_override=TradeLimits(
                    max_trades_daily=settings.max_trades_daily,
                    max_trades_weekly=settings.max_trades_weekly,
                    max_open_positions=settings.max_open_positions,
                    risk_per_trade_pct=settings.risk_per_trade_pct,
                ),
                min_confidence=min_conf,
                drawdown_throttle_pct=settings.drawdown_throttle_pct,
            ).evaluate(
                confidence=opp.score, trades_today=today_count,
                regime=regime_of(ind), volatility_index=ind.volatility_index)
            # LIMITS DO NOT STOP SIGNAL GENERATION — they only stop ORDER
            # EXECUTION (the auto-trader/approve gate enforces them). The
            # signals page must keep generating cards all day so the user
            # always sees what the strategy WOULD trade; blocked cards carry
            # the reason ("ไม่ได้เปิดออเดอร์เพราะถึง limit") instead.
            blocked_reason = "" if freq.allowed else freq.reason

            bullish = ind.ema_fast > ind.ema_slow
            proposal = engine.build_proposal(
                ind, opp, risk_per_trade_pct=settings.risk_per_trade_pct,
                regime_bullish=bullish)
            # Re-anchor the proposal at the LIVE spot price before persisting.
            # ind.price comes from fetch_all_snapshots (Frankfurter daily ECB
            # closes + TwelveData gold) — one close per business day — so a
            # card created at 10:00 would carry the SAME price as one created
            # at 20:00 ("ราคาเก่า" on the signals page). The intraday spot
            # feed (Yahoo) is the freshest price we have; when it fails we
            # keep the snapshot price rather than guessing.
            try:
                spot, _spot_fail = await quotes.fetch_spot_prices([asset])
                live_price = float(spot.get(asset) or 0)
            except Exception as exc:
                log.warning("spot re-anchor failed for %s: %s", asset, exc)
                live_price = 0.0
            if live_price > 0 and live_price != ind.price:
                shift = live_price / ind.price
                proposal = proposal.model_copy(update={
                    "entry": round(live_price, 5),
                    "stop_loss": round(proposal.stop_loss * shift, 5),
                    "take_profit": round(proposal.take_profit * shift, 5),
                    "limit_levels": [
                        lv.model_copy(update={"price": round(lv.price * shift, 5)})
                        for lv in proposal.limit_levels
                    ],
                })
            inserted = db.insert("signals", {
                "asset": asset, "direction": proposal.direction.lower(),
                "confidence": proposal.confidence, "opportunity_score": opp.score,
                "entry": proposal.entry, "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit, "expected_rr": proposal.expected_rr,
                "approval": "pending", "explanation": " | ".join(proposal.reason[:4]),
            })
            # Lifecycle log: the signal was created (survives 7 days even after
            # the signals row itself expires/approves — audit trail).
            signal_log.log_event(
                db=db, event="created",
                signal_id=str((inserted or {}).get("id") or ""),
                asset=asset, direction=proposal.direction,
                confidence=proposal.confidence, entry=proposal.entry,
                stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
                source="scanner", reason=" | ".join(proposal.reason[:3]),
            )

    log.info("Scan done: %d live, %d demo", live_used, demo_used)
    return results


def _market_closed(now: datetime | None = None) -> bool:
    """True when the FX/gold market is closed (weekend).

    FX & gold trade continuously from Sunday 21:00 UTC to Friday 21:00 UTC
    (Friday close rolls into Saturday 00:00 UTC). Signals emitted during the
    weekend would carry Friday's close price all weekend — the "ราคาเก่า"
    complaint — so the scanner simply stops generating until reopen.
    """
    now = now or datetime.now(timezone.utc)
    wd, h = now.weekday(), now.hour
    if wd == 5:                      # Saturday
        return True
    if wd == 6 and h < 21:           # Sunday before 21:00 UTC reopen
        return True
    if wd == 4 and h >= 21:          # Friday after 21:00 UTC close
        return True
    return False


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
