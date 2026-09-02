"""Risk Engine — enforces per-trade and cumulative loss limits.

Default config (percent of equity):
    risk_per_trade 0.5 | max_daily_loss 2 | max_weekly_loss 5
    max_monthly_loss 8 | max_drawdown 10

When any limit is breached → trading paused, manual review required.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.core.config import get_settings
from app.models.schemas import RiskStatus


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_monthly_loss_pct: float = 8.0
    max_drawdown_pct: float = 10.0

    @classmethod
    def from_settings(cls) -> "RiskConfig":
        s = get_settings()
        return cls(
            risk_per_trade_pct=s.default_risk_per_trade,
            max_daily_loss_pct=s.default_max_daily_loss,
            max_weekly_loss_pct=s.default_max_weekly_loss,
            max_monthly_loss_pct=s.default_max_monthly_loss,
            max_drawdown_pct=s.default_max_drawdown,
        )


@dataclass
class PortfolioSnapshot:
    """Input metrics the Risk Engine evaluates each cycle."""

    starting_capital: float
    peak_equity: float
    current_equity: float
    realized_pnl_today: float
    realized_pnl_week: float
    realized_pnl_month: float
    open_risk: float  # sum of (entry - stop_loss) * exposure for open positions
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    @property
    def drawdown_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.current_equity) / self.peak_equity * 100.0)

    @property
    def daily_loss_pct(self) -> float:
        return max(0.0, -self.realized_pnl_today / self.starting_capital * 100.0)

    @property
    def weekly_loss_pct(self) -> float:
        return max(0.0, -self.realized_pnl_week / self.starting_capital * 100.0)

    @property
    def monthly_loss_pct(self) -> float:
        return max(0.0, -self.realized_pnl_month / self.starting_capital * 100.0)

    @property
    def open_risk_pct(self) -> float:
        if self.current_equity <= 0:
            return 0.0
        return self.open_risk / self.current_equity * 100.0


class RiskEngine:
    """Evaluates a portfolio snapshot against the risk config."""

    def __init__(self, config: Optional[RiskConfig] = None) -> None:
        self.config = config or RiskConfig.from_settings()
        self._paused_until: Optional[datetime] = None

    # ------------------------------------------------------------------
    def check(self, snap: PortfolioSnapshot) -> RiskStatus:
        breaches: list[str] = []

        if snap.daily_loss_pct >= self.config.max_daily_loss_pct:
            breaches.append(f"Daily loss {snap.daily_loss_pct:.2f}% ≥ limit {self.config.max_daily_loss_pct:.2f}%")
        if snap.weekly_loss_pct >= self.config.max_weekly_loss_pct:
            breaches.append(f"Weekly loss {snap.weekly_loss_pct:.2f}% ≥ limit {self.config.max_weekly_loss_pct:.2f}%")
        if snap.monthly_loss_pct >= self.config.max_monthly_loss_pct:
            breaches.append(f"Monthly loss {snap.monthly_loss_pct:.2f}% ≥ limit {self.config.max_monthly_loss_pct:.2f}%")
        if snap.drawdown_pct >= self.config.max_drawdown_pct:
            breaches.append(f"Drawdown {snap.drawdown_pct:.2f}% ≥ limit {self.config.max_drawdown_pct:.2f}%")

        # Guard: a new trade's risk must fit inside the remaining daily budget.
        if snap.open_risk_pct + self.config.risk_per_trade_pct > self.config.max_daily_loss_pct:
            breaches.append(
                f"Open risk {snap.open_risk_pct:.2f}% + new trade risk {self.config.risk_per_trade_pct:.2f}% "
                f"would exceed daily loss limit {self.config.max_daily_loss_pct:.2f}%"
            )

        paused = bool(breaches)
        if paused:
            self.pause()

        return RiskStatus(
            risk_level=self._level(snap, paused),
            current_drawdown_pct=round(snap.drawdown_pct, 2),
            max_drawdown_pct=self.config.max_drawdown_pct,
            daily_loss_pct=round(snap.daily_loss_pct, 2),
            weekly_loss_pct=round(snap.weekly_loss_pct, 2),
            monthly_loss_pct=round(snap.monthly_loss_pct, 2),
            open_risk_pct=round(snap.open_risk_pct, 2),
            trading_paused=paused,
            message=(
                "TRADING PAUSED — MANUAL REVIEW REQUIRED. " + "; ".join(breaches)
                if paused
                else "All risk metrics within limits."
            ),
        )

    # ------------------------------------------------------------------
    def position_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        contract_value_per_unit: float = 1.0,
    ) -> float:
        """Risk-based position sizing: size such that (entry-SL) risk == risk_per_trade % of equity."""
        risk_amount = equity * self.config.risk_per_trade_pct / 100.0
        stop_distance = math.fabs(entry_price - stop_loss_price)
        if stop_distance <= 0:
            return 0.0
        size = risk_amount / (stop_distance * contract_value_per_unit)
        return round(size, 2)

    def pause(self, until: Optional[datetime] = None) -> None:
        self._paused_until = until or datetime.max.replace(tzinfo=timezone.utc)

    def resume(self) -> None:
        self._paused_until = None

    @property
    def is_paused(self) -> bool:
        if self._paused_until is None:
            return False
        return datetime.now(timezone.utc) < self._paused_until

    @staticmethod
    def _level(snap: PortfolioSnapshot, paused: bool) -> str:
        dd = snap.drawdown_pct
        if paused or dd >= 8:
            return "critical"
        if dd >= 5:
            return "high"
        if dd >= 2:
            return "medium"
        return "low"

    # ------------------------------------------------------------------
    @staticmethod
    def week_start(day: date) -> date:
        """Monday of the given week."""
        return day.fromordinal(day.toordinal() - day.weekday())
