"""Worker #1 — Market Scanner (every 5 min).

Analyzes the FULL priceable universe (quotes.SUPPORTED_ASSETS — 28 pairs)
for the dashboard's Confidence % display, but only allowed_assets (user's
Settings) can generate signals: trend, volatility, opportunity score →
persists to market_analysis + signals when strong (allowed assets only).

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
    is_market_closed,
)
from app.services import signal_log
from app.services.database import Database

log = logging.getLogger(__name__)

SCAN_ASSETS = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"]

# Per-cycle preloaded feeds (set by scan_once, read by _snapshot_for/_spot_for).
# Globals — not params — so existing unit-test monkeypatches of _snapshot_for
# keep working: tests bypass the batch path entirely, prod uses the batch.
_PRELOADED_SNAPS: dict[str, dict] = {}
_PRELOADED_SPOT: dict[str, float] = {}
_HIGH_IMPACT_EVENT: bool = False


def _scan_assets(settings) -> list[str]:
    """FULL analysis universe: every priceable pair (SUPPORTED_ASSETS).

    The user asked (2026-09-07) for Confidence % on ALL symbols to help
    decision-making — including pairs NOT in allowed_assets. allowed_assets
    remains the TRADING whitelist only (signal emission below).
    Falls back to the hardcoded SCAN_ASSETS when quotes is unavailable
    (fresh install) so the scanner never goes blind.
    """
    try:
        return list(quotes.SUPPORTED_ASSETS)
    except Exception:
        return list(SCAN_ASSETS)


def _tradable_assets(settings) -> set[str]:
    """Trading whitelist (allowed_assets) — only these may emit signals."""
    try:
        return set(settings.effective_assets() if settings else [])
    except Exception:
        return set()


async def scan_once(db: Database) -> list[dict]:
    """One scan cycle. Live quotes when available, demo feed otherwise."""
    global _PRELOADED_SNAPS, _PRELOADED_SPOT, _HIGH_IMPACT_EVENT
    engine = StrategyEngine()
    results: list[dict] = []
    news_by_asset = _news_sentiment_by_asset(db)
    live_used, demo_used = 0, 0
    # Settings loaded once per cycle — the per-asset Min Confidence (gold)
    # gate below needs them BEFORE the emit block.
    settings = get_app_settings(db)
    tradable = _tradable_assets(settings)
    universe = _scan_assets(settings)

    # ---- One batched quote fetch per cycle (was 28× fetch_all + N× spot) --
    # Snapshot batch covers the FULL analysis universe; spot batch covers the
    # tradable whitelist for the emit re-anchor. 30s/60s feed caches keep
    # both cheap; _snapshot_for/_spot_for read these globals first and only
    # single-fetch on cache miss (keeps unit-test monkeypatches working).
    _HIGH_IMPACT_EVENT = _calendar_high_impact(db, settings)
    try:
        _PRELOADED_SNAPS = await quotes.fetch_all_snapshots(universe)
    except Exception as exc:
        log.warning("scanner snapshot batch failed: %s — demo feed", exc)
        _PRELOADED_SNAPS = {}
    try:
        _spot_all, _spot_fail = await quotes.fetch_spot_prices(sorted(tradable))
        _PRELOADED_SPOT = dict(_spot_all or {})
    except Exception as exc:
        log.warning("scanner spot batch failed: %s", exc)
        _PRELOADED_SPOT = {}

    # ---- Hoisted per-cycle guards (were re-queried per strong asset) -----
    _market_is_closed = _market_closed()
    try:
        _all_signals = db.select("signals", limit=200)
    except Exception:
        _all_signals = []
    pending_assets = {
        str(r.get("asset") or "").upper()
        for r in _all_signals
        if str(r.get("approval") or "") == "pending"
    }
    _today = datetime.now(timezone.utc).date().isoformat()
    _today_count = len([r for r in _all_signals
                        if str(r.get("created_at", ""))[:10] == _today])

    for asset in universe:
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
            # Full scoring breakdown — home Opportunity-Score popup shows HOW
            # the score was computed (every component line, not just the 3
            # folded into explanation). Empty reasons never written ("" column).
            "score_reasons": "\n".join(opp.reasons),
        }
        db.insert("market_analysis", row)
        results.append({"asset": asset, "opportunity": opp.model_dump(), "snapshot": vars(ind)})

        # No mockup signals: snapshots from the random-walk demo feed (live
        # feed down) may still write market_analysis for the Confidence %
        # display, but must NEVER emit tradeable signals — demo prices never
        # passed any real market gate and look like real cards on /signals.
        if ind.source != "live":
            results[-1]["demo_no_signal"] = True
            continue

        # Only allowed_assets TRADE — every other pair is analysis-only
        # (Confidence % display). Everything below this guard is signal
        # generation, which must stay confined to the user's whitelist.
        if asset not in tradable:
            results[-1]["not_tradable"] = True
            continue

        # Strong setups produce a signal (SEMI-AUTO approval flow)
        # Signal quality filter: confidence < min_confidence => NO TRADE
        # (gold uses its own Min Confidence (gold) threshold).
        min_conf = effective_min_confidence(settings, asset)
        if opp.score >= min_conf:
            # Strategy D — gold breakout-retest gate. Gold's high ATR makes
            # narrow pullback entries unattractive; XAUUSD only trades an
            # active breakout (close above the prior 20-bar high) or a
            # successful retest of it. Prod evidence: gold scored 65+ every
            # day in a bull market and opened chase entries that lost.
            # gold_breakout_only=False restores the old behaviour.
            if (asset.upper() == GOLD_ASSET
                    and getattr(settings, "gold_breakout_only", True)
                    and ind.breakout_state <= 0):
                log.info("Signal for %s skipped: no breakout/retest setup "
                         "(strategy D gate)", asset)
                results[-1]["breakout_gate_skipped"] = True
                continue
            # Market-closed guard — FX/gold trade Sun 21:00 UTC → Fri 21:00
            # UTC. Emitting signals into a closed market would pin entries at
            # Friday's close for the whole weekend (the "ราคาเก่า" complaint).
            # Hoisted per-cycle (_market_is_closed) — was re-evaluated per asset.
            if _market_is_closed:
                results[-1]["market_closed"] = True
                continue

            # Pending-dedup guard — a strong regime persists for hours, so
            # without this the scanner re-emits the SAME setup every cycle
            # (~4 min) and the queue floods with identical cards. Skip only
            # while a pending signal for the asset is still awaiting action.
            # NOTE: an OPEN position does NOT suppress signals — the user
            # wants the page to keep generating all day; the auto-trader's
            # open-position gate is what prevents duplicate orders.
            # Hoisted per-cycle (pending_assets) — was re-queried per asset.
            if asset in pending_assets:
                log.info("Signal for %s skipped: pending signal already "
                         "awaiting action for this asset", asset)
                results[-1]["dedup_skipped"] = True
                continue

            # Frequency guard — count today's emitted signals before adding another.
            # Limits come from the user's saved settings (Settings page) — NOT the
            # hardcoded moderate profile. Bug (2026-09-04): user raised max_trades_daily
            # to 20 but the scanner kept throttling at the profile default 6/day.
            # Hoisted per-cycle (_today_count) — was re-queried per asset;
            # incremented below on every insert so intra-cycle emits count.
            today_count = _today_count
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
                regime_bullish=bullish,
                # Reward:Risk target (Settings) — TP = SL distance × rr_target.
                rr_target=max(0.5, float(getattr(settings, "rr_target", 2.0) or 2.0)),
                # SL distance clamp (Settings) — equal risk distance per asset.
                sl_min_pct=settings.sl_distance_min_pct,
                sl_max_pct=settings.sl_distance_max_pct,
                # Strategy D — gold SL anchored at the broken level (ATR
                # invalidation buffer) instead of the plain ATR stop.
                invalidation_level=(ind.breakout_level
                                    if asset.upper() == GOLD_ASSET else 0.0))
            # Re-anchor the proposal at the LIVE spot price before persisting.
            # ind.price comes from fetch_all_snapshots (Frankfurter daily ECB
            # closes + TwelveData gold) — one close per business day — so a
            # card created at 10:00 would carry the SAME price as one created
            # at 20:00 ("ราคาเก่า" on the signals page). The intraday spot
            # feed (Yahoo) is the freshest price we have; when it fails we
            # keep the snapshot price rather than guessing.
            # Preloaded batch first (_spot_for) — single-fetch only on miss.
            live_price = _spot_for(asset)
            if not live_price:
                try:
                    spot, _spot_fail = await quotes.fetch_spot_prices([asset])
                    live_price = float((spot or {}).get(asset) or 0)
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
                    "sltp_levels": [
                        lv.model_copy(update={
                            "stop_loss": round(lv.stop_loss * shift, 5),
                            "take_profit": round(lv.take_profit * shift, 5),
                        })
                        for lv in proposal.sltp_levels
                    ],
                })
            inserted = db.insert("signals", {
                "asset": asset, "direction": proposal.direction.lower(),
                "confidence": proposal.confidence, "opportunity_score": opp.score,
                "entry": proposal.entry, "stop_loss": proposal.stop_loss,
                "take_profit": proposal.take_profit, "expected_rr": proposal.expected_rr,
                # เก็บเหตุผลครบทุกข้อ (build_proposal ให้สูงสุด 6) — หน้า
                # signals แตกกลับเป็นรายข้อเพื่อจัดหมวด (เดิมตัด [:4])
                "approval": "pending", "explanation": " | ".join(proposal.reason),
            })
            # Intra-cycle count: today's tally grows with every emit so the
            # frequency note on later cards in the SAME cycle stays honest.
            _today_count += 1
            pending_assets.add(asset.upper())
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

    Thin wrapper over the shared helper in schemas (same rules power the
    /api/trading/session endpoint and the UI's "ตลาดปิด" banner).
    """
    return is_market_closed(now)


