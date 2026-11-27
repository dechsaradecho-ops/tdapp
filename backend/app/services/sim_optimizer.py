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
import logging
import math
import random
import threading
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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

log = logging.getLogger(__name__)

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


def _sl_refine_ok(dim: str, value: float, other: float) -> bool:
    """True when an SL-side candidate keeps max >= min (rounded compare)."""
    if dim == "sl_distance_min_pct":
        return value <= other
    return value >= other


def refine_from_parent(
    parent: AppSettings, rng: random.Random
) -> AppSettings:
    """Small bounded step away from a parent config (guided-search refine).

    Deep-copies ``parent`` then moves 1–2 sampled search dimensions: numeric
    dimensions move by exactly one grid step (off-grid values snap to the
    nearest grid value; a ``None`` ``min_confidence_gold`` becomes an
    explicit grid value); booleans flip; ``sl_distance_mode`` picks a
    different mode. SL min/max moves choose a direction that preserves
    ``sl_distance_max_pct >= sl_distance_min_pct`` — no third-field repair.
    Never mutates ``parent`` and consumes only ``rng``.
    """
    child = parent.model_copy(deep=True)
    dims: list[str] = (
        list(SEARCH_BOUNDS) + ["sl_distance_mode"] + list(BOOLEAN_SEARCH_DIMS)
    )
    n_mutate = rng.randint(1, 2)
    for dim in rng.sample(dims, n_mutate):
        if dim in SEARCH_BOUNDS:
            lo, hi, step = SEARCH_BOUNDS[dim]
            decimals = _step_decimals(step)
            n_steps = int(round((hi - lo) / step))
            current = getattr(child, dim)
            if current is None:
                # Only min_confidence_gold is Optional: inherit-base (None)
                # becomes an explicit grid value.
                value = lo + rng.randint(0, n_steps) * step
                setattr(child, dim, round(value, decimals))
                continue
            cur = float(current)
            k_near = int(round((cur - lo) / step))
            k_near = max(0, min(k_near, n_steps))
            if math.isclose(cur, lo + k_near * step, abs_tol=1e-6):
                options = []
                if k_near > 0:
                    options.append(k_near - 1)
                if k_near < n_steps:
                    options.append(k_near + 1)
                if dim in ("sl_distance_min_pct", "sl_distance_max_pct"):
                    other = float(getattr(
                        child, "sl_distance_max_pct"
                        if dim == "sl_distance_min_pct"
                        else "sl_distance_min_pct"))
                    kept = [k for k in options if _sl_refine_ok(
                        dim, round(lo + k * step, decimals), other)]
                    if kept:
                        options = kept
                    else:
                        continue
                if not options:
                    continue
                chosen = rng.choice(options)
            else:
                chosen = k_near
                if dim in ("sl_distance_min_pct", "sl_distance_max_pct"):
                    other = float(getattr(
                        child, "sl_distance_max_pct"
                        if dim == "sl_distance_min_pct"
                        else "sl_distance_min_pct"))
                    if not _sl_refine_ok(
                            dim, round(lo + chosen * step, decimals), other):
                        allowed = [
                            k for k in range(n_steps + 1) if _sl_refine_ok(
                                dim, round(lo + k * step, decimals), other)]
                        if not allowed:
                            continue
                        chosen = min(
                            allowed,
                            key=lambda k: (abs(lo + k * step - cur), k))
            value = round(lo + chosen * step, decimals)
            if dim in _INT_SEARCH_DIMS:
                setattr(child, dim, int(value))
            else:
                setattr(child, dim, value)
        elif dim == "sl_distance_mode":
            current = getattr(child, dim)
            options = [m for m in SL_DISTANCE_MODES if m != current]
            setattr(child, dim, rng.choice(options or list(SL_DISTANCE_MODES)))
        else:
            setattr(child, dim, not getattr(child, dim))
    return child


def _explore_iterations(max_iterations: int, explore_fraction: float) -> int:
    """Count of leading candidate iterations drawn from the baseline.

    Zero when ``max_iterations == 1`` (baseline only); otherwise
    ``min(max_iterations - 1, max(1, floor((max_iterations - 1) *
    explore_fraction)))``.
    """
    try:
        total = int(max_iterations)
    except (TypeError, ValueError):
        return 0
    if total <= 1:
        return 0
    try:
        fraction = float(explore_fraction)
    except (TypeError, ValueError):
        fraction = 0.5
    if not math.isfinite(fraction):
        fraction = 0.5
    return min(total - 1, max(1, math.floor((total - 1) * fraction)))


def _candidate_for_iteration(
    baseline: AppSettings,
    best_config: AppSettings,
    rng: random.Random,
    iteration: int,
    explore_iterations: int,
    restart_prob: float,
) -> tuple[AppSettings, bool]:
    """One candidate for a 1-based candidate iteration plus its kind.

    Exploration iterations (``iteration <= explore_iterations``) clone from
    the original ``baseline`` via ``candidate_from_baseline`` and return
    ``False``. Refinement iterations take the restart draw: with probability
    ``restart_prob`` they restart from the baseline (``False``), otherwise
    they refine ``best_config`` via ``refine_from_parent`` (``True``).
    """
    if iteration <= explore_iterations:
        return candidate_from_baseline(baseline, rng, iteration), False
    if rng.random() < restart_prob:
        return candidate_from_baseline(baseline, rng, iteration), False
    return refine_from_parent(best_config, rng), True


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
    workers: int = 1
    explore_fraction: float = 0.5
    restart_prob: float = 0.15

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
        if isinstance(self.workers, bool) or not isinstance(self.workers, int):
            raise ValueError("workers must be an integer >= 1")
        if self.workers < 1:
            raise ValueError("workers must be >= 1")
        if (isinstance(self.explore_fraction, bool)
                or not isinstance(self.explore_fraction, (int, float))):
            raise ValueError("explore_fraction must be a number in (0, 1)")
        explore = float(self.explore_fraction)
        if not math.isfinite(explore) or not 0.0 < explore < 1.0:
            raise ValueError("explore_fraction must be in (0, 1)")
        if (isinstance(self.restart_prob, bool)
                or not isinstance(self.restart_prob, (int, float))):
            raise ValueError("restart_prob must be a number in [0, 1]")
        restart = float(self.restart_prob)
        if not math.isfinite(restart) or not 0.0 <= restart <= 1.0:
            raise ValueError("restart_prob must be in [0, 1]")


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


def _train_replay_job(payload: tuple) -> ReplayResult:
    """Process-pool entry point: exactly one train replay, no search state.

    The replay callable travels IN the payload (resolved from the module
    global in the parent) so Windows spawn children — which re-import this
    module fresh — call the same function the parent loop uses. Only
    module-level importable callables survive the trip; the random search
    itself never enters workers.
    """
    replay_fn, train_series, cost_config, candidate, capital = payload
    return replay_fn(
        train_series, candidate, starting_capital=capital,
        cost_config=cost_config,
    )


