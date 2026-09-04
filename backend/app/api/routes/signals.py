"""Signal generation + SEMI-AUTO approval endpoints."""
from __future__ import annotations

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
    from datetime import datetime, timezone
    return QuoteFeedStatus(
        state="ok" if not failures else "error",
        source="exchangerate+yahoo",
        fetched_at=datetime.now(timezone.utc),
        failed_assets=sorted(failures),
        message="; ".join(failures[a] for a in sorted(failures))[:300],
    )


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
    # Feed health probe (shared by every card below) — non-fatal.
    feed = await _feed_status_for(
        sorted({str(r.get("asset") or "").upper() for r in rows}))
    # Pending signals past the TTL were already expired by the pass above;
    # approved rows always stay visible — the user explicitly asked to see
    # them at the bottom with an approval-time stamp (GBPUSD 1.26797 bug).
    if rows:
        # Pending first (waiting for action), then approved (already fired)
        # newest-first so the newest approval is always the top card.
        rows.sort(key=lambda r: (
            (r.get("approval") or "pending") == "approved",
            r.get("approved_at") or r.get("created_at") or "",
        ))
        for r in rows[:8]:
            entry = float(r["entry"] or 0)
            stop_loss = float(r["stop_loss"] or 0)
            sl_distance = abs(entry - stop_loss)
            ladder = (
                StrategyEngine.limit_ladder(r["direction"].upper(), entry, sl_distance)
                if entry > 0 and sl_distance > 0 else []
            )
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
