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
    refine_from_parent,
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


# ---------------------------------------------------------------------------
# Task B fixtures: flat/boom replays must be module-level so Windows spawn
# children can unpickle them by reference.
# ---------------------------------------------------------------------------

def flat_replay(series, settings, *, starting_capital=1000.0, cost_config=None, warnings=None):
    """Constant-net stand-in: no candidate ever improves, so stall always wins."""
    dates = sorted(
        {datetime.fromtimestamp(b.t, tz=timezone.utc).date() for bars in series.values() for b in bars}
    )
    if not dates:
        dates = [date(2024, 1, 1)]
    trades: list[ReplayTrade] = []
    for i in range(120):
        day = dates[i % len(dates)]
        trades.append(
            ReplayTrade(
                trade_id=f"C-{i:04d}", asset="EURUSD", direction="BUY",
                entry_date=day, entry_price=100.0, exit_date=day,
                exit_price=101.0, volume=1.0, pnl_usd=10.0,
                r_multiple=1.0, exit_reason="tp", mae_r=0.0, mfe_r=1.0,
                ambiguous=False, gross_pnl_usd=11.0, costs_usd=1.0,
            )
        )
    curve = [
        EquityPoint(date=d, equity=float(starting_capital) + 100.0,
                    realized_pnl=0.0, unrealized_pnl=0.0, drawdown_pct=0.0)
        for d in dates
    ]
    metrics = ReplayMetrics(net_pnl=1200.0, gross_pnl=1320.0, costs=120.0,
                            trades=len(trades), wins=len(trades),
                            win_rate_pct=100.0, profit_factor=None,
                            average_r=1.0, max_drawdown_pct=0.0,
                            ambiguous_pct=0.0)
    return ReplayResult(trades=trades, equity_curve=curve,
                        metrics=metrics, warnings=list(warnings or []))


def boom_replay(series, settings, *, starting_capital=1000.0, cost_config=None, warnings=None):
    """Always-raising stand-in: proves worker errors fail the run closed.

    The baseline replay runs in the parent, so it is recognized by object
    identity and succeeds; every candidate replays in a spawn child (where
    the module global is a fresh ``None`` and settings arrive as unpickled
    copies) and raises there.
    """
    if settings is not _boom_ok_settings:
        raise RuntimeError("boom")
    return flat_replay(series, settings, starting_capital=starting_capital,
                       cost_config=cost_config, warnings=warnings)


_boom_ok_settings = None


def _snapshot(result):
    return {
        "configs": [canonical_config_hash(i.config) for i in result.iterations],
        "diffs": [dict(i.config_diff) for i in result.iterations],
        "trains": [float(i.train_result.metrics.net_pnl) for i in result.iterations],
        "improved": [bool(i.improved) for i in result.iterations],
        "best_iter": None if result.best is None else int(result.best.iteration),
        "best_val": (None if result.best is None
                     else float(result.best.validation_result.metrics.net_pnl)),
        "board": [(int(i.iteration), float(i.validation_result.metrics.net_pnl))
                  for i in result.validation_leaderboard],
    }


def test_search_config_workers_defaults_to_sequential():
    assert SearchConfig().workers == 1


@pytest.mark.parametrize("bad", [0, -1, -8, True, False, "4", 2.5])
def test_search_config_rejects_bad_workers(bad):
    with pytest.raises(ValueError):
        SearchConfig(workers=bad)