def _replay_train_batch(executor, batch: list, train_series: dict,
                        capital: float) -> list:
    """Run one batch of train replays; results return in batch order.

    Futures are awaited STRICTLY in batch (iteration) order, never
    completion order, so the caller folds them exactly as the sequential
    loop would. The first worker failure cancels the rest of the batch and
    raises RuntimeError — fail closed, never partial-silent.
    """
    replay_fn = replay_portfolio  # parent-side global: honors test doubles
    payloads = [
        (replay_fn, train_series, default_cost_config(candidate),
         candidate, capital)
        for candidate in batch
    ]
    futures = [executor.submit(_train_replay_job, payload)
               for payload in payloads]
    results: list = []
    for offset, future in enumerate(futures):
        try:
            results.append(future.result())
        except Exception as exc:
            for pending in futures:
                pending.cancel()
            raise RuntimeError(
                "optimizer: parallel train replay failed at batch offset "
                f"{offset}: {type(exc).__name__}: {exc}"
            ) from exc
    return results


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

    Evaluates the baseline on the train split, then iterates candidates on
    train only: the first ``explore_fraction`` of candidate iterations draw
    from the original baseline, then refinement mutates the moving best
    (highest train net PnL) with ``restart_prob`` full-random restarts for
    diversity — tracking best by train net PnL, deduplicating by config
    hash, stopping on max iterations / cancel / stall (20 candidate
    iterations without train improvement). Validation replays the top-N
    distinct candidates once over the full timeline, slices each full replay
    at the cutoff, and recommends the highest validation net PnL among
    trade-eligible configs. ``workers=1`` (default) replays every candidate
    inline; ``workers>1`` replays each exploration batch in a process pool
    but folds them STRICTLY in iteration order, then shuts the pool down
    and runs refinement candidates inline one at a time, so best-tracking,
    improved flags, stall counting, cancel checks, and progress callbacks
    behave exactly as the sequential loop at any worker count. Never touches
    DB, broker, network, notifications, or live settings writes.
    """
    cfg = search_config if search_config is not None else SearchConfig()
    rng = random.Random(cfg.seed)
    explore_n = _explore_iterations(cfg.max_iterations, cfg.explore_fraction)
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
    best_config = baseline

    # Iterations 1..N: exploration clones the original baseline, then
    # refinement mutates the moving best (restart draws excepted).
    stall = 0
    dup_retries = 0

    def _fold(candidate: AppSettings, train_result) -> bool:
        """Record one train replay in iteration order.

        Returns False when the stall limit is reached and the search must
        stop. Shared by the sequential and parallel paths so best-tracking
        (strict ``>``), ``best_config`` parent timing, ``improved`` flags,
        stall counting, and progress callbacks are identical under both.
        """
        nonlocal best_train_net, stall, best_config
        net = float(train_result.metrics.net_pnl)
        improved = net > best_train_net
        if improved:
            best_train_net = net
            best_config = candidate
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
        return stall < _STALL_LIMIT

    if cfg.workers <= 1:
        while len(iterations) < cfg.max_iterations:
            if _cancelled():
                break
            candidate, _ = _candidate_for_iteration(
                baseline, best_config, rng, len(iterations), explore_n,
                cfg.restart_prob)
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
            if not _fold(candidate, train_result):
                break
    else:
        # Parallel train replays with an ordered fold. Candidate generation
        # stays sequential (cheap, RNG-ordered) so the candidate stream — and
        # hence `seen`/dup counting — matches the sequential loop exactly;
        # only the train replay_portfolio calls run in a process pool
        # (threads do not help CPU-bound pure-Python replays). Exploration
        # candidates never touch best_config, so whole batches replay in the
        # pool. Refinement candidates depend on the moving best (fold
        # order), so once refinement begins the pool shuts down and the
        # remaining iterations run inline one at a time — the exact
        # sequential candidate stream and best-parent timing at any worker
        # count. A stall or cancel stop discards the rest of the in-flight
        # batch: the sequential loop would never have generated those
        # iterations.
        executor: Optional[ProcessPoolExecutor] = None
        try:
            stop = False
            while (len(iterations) < cfg.max_iterations and not stop
                   and len(iterations) <= explore_n):
                if _cancelled():
                    break
                batch: list[AppSettings] = []
                while (len(batch) < cfg.workers
                       and len(iterations) + len(batch) < cfg.max_iterations
                       and len(iterations) + len(batch) <= explore_n):
                    if _cancelled():
                        break
                    candidate, _ = _candidate_for_iteration(
                        baseline, best_config, rng,
                        len(iterations) + len(batch), explore_n,
                        cfg.restart_prob)
                    digest = canonical_config_hash(candidate)
                    if digest in seen:
                        dup_retries += 1
                        if dup_retries > _MAX_DUP_RETRIES:
                            stop = True
                            break
                        continue
                    dup_retries = 0
                    seen.add(digest)
                    batch.append(candidate)
                if stop or not batch:
                    break
                if executor is None:
                    try:
                        executor = ProcessPoolExecutor(
                            max_workers=cfg.workers)
                    except Exception as exc:
                        raise RuntimeError(
                            "optimizer: could not start parallel pool "
                            f"(workers={cfg.workers}): {exc}"
                        ) from exc
                results = _replay_train_batch(
                    executor, batch, train_series, capital)
                for candidate, train_result in zip(batch, results):
                    if _cancelled():
                        stop = True
                        break
                    if not _fold(candidate, train_result):
                        stop = True
                        break
        finally:
            if executor is not None:
                executor.shutdown(wait=True)
        if not stop:
            while len(iterations) < cfg.max_iterations:
                if _cancelled():
                    break
                candidate, _ = _candidate_for_iteration(
                    baseline, best_config, rng, len(iterations), explore_n,
                    cfg.restart_prob)
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
                if not _fold(candidate, train_result):
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


# ---------------------------------------------------------------------------
# Background optimization job + persistence (Task 4).
#
# WHY A JOB AND NOT A REQUEST
# ---------------------------
# A full search replays the whole immutable dataset per candidate (up to 500
# iterations plus validation replays), so it cannot live inside a
# request/response cycle. It runs on a single-worker thread pool mirroring
# the Plan 1 dataset-build job (app/services/sim_market_data.py): one
# optimization at a time, progress stages on the run row, and a terminal
# done/failed/cancelled verdict. Datasets stay immutable — the worker only
# reads sim_dataset_meta_v2/sim_price_bars_v2 and writes sim_opt_* rows.
#
# SAFETY
# ------
# * Read-only baseline: the live AppSettings snapshot is captured once at
#   start through capture_live_baseline(); the worker NEVER writes
#   live settings row and never touches broker/notifier/orders.
# * Primary objective is highest net simulated PnL; drawdown is reported,
#   not penalized (inherited from optimize_settings).
# * Evidence phases: every detailed non-baseline iteration consistently
#   carries validation-phase trades/equity. Baseline (iteration 0) details
#   are stored once at search start from its train slice and are PINNED —
#   the final top-N pass never replaces them. The top-N pass deletes then
#   re-inserts detail rows for its non-baseline iterations, so a best
#   iteration first stored train-phase during the search ends
#   validation-phase. Iterations are summary rows always; detail rows are
#   evidence for baseline, the moving best, and the final top-N only.
# * Progress writes are throttled to at most one run-row patch per
#   iteration (a cheap dict update, no extra tables).
# * Cancellation is cooperative: the worker's cancel event is checked
#   between iterations and validation replays, preserving everything
#   completed so far.
# ---------------------------------------------------------------------------

RUNS_TABLE = "sim_opt_runs"
ITER_TABLE = "sim_opt_iterations"
TRADES_TABLE = "sim_opt_trades"
EQUITY_TABLE = "sim_opt_equity"
EVENTS_TABLE = "sim_opt_run_events"

CODE_VERSION = "sim-opt-v1"

TERMINAL_STATUSES = ("complete", "failed", "cancelled")

#: One worker on purpose — see "WHY A JOB" above.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim-opt")

#: The live run id, so a second start is refused while one runs.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: str | None = None

#: run_id -> cancel flag, so /cancel can reach a run already in flight.
_CANCEL: dict[str, threading.Event] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    """Mint ``opt-YYYYMMDD-HHMMSS-xxxxxx`` (Plan 1 scheme, opt- prefix)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"opt-{stamp}-{uuid.uuid4().hex[:6]}"


