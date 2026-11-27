from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.engine.portfolio_replay import (
    EquityPoint,
    ReplayMetrics,
    ReplayResult,
    ReplayTrade,
    default_cost_config,
)
from app.integrations.quotes import Candle
from app.models.schemas import AppSettings
from app.services import sim_walk_forward


def bar(day: date) -> Candle:
    stamp = int(datetime(day.year, day.month, day.day,
                         tzinfo=timezone.utc).timestamp())
    return Candle(o=1.0, h=1.0, l=1.0, c=1.0, t=stamp)


def test_build_walk_forward_folds_is_chronological_and_non_overlapping():
    folds = sim_walk_forward.build_walk_forward_folds(
        date(2020, 1, 1), date(2026, 11, 25),
        fold_count=4, train_days=365, test_days=90,
    )
    assert [(f.test_start, f.test_end) for f in folds] == [
        (date(2025, 11, 30), date(2026, 2, 28)),
        (date(2026, 2, 28), date(2026, 5, 29)),
        (date(2026, 5, 29), date(2026, 8, 27)),
        (date(2026, 8, 27), date(2026, 11, 25)),
    ]
    assert folds[0].train_start == date(2024, 11, 30)
    assert folds[0].train_end == folds[0].test_start
    assert all(folds[i].test_end == folds[i + 1].test_start for i in range(3))


def test_build_walk_forward_folds_rejects_insufficient_history():
    with pytest.raises(ValueError, match="insufficient history"):
        sim_walk_forward.build_walk_forward_folds(
            date(2025, 1, 1), date(2025, 6, 1),
            fold_count=4, train_days=365, test_days=90,
        )


def test_slice_series_by_date_is_start_inclusive_end_exclusive():
    series = {"EURUSD": [bar(date(2026, 1, 1)), bar(date(2026, 1, 2)),
                         bar(date(2026, 1, 3))]}
    sliced = sim_walk_forward.slice_series_by_date(
        series, date(2026, 1, 2), date(2026, 1, 3))
    assert [b.t for b in sliced["EURUSD"]] == [bar(date(2026, 1, 2)).t]


def test_series_date_bounds_uses_utc_dates():
    series = {"EURUSD": [bar(date(2026, 1, 3)), bar(date(2026, 1, 1))]}
    assert sim_walk_forward.series_date_bounds(series) == (
        date(2026, 1, 1), date(2026, 1, 3))


def trade(trade_id: str, exit_day: date) -> ReplayTrade:
    return ReplayTrade(
        trade_id=trade_id, asset="EURUSD", direction="BUY",
        entry_date=exit_day - timedelta(days=1), entry_price=1.0,
        exit_date=exit_day, exit_price=1.0, volume=0.01,
        pnl_usd=1.0, r_multiple=0.1, exit_reason="time",
        mae_r=0.0, mfe_r=0.1, ambiguous=False,
    )


def point(day: date) -> EquityPoint:
    return EquityPoint(
        date=day, equity=1001.0, realized_pnl=1.0,
        unrealized_pnl=0.0, drawdown_pct=0.0,
    )


def test_replay_test_fold_uses_warmup_but_excludes_pre_test_exits(monkeypatch):
    fold = sim_walk_forward.WalkForwardFold(
        0, date(2025, 1, 1), date(2025, 11, 30),
        date(2025, 11, 30), date(2026, 2, 28),
    )
    full = ReplayResult(
        trades=[trade("before", date(2025, 11, 29)),
                trade("carry", date(2025, 12, 2)),
                trade("inside", date(2026, 2, 1))],
        equity_curve=[point(date(2025, 11, 29)), point(date(2025, 12, 2)),
                      point(date(2026, 2, 1))],
        metrics=ReplayMetrics(), warnings=[],
    )
    seen = {}

    def fake_replay(series, config, *, starting_capital, cost_config, **kwargs):
        seen["capital"] = starting_capital
        seen["cost_config"] = cost_config
        return full

    monkeypatch.setattr(sim_walk_forward, "replay_portfolio", fake_replay)
    result = sim_walk_forward.replay_test_fold(
        {"EURUSD": [bar(date(2025, 6, 1))]}, fold, AppSettings(),
        starting_capital=1000, cost_config=default_cost_config(AppSettings()),
        warmup_days=200,
    )
    assert seen["capital"] == 1000
    assert [t.trade_id for t in result.trades] == ["carry", "inside"]


