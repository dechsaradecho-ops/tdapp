"""Task 1: replay models and shared timeline (TDD)."""

from datetime import date, datetime, timezone

import pytest

from app.engine.portfolio_replay import (
    EquityPoint,
    ReplayMetrics,
    ReplayResult,
    ReplayTrade,
    build_timeline,
    replay_portfolio,
)
from app.integrations.quotes import Candle
from app.models.schemas import AppSettings


def _candle(day: int, close: float = 100.0) -> Candle:
    ts = int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
    return Candle(o=close, h=close + 1, l=close - 1, c=close, t=ts)


def test_timeline_merges_assets_without_future_bars():
    c1 = _candle(1)
    c3 = _candle(3)
    c2 = _candle(2)
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    timeline = build_timeline({"EURUSD": [c1, c3], "USDJPY": [c2]})
    assert [d for d, _ in timeline] == sorted([d1, d2, d3])
    assert set(timeline[0][1]) <= {"EURUSD", "USDJPY"}


def test_replay_result_starts_at_user_capital():
    flat_series = {"EURUSD": [_candle(1), _candle(2), _candle(3)]}
    result = replay_portfolio(flat_series, AppSettings(), starting_capital=1000)
    assert result.equity_curve[0].equity == 1000


def test_timeline_rejects_unsorted_bars():
    with pytest.raises(ValueError):
        build_timeline({"EURUSD": [_candle(2), _candle(1)]})


def test_timeline_rejects_duplicate_dates():
    with pytest.raises(ValueError):
        build_timeline({"EURUSD": [_candle(1), _candle(1)]})


def test_timeline_does_not_invent_missing_bars():
    timeline = build_timeline({"EURUSD": [_candle(1)], "USDJPY": [_candle(2)]})
    assert set(timeline[0][1]) == {"EURUSD"}
    assert set(timeline[1][1]) == {"USDJPY"}


def test_flat_replay_has_no_trades_zero_metrics_and_news_warning():
    flat_series = {"EURUSD": [_candle(1), _candle(2)]}
    result = replay_portfolio(flat_series, AppSettings(), starting_capital=1000)
    assert result.trades == []
    assert result.metrics.net_pnl == 0
    assert result.metrics.trades == 0
    assert result.metrics.profit_factor == 0
    assert result.warnings.count(
        "News-optimistic: historical news replay is unavailable."
    ) == 1


def test_empty_series_returns_empty_equity_curve_with_warning():
    result = replay_portfolio({}, AppSettings(), starting_capital=1000)
    assert result.equity_curve == []
    assert (
        "News-optimistic: historical news replay is unavailable." in result.warnings
    )


def test_caller_warnings_are_copied_and_deduplicated():
    caller = ["custom", "News-optimistic: historical news replay is unavailable."]
    flat_series = {"EURUSD": [_candle(1)]}
    result = replay_portfolio(
        flat_series, AppSettings(), starting_capital=1000, warnings=caller
    )
    assert caller == ["custom", "News-optimistic: historical news replay is unavailable."]
    assert result.warnings.count(
        "News-optimistic: historical news replay is unavailable."
    ) == 1
    assert "custom" in result.warnings
