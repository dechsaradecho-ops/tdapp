"""Signal generation + SEMI-AUTO approval endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
from app.models.schemas import FinalDecision, SignalProposal
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
            proposals.append(SignalProposal(
                asset=r["asset"], direction=r["direction"].upper(),
                confidence=float(r["confidence"]), entry=float(r["entry"] or 0),
                stop_loss=float(r["stop_loss"] or 0), take_profit=float(r["take_profit"] or 0),
                expected_rr=float(r["expected_rr"] or 2.0), risk_per_trade_pct=0.5,
                reason=[r.get("explanation", "")],
                recommendation=FinalDecision.trade,
            ))
        return proposals

    for asset, ind in DEMO.items():
        opp = engine.opportunity_score(ind)
        bullish = ind.ema_fast > ind.ema_slow
        proposals.append(engine.build_proposal(ind, opp, 0.5, bullish))
    return proposals


@router.post("/approve")
async def approve_signal(payload: ApprovalRequest, request: Request):
    """SEMI-AUTO flow: user approves/rejects → execute via broker → LINE confirmation."""
    db = request.app.state.db
    status = "approved" if payload.approve else "rejected"
    db.update("signals", payload.signal_id, {"approval": status})

    if payload.approve:
        broker = request.app.state.broker
        signals = db.select("signals", filters={"id": payload.signal_id}, limit=1)
        if signals:
            s = signals[0]
            from app.integrations.brokers import OrderRequest
            result = await broker.place_order(OrderRequest(
                user_id=s.get("user_id", "demo"),
                asset=s["asset"], direction=s["direction"].upper(), volume=0.01,
                entry_price=float(s["entry"] or 0), stop_loss=s.get("stop_loss"),
                take_profit=s.get("take_profit"),
            ))
            notifier = NotificationService(db, request.app.state.line)
            await notifier.notify(
                s.get("user_id", "demo"), "trade_opened",
                f"✅ Trade Opened\nAsset: {s['asset']}\nDirection: {s['direction']}"
                f"\nVolume: 0.01\nTicket: {result.broker_order_id}",
            )
            return {"status": "executed", "ticket": result.broker_order_id}
    return {"status": status}