def _set_active(run_id: str | None) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = run_id


def active_optimization() -> str | None:
    with _ACTIVE_LOCK:
        return _ACTIVE


def _try_start_active(run_id: str) -> bool:
    """Claim the single-worker slot iff it is free — one locked CAS.

    The check and the set happen under the same lock acquisition so two
    concurrent starts cannot both pass the guard.
    """
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            return False
        _ACTIVE = run_id
        return True


def _release_active(run_id: str) -> None:
    """Clear the slot, but only if this run still holds it."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE == run_id:
            _ACTIVE = None


def capture_live_baseline(db) -> AppSettings:
    """Read-only snapshot of the live settings for this run (test seam).

    Returns schema defaults when no stored row is readable (first-run DB);
    never raises and never writes — the optimizer must not move live risk
    limits as a side effect of searching. Tests monkeypatch this seam.
    """
    try:
        from app.api.routes.settings import try_load_settings
        loaded = try_load_settings(db)
        return loaded if loaded is not None else AppSettings()
    except Exception:
        return AppSettings()


def _coerce_search_config(value: Any) -> tuple[SearchConfig | None, str | None]:
    """Accept None/a SearchConfig/a plain dict; dicts are validated."""
    if value is None:
        return SearchConfig(), None
    if isinstance(value, SearchConfig):
        return value, None
    if isinstance(value, dict):
        known = {"max_iterations", "seed", "train_ratio", "top_n",
                 "min_trades", "min_validation_trades", "workers",
                 "explore_fraction", "restart_prob"}
        try:
            cfg = SearchConfig(**{k: v for k, v in value.items()
                                  if k in known})
        except (TypeError, ValueError) as exc:
            return None, f"invalid search_config: {exc}"
        unknown = sorted(set(value) - known - {"code_version"})
        if unknown:
            return None, f"invalid search_config: unknown keys {unknown}"
        return cfg, None
    return None, "invalid search_config: must be an object"


def _search_config_json(cfg: SearchConfig) -> dict[str, Any]:
    return {"max_iterations": cfg.max_iterations, "seed": cfg.seed,
            "train_ratio": cfg.train_ratio, "top_n": cfg.top_n,
            "min_trades": cfg.min_trades,
            "min_validation_trades": cfg.min_validation_trades,
            "workers": cfg.workers,
            "explore_fraction": cfg.explore_fraction,
            "restart_prob": cfg.restart_prob,
            "code_version": CODE_VERSION}


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _json_safe(value: Any) -> Any:
    """Recursively force JSON-safe (no NaN/Infinity, dates to ISO)."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _metrics_dict(result: Any | None) -> dict[str, Any] | None:
    """JSON-safe summary metrics; profit_factor None stays null."""
    metrics = getattr(result, "metrics", None)
    if metrics is None:
        return None
    profit = getattr(metrics, "profit_factor", None)
    try:
        profit_value = (None if profit is None
                        else (float(profit)
                              if math.isfinite(float(profit)) else None))
    except (TypeError, ValueError):
        profit_value = None
    return {
        "net_pnl": _finite(getattr(metrics, "net_pnl", 0.0)),
        "gross_pnl": _finite(getattr(metrics, "gross_pnl", 0.0)),
        "costs": _finite(getattr(metrics, "costs", 0.0)),
        "trades": int(getattr(metrics, "trades", 0) or 0),
        "wins": int(getattr(metrics, "wins", 0) or 0),
        "win_rate_pct": _finite(getattr(metrics, "win_rate_pct", 0.0)),
        "profit_factor": profit_value,
        "average_r": _finite(getattr(metrics, "average_r", 0.0)),
        "max_drawdown_pct": _finite(
            getattr(metrics, "max_drawdown_pct", 0.0)),
        "ambiguous_pct": _finite(getattr(metrics, "ambiguous_pct", 0.0)),
    }


def _trade_direction(raw: Any) -> str:
    """Normalize engine BUY/SELL to the sim_opt_trades long/short check."""
    text = str(raw or "").strip().upper()
    if text in ("LONG", "BUY", "B", "L"):
        return "long"
    if text in ("SHORT", "SELL", "S"):
        return "short"
    return "long"


def _trade_rows(run_id: str, iteration: int, trades: list | None) -> list[dict]:
    rows: list[dict] = []
    for trade in trades or []:
        try:
            entry_date = getattr(trade, "entry_date", None)
            exit_date = getattr(trade, "exit_date", None)
            rows.append({
                "run_id": run_id,
                "iteration": int(iteration),
                "trade_id": str(getattr(trade, "trade_id", "")),
                "asset": str(getattr(trade, "asset", "")),
                "direction": _trade_direction(getattr(trade, "direction", "")),
                "entry_date": (entry_date.isoformat()
                               if hasattr(entry_date, "isoformat")
                               else str(entry_date)),
                "entry_price": _finite(getattr(trade, "entry_price", 0.0)),
                "exit_date": (exit_date.isoformat()
                              if hasattr(exit_date, "isoformat")
                              else (str(exit_date) if exit_date else None)),
                "exit_price": _finite(getattr(trade, "exit_price", 0.0)),
                "volume": _finite(getattr(trade, "volume", 0.0)),
                "pnl_usd": _finite(getattr(trade, "pnl_usd", 0.0)),
                "r_multiple": _finite(getattr(trade, "r_multiple", 0.0)),
                "exit_reason": str(getattr(trade, "exit_reason", "") or ""),
                "mae_r": _finite(getattr(trade, "mae_r", 0.0)),
                "mfe_r": _finite(getattr(trade, "mfe_r", 0.0)),
                "ambiguous": bool(getattr(trade, "ambiguous", False)),
            })
        except Exception as exc:  # noqa: BLE001 - skip one bad row, keep run
            log.debug("opt %s iter %s: skipping bad trade row: %s",
                      run_id, iteration, exc)
    return rows


