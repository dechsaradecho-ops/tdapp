"""Optimizer models — SearchConfig/dataclass structural tests (no DB/broker).

Run from backend/: pytest tests/test_sim_optimizer.py -v
"""
from __future__ import annotations

import pytest

from app.services.sim_optimizer import (
    BOOLEAN_SEARCH_DIMS,
    SEARCH_BOUNDS,
    SL_DISTANCE_MODES,
    IterationResult,
    OptimizationResult,
    SearchConfig,
    candidate_from_baseline,
    canonical_config_hash,
    canonical_config_json,
    config_diff,
    optimize_settings,
    slice_replay_result,
    split_series,
)
import app.services.sim_optimizer as sim_optimizer
from app.engine.portfolio_replay import (
    EquityPoint,
    ReplayMetrics,
    ReplayResult,
    ReplayTrade,
)
from app.integrations.quotes import Candle
from app.models.schemas import AppSettings
import math
import random
from datetime import date, datetime, timedelta, timezone


def test_default_search_config_matches_spec():
    cfg = SearchConfig()
    assert cfg.max_iterations == 100
    assert cfg.top_n == 5
    assert cfg.min_trades == 100


def test_default_search_config_full_defaults():
    cfg = SearchConfig()
    assert cfg.seed == 0
    assert cfg.train_ratio == 0.7
    assert cfg.min_validation_trades == 30


def test_search_config_rejects_out_of_range():
    with pytest.raises(ValueError):
        SearchConfig(max_iterations=501)
    with pytest.raises(ValueError):
        SearchConfig(max_iterations=0)
    with pytest.raises(ValueError):
        SearchConfig(train_ratio=0.0)
    with pytest.raises(ValueError):
        SearchConfig(train_ratio=1.0)
    with pytest.raises(ValueError):
        SearchConfig(top_n=0)
    with pytest.raises(ValueError):
        SearchConfig(min_trades=0)
    with pytest.raises(ValueError):
        SearchConfig(min_validation_trades=0)


def test_canonical_config_json_is_stable():
    a = {"b": 1, "a": 2}
    b = {"a": 2, "b": 1}
    assert canonical_config_json(a) == canonical_config_json(b)
    assert canonical_config_json(a) == '{"a": 2, "b": 1}'


def test_iteration_and_optimization_result_shapes():
    it = IterationResult(iteration=0, config={"a": 1}, config_diff={}, train_result=None)
    assert it.improved is False
    assert it.validation_result is None
    res = OptimizationResult(best=None, baseline=None, iterations=[], validation_leaderboard=[], warnings=[])
    assert res.best is None
    assert res.warnings == []


def test_candidate_respects_all_bounds():
    rng = random.Random(7)
    for _ in range(100):
        candidate = candidate_from_baseline(AppSettings(), rng, 1)
        assert 0.25 <= candidate.risk_per_trade_pct <= 5.0
        assert 1 <= candidate.max_open_positions <= 12
        assert candidate.sl_distance_max_pct >= candidate.sl_distance_min_pct


def test_diff_only_contains_changed_fields():
    before = AppSettings(min_confidence=0)
    after = AppSettings(min_confidence=70)
    assert config_diff(before, after) == {"min_confidence": {"from": 0, "to": 70}}


def test_candidate_does_not_mutate_baseline():
    baseline = AppSettings()
    snapshot = baseline.model_dump(mode="json")
    rng = random.Random(123)
    candidate_from_baseline(baseline, rng, 2)
    assert baseline.model_dump(mode="json") == snapshot


def test_candidate_sl_max_gte_min_over_many_seeds():
    for seed in range(10):
        rng = random.Random(seed)
        for _ in range(20):
            candidate = candidate_from_baseline(AppSettings(), rng, 1)
            assert candidate.sl_distance_max_pct >= candidate.sl_distance_min_pct


