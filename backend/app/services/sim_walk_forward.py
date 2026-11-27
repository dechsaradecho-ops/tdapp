"""Deterministic rolling walk-forward helpers for the sandbox optimizer."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.engine.portfolio_replay import (
    CostConfig,
    ReplayResult,
    default_cost_config,
    replay_portfolio,
)
from app.models.schemas import AppSettings
from app.services.sim_market_data import load_dataset
from app.services.sim_optimizer import (
    SearchConfig,
    optimize_settings,
    slice_replay_result,
)


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


def _metrics_dict(metrics: Any) -> dict:
    if hasattr(metrics, "__dataclass_fields__"):
        return asdict(metrics)
    return {
        key: getattr(metrics, key)
        for key in (
            "net_pnl", "gross_pnl", "costs", "trades", "wins",
            "win_rate_pct", "profit_factor", "average_r",
            "max_drawdown_pct", "ambiguous_pct",
        )
        if hasattr(metrics, key)
    }


def run_walk_forward(
    db,
    dataset_version: str,
    assets: list[str],
    baseline_config: AppSettings,
    *,
    fold_count: int = 4,
    train_days: int = 365,
    test_days: int = 90,
    warmup_days: int = 200,
    max_iterations: int = 50,
    workers: int = 4,
    seed_start: int = 100,
    starting_capital: float = 1000,
    commission_per_lot_per_side: float = 3.5,
    exit_spread_mult: float = 0.5,
    progress_callback=None,
) -> dict:
    series = load_dataset(db, dataset_version, assets)
    if not series:
        raise ValueError("dataset unavailable or empty")
    dataset_start, dataset_end = series_date_bounds(series)
    folds = build_walk_forward_folds(
        dataset_start, dataset_end,
        fold_count=fold_count, train_days=train_days, test_days=test_days,
    )

    report = {
        "status": "complete",
        "dataset_version": dataset_version,
        "dataset_start": dataset_start.isoformat(),
        "dataset_end": dataset_end.isoformat(),
        "assets": sorted({str(asset).upper() for asset in assets}),
        "baseline_config": baseline_config.model_dump(mode="json"),
        "folds": [],
        "total_net_pnl": 0.0,
        "total_trades": 0,
        "positive_folds": 0,
        "pass_overall_profit": False,
        "note": "Historical rolling walk-forward; not future out-of-sample evidence.",
    }
    current_config = baseline_config

    for fold in folds:
        train_series = slice_series_by_date(
            series, fold.train_start, fold.train_end)
        cfg = SearchConfig(
            max_iterations=max_iterations,
            seed=seed_start + fold.index,
            workers=workers,
            commission_per_lot_per_side=commission_per_lot_per_side,
            exit_spread_mult=exit_spread_mult,
        )
        outcome = optimize_settings(
            train_series, current_config,
            starting_capital=starting_capital,
            search_config=cfg,
            progress_callback=progress_callback,
        )
        best = getattr(outcome, "best", None)
        if best is None:
            report["status"] = "stopped_no_recommendation"
            report["stopped_at_fold"] = fold.index + 1
            report["completed_folds"] = len(report["folds"])
            return report

        selected = best.config
        test_costs = replace(
            default_cost_config(selected),
            commission_per_lot_per_side=commission_per_lot_per_side,
            exit_spread_mult=exit_spread_mult,
        )
        test_result = replay_test_fold(
            series, fold, selected,
            starting_capital=starting_capital,
            cost_config=test_costs,
            warmup_days=warmup_days,
        )
        test_metrics = _metrics_dict(test_result.metrics)
        train_result = getattr(best, "train_result", None)
        report["folds"].append({
            "fold": fold.index + 1,
            "seed": cfg.seed,
            "train_start": fold.train_start.isoformat(),
            "train_end": fold.train_end.isoformat(),
            "test_start": fold.test_start.isoformat(),
            "test_end": fold.test_end.isoformat(),
            "selected_iteration": int(getattr(best, "iteration", 0) or 0),
            "selected_config": selected.model_dump(mode="json"),
            "train_metrics": _metrics_dict(
                getattr(train_result, "metrics", None)),
            "test_metrics": test_metrics,
        })
        report["total_net_pnl"] += float(test_metrics.get("net_pnl") or 0.0)
        report["total_trades"] += int(test_metrics.get("trades") or 0)
        if float(test_metrics.get("net_pnl") or 0.0) > 0:
            report["positive_folds"] += 1
        current_config = selected

    report["completed_folds"] = len(report["folds"])
    report["pass_overall_profit"] = report["total_net_pnl"] > 0
    return report
