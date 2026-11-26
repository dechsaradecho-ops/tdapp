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
)
from app.models.schemas import AppSettings
import math
import random


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
