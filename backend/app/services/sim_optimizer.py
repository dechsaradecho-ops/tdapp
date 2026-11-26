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
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.engine.portfolio_replay import (
    ReplayMetrics,
    ReplayResult,
    default_cost_config,
    replay_portfolio,
)
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


# Search-loop tunables (Task 3 controller rulings).
_STALL_LIMIT = 20
_MAX_DUP_RETRIES = 100


def _bar_date_utc(bar: Any) -> date:
    """UTC calendar date of one series bar (never invents or guesses)."""
    raw = getattr(bar, "t", None)
    try:
        stamp = float(raw)
    except (TypeError, ValueError):
        raise ValueError("series bar is missing its UTC timestamp")
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError("series bar is missing its UTC timestamp")
    return datetime.fromtimestamp(stamp, tz=timezone.utc).date()


def split_series(series: dict, train_ratio: float = 0.7) -> tuple[dict, Optional[date]]:
    """Chronological train/validation split shared by the whole search.

    Unions every asset's bar dates (UTC), sorts them, and takes the cutoff
    index ``int(len(dates) * train_ratio)`` clamped to ``[1, len-1]``; the
    train series holds bars with ``date < cutoff``. Empty (or dateless)
    input fails closed as ``({}, None)`` — never a wall-clock date.
    """
    if not series:
        return {}, None
    try:
        ratio = float(train_ratio)
    except (TypeError, ValueError):
        raise ValueError("train_ratio must be in (0, 1)")
    if not math.isfinite(ratio) or not 0.0 < ratio < 1.0:
        raise ValueError("train_ratio must be in (0, 1)")
    dates = sorted(
        {_bar_date_utc(bar) for bars in series.values() for bar in (bars or [])}
    )
    if len(dates) < 2:
        return {}, None
    idx = int(len(dates) * ratio)
    idx = max(1, min(idx, len(dates) - 1))
    cutoff = dates[idx]
    train = {
        asset: [bar for bar in (bars or []) if _bar_date_utc(bar) < cutoff]
        for asset, bars in series.items()
    }
    return train, cutoff


