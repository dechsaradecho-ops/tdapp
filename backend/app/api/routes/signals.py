"""Signal generation + SEMI-AUTO approval endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.integrations import quotes
from app.models.schemas import FinalDecision, SignalProposal
from app.services import execution
from app.services.notification_service import NotificationService

from app.api.routes.market import DEMO

router = APIRouter()


class ApprovalRequest(BaseModel):
    signal_id: str
    approve: bool


@router.get("/latest", response_model=list[SignalProposal])
async def latest_signals(request: Request) -> list[SignalProposal]:
    """Build explainable proposals from the latest opportunity snapshot."""
    db = request.app.state.db
    engine = StrategyEngine()
    proposals: list[SignalProposal] = []

    rows = db.select("signals", limit=5)
    if rows:
        for r in rows:
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
                expected_rr=float(r["expected_rr"] or 2.0), risk_per_trade_pct=0.5,
                reason=[r.get("explanation", "")],
                recommendation=FinalDecision.trade,
                limit_levels=ladder,
            ))
        return proposals

    # No stored signals → analyze live quotes right now (demo only as last resort)
    try:
        snaps = await quotes.fetch_all_snapshots(list(DEMO.keys()))
    except Exception:
        snaps = {}
    for asset, snap in snaps.items():
        ind = IndicatorSnapshot(**{**snap, "source": "live"})
        opp = engine.opportunity_score(ind)
        proposals.append(engine.build_proposal(
            ind, opp, 0.5, ind.ema_fast > ind.ema_slow))
    if proposals:
        return proposals

    for asset, ind in DEMO.items():
        opp = engine.opportunity_score(ind)
        bullish = ind.ema_fast > ind.ema_slow
        proposals.append(engine.build_proposal(ind, opp, 0.5, bullish))
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
    return {"status": "executed", "executed": True,
            "volume": report.size_lots, "checks": report.checks}
