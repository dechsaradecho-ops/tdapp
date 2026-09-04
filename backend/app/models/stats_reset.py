"""Stats-reset result schema — shared by the reset endpoint (monitor page)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class StatsResetResult(BaseModel):
    """Response of POST /api/trading/stats/reset.

    Reset = delete every CLOSED paper trade (open positions are preserved —
    the SL/TP guard still needs them). All monitor stats (PnL วันนี้ / 7 วัน /
    รวม, Win Rate, ไม้ที่ปิดแล้ว) are recomputed from the remaining rows, so
    they come back zeroed while open positions keep trading normally.
    """
    ok: bool
    deleted: int = 0
    message: str = ""
    # Fresh stats after the reset (same shape as MonitorStats) so the UI can
    # update instantly without a second /monitor round-trip.
    stats: dict = Field(default_factory=dict)
    # Non-fatal warnings (e.g. LINE notify failed) — reset itself succeeded.
    warnings: list[str] = Field(default_factory=list)