def test_candidate_hash_deterministic_for_same_config():
    rng = random.Random(7)
    candidate = candidate_from_baseline(AppSettings(), rng, 1)
    assert canonical_config_hash(candidate) == canonical_config_hash(candidate)
    assert len(canonical_config_hash(candidate)) == 64


@pytest.mark.parametrize("seed", [7, 42, 123])
def test_candidate_all_search_dims_within_spec_table(seed):
    """Every MUTATED value sits inside its spec table bound and on its step grid.

    Bounds/grids come from SEARCH_BOUNDS / BOOLEAN_SEARCH_DIMS /
    SL_DISTANCE_MODES directly (no second copy of the table). Unmutated
    fields keep baseline values and are out of scope here; the diff tells us
    exactly what the candidate changed. Also asserts full coverage: every
    search dim is exercised at least once per seed.
    """
    rng = random.Random(seed)
    seen: set[str] = set()
    for i in range(300):
        baseline = AppSettings()
        candidate = candidate_from_baseline(baseline, rng, i)
        for field, change in config_diff(baseline, candidate).items():
            seen.add(field)
            value = change["to"]
            if field in SEARCH_BOUNDS:
                lo, hi, step = SEARCH_BOUNDS[field]
                if value is None:
                    assert field == "min_confidence_gold"  # inherit-base only
                    continue
                assert lo - 1e-9 <= value <= hi + 1e-9, (field, value)
                n_steps = int(round((hi - lo) / step))
                k = round((value - lo) / step)
                assert 0 <= k <= n_steps, (field, value)
                assert math.isclose(value, lo + k * step, abs_tol=1e-6), (field, value)
                baseline_value = getattr(baseline, field)
                if isinstance(baseline_value, int) and not isinstance(baseline_value, bool):
                    assert isinstance(value, int) and not isinstance(value, bool), (field, value)
            elif field in BOOLEAN_SEARCH_DIMS:
                assert isinstance(value, bool), (field, value)
            elif field == "sl_distance_mode":
                assert value in SL_DISTANCE_MODES, (field, value)
            else:
                pytest.fail(f"candidate mutated non-search dim: {field}")
    expected = set(SEARCH_BOUNDS) | set(BOOLEAN_SEARCH_DIMS) | {"sl_distance_mode"}
    assert expected <= seen, f"unexercised dims: {sorted(expected - seen)}"


# ---------------------------------------------------------------------------
# Task 3 fixtures: small deterministic series + controllable fake replay.
# ---------------------------------------------------------------------------

def _bars(n_days: int, start: date = date(2024, 1, 1), price: float = 100.0) -> list[Candle]:
    out: list[Candle] = []
    for i in range(n_days):
        day = start + timedelta(days=i)
        t = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
        px = price + i * 0.5
        out.append(Candle(o=px, h=px + 1.0, l=px - 1.0, c=px + 0.25, t=t))
    return out


def _series(n_days: int = 10) -> dict[str, list[Candle]]:
    return {"EURUSD": _bars(n_days), "XAUUSD": _bars(n_days, price=2000.0)}


def _sparse_series() -> dict[str, list[Candle]]:
    return {"EURUSD": _bars(5)}