def _equity_rows(run_id: str, iteration: int, curve: list | None) -> list[dict]:
    rows: list[dict] = []
    for point in curve or []:
        try:
            stamp = getattr(point, "date", None)
            rows.append({
                "run_id": run_id,
                "iteration": int(iteration),
                "date": (stamp.isoformat() if hasattr(stamp, "isoformat")
                         else str(stamp)),
                "equity": _finite(getattr(point, "equity", 0.0)),
                "realized_pnl": _finite(getattr(point, "realized_pnl", 0.0)),
                "unrealized_pnl": _finite(
                    getattr(point, "unrealized_pnl", 0.0)),
                "drawdown_pct": _finite(getattr(point, "drawdown_pct", 0.0)),
            })
        except Exception as exc:  # noqa: BLE001 - skip one bad point
            log.debug("opt %s iter %s: skipping bad equity point: %s",
                      run_id, iteration, exc)
    return rows


def _patch_run(db, run_id: str, changes: dict) -> bool:
    """Update the run row by PK. Returns False (logged) when unwritable."""
    try:
        if db.update(RUNS_TABLE, run_id, changes):
            return True
    except Exception as exc:
        log.warning("opt %s: run patch failed: %s", run_id, exc)
        return False
    log.warning("opt %s: run patch missed (row gone?)", run_id)
    return False


def _read_data_hash(db, dataset_version: str) -> str | None:
    """Best-effort reproducibility hash from the dataset meta row."""
    try:
        from app.services.sim_market_data import META_TABLE
        rows = db.select(META_TABLE,
                         filters={"dataset_version": str(dataset_version)},
                         order="started_at", desc=True, limit=1)
    except Exception:
        return None
    if not rows:
        return None
    row = rows[0] or {}
    report = row.get("report") or {}
    return row.get("data_hash") or report.get("data_hash")


def _cancel_requested(db, run_id: str, cancel=None) -> bool:
    """True if a cancel landed via the worker event or the run row itself.

    Checked immediately before the terminal ``complete`` write so a cancel
    landing during best-summary computation — or between the last
    validation check and the terminal write — cannot be silently
    overwritten to ``complete``. The row read covers the case where
    ``cancel_optimization`` patched the row but the worker's in-flight
    event handle is stale.
    """
    try:
        if cancel is not None and bool(cancel.is_set()):
            return True
    except Exception:
        pass
    try:
        live = _CANCEL.get(str(run_id))
        if live is not None and live is not cancel and bool(live.is_set()):
            return True
    except Exception:
        pass
    try:
        row = _run_row(db, str(run_id))
        if row is not None and str(row.get("status")) == "cancelled":
            return True
    except Exception:
        pass
    return False


def _finish_run(db, run_id: str, status: str, *,
                result: dict | None = None, error: str | None = None) -> None:
    """Write the terminal verdict.

    ``status`` honors the migration 063 check constraint
    (pending/running/complete/failed/cancelled); ``stage`` carries the
    worker vocabulary (done/failed/cancelled), mirroring the Plan 1
    dataset-build job (status complete + report stage done).

    Cancel wins the terminal race: a ``complete`` write requested after a
    cancel landed (event set or row already ``cancelled``) is downgraded
    to ``cancelled`` so the worker can never silently overwrite a
    cancellation. Completed iteration rows are untouched on every path.
    """
    if status == "complete" and _cancel_requested(
            db, run_id, _CANCEL.get(str(run_id))):
        status = "cancelled"
        base = dict(result or {})
        warnings = list(base.get("warnings") or [])
        warnings.append("cancelled before completion — "
                        "completed iterations preserved")
        result = {**base, "stage": "cancelled", "warnings": warnings}
        error = "cancelled"
    stage = {"complete": "done", "failed": "failed",
             "cancelled": "cancelled"}.get(status, status)
    _patch_run(db, run_id, {
        "status": status,
        "stage": stage,
        "finished_at": _now_iso(),
        "result": _json_safe(result or {}),
        "error": None if status == "complete" else (error or status),
    })


def _best_summary(entry: Any) -> dict[str, Any] | None:
    if entry is None:
        return None
    return {
        "iteration": int(getattr(entry, "iteration", 0) or 0),
        "train_metrics": _metrics_dict(getattr(entry, "train_result", None)),
        "validation_metrics": _metrics_dict(
            getattr(entry, "validation_result", None)),
        "config_diff": _json_safe(getattr(entry, "config_diff", None) or {}),
    }


