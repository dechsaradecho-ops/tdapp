"""Risk status endpoint — evaluates a portfolio snapshot against Risk Engine limits."""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.engine.risk_engine import PortfolioSnapshot, RiskEngine
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
async def check_risk(payload: RiskCheckRequest) -> RiskStatus:
    snap = PortfolioSnapshot(
        starting_capital=payload.starting_capital,
        peak_equity=payload.peak_equity,
        current_equity=payload.current_equity,
        realized_pnl_today=payload.realized_pnl_today,
        realized_pnl_week=payload.realized_pnl_week,
        realized_pnl_month=payload.realized_pnl_month,
        open_risk=payload.open_risk,
    )
    return RiskEngine().check(snap)