def _calendar_high_impact(db, settings) -> bool:
    """True when a high-impact event is near (calendar → news gate).

    Was hardcoded False — the scanner never saw news risk, so the
    news_driven_market regime and the -5 score penalty never fired.
    Single shared math: EconomicCalendarEngine.news_risk over the
    economic_calendar rows (same table the execution gate reads).
    Fail-safe False (no calendar → trade as usual).
    """
    try:
        from app.models.schemas import EconomicCalendarEngine, EconomicEvent
        rows = db.select("economic_calendar", limit=50)
        events: list[EconomicEvent] = []
        for r in rows or []:
            t = r.get("event_time")
            if isinstance(t, str):
                try:
                    t = datetime.fromisoformat(t.replace("Z", "+00:00"))
                except ValueError:
                    t = None
            if isinstance(t, datetime) and t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
            try:
                events.append(EconomicEvent(
                    event=r.get("event") or "event",
                    currency=r.get("currency", "USD"),
                    time_utc=t, impact=r.get("impact", "high")))
            except Exception:
                continue
        block_min = float(getattr(settings, "news_block_minutes", 30) or 30)
        status = EconomicCalendarEngine(
            block_minutes=block_min).news_risk(events)
        return str(getattr(status, "status", "SAFE")) != "SAFE"
    except Exception:
        return False