def start_optimization(db, *, dataset_version: str,
                       assets: list[str] | None = None,
                       starting_capital: float = 1000.0,
                       search_config: Any = None) -> dict:
    """Queue one optimization run and hand it to the worker. Returns at once.

    One run at a time: a second start while one is live is refused with
    ``ok: False``. ``search_config`` may be a SearchConfig or a plain dict
    (validated — ``max_iterations > 500`` is refused). Assets default to the
    baseline's ``effective_assets()`` when omitted; at most 40 assets per run
    (same bound as the Plan 1 dataset-build job). Never writes live
    settings; the baseline is a read-only snapshot.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    try:
        capital = float(starting_capital)
    except (TypeError, ValueError):
        return {"ok": False, "error": "starting_capital must be a number"}
    if not math.isfinite(capital) or capital <= 0:
        return {"ok": False,
                "error": "starting_capital must be finite and > 0"}
    cfg, cfg_error = _coerce_search_config(search_config)
    if cfg_error is not None or cfg is None:
        return {"ok": False, "error": cfg_error or "bad search_config"}
    version = str(dataset_version or "").strip()
    if not version:
        return {"ok": False, "error": "dataset_version required"}
    try:
        baseline = capture_live_baseline(db)
    except Exception:
        baseline = AppSettings()
    if not isinstance(baseline, AppSettings):
        baseline = AppSettings()
    clean = sorted({str(a).strip().upper() for a in (assets or [])
                    if str(a).strip()})
    if not clean:
        try:
            clean = [str(a).upper() for a in baseline.effective_assets()
                     if str(a).strip()]
        except Exception:
            clean = []
        if not clean:
            return {"ok": False, "error": "assets required"}
    if len(clean) > 40:
        return {"ok": False, "error": "assets required (1-40)"}
    run_id = _new_run_id()
    if not _try_start_active(run_id):
        live = active_optimization()
        return {"ok": False,
                "error": "an optimization run is already live",
                "run_id": live}
    now = _now_iso()
    try:
        row = {
            "id": run_id,
            "status": "running",
            "stage": "queued",
            "progress": 0.0,
            "seed": int(cfg.seed),
            "starting_capital": capital,
            "search_config": _search_config_json(cfg),
            "baseline_settings": _json_safe(
                baseline.model_dump(mode="json")),
            "dataset_version": version,
            "data_hash": None,
            "result": {"stage": "queued", "best_train_pnl": None,
                       "best": None, "recommendation": None,
                       "warnings": []},
            "error": None,
            "created_at": now,
            "started_at": now,
            "finished_at": None,
        }
        if db.insert(RUNS_TABLE, row) is None:
            _release_active(run_id)
            return {"ok": False, "error": "could not create optimization run"}
    except Exception as exc:
        log.error("opt: could not create run row: %s", exc)
        _release_active(run_id)
        return {"ok": False, "error": "could not create optimization run"}
    _CANCEL[run_id] = threading.Event()
    _POOL.submit(_run_optimization, db, run_id, {
        "dataset_version": version,
        "assets": clean,
        "starting_capital": capital,
        "search_config": cfg,
        "baseline": baseline,
    })
    return {"ok": True, "run_id": run_id}


def _store_details(db, run_id: str, iteration: int,
                   replay: Any | None) -> bool:
    """Persist full trades/equity for one iteration via bulk inserts.

    At most two ``db.insert(table, rows)`` calls (one trade array, one
    equity array) instead of one call per row. Returns True when every
    row landed (or there was nothing to persist); a failed bulk write is
    logged and returns False so the caller never marks details complete.
    Best-effort: never raises.
    """
    if replay is None:
        return True
    try:
        trade_rows = _trade_rows(run_id, iteration,
                                 list(getattr(replay, "trades", None) or []))
        if trade_rows:
            inserted = db.insert(TRADES_TABLE, trade_rows)
            if not isinstance(inserted, list) or \
                    len(inserted) != len(trade_rows):
                log.warning("opt %s iter %s: trade bulk insert failed "
                            "(%d rows)", run_id, iteration, len(trade_rows))
                return False
        equity_rows = _equity_rows(
            run_id, iteration,
            list(getattr(replay, "equity_curve", None) or []))
        if equity_rows:
            inserted = db.insert(EQUITY_TABLE, equity_rows)
            if not isinstance(inserted, list) or \
                    len(inserted) != len(equity_rows):
                log.warning("opt %s iter %s: equity bulk insert failed "
                            "(%d rows)", run_id, iteration, len(equity_rows))
                return False
    except Exception as exc:
        log.warning("opt %s iter %s: detail persist failed: %s",
                    run_id, iteration, exc)
        return False
    return True


def _emit_run_event(db, run_id: str, *, seq: int, level: str, stage: str,
                    event: str, message: str = "",
                    iteration: int | None = None,
                    data: dict | None = None) -> int:
    """Append one row to the per-run event log. Best-effort, never raises.

    Returns the next sequence number (``seq + 1``) so the caller keeps a
    per-run monotonic ``seq`` even when a row fails to land — advancing on
    failure keeps later rows unique under the ``(run_id, seq)`` primary
    key instead of retrying a taken sequence. ``data`` must stay a small
    summary (never secrets, raw exception dumps, or full configs).
    """
    nxt = seq + 1
    try:
        clean_level = str(level or "info")
        if clean_level not in ("info", "warning", "error"):
            clean_level = "info"
        row = {
            "run_id": str(run_id),
            "seq": int(seq),
            "level": clean_level,
            "stage": (None if stage is None else str(stage)),
            "iteration": (None if iteration is None
                          else int(iteration)),
            "event": str(event),
            "message": str(message or ""),
            "data": _json_safe(dict(data or {})),
        }
        if db is not None and getattr(db, "available", True):
            db.insert(EVENTS_TABLE, row)
    except Exception as exc:  # noqa: BLE001 - events must never fail a run
        log.debug("opt %s: run event %s failed: %s", run_id, event, exc)
    return nxt


def _delete_details(db, run_id: str, iteration: int) -> None:
    """Drop one iteration's detail rows (best-effort, logged)."""
    for table in (TRADES_TABLE, EQUITY_TABLE):
        try:
            db.delete(table, {"run_id": run_id, "iteration": int(iteration)})
        except Exception as exc:
            log.debug("opt %s iter %s: detail delete %s failed: %s",
                      run_id, iteration, table, exc)


