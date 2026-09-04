"""Signal generation + SEMI-AUTO approval endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.integrations import quotes
from app.models.schemas import (FinalDecision, QuoteFeedStatus, SignalProposal)
from app.services import execution
from app.services.execution import (
    SIGNAL_TTL_MIN,
    expire_stale_pending_signals,
    now_iso,
)
from app.services.notification_service import NotificationService

from app.api.routes.market import DEMO
from app.api.routes.settings import get_app_settings

router = APIRouter()


class ApprovalRequest(BaseModel):
    signal_id: str
    approve: bool


async def _feed_status_for(assets: list[str]) -> QuoteFeedStatus | None:
    """Probe the intraday spot feed for the signal assets — never raises.

    Failures (timeout/HTTP/missing data) surface on the signals page so the
    user can see WHY an entry price may be stale instead of trusting a
    silently-fallen-back number.
    """
    if not assets:
        return None
    try:
        _prices, failures = await quotes.fetch_spot_prices(assets)
    except Exception as exc:  # fetch_spot_prices isolates per-asset errors;
        # this guard is for anything unexpected above it
        failures = {a: str(exc) for a in assets}
    return QuoteFeedStatus(
        state="ok" if not failures else "error",
        source="exchangerate+yahoo",
        fetched_at=datetime.now(timezone.utc),
        failed_assets=sorted(failures),
        message="; ".join(failures[a] for a in sorted(failures))[:300],
    )


async def _live_prices(assets: list[str]) -> tuple[dict[str, float], QuoteFeedStatus | None]:
    """Spot prices + feed health in one probe (prices feed the cards' live_price)."""
    if not assets:
        return {}, None
    try:
        prices, failures = await quotes.fetch_spot_prices(assets)
    except Exception as exc:
        prices, failures = {}, {a: str(exc) for a in assets}
    status = QuoteFeedStatus(
        state="ok" if not failures else "error",
        source="exchangerate+yahoo",
        fetched_at=datetime.now(timezone.utc),
        failed_assets=sorted(failures),
        message="; ".join(failures[a] for a in sorted(failures))[:300],
    )
    return prices, status


@router.get("/latest", response_model=list[SignalProposal])
async def latest_signals(request: Request) -> list[SignalProposal]:
    """Build explainable proposals from the latest opportunity snapshot."""
    db = request.app.state.db
    engine = StrategyEngine()
    proposals: list[SignalProposal] = []
    # One settings load per request — risk sizing must follow the user's
    # saved risk_per_trade_pct, not a hardcoded 0.5.
    s = get_app_settings(db)

    # Self-heal: pending signals older than 30 min leave the queue first —
    # otherwise the page pins yesterday's entry prices (e.g. GBPUSD stuck
    # at 1.26797 while the live rate is 1.35) in semi_auto/manual modes
    # where the auto-trader never runs its expiry pass.
    expire_stale_pending_signals(db)

    rows = db.select("signals", limit=20)
    # Only live candidates: pending (semi-auto queue) + approved (auto-fired).
    # 'expired'/'rejected' rows are history — showing them made the page look
    # stuck on yesterday's entries. Rows with no approval value (legacy) count
    # as pending.
    rows = [r for r in rows
            if (r.get("approval") or "pending") in ("pending", "approved")]
    # Feed health probe + live spot prices (shared by every card below) —
    # non-fatal. live_price lets each card show the CURRENT market price
    # next to its entry, so a stale entry is visible at a glance instead of
    # silently looking like a fresh quote.
    live_prices, feed = await _live_prices(
        sorted({str(r.get("asset") or "").upper() for r in rows}))
    # Pending signals past the TTL were already expired by the pass above;
    # approved rows always stay visible. Cards render NEWEST → OLDEST (the
    # user's requested order): approved cards first (newest approval first),
    # then pending cards newest-first — the newest setup is the first card.
    if rows:
        # NEWEST → OLDEST across the whole page (user request 2026-09-04):
        # approved cards first in approval order (newest first), then pending
        # cards newest-first — the newest setup is the first card.
        rows.sort(key=lambda r: (
            (r.get("approval") or "pending") == "approved",
            r.get("approved_at") or r.get("created_at") or "",
        ), reverse=True)
        # Read-time limit note — the scanner keeps generating signals all day
        # even past the user's limits (limits gate ORDER EXECUTION, not signal
        # generation), so pending cards that cannot fire right now carry the
        # reason: "ไม่ได้เปิดออเดอร์เพราะถึง limit แล้ว".
        open_rows = db.select("paper_trades", filters={"status": "open"},
                              limit=100)
        open_count = len(open_rows)
        # Assets that already hold an open position — the auto-trader skips
        # pending signals for these (duplicate-position gate), so the card
        # must say so instead of promising "~1 นาที" forever.
        open_assets = {str(r.get("asset") or "").upper() for r in open_rows}
        today = datetime.now(timezone.utc).date().isoformat()
        todays = db.select("paper_trades", limit=500)
        today_count = len([r for r in todays
                           if str(r.get("created_at", ""))[:10] == today
                           and r.get("status") != "rejected"])
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()[:10]
        week_count = len([r for r in todays
                          if str(r.get("created_at", "")) >= week_ago
                          and r.get("status") != "rejected"])
        for r in rows[:8]:
            entry = float(r["entry"] or 0)
            stop_loss = float(r["stop_loss"] or 0)
            sl_distance = abs(entry - stop_loss)
            ladder = (
                StrategyEngine.limit_ladder(r["direction"].upper(), entry, sl_distance)
                if entry > 0 and sl_distance > 0 else []
            )
            # Why this pending signal cannot become an order right now —
            # open-position-per-asset gate + limits (quality/regime throttles
            # are scanner-side).
            order_block = ""
            if (r.get("approval") or "pending") == "pending":
                asset = str(r.get("asset") or "").upper()
                if asset in open_assets:
                    order_block = (f"ไม่ได้เปิดออเดอร์ใหม่เพราะ {asset} "
                                   f"มีไม้เปิดอยู่แล้ว — รอปิดไม้เดิมก่อน")
                elif open_count >= s.max_open_positions:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(open positions {open_count}/"
                                   f"{s.max_open_positions})")
                elif today_count >= s.max_trades_daily:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(วันนี้ {today_count}/"
                                   f"{s.max_trades_daily})")
                elif week_count >= s.max_trades_weekly:
                    order_block = (f"ไม่ได้เปิดออเดอร์นี้เพราะถึง limit แล้ว "
                                   f"(สัปดาห์นี้ {week_count}/"
                                   f"{s.max_trades_weekly})")
            proposals.append(SignalProposal(
                asset=r["asset"], direction=r["direction"].upper(),
                confidence=float(r["confidence"]), entry=entry,
                stop_loss=stop_loss, take_profit=float(r["take_profit"] or 0),
                expected_rr=float(r["expected_rr"] or 2.0),
                risk_per_trade_pct=s.risk_per_trade_pct,
                reason=[r.get("explanation", "")],
                recommendation=FinalDecision.trade,
                limit_levels=ladder,
                approval=r.get("approval") or "pending",
                approved_at=r.get("approved_at"),
                created_at=r.get("created_at"),
                order_blocked=order_block or None,
                live_price=live_prices.get(str(r["asset"]).upper()),
                feed_status=feed,
            ))
        return proposals

    # No stored signals → analyze live quotes right now (demo only as last resort)
    try:
        snaps = await quotes.fetch_all_snapshots(list(DEMO.keys()))
    except Exception:
        snaps = {}
    live_feed = await _feed_status_for(sorted(snaps.keys()))
    for asset, snap in snaps.items():
        ind = IndicatorSnapshot(**{**snap, "source": "live"})
        opp = engine.opportunity_score(ind)
        proposals.append(engine.build_proposal(
            ind, opp, s.risk_per_trade_pct, ind.ema_fast > ind.ema_slow))
    if proposals:
        for p in proposals:
            p.feed_status = live_feed
        return proposals

    demo_feed = await _feed_status_for(list(DEMO.keys()))
    for asset, ind in DEMO.items():
        opp = engine.opportunity_score(ind)
        bullish = ind.ema_fast > ind.ema_slow
        proposals.append(engine.build_proposal(ind, opp, s.risk_per_trade_pct, bullish))
    for p in proposals:
        p.feed_status = demo_feed
    return proposals


@router.post("/approve")
async def approve_signal(payload: ApprovalRequest, request: Request):
    """SEMI-AUTO flow: user approves/rejects → execution gate → broker.

    The approved order goes through the SAME gate pipeline as the auto trader
    (pause → kill switch → frequency → news → correlation → risk officer) and
    is sized by risk_to_lot from settings — never the old hardcoded 0.01.
    """
    db = request.app.state.db
    status = "approved" if payload.approve else "rejected"
    db.update("signals", payload.signal_id, {"approval": status})

    if not payload.approve:
        return {"status": status}

    broker = request.app.state.broker
    signals = db.select("signals", filters={"id": payload.signal_id}, limit=1)
    if not signals:
        return {"status": status, "executed": False,
                "message": "signal row not found"}

    s = signals[0]
    srow = execution.get_app_settings(db)
    notifier = NotificationService(db, request.app.state.line)
    report = await execution.execute_signal(
        db, broker, notifier, srow,
        user_id=s.get("user_id", execution.DEFAULT_USER),
        asset=s["asset"], direction=s["direction"].upper(),
        entry=float(s["entry"] or 0), stop_loss=s.get("stop_loss"),
        take_profit=s.get("take_profit"),
        confidence=float(s.get("confidence") or 0),
        opportunity=float(s.get("opportunity_score") or s.get("confidence") or 0),
        signal_id=payload.signal_id, source="approved",
    )
    if not report.allowed:
        db.update("signals", payload.signal_id, {"approval": "rejected"})
        return {"status": "blocked", "executed": False,
                "rejects": report.rejects, "checks": report.checks}
    # Approval stamp — shown on the signals page (010 migration). The stamp
    # is a separate update on purpose: until 010 is applied the second call
    # is a no-op instead of failing the whole approval write.
    db.update("signals", payload.signal_id, {"approval": "approved"})
    db.update("signals", payload.signal_id, {"approved_at": now_iso()})
    return {"status": "executed", "executed": True,
            "volume": report.size_lots, "checks": report.checks}
