"""Optimizer models — SearchConfig/dataclass structural tests (no DB/broker).

Run from backend/: pytest tests/test_sim_optimizer.py -v
"""
from __future__ import annotations

import pytest

from app.services.sim_optimizer import (
    IterationResult,
    OptimizationResult,
    SearchConfig,
    canonical_config_json,
)


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