def _slice_metrics(trades: list, curve: list) -> ReplayMetrics:
    """Plan 2 metric semantics recomputed over one slice of a replay."""
    net = sum(t.pnl_usd for t in trades)
    gross = sum(t.gross_pnl_usd for t in trades)
    costs = sum(t.costs_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    win_rate = (wins / len(trades) * 100.0) if trades else 0.0
    win_sum = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    loss_sum = sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    profit_factor: float | None = None
    if loss_sum < 0:
        candidate = win_sum / abs(loss_sum)
        if math.isfinite(candidate):
            profit_factor = candidate
    average_r = (sum(t.r_multiple for t in trades) / len(trades)) if trades else 0.0
    ambiguous = sum(1 for t in trades if t.ambiguous)
    ambiguous_pct = (ambiguous / len(trades) * 100.0) if trades else 0.0
    max_dd = max((p.drawdown_pct for p in curve), default=0.0)
    return ReplayMetrics(
        net_pnl=net if math.isfinite(net) else 0.0,
        gross_pnl=gross if math.isfinite(gross) else 0.0,
        costs=costs if math.isfinite(costs) else 0.0,
        trades=len(trades),
        wins=wins,
        win_rate_pct=win_rate if math.isfinite(win_rate) else 0.0,
        profit_factor=profit_factor,
        average_r=average_r if math.isfinite(average_r) else 0.0,
        max_drawdown_pct=max_dd if math.isfinite(max_dd) else 0.0,
        ambiguous_pct=ambiguous_pct if math.isfinite(ambiguous_pct) else 0.0,
    )


def slice_replay_result(
    result: ReplayResult, cutoff_date: Optional[date]
) -> tuple[ReplayResult, ReplayResult]:
    """Split one full-timeline replay into train/validation slices.

    Train holds trades with ``exit_date < cutoff`` and equity points with
    ``date < cutoff``; validation holds the rest with absolute equity values
    preserved (no reset). Slice metrics are recomputed with Plan 2 semantics;
    warnings are preserved on both slices.
    """
    if isinstance(cutoff_date, datetime):
        cutoff_date = cutoff_date.date()
    warnings = list(result.warnings or [])
    if cutoff_date is None:
        empty = ReplayResult(
            trades=[], equity_curve=[],
            metrics=ReplayMetrics(), warnings=warnings,
        )
        rest = ReplayResult(
            trades=list(result.trades),
            equity_curve=list(result.equity_curve),
            metrics=_slice_metrics(result.trades, result.equity_curve),
            warnings=warnings,
        )
        return empty, rest
    train_trades = [t for t in result.trades if t.exit_date < cutoff_date]
    val_trades = [t for t in result.trades if t.exit_date >= cutoff_date]
    train_curve = [p for p in result.equity_curve if p.date < cutoff_date]
    val_curve = [p for p in result.equity_curve if p.date >= cutoff_date]
    train_slice = ReplayResult(
        trades=train_trades, equity_curve=train_curve,
        metrics=_slice_metrics(train_trades, train_curve), warnings=warnings,
    )
    val_slice = ReplayResult(
        trades=val_trades, equity_curve=val_curve,
        metrics=_slice_metrics(val_trades, val_curve), warnings=warnings,
    )
    return train_slice, val_slice


def _train_net_pnl(iteration: IterationResult) -> float:
    try:
        return float(iteration.train_result.metrics.net_pnl)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _validation_net_pnl(entry: IterationResult) -> float:
    try:
        return float(entry.validation_result.metrics.net_pnl)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def optimize_settings(
    series: dict,
    baseline: AppSettings,
    *,
    starting_capital: float = 1000.0,
    search_config: Optional[SearchConfig] = None,
    progress_callback=None,
    cancel_event=None,
) -> OptimizationResult:
    """Profit-max search with chronological validation (synchronous).

    Evaluates the baseline on the train split, then iterates candidates from
    the original baseline on train only — tracking best by train net PnL,
    deduplicating by config hash, stopping on max iterations / cancel /
    stall (20 candidate iterations without train improvement). Validation
    replays the top-N distinct candidates once over the full timeline,
    slices each full replay at the cutoff, and recommends the highest
    validation net PnL among trade-eligible configs. Never touches DB,
    broker, network, notifications, or live settings writes.
    """
    cfg = search_config if search_config is not None else SearchConfig()
    rng = random.Random(cfg.seed)
    try:
        capital = float(starting_capital)
    except (TypeError, ValueError):
        capital = 1000.0
    if not math.isfinite(capital) or capital <= 0:
        capital = 1000.0

    def _cancelled() -> bool:
        try:
            return cancel_event is not None and bool(cancel_event.is_set())
        except Exception:
            return False

    def _emit(entry: IterationResult) -> None:
        if progress_callback is not None:
            progress_callback(entry)

    train_series, cutoff = split_series(series, train_ratio=cfg.train_ratio)
    if cutoff is None:
        return OptimizationResult(
            best=None, baseline=None, iterations=[],
            validation_leaderboard=[],
            warnings=["optimizer: empty series, nothing to search"],
        )

    iterations: list[IterationResult] = []
    seen: set[str] = set()

    # Iteration 0: the baseline itself. Sets the initial best by train net.
    baseline_train = replay_portfolio(
        train_series, baseline, starting_capital=capital,
        cost_config=default_cost_config(baseline),
    )
    seen.add(canonical_config_hash(baseline))
    best_train_net = float(baseline_train.metrics.net_pnl)
    baseline_entry = IterationResult(
        iteration=0, config=baseline, config_diff=config_diff(baseline, baseline),
        train_result=baseline_train, validation_result=None, improved=False,
    )
    iterations.append(baseline_entry)
    _emit(baseline_entry)

    # Iterations 1..N: candidates cloned from the original baseline.
    stall = 0
    dup_retries = 0
    while len(iterations) < cfg.max_iterations:
        if _cancelled():
            break
        candidate = candidate_from_baseline(baseline, rng, len(iterations))
        digest = canonical_config_hash(candidate)
        if digest in seen:
            dup_retries += 1
            if dup_retries > _MAX_DUP_RETRIES:
                break
            continue
        dup_retries = 0
        seen.add(digest)
        train_result = replay_portfolio(
            train_series, candidate, starting_capital=capital,
            cost_config=default_cost_config(candidate),
        )
        net = float(train_result.metrics.net_pnl)
        improved = net > best_train_net
        if improved:
            best_train_net = net
            stall = 0
        else:
            stall += 1
        entry = IterationResult(
            iteration=len(iterations), config=candidate,
            config_diff=config_diff(baseline, candidate),
            train_result=train_result, validation_result=None,
            improved=improved,
        )
        iterations.append(entry)
        _emit(entry)
        if stall >= _STALL_LIMIT:
            break

    # Validation: top-N distinct configs by train net, one full replay each.
    ranked = sorted(iterations, key=_train_net_pnl, reverse=True)[: max(1, cfg.top_n)]
    board: list[IterationResult] = []
    for source in ranked:
        if _cancelled():
            break
        full = replay_portfolio(
            series, source.config, starting_capital=capital,
            cost_config=default_cost_config(source.config),
        )
        _train_slice, val_slice = slice_replay_result(full, cutoff)
        board.append(
            IterationResult(
                iteration=source.iteration, config=source.config,
                config_diff=dict(source.config_diff),
                train_result=source.train_result,
                validation_result=val_slice, improved=source.improved,
            )
        )
    board.sort(key=_validation_net_pnl, reverse=True)
    eligible = [
        entry for entry in board
        if (entry.train_result.metrics.trades
            + entry.validation_result.metrics.trades) >= cfg.min_trades
        and entry.validation_result.metrics.trades >= cfg.min_validation_trades
    ]
    best = eligible[0] if eligible else None
    return OptimizationResult(
        best=best, baseline=iterations[0], iterations=iterations,
        validation_leaderboard=board, warnings=[],
    )