def make_series(start: date, end: date) -> dict:
    days = []
    day = start
    while day <= end:
        days.append(bar(day))
        day += timedelta(days=1)
    return {"EURUSD": days}


def test_run_walk_forward_refits_only_on_training_windows(monkeypatch):
    series = make_series(date(2020, 1, 1), date(2026, 11, 25))
    configs = [AppSettings(max_hold_days=5), AppSettings(max_hold_days=6),
               AppSettings(max_hold_days=7), AppSettings(max_hold_days=8),
               AppSettings(max_hold_days=9)]
    seen = []

    def fake_optimize(train_series, baseline, *, starting_capital,
                      search_config, **kwargs):
        idx = len(seen)
        seen.append((baseline, search_config.seed,
                     sim_walk_forward.series_date_bounds(train_series)))
        best = configs[idx + 1]
        return SimpleNamespace(best=SimpleNamespace(
            config=best, iteration=9,
            train_result=SimpleNamespace(metrics=ReplayMetrics(net_pnl=1)),
        ))

    def fake_test(full_series, fold, config, **kwargs):
        return ReplayResult(
            trades=[], equity_curve=[],
            metrics=ReplayMetrics(net_pnl=10, trades=1), warnings=[],
        )

    monkeypatch.setattr(sim_walk_forward, "load_dataset", lambda *a: series)
    monkeypatch.setattr(sim_walk_forward, "optimize_settings", fake_optimize)
    monkeypatch.setattr(sim_walk_forward, "replay_test_fold", fake_test)

    result = sim_walk_forward.run_walk_forward(
        object(), "v20261126-120951-1d5254", ["EURUSD"], configs[0],
        fold_count=4, train_days=365, test_days=90,
    )
    assert [s[1] for s in seen] == [100, 101, 102, 103]
    assert all(seen[i][0] is configs[i] for i in range(4)), [
        (i, seen[i][0] is configs[i], seen[i][0].max_hold_days,
         configs[i].max_hold_days)
        for i in range(4)
    ]
    assert result["total_net_pnl"] == 40
    assert result["pass_overall_profit"] is True
    assert result["folds"][-1]["test_end"] == "2026-11-26"


def test_run_walk_forward_stops_when_refit_has_no_recommendation(monkeypatch):
    monkeypatch.setattr(
        sim_walk_forward, "load_dataset",
        lambda *a: make_series(date(2020, 1, 1), date(2026, 11, 25)),
    )
    monkeypatch.setattr(
        sim_walk_forward, "optimize_settings",
        lambda *a, **k: SimpleNamespace(best=None),
    )
    result = sim_walk_forward.run_walk_forward(
        object(), "v20261126-120951-1d5254", ["EURUSD"], AppSettings(),
    )
    assert result["status"] == "stopped_no_recommendation"
    assert result["pass_overall_profit"] is False


def test_run_walk_forward_rejects_invalid_warmup_before_search(monkeypatch):
    monkeypatch.setattr(
        sim_walk_forward, "load_dataset",
        lambda *a: make_series(date(2020, 1, 1), date(2026, 11, 25)),
    )
    monkeypatch.setattr(
        sim_walk_forward, "optimize_settings",
        lambda *a, **k: SimpleNamespace(best=None),
    )
    with pytest.raises(ValueError, match="warmup_days"):
        sim_walk_forward.run_walk_forward(
            object(), "v20261126-120951-1d5254", ["EURUSD"], AppSettings(),
            warmup_days=-1,
        )
