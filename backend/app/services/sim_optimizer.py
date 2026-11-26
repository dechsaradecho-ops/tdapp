"""Sandbox optimizer schema and models (Task 1 — pure, no DB/broker/network).

Binding constraints: starting capital is a user input in USD (default
$1,000, fixed per run); primary objective is highest net simulated PnL;
default max iterations 100 with a hard maximum of 500; chronological
70/30 train/validation split; top N=5 distinct configs evaluated on
validation; minimum 100 closed trades overall and 30 in validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
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

# Spec §9.1.1 numeric search bounds: name -> (min, max, step). Exact per plan.
SEARCH_BOUNDS: dict[str, tuple[float, float, float]] = {
    "min_confidence": (0, 90, 5),
    "min_confidence_gold": (0, 90, 5),
    "min_opportunity": (0, 90, 5),
    "gold_max_atr_pct": (0, 10, 0.25),
    "risk_per_trade_pct": (0.25, 5.0, 0.25),
    "max_open_positions": (1, 12, 1),
    "max_trades_daily": (1, 30, 1),
    "max_trades_weekly": (5, 100, 5),
    "reentry_cooldown_min": (0, 240, 15),
    "sl_distance_min_pct": (0.2, 3.0, 0.1),
    "sl_distance_max_pct": (0.3, 5.0, 0.1),
    "rr_target": (0.5, 3.0, 0.25),
    "breakeven_trigger_r": (0, 3.0, 0.25),
    "trail_atr_mult": (0.5, 5.0, 0.25),
    "partial_close_pct": (0, 100, 10),
    "partial_trigger_r": (0.25, 3.0, 0.25),
    "max_hold_days": (1, 20, 1),
    "exit_score_close": (20, 80, 5),
    "no_behind_min_r": (-1.0, 2.0, 0.25),
    "no_behind_hold_mult": (0, 5.0, 0.25),
    "no_behind_min_days": (0, 20, 1),
    "profit_protect_r": (0, 5.0, 0.25),
    "reversal_opp_min": (0, 90, 5),
    "volatility_exit_atr": (0, 6.0, 0.25),
    "time_stop_min_r": (-2.0, 3.0, 0.25),
}

# Boolean search dimensions (spec §9.1.1: sl_cap_enabled, trailing_ladder,
# and gold_breakout_only are boolean search dimensions).
BOOLEAN_SEARCH_DIMS: tuple[str, ...] = (
    "gold_breakout_only",
    "sl_cap_enabled",
    "trailing_ladder",
)

# Enum search dimension for sl_distance_mode.
SL_DISTANCE_MODES: tuple[str, ...] = ("short", "medium", "long")

# AppSettings fields stored as int (everything else numeric is float).
_INT_SEARCH_DIMS: frozenset[str] = frozenset(
    {
        "max_open_positions",
        "max_trades_daily",
        "max_trades_weekly",
        "reentry_cooldown_min",
        "max_hold_days",
    }
)

# Probability that a min_confidence_gold mutation yields None (inherit base).
_GOLD_INHERIT_PROBABILITY = 0.15

# Max fields mutated per candidate (spec: mutate a subset per iteration).
_MAX_MUTATIONS_PER_CANDIDATE = 5


def _step_decimals(step: float) -> int:
    text = repr(float(step))
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1].rstrip("0"))


def _stepped_float(rng: random.Random, lo: float, hi: float, step: float) -> float:
    n_steps = int(round((hi - lo) / step))
    value = lo + rng.randint(0, n_steps) * step
    return round(value, _step_decimals(step))


def _stepped_int(rng: random.Random, lo: float, hi: float, step: float) -> int:
    n_steps = int(round((hi - lo) / step))
    return int(lo + rng.randint(0, n_steps) * step)


def candidate_from_baseline(
    baseline: AppSettings, rng: random.Random, iteration: int
) -> AppSettings:
    """Randomized bounded candidate cloned from an immutable baseline snapshot.

    Mutates a small random subset of search dimensions with step-aligned
    values inside SEARCH_BOUNDS. Never mutates ``baseline``. Repairs the
    SL pair so ``sl_distance_max_pct >= sl_distance_min_pct``.
    """
    _ = iteration  # accepted per interface; randomness flows from ``rng``.
    candidate = baseline.model_copy(deep=True)
    dims: list[str] = (
        list(SEARCH_BOUNDS) + ["sl_distance_mode"] + list(BOOLEAN_SEARCH_DIMS)
    )
    n_mutate = rng.randint(1, min(_MAX_MUTATIONS_PER_CANDIDATE, len(dims)))
    for dim in rng.sample(dims, n_mutate):
        if dim in SEARCH_BOUNDS:
            lo, hi, step = SEARCH_BOUNDS[dim]
            if dim == "min_confidence_gold" and rng.random() < _GOLD_INHERIT_PROBABILITY:
                setattr(candidate, dim, None)
            elif dim in _INT_SEARCH_DIMS:
                setattr(candidate, dim, _stepped_int(rng, lo, hi, step))
            else:
                setattr(candidate, dim, _stepped_float(rng, lo, hi, step))
        elif dim == "sl_distance_mode":
            setattr(candidate, dim, rng.choice(list(SL_DISTANCE_MODES)))
        else:
            setattr(candidate, dim, bool(rng.getrandbits(1)))
    min_pct = float(candidate.sl_distance_min_pct)
    max_pct = float(candidate.sl_distance_max_pct)
    if max_pct < min_pct:
        max_lo, max_hi, max_step = SEARCH_BOUNDS["sl_distance_max_pct"]
        floor = max(min_pct, max_lo)
        n_steps = int(round((max_hi - max_lo) / max_step))
        k_min = int(math.ceil((floor - max_lo) / max_step - 1e-9))
        k_min = max(0, min(k_min, n_steps))
        value = max_lo + rng.randint(k_min, n_steps) * max_step
        candidate.sl_distance_max_pct = round(value, _step_decimals(max_step))
    return candidate


def config_diff(before: AppSettings, after: AppSettings) -> dict:
    """Field-level diff of two settings snapshots: {field: {from, to}}."""
    diff: dict[str, dict[str, Any]] = {}
    for name in type(before).model_fields:
        old = getattr(before, name)
        new = getattr(after, name)
        if old != new:
            diff[name] = {"from": old, "to": new}
    return diff


def canonical_config_hash(settings: AppSettings) -> str:
    """sha256 over Task 1 canonical JSON of the FULL candidate settings."""
    payload = settings.model_dump(mode="json")
    return hashlib.sha256(canonical_config_json(payload).encode("utf-8")).hexdigest()


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