def _run_optimization(db, run_id: str, job: dict) -> None:
    """Execute one optimization to a terminal done/failed/cancelled row.

    Never raises and never leaves the version ``running``: every exit path
    writes a verdict and clears the single-worker slot. Completed iteration
    rows are preserved on every path, including cancellation.
    """
    cancel = _CANCEL.setdefault(run_id, threading.Event())
    cfg: SearchConfig = job["search_config"]
    baseline: AppSettings = job["baseline"]
    assets: list[str] = job["assets"]
    capital: float = job["starting_capital"]
    version: str = job["dataset_version"]
    iters_done = 0
    best_train: float | None = None
    best_detail_iter: int | None = None
    detailed: set[int] = set()
    live_result: dict[str, Any] = {"stage": "loading",
                                   "best_train_pnl": None, "best": None,
                                   "recommendation": None, "warnings": []}
    seq = 0

    def _log(level: str, stage: str, event: str, message: str = "",
             iteration: int | None = None,
             data: dict | None = None) -> None:
        """Append one run-log event; best-effort, advances the seq."""
        nonlocal seq
        seq = _emit_run_event(db, run_id, seq=seq, level=level,
                              stage=stage, event=event, message=message,
                              iteration=iteration, data=data)

    def _progress_patch(stage: str) -> None:
        live_result["stage"] = stage
        live_result["best_train_pnl"] = best_train
        _patch_run(db, run_id, {
            "stage": stage,
            "progress": min(1.0, iters_done / max(1, cfg.max_iterations)),
            "result": dict(live_result),
        })

    try:
        _patch_run(db, run_id, {"stage": "loading",
                                "result": dict(live_result)})
        _log("info", "loading", "run_started",
             f"optimization run started on dataset {version}",
             data={"dataset_version": version, "assets": list(assets),
                   "starting_capital": capital})
        data_hash = _read_data_hash(db, version)
        if data_hash:
            _patch_run(db, run_id, {"data_hash": data_hash})
        try:
            from app.services.sim_market_data import load_dataset
            series = load_dataset(db, version, assets)
        except Exception as exc:
            _finish_run(db, run_id, "failed",
                        result={**live_result, "stage": "failed"},
                        error=f"dataset load failed: {exc}"[:300])
            _log("error", "failed", "run_failed",
                 "dataset load failed", data={"error": "dataset load failed"})
            return
        if not series:
            _finish_run(db, run_id, "failed",
                        result={**live_result, "stage": "failed"},
                        error="dataset unavailable or incomplete "
                              "(never partial replay)")
            _log("error", "failed", "run_failed",
                 "dataset unavailable or incomplete (never partial replay)")
            return
        try:
            bars = sum(len(v or []) for v in series.values())
        except Exception:
            bars = 0
        _log("info", "loading", "dataset_loaded",
             f"dataset {version} loaded: "
             f"{len(series)} assets, {bars} bars",
             data={"assets": sorted(series), "bars": bars,
                   "data_hash": data_hash})
        if cancel.is_set():
            _finish_run(db, run_id, "cancelled",
                        result={**live_result, "stage": "cancelled",
                                "warnings": ["cancelled before search"]})
            _log("info", "cancelled", "cancel_requested",
                 "cancelled before search")
            _log("info", "cancelled", "run_cancelled",
                 "cancelled before search")
            return
        _progress_patch("searching")
        _log("info", "searching", "search_started",
             f"search started: up to {cfg.max_iterations} iterations",
             data={"max_iterations": cfg.max_iterations, "seed": cfg.seed,
                   "train_ratio": cfg.train_ratio, "top_n": cfg.top_n,
                   "workers": cfg.workers,
                   "explore_fraction": cfg.explore_fraction,
                   "restart_prob": cfg.restart_prob})

        def on_progress(entry: Any) -> None:
            nonlocal iters_done, best_train, best_detail_iter, seq
            if cancel.is_set():
                return
            train: Any | None = None
            train_metrics: dict[str, Any] | None = None
            try:
                train = getattr(entry, "train_result", None)
                train_metrics = _metrics_dict(train)
                try:
                    diff = _json_safe(
                        getattr(entry, "config_diff", None) or {})
                    config = getattr(entry, "config", None)
                    config_json = _json_safe(
                        config.model_dump(mode="json")
                        if isinstance(config, AppSettings)
                        else (dict(config) if isinstance(config, dict)
                              else {}))
                except Exception:
                    diff, config_json = {}, {}
                db.insert(ITER_TABLE, {
                    "run_id": run_id,
                    "iteration": int(getattr(entry, "iteration", 0) or 0),
                    "config": config_json,
                    "config_diff": diff,
                    "train_metrics": train_metrics,
                    "validation_metrics": _metrics_dict(
                        getattr(entry, "validation_result", None)),
                    "improved": bool(getattr(entry, "improved", False)),
                })
            except Exception as exc:
                log.warning("opt %s: iteration persist failed: %s",
                            run_id, exc)
            iters_done += 1
            try:
                net = float(train_metrics["net_pnl"]) \
                    if train_metrics else None
            except (TypeError, ValueError):
                net = None
            new_best = (net is not None
                        and (best_train is None or net > best_train))
            if new_best:
                best_train = net
            iteration = int(getattr(entry, "iteration", 0) or 0)
            improved = bool(getattr(entry, "improved", False))
            _log("info", "searching", "iteration_completed",
                 f"iteration {iteration} completed: "
                 f"train net {net if net is not None else 'n/a'}",
                 iteration=iteration,
                 data={"net_pnl": net, "improved": improved})
            if new_best:
                _log("info", "searching", "best_improved",
                     f"new best train net "
                     f"{best_train if best_train is not None else 'n/a'} "
                     f"at iteration {iteration}",
                     iteration=iteration, data={"net_pnl": best_train})
            # Fresh status FIRST: the live run row (stage/best_train_pnl)
            # must stay current even when the detail write below is slow
            # or fails — the old order hid a new best until persistence
            # finished.
            _progress_patch("searching")
            if iteration == 0 or improved:
                if (best_detail_iter is not None
                        and best_detail_iter != iteration
                        and best_detail_iter != 0):
                    _delete_details(db, run_id, best_detail_iter)
                    detailed.discard(best_detail_iter)
                if _store_details(db, run_id, iteration, train):
                    detailed.add(iteration)
                best_detail_iter = iteration

        outcome = optimize_settings(
            series, baseline, starting_capital=capital,
            search_config=cfg, progress_callback=on_progress,
            cancel_event=cancel)
        if cancel.is_set():
            _finish_run(db, run_id, "cancelled",
                        result={**live_result, "stage": "cancelled",
                                "warnings": ["cancelled — completed "
                                             "iterations preserved"]})
            _log("info", "cancelled", "cancel_requested",
                 "cancelled after search — completed iterations preserved")
            _log("info", "cancelled", "run_cancelled",
                 "cancelled after search — completed iterations preserved")
            return
        board = list(getattr(outcome, "validation_leaderboard", None) or [])
        top_board = board[:max(1, cfg.top_n)]
        _log("info", "validating", "validation_started",
             f"validating top {len(top_board)} candidates",
             data={"candidates": len(top_board)})
        for entry in top_board:
            if cancel.is_set():
                _finish_run(
                    db, run_id, "cancelled",
                    result={**live_result, "stage": "cancelled",
                            "warnings": ["cancelled during validation — "
                                         "completed iterations preserved"]})
                _log("info", "cancelled", "cancel_requested",
                     "cancelled during validation — "
                     "completed iterations preserved")
                _log("info", "cancelled", "run_cancelled",
                     "cancelled during validation — "
                     "completed iterations preserved")
                return
            iteration = int(getattr(entry, "iteration", 0) or 0)
            if iteration == 0:
                continue  # pinned: baseline evidence stays train-phase
            _delete_details(db, run_id, iteration)
            detailed.discard(iteration)
            if _store_details(db, run_id, iteration,
                              getattr(entry, "validation_result", None)):
                detailed.add(iteration)
        best_entry = getattr(outcome, "best", None)
        best = _best_summary(best_entry)
        recommendation = None
        if best_entry is not None:
            config = getattr(best_entry, "config", None)
            try:
                config_json = _json_safe(
                    config.model_dump(mode="json")
                    if isinstance(config, AppSettings)
                    else (dict(config) if isinstance(config, dict) else {}))
            except Exception:
                config_json = {}
            recommendation = {
                "iteration": int(getattr(best_entry, "iteration", 0) or 0),
                "config": config_json,
                "validation_metrics": _metrics_dict(
                    getattr(best_entry, "validation_result", None)),
                "note": ("highest validation net PnL among trade-eligible "
                         "candidates — apply manually via Settings; the "
                         "optimizer never writes live settings"),
            }
        final = {"stage": "done", "best_train_pnl": best_train,
                 "best": best, "recommendation": recommendation,
                 "warnings": list(getattr(outcome, "warnings", None) or [])}
        if best_entry is not None:
            _log("info", "validating", "recommendation_ready",
                 f"recommendation ready at iteration "
                 f"{recommendation['iteration']}",
                 iteration=int(recommendation.get("iteration", 0) or 0),
                 data={"iteration": recommendation.get("iteration")})
        else:
            _log("info", "validating", "no_recommendation",
                 "no trade-eligible candidate — no recommendation",
                 data={"reason": "no trade-eligible candidate"})
        if _cancel_requested(db, run_id, cancel):
            _finish_run(db, run_id, "cancelled",
                        result={**live_result, "stage": "cancelled",
                                "warnings": ["cancelled before completion — "
                                             "completed iterations "
                                             "preserved"]})
            _log("info", "cancelled", "cancel_requested",
                 "cancelled before completion — "
                 "completed iterations preserved")
            _log("info", "cancelled", "run_cancelled",
                 "cancelled before completion — "
                 "completed iterations preserved")
            return
        _patch_run(db, run_id, {"progress": 1.0})
        _finish_run(db, run_id, "complete", result=final)
        best_iter = (recommendation or {}).get("iteration")
        _log("info", "done", "run_completed",
             f"run completed: best train net "
             f"{best_train if best_train is not None else 'n/a'}",
             data={"best_train_pnl": best_train,
                   "best_iteration": best_iter})
    except Exception as exc:                       # noqa: BLE001 - verdict, not a crash
        log.exception("opt run %s failed", run_id)
        short = f"{type(exc).__name__}: {exc}"[:300]
        try:
            _finish_run(db, run_id, "failed",
                        result={**live_result, "stage": "failed"},
                        error=short)
        except Exception:
            pass
        _log("error", "failed", "run_failed", "run failed",
             data={"error": short})
    finally:
        _release_active(run_id)
        _CANCEL.pop(run_id, None)


