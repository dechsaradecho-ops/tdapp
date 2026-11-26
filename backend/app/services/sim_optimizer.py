"""Sandbox optimizer schema and models (Task 1 — pure, no DB/broker/network).

Binding constraints: starting capital is a user input in USD (default
$1,000, fixed per run); primary objective is highest net simulated PnL;
default max iterations 100 with a hard maximum of 500; chronological
70/30 train/validation split; top N=5 distinct configs evaluated on
validation; minimum 100 closed trades overall and 30 in validation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    from typing import TYPE_CHECKING
except ImportError:  # pragma: no cover - typing always has this
    TYPE_CHECKING = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from app.engine.portfolio_replay import ReplayResult
    from app.models.schemas import AppSettings

MAX_ITERATIONS_HARD_MAX = 500


def canonical_config_json(config: dict[str, Any]) -> str:
    """Stable serialization of a full candidate settings dict.

    Sorted keys so Task 2 config hashing is deterministic.
    """
    return json.dumps(config, sort_keys=True)


@dataclass
class SearchConfig:
    max_iterations: int = 100
    seed: int = 0
    train_ratio: float = 0.7
    top_n: int = 5
    min_trades: int = 100
    min_validation_trades: int = 30

    def __post_init__(self) -> None:
        if not 1 <= self.max_iterations <= MAX_ITERATIONS_HARD_MAX:
            raise ValueError(
                f"max_iterations must be in [1, {MAX_ITERATIONS_HARD_MAX}]"
            )
        if not 0.0 < self.train_ratio < 1.0:
            raise ValueError("train_ratio must be in (0, 1)")
        if self.top_n < 1:
            raise ValueError("top_n must be >= 1")
        if self.min_trades < 1:
            raise ValueError("min_trades must be >= 1")
        if self.min_validation_trades < 1:
            raise ValueError("min_validation_trades must be >= 1")


@dataclass
class IterationResult:
    iteration: int
    config: Any  # full candidate AppSettings
    config_diff: dict[str, Any]
    train_result: Optional[Any] = None  # Plan 2 ReplayResult
    validation_result: Optional[Any] = None  # Plan 2 ReplayResult or None
    improved: bool = False


@dataclass
class OptimizationResult:
    best: Optional[IterationResult] = None
    baseline: Optional[IterationResult] = None
    iterations: list[IterationResult] = field(default_factory=list)
    validation_leaderboard: list[IterationResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
