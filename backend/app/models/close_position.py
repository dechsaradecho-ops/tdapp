"""Close-position result schema — shared by the manual close endpoint."""
from __future__ import annotations

from pydantic import BaseModel, Field


class ClosePositionResult(BaseModel):
    """Response of POST /api/trading/positions/close (manual close)."""
    ok: bool
    ticket: str = ""
    asset: str = ""
    direction: str = ""
    volume: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    holding_time_min: float | None = None
    close_reason: str = "manual"
    message: str = ""
    # Remaining open positions after this close (for the popup summary).
    remaining_open: int = 0
    # Total realized PnL across all closed trades (for the popup summary).
    total_realized_pnl: float = 0.0
    # Today's realized PnL (for the popup summary).
    pnl_today: float = 0.0
    # Win/loss counts across closed trades (for the popup summary).
    wins: int = 0
    losses: int = 0
    # Journal row id of the closed trade (for the popup to link to history).
    trade_id: str = ""
    # Non-fatal warnings (e.g. LINE notify failed) — close itself succeeded.
    warnings: list[str] = Field(default_factory=list)