def _status_dict(row: dict) -> dict:
    result = row.get("result") or {}
    return {
        "ok": True,
        "run_id": str(row.get("id")),
        "status": str(row.get("status") or "running"),
        "stage": str(row.get("stage") or row.get("status") or "running"),
        "progress": _finite(row.get("progress"), 0.0),
        "seed": int(row.get("seed") or 0),
        "starting_capital": _finite(row.get("starting_capital"), 1000.0),
        "dataset_version": str(row.get("dataset_version") or ""),
        "best_train_pnl": result.get("best_train_pnl"),
        "best": result.get("best"),
        "recommendation": result.get("recommendation"),
        "warnings": list(result.get("warnings") or []),
        "error": row.get("error"),
    }


def _run_row(db, run_id: str) -> dict | None:
    try:
        rows = db.select(RUNS_TABLE, filters={"id": str(run_id)}, limit=1)
    except Exception:
        return None
    return rows[0] if rows else None


def optimization_status(db, run_id: str) -> dict:
    """Status + stage + partial verdict for one run. Never raises."""
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    row = _run_row(db, run_id)
    if row is None:
        return {"ok": False, "error": "unknown optimization run"}
    return _status_dict(row)


def list_optimizations(db, limit: int = 20) -> dict:
    """Recent runs, newest first. Never raises."""
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    try:
        page = max(1, min(int(limit or 20), 100))
    except (TypeError, ValueError):
        page = 20
    try:
        rows = db.select(RUNS_TABLE, order="created_at", desc=True,
                         limit=page)
    except Exception as exc:
        return {"ok": False, "error": f"{exc}"[:200]}
    try:
        total = db.count(RUNS_TABLE)
    except Exception:
        total = None
    return {"ok": True, "runs": [_status_dict(r) for r in rows or []],
            "total": total if total is not None else len(rows or [])}


def _iteration_dict(row: dict) -> dict:
    return {
        "iteration": int(row.get("iteration") or 0),
        "config": row.get("config") or {},
        "config_diff": row.get("config_diff") or {},
        "train_metrics": row.get("train_metrics"),
        "validation_metrics": row.get("validation_metrics"),
        "improved": bool(row.get("improved", False)),
    }


def optimization_iterations(db, run_id: str, offset: int = 0,
                            limit: int = 100) -> dict:
    """Paged per-iteration summaries, chronological. Never raises."""
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    if _run_row(db, run_id) is None:
        return {"ok": False, "error": "unknown optimization run"}
    try:
        page = max(1, min(int(limit or 100), 500))
    except (TypeError, ValueError):
        page = 100
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        rows = db.select(ITER_TABLE, filters={"run_id": str(run_id)},
                         order="iteration", desc=False,
                         limit=page, offset=start)
    except Exception as exc:
        return {"ok": False, "error": f"{exc}"[:200]}
    try:
        total = db.count(ITER_TABLE, filters={"run_id": str(run_id)})
    except Exception:
        total = None
    rows = rows or []
    return {"ok": True, "run_id": str(run_id),
            "rows": [_iteration_dict(r) for r in rows],
            "total": total if total is not None else len(rows)}


def optimization_equity(db, run_id: str,
                        iteration: int | None = None) -> dict:
    """Trades + equity curve for one iteration (default: best, else baseline).

    Never raises. Unknown runs or iterations without evidence are refused
    with ``ok: False`` instead of inventing a curve.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    row = _run_row(db, run_id)
    if row is None:
        return {"ok": False, "error": "unknown optimization run"}
    if iteration is None:
        best = (row.get("result") or {}).get("best") or {}
        try:
            iteration = int(best.get("iteration", 0))
        except (TypeError, ValueError):
            iteration = 0
    try:
        iteration = int(iteration)
    except (TypeError, ValueError):
        return {"ok": False, "error": "iteration must be an integer"}
    if iteration < 0:
        return {"ok": False, "error": "iteration must be >= 0"}
    try:
        it_rows = db.select(ITER_TABLE,
                            filters={"run_id": str(run_id),
                                     "iteration": iteration}, limit=1)
        eq_rows = db.select(EQUITY_TABLE,
                            filters={"run_id": str(run_id),
                                     "iteration": iteration},
                            order="date", desc=False, limit=5000)
        trade_rows = db.select(TRADES_TABLE,
                               filters={"run_id": str(run_id),
                                        "iteration": iteration},
                               order="trade_id", desc=False, limit=5000)
    except Exception as exc:
        return {"ok": False, "error": f"{exc}"[:200]}
    if not it_rows and not (eq_rows or trade_rows):
        return {"ok": False, "error": "unknown iteration"}
    equity = [{
        "date": r.get("date"),
        "equity": r.get("equity"),
        "realized_pnl": r.get("realized_pnl"),
        "unrealized_pnl": r.get("unrealized_pnl"),
        "drawdown_pct": r.get("drawdown_pct"),
    } for r in (eq_rows or [])]
    trades = [{
        "trade_id": r.get("trade_id"),
        "asset": r.get("asset"),
        "direction": r.get("direction"),
        "entry_date": r.get("entry_date"),
        "entry_price": r.get("entry_price"),
        "exit_date": r.get("exit_date"),
        "exit_price": r.get("exit_price"),
        "volume": r.get("volume"),
        "pnl_usd": r.get("pnl_usd"),
        "r_multiple": r.get("r_multiple"),
        "exit_reason": r.get("exit_reason"),
        "mae_r": r.get("mae_r"),
        "mfe_r": r.get("mfe_r"),
        "ambiguous": bool(r.get("ambiguous", False)),
    } for r in (trade_rows or [])]
    return {"ok": True, "run_id": str(run_id), "iteration": iteration,
            "rows": equity, "trades": trades, "total": len(equity)}


def _event_dict(row: dict) -> dict:
    return {
        "seq": row.get("seq"),
        "level": row.get("level"),
        "stage": row.get("stage"),
        "iteration": row.get("iteration"),
        "event": row.get("event"),
        "message": row.get("message"),
        "data": row.get("data") or {},
        "created_at": row.get("created_at"),
    }


def optimization_logs(db, run_id: str, offset: int = 0,
                      limit: int = 200) -> dict:
    """Paged per-run event log, ordered by seq ascending. Never raises.

    Validates the run exists, then pages ``(run_id, seq)`` with ``limit``
    capped at 500. Returns ``{ok, run_id, rows, last_seq, more}`` where
    ``more`` is ``len(rows) == limit``. No ``Database.count`` — the events
    table has no ``id`` column for it.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    if _run_row(db, run_id) is None:
        return {"ok": False, "error": "unknown optimization run"}
    try:
        page = max(1, min(int(limit or 200), 500))
    except (TypeError, ValueError):
        page = 200
    try:
        start = max(0, int(offset or 0))
    except (TypeError, ValueError):
        start = 0
    try:
        rows = db.select(EVENTS_TABLE, filters={"run_id": str(run_id)},
                         order="seq", desc=False, limit=page, offset=start)
    except Exception as exc:
        return {"ok": False, "error": f"{exc}"[:200]}
    rows = rows or []
    try:
        rows = sorted(rows, key=lambda r: r.get("seq", 0))
    except Exception:
        pass
    out = [_event_dict(r) for r in rows]
    return {"ok": True, "run_id": str(run_id), "rows": out,
            "last_seq": out[-1]["seq"] if out else None,
            "more": len(out) == page}