def test_workers_parallel_matches_sequential_fake(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    cfg1 = SearchConfig(max_iterations=12, seed=5)
    cfg4 = SearchConfig(max_iterations=12, seed=5, workers=4)
    seq_progress: list[tuple[int, bool]] = []
    par_progress: list[tuple[int, bool]] = []
    a = optimize_settings(series, baseline, starting_capital=1000, search_config=cfg1,
                          progress_callback=lambda e: seq_progress.append((e.iteration, e.improved)))
    b = optimize_settings(series, baseline, starting_capital=1000, search_config=cfg4,
                          progress_callback=lambda e: par_progress.append((e.iteration, e.improved)))
    assert _snapshot(a) == _snapshot(b)
    assert seq_progress == par_progress
    assert [i for i, _ in seq_progress] == list(range(len(a.iterations)))


def test_workers_parallel_matches_sequential_real():
    series = _series(30)
    baseline = AppSettings()
    a = optimize_settings(series, baseline, starting_capital=1000,
                          search_config=SearchConfig(max_iterations=8, seed=3))
    b = optimize_settings(series, baseline, starting_capital=1000,
                          search_config=SearchConfig(max_iterations=8, seed=3, workers=3))
    assert _snapshot(a) == _snapshot(b)


def test_stall_counting_identical_under_parallel(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", flat_replay)
    a = optimize_settings(series, baseline, starting_capital=1000,
                          search_config=SearchConfig(max_iterations=50, seed=5))
    b = optimize_settings(series, baseline, starting_capital=1000,
                          search_config=SearchConfig(max_iterations=50, seed=5, workers=4))
    assert len(a.iterations) == 1 + 20
    assert [i.improved for i in a.iterations] == [i.improved for i in b.iterations]
    assert [canonical_config_hash(i.config) for i in a.iterations] == [
        canonical_config_hash(i.config) for i in b.iterations]


def test_cancel_between_batches_preserves_completed(monkeypatch):
    import threading

    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    cancel = threading.Event()

    def on_progress(entry):
        if entry.iteration >= 2:
            cancel.set()

    result = optimize_settings(
        series, baseline, starting_capital=1000,
        search_config=SearchConfig(max_iterations=30, seed=9, workers=4),
        progress_callback=on_progress, cancel_event=cancel)
    assert 3 <= len(result.iterations) < 30
    assert all(i.train_result is not None for i in result.iterations)
    assert [i.iteration for i in result.iterations] == list(range(len(result.iterations)))


def test_worker_error_fails_closed(monkeypatch):
    import sys

    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", boom_replay)
    monkeypatch.setattr(sys.modules[__name__], "_boom_ok_settings", baseline)
    with pytest.raises(RuntimeError, match="parallel train replay failed"):
        optimize_settings(series, baseline, starting_capital=1000,
                          search_config=SearchConfig(max_iterations=6, seed=1, workers=4))


def test_parallel_payloads_pickle_cleanly(monkeypatch):
    import pickle

    series = _series(10)
    candidate = AppSettings()
    assert pickle.loads(pickle.dumps(fake_replay)) is fake_replay
    assert pickle.loads(pickle.dumps(series)) == series
    assert pickle.loads(pickle.dumps(candidate)) == candidate
    assert pickle.loads(pickle.dumps(
        sim_optimizer.default_cost_config(candidate))) == sim_optimizer.default_cost_config(candidate)
    assert pickle.loads(pickle.dumps(fake_replay(series, candidate))) == fake_replay(series, candidate)


def test_guided_search_config_defaults_and_validation():
    cfg = SearchConfig()
    assert cfg.explore_fraction == 0.5
    assert cfg.restart_prob == 0.15
    for kwargs in (
        {"explore_fraction": 0.0}, {"explore_fraction": 1.0},
        {"explore_fraction": True}, {"explore_fraction": "0.5"},
        {"restart_prob": -0.01}, {"restart_prob": 1.01},
        {"restart_prob": False}, {"restart_prob": "0.15"},
    ):
        with pytest.raises(ValueError):
            SearchConfig(**kwargs)


def test_refine_from_parent_is_small_bounded_and_pure():
    parent = AppSettings(
        min_confidence=40, min_opportunity=45, risk_per_trade_pct=1.0,
        rr_target=1.5, sl_distance_min_pct=0.5, sl_distance_max_pct=1.0,
    )
    before = parent.model_dump(mode="json")
    for seed in range(100):
        child = refine_from_parent(parent, random.Random(seed))
        diff = config_diff(parent, child)
        assert 1 <= len(diff) <= 2
        assert child.sl_distance_max_pct >= child.sl_distance_min_pct
        for field, change in diff.items():
            assert field in set(SEARCH_BOUNDS) | set(BOOLEAN_SEARCH_DIMS) | {"sl_distance_mode"}
            if field in SEARCH_BOUNDS:
                lo, hi, step = SEARCH_BOUNDS[field]
                value = change["to"]
                assert lo - 1e-9 <= value <= hi + 1e-9
                k = round((value - lo) / step)
                assert math.isclose(value, lo + k * step, abs_tol=1e-6)
                old = change["from"]
                if old is not None:
                    assert abs(value - old) <= step + 1e-9
        assert parent.model_dump(mode="json") == before


def test_refine_from_parent_same_seed_is_identical():
    parent = AppSettings(min_confidence=40, risk_per_trade_pct=1.0)
    a = refine_from_parent(parent, random.Random(7))
    b = refine_from_parent(parent, random.Random(7))
    assert a == b


def test_candidate_for_iteration_phase_boundary(monkeypatch):
    _candidate_for_iteration = sim_optimizer._candidate_for_iteration
    baseline = AppSettings()
    best = AppSettings(min_confidence=40)
    calls = []
    monkeypatch.setattr(sim_optimizer, "candidate_from_baseline",
                        lambda *args: calls.append("explore") or AppSettings(min_confidence=5))
    monkeypatch.setattr(sim_optimizer, "refine_from_parent",
                        lambda *args: calls.append("refine") or AppSettings(min_confidence=45))
    _candidate_for_iteration(baseline, best, random.Random(0), 1, 2, 0.0)
    _candidate_for_iteration(baseline, best, random.Random(0), 2, 2, 0.0)
    _candidate_for_iteration(baseline, best, random.Random(0), 3, 2, 0.0)
    assert calls == ["explore", "explore", "refine"]


def test_guided_search_same_seed_workers_1_and_4_match(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    cfg1 = SearchConfig(max_iterations=8, seed=11, workers=1)
    cfg4 = SearchConfig(max_iterations=8, seed=11, workers=4)
    a = optimize_settings(series, baseline, starting_capital=1000, search_config=cfg1)
    b = optimize_settings(series, baseline, starting_capital=1000, search_config=cfg4)
    assert [i.config for i in a.iterations] == [i.config for i in b.iterations]
    assert [i.config_diff for i in a.iterations] == [i.config_diff for i in b.iterations]
    assert a.best == b.best
    assert a.validation_leaderboard == b.validation_leaderboard


def test_guided_search_restart_probability_one_skips_refinement(monkeypatch):
    series = _series()
    baseline = AppSettings()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio", fake_replay)
    monkeypatch.setattr(sim_optimizer, "refine_from_parent",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not refine")))
    cfg = SearchConfig(max_iterations=6, seed=3, restart_prob=1.0)
    optimize_settings(series, baseline, starting_capital=1000, search_config=cfg)


def test_search_config_json_round_trips_guided_fields():
    _coerce_search_config = sim_optimizer._coerce_search_config
    _search_config_json = sim_optimizer._search_config_json
    cfg = _coerce_search_config({"max_iterations": 10, "explore_fraction": 0.4,
                                 "restart_prob": 0.2})[0]
    payload = _search_config_json(cfg)
    assert payload["explore_fraction"] == 0.4
    assert payload["restart_prob"] == 0.2
