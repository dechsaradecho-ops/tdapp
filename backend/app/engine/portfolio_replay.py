"""Portfolio replay engine — Task 1: models, shared timeline, flat skeleton.

Pure replay code: no network, no live database, no broker calls.
Later tasks (signals, sizing, full loop) extend the `replay_portfolio`
skeleton without changing these model field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.integrations.quotes import Candle
from app.models.schemas import AppSettings

NEWS_WARNING = "News-optimistic: historical news replay is unavailable."


@dataclass(frozen=True)
class ReplayTrade:
    trade_id: str
    asset: str
    direction: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    volume: float
    pnl_usd: float
    r_multiple: float
    exit_reason: str
    mae_r: float
    mfe_r: float
    ambiguous: bool


@dataclass(frozen=True)
class EquityPoint:
    date: date
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float


@dataclass(frozen=True)
class ReplayMetrics:
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    trades: int = 0
    wins: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    average_r: float = 0.0
    max_drawdown_pct: float = 0.0
    ambiguous_pct: float = 0.0


@dataclass
class ReplayResult:
    trades: list[ReplayTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    metrics: ReplayMetrics = field(default_factory=ReplayMetrics)
    warnings: list[str] = field(default_factory=list)


def _candle_date(candle: Candle, asset: str) -> date:
    if candle.t <= 0:
        raise ValueError(f"{asset}: candle is missing its UTC timestamp")
    return datetime.fromtimestamp(candle.t, tz=timezone.utc).date()


def build_timeline(
    series: dict[str, list[Candle]],
) -> list[tuple[date, dict[str, Candle]]]:
    """Merge per-asset bars onto one chronological date axis.

    Each asset's bars must be sorted oldest-first with unique dates;
    unsorted or duplicate bars raise ValueError. Missing bars are never
    invented: a date entry contains only assets that traded that date.
    """
    by_asset: dict[str, dict[date, Candle]] = {}
    for asset in sorted(series):
        bars = series[asset]
        dated: dict[date, Candle] = {}
        prev: Optional[date] = None
        for bar in bars:
            day = _candle_date(bar, asset)
            if prev is not None and day < prev:
                raise ValueError(f"{asset}: bars are not sorted oldest-first")
            if day == prev or day in dated:
                raise ValueError(f"{asset}: duplicate bars for date {day}")
            dated[day] = bar
            prev = day
        by_asset[asset] = dated

    days = sorted({day for dated in by_asset.values() for day in dated})
    timeline: list[tuple[date, dict[str, Candle]]] = []
    for day in days:
        row = {asset: by_asset[asset][day] for asset in sorted(by_asset) if day in by_asset[asset]}
        timeline.append((day, row))
    return timeline


def replay_portfolio(
    series: dict[str, list[Candle]],
    settings: AppSettings,
    *,
    starting_capital: float = 1000.0,
    cost_config: Any | None = None,
    warnings: list[str] | None = None,
) -> ReplayResult:
    """Minimal flat skeleton (Task 1): no signals or exits yet.

    Validates the shared timeline, then returns no trades, zero metrics,
    and an equity curve starting at the user-supplied capital. Later tasks
    replace the flat body with the full replay loop.
    """
    _ = settings
    _ = cost_config
    timeline = build_timeline(series)

    merged = list(warnings) if warnings else []
    if NEWS_WARNING not in merged:
        merged.append(NEWS_WARNING)

    if not timeline:
        return ReplayResult(
            trades=[], equity_curve=[], metrics=ReplayMetrics(), warnings=merged
        )

    first_day = timeline[0][0]
    curve = [
        EquityPoint(
            date=first_day,
            equity=starting_capital,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
        )
    ]
    return ReplayResult(
        trades=[], equity_curve=curve, metrics=ReplayMetrics(), warnings=merged
    )