def cancel_optimization(db, run_id: str) -> dict:
    """Cooperative stop — the worker checks the flag between iterations.

    Cancelling an already-terminal run returns its status unchanged.
    Completed iteration rows are always preserved. Never raises.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    row = _run_row(db, run_id)
    if row is None:
        return {"ok": False, "error": "unknown optimization run"}
    if str(row.get("status")) in TERMINAL_STATUSES:
        return _status_dict(row)
    event = _CANCEL.setdefault(str(run_id), threading.Event())
    try:
        event.set()
    except Exception:
        pass
    now = _now_iso()
    _patch_run(db, str(run_id), {"status": "cancelled",
                                 "stage": "cancelled",
                                 "finished_at": now,
                                 "error": "cancelled"})
    # Return the fresh row, not the pre-patch in-memory copy: the worker may
    # have advanced progress/stage between our read and our patch.
    fresh = _run_row(db, str(run_id))
    if fresh is not None:
        return _status_dict(fresh)
    patched = dict(row)
    patched.update({"status": "cancelled", "stage": "cancelled",
                    "finished_at": now, "error": "cancelled"})
    return _status_dict(patched)


def apply_recommendation(db, run_id: str, *, confirm: bool) -> dict:
    """Guarded diff-only apply of one run's recommendation. Never raises.

    This is the single explicit exception to "the optimizer never writes
    live settings": it writes ONLY the recommended diff fields, through the
    shared settings merge/audit path, and only after explicit confirmation.

    Guards, in order (every refusal performs zero writes):
    (1) unknown run -> refuse; run not terminal-``complete``, or with no
        recommendation/best -> refuse;
    (2) ``confirm is True`` required — any other value, including truthy
        non-``True``, refuses;
    (3) recompute ``diff_settings`` between the run's captured baseline and
        live settings — non-empty means live moved since the run -> refuse
        with a ``baseline``-mentioning error;
    (4) recompute the recommendation diff — empty means nothing to change ->
        success with ``applied: {}`` and no row write;
    (5) otherwise write ``{field: to-value}`` via the shared settings merge
        helper with ``source="simulation_apply"`` and the run id in the
        audit row.

    A successful apply inherently makes the next apply stale (live now
    differs from the stored baseline). Returns ``{ok: True, run_id,
    applied: {field: {from, to}}, settings}`` on success.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    row = _run_row(db, str(run_id))
    if row is None:
        return {"ok": False, "error": "unknown optimization run"}
    if str(row.get("status")) != "complete" or str(row.get("stage")) != "done":
        return {"ok": False, "error": (
            f"run {run_id} is not complete "
            f"(status={row.get('status')}) — apply refused")}
    result = row.get("result") or {}
    rec = result.get("recommendation")
    if (result.get("best") is None or not isinstance(rec, dict)
            or not rec.get("config")):
        return {"ok": False, "error": "run has no recommendation to apply"}
    if confirm is not True:
        return {"ok": False, "error": (
            "apply requires confirm: true — refusing to write live settings")}
    stored_raw = row.get("baseline_settings")
    if not isinstance(stored_raw, dict) or not stored_raw:
        return {"ok": False, "error": (
            "run baseline is missing or unreadable — apply refused")}
    try:
        stored_baseline = AppSettings.model_validate(stored_raw)
    except Exception:
        return {"ok": False, "error": (
            "run baseline is unreadable — apply refused")}
    try:
        best_config = AppSettings.model_validate(rec.get("config") or {})
    except Exception:
        return {"ok": False, "error": (
            "run recommendation is unreadable — apply refused")}
    try:
        from app.api.routes.settings import (
            apply_settings_patch,
            diff_settings,
            try_load_settings,
        )
        live_loaded = try_load_settings(db)
        if live_loaded is None:
            return {"ok": False, "error": (
                "live settings are unreadable — apply refused")}
        live_now = live_loaded
    except Exception:
        return {"ok": False, "error": (
            "live settings are unreadable — apply refused")}
    drift = diff_settings(stored_baseline, live_now)
    if drift:
        fields = ", ".join(str(d.get("field", "?")) for d in drift[:8])
        more = f" (+{len(drift) - 8} more)" if len(drift) > 8 else ""
        return {"ok": False, "error": (
            "live settings changed since the run baseline was captured "
            f"({len(drift)} field(s) differ: {fields}{more}) — apply "
            "refused as stale baseline")}
    applied = _json_safe(config_diff(stored_baseline, best_config))
    if not applied:
        return {"ok": True, "run_id": str(run_id), "applied": {},
                "settings": live_now.model_dump(mode="json")}
    diff_values = {field: entry["to"] for field, entry in applied.items()
                   if isinstance(entry, dict) and "to" in entry}
    if not diff_values:
        return {"ok": True, "run_id": str(run_id), "applied": {},
                "settings": live_now.model_dump(mode="json")}
    try:
        saved = apply_settings_patch(db, diff_values,
                                     source="simulation_apply",
                                     run_id=str(run_id))
    except Exception as exc:
        return {"ok": False, "error": f"settings write failed: {exc}"[:200]}
    if not getattr(saved, "ok", False):
        return {"ok": False, "error": (
            getattr(saved, "message", "") or "settings write failed")}
    return {"ok": True, "run_id": str(run_id), "applied": applied,
            "settings": saved.settings.model_dump(mode="json")}
