"""Deterministic rolling walk-forward helpers for the sandbox optimizer."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.engine.portfolio_replay import (
    CostConfig,
    ReplayResult,
    replay_portfolio,
)
from app.models.schemas import AppSettings
from app.services.sim_optimizer import slice_replay_result


@dataclass(frozen=True)
class WalkForwardFold:
    index: int
    train_start: date
    train_end: date
    test_start: date
    test_end: date


def _bar_date(bar: Any) -> date:
    raw = getattr(bar, "t", None)
    try:
        stamp = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("series bar is missing its UTC timestamp") from exc
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("series bar is missing its UTC timestamp")
    return datetime.fromtimestamp(stamp, tz=timezone.utc).date()


def series_date_bounds(series: dict) -> tuple[date, date]:
    dates = sorted({
        _bar_date(bar)
        for bars in (series or {}).values()
        for bar in (bars or [])
    })
    if len(dates) < 2:
        raise ValueError("series has fewer than two distinct dates")
    return dates[0], dates[-1]


def slice_series_by_date(series: dict, start: date, end: date) -> dict:
    if start >= end:
        raise ValueError("slice start must be before end")
    return {
        asset: [
            bar for bar in (bars or [])
            if start <= _bar_date(bar) < end
        ]
        for asset, bars in (series or {}).items()
    }


def replay_test_fold(
    series: dict,
    fold: WalkForwardFold,
    config: AppSettings,
    *,
    starting_capital: float,
    cost_config: CostConfig,
    warmup_days: int = 200,
) -> ReplayResult:
    if isinstance(warmup_days, bool) or not isinstance(warmup_days, int) or warmup_days < 0:
        raise ValueError("warmup_days must be a non-negative integer")
    context_start = fold.test_start - timedelta(days=warmup_days)
    context = slice_series_by_date(series, context_start, fold.test_end)
    full = replay_portfolio(
        context, config, starting_capital=starting_capital,
        cost_config=cost_config,
    )
    _, test_result = slice_replay_result(full, fold.test_start)
    return test_result


def build_walk_forward_folds(
    dataset_start: date,
    dataset_end: date,
    *,
    fold_count: int = 4,
    train_days: int = 365,
    test_days: int = 90,
) -> list[WalkForwardFold]:
    values = (fold_count, train_days, test_days)
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
           for value in values):
        raise ValueError("fold_count, train_days and test_days must be positive integers")
    if dataset_end <= dataset_start:
        raise ValueError("dataset_end must be after dataset_start")

    first_test_start = dataset_end - timedelta(
        days=test_days * fold_count)
    if first_test_start < dataset_start:
        raise ValueError("insufficient history for requested test folds")
    first_train_start = max(
        dataset_start, first_test_start - timedelta(days=train_days))
    if (first_test_start - first_train_start).days < 30:
        raise ValueError("insufficient history for training window")

    folds: list[WalkForwardFold] = []
    for index in range(fold_count):
        test_end = dataset_end - timedelta(
            days=test_days * (fold_count - index - 1))
        test_start = test_end - timedelta(days=test_days)
        train_end = test_start
        train_start = max(
            dataset_start, train_end - timedelta(days=train_days))
        if (train_end - train_start).days < 30:
            raise ValueError("insufficient history for training window")
        folds.append(WalkForwardFold(
            index=index,
            train_start=train_start,
            train_end=train_end,
            test_start=test_start,
            test_end=test_end,
        ))
    return folds