def fake_replay(series, settings, *, starting_capital=1000.0, cost_config=None, warnings=None):
    """Deterministic stand-in for replay_portfolio keyed by config hash.

    Higher hash-bucket k => higher net PnL AND higher drawdown, so a
    drawdown-penalizing ranker would pick a different winner than net rank.
    Trades are spread across every series date so both train and validation
    slices hold trades after the 70/30 split.
    """
    h = int(canonical_config_hash(settings)[:8], 16)
    k = h % 7
    dates = sorted(
        {datetime.fromtimestamp(b.t, tz=timezone.utc).date() for bars in series.values() for b in bars}
    )
    if not dates:
        dates = [date(2024, 1, 1)]
    trades: list[ReplayTrade] = []
    for i in range(120):
        day = dates[i % len(dates)]
        pnl = float((k + 1) * 10)
        trades.append(
            ReplayTrade(
                trade_id=f"F-{i:04d}", asset="EURUSD", direction="BUY",
                entry_date=day, entry_price=100.0, exit_date=day,
                exit_price=101.0, volume=1.0, pnl_usd=pnl,
                r_multiple=1.0, exit_reason="tp", mae_r=0.0, mfe_r=1.0,
                ambiguous=False, gross_pnl_usd=pnl + 1.0, costs_usd=1.0,
            )
        )
    dd = float(k * 5)
    curve = [
        EquityPoint(date=d, equity=float(starting_capital) + (k + 1) * 100.0,
                    realized_pnl=0.0, unrealized_pnl=0.0, drawdown_pct=dd)
        for d in dates
    ]
    net = sum(t.pnl_usd for t in trades)
    gross = sum(t.gross_pnl_usd for t in trades)
    costs = sum(t.costs_usd for t in trades)
    metrics = ReplayMetrics(net_pnl=net, gross_pnl=gross, costs=costs,
                            trades=len(trades), wins=len(trades),
                            win_rate_pct=100.0, profit_factor=None,
                            average_r=1.0, max_drawdown_pct=dd,
                            ambiguous_pct=0.0)
    return ReplayResult(trades=trades, equity_curve=curve,
                        metrics=metrics, warnings=list(warnings or []))


def test_search_ranks_by_net_pnl_not_drawdown(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    result = optimize_settings(series, baseline, starting_capital=1000, search_config=SearchConfig(max_iterations=3))
    eligible = [i for i in result.validation_leaderboard if i.validation_result.metrics.trades >= 30]
    assert eligible
    assert result.best.validation_result.metrics.net_pnl == max(i.validation_result.metrics.net_pnl for i in eligible)


def test_validation_is_not_used_for_intermediate_selection(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    result = optimize_settings(series, baseline, starting_capital=1000, search_config=SearchConfig(max_iterations=2))
    assert all(i.validation_result is None for i in result.iterations)


def test_low_trade_candidate_cannot_win():
    sparse_series = _sparse_series()
    baseline = AppSettings()
    result = optimize_settings(sparse_series, baseline, starting_capital=1000)
    assert result.best is None or (
        result.best.validation_result.metrics.trades >= 30
        and result.best.train_result.metrics.trades + result.best.validation_result.metrics.trades >= 100
    )


def test_same_seed_reproduces_search(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    a = optimize_settings(series, baseline, starting_capital=1000, search_config=SearchConfig(seed=11))
    b = optimize_settings(series, baseline, starting_capital=1000, search_config=SearchConfig(seed=11))
    assert [i.config for i in a.iterations] == [i.config for i in b.iterations]


def test_split_series_uses_chronological_70_30_cutoff():
    series = _series(10)
    train, cutoff = split_series(series, train_ratio=0.7)
    assert cutoff == date(2024, 1, 1) + timedelta(days=7)
    for asset, bars in train.items():
        assert bars
        assert all(
            datetime.fromtimestamp(b.t, tz=timezone.utc).date() < cutoff for b in bars
        )
    assert len(train["EURUSD"]) == 7


def test_split_series_empty_returns_none_cutoff():
    train, cutoff = split_series({})
    assert train == {}
    assert cutoff is None


def test_slice_replay_result_partitions_and_recomputes(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    full = fake_replay(series, baseline, starting_capital=1000.0)
    _, cutoff = split_series(series, train_ratio=0.7)
    train_slice, val_slice = slice_replay_result(full, cutoff)
    assert train_slice.metrics.trades + val_slice.metrics.trades == len(full.trades)
    assert val_slice.metrics.trades >= 30
    assert train_slice.metrics.net_pnl + val_slice.metrics.net_pnl == full.metrics.net_pnl
    assert val_slice.equity_curve
    assert all(p.date >= cutoff for p in val_slice.equity_curve)
    assert all(p.date < cutoff for p in train_slice.equity_curve)
    assert train_slice.warnings == full.warnings
    assert val_slice.warnings == full.warnings
