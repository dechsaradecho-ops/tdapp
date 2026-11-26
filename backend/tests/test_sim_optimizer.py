"""Optimizer models — SearchConfig/dataclass structural tests (no DB/broker).

Run from backend/: pytest tests/test_sim_optimizer.py -v
"""
from __future__ import annotations

import pytest

from app.services.sim_optimizer import (
    IterationResult,
    OptimizationResult,
    SearchConfig,
    candidate_from_baseline,
    canonical_config_hash,
    canonical_config_json,
    config_diff,
)
from app.models.schemas import AppSettings
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
