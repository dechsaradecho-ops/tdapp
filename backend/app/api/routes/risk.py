"""Risk status endpoint — evaluates a portfolio snapshot against Risk Engine limits."""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from app.api.routes.settings import get_app_settings
from app.engine.risk_engine import PortfolioSnapshot, risk_engine_for_settings
from app.models.schemas import RiskStatus

router = APIRouter()


class RiskCheckRequest(BaseModel):
    starting_capital: float = Field(gt=0)
    peak_equity: float = Field(gt=0)
    current_equity: float = Field(gt=0)
    realized_pnl_today: float = 0.0
    realized_pnl_week: float = 0.0
    realized_pnl_month: float = 0.0
    open_risk: float = 0.0


@router.post("/check", response_model=RiskStatus)
async def check_risk(payload: RiskCheckRequest, request: Request) -> RiskStatus:
    # Limits = the user's Settings page row (kill_daily_loss_pct etc.),
    # NOT the ENV defaults — the old code kept reporting 2% after the user
    # set daily loss to 5% (2026-09-07).
    s = get_app_settings(getattr(request.app.state, "db", None))
    snap = PortfolioSnapshot(
        starting_capital=payload.starting_capital,
        peak_equity=payload.peak_equity,
        current_equity=payload.current_equity,
        realized_pnl_today=payload.realized_pnl_today,
        realized_pnl_week=payload.realized_pnl_week,
        realized_pnl_month=payload.realized_pnl_month,
        open_risk=payload.open_risk,
    )
    return risk_engine_for_settings(s).check(snap)