async def _snapshot_for(asset: str, news_sentiment: float) -> IndicatorSnapshot:
    """Live snapshot from the quote feed; random-walk demo as fallback.

    Reads the per-cycle batch (_PRELOADED_SNAPS) first — only single-fetches
    on cache miss (cold start / unit-test direct calls without scan_once).
    """
    snap = dict(_PRELOADED_SNAPS.get(asset) or {})
    if not snap:
        try:
            snaps = await quotes.fetch_all_snapshots([asset])
        except Exception as exc:  # defensive — never kill the scan
            log.warning("Quote fetch crashed for %s: %s — demo feed", asset, exc)
            snaps = {}
        snap = dict(snaps.get(asset) or {})

    if not snap:
        return _random_walk_snapshot(asset, news_sentiment)

    snap["source"] = "live"
    snap["news_sentiment"] = news_sentiment
    snap["high_impact_event"] = bool(_HIGH_IMPACT_EVENT)
    return IndicatorSnapshot(**snap)


def _spot_for(asset: str) -> float:
    """Preloaded intraday spot for the emit re-anchor (0.0 on miss)."""
    try:
        return float(_PRELOADED_SPOT.get(asset) or 0)
    except Exception:
        return 0.0


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
    base = {"EURUSD": 1.085, "GBPUSD": 1.265, "USDJPY": 149.5, "AUDUSD": 0.652, "XAUUSD": 2400.0}.get(asset)
    if base is None:
        # Non-legacy pair (SUPPORTED_ASSETS beyond the original 5) — derive a
        # plausible base from the pair structure so the demo feed still works.
        parts = quotes.fx_parts(asset)
        if parts and parts[1] == "JPY":
            base = 150.0
        elif parts:
            base = 1.0
        else:
            base = 100.0
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
