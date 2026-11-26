"""Portfolio replay engine — Task 1: models, shared timeline, flat skeleton.

Pure replay code: no network, no live database, no broker calls.
Later tasks (signals, sizing, full loop) extend the `replay_portfolio`
skeleton without changing these model field names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Optional

from app.integrations.quotes import Candle, snapshot_from_candles
from app.models.schemas import (
    DEFAULT_SPREADS,
    G,
    AppSettings,
    quote_usd_rate,
    usd_per_quote_unit,
)

NEWS_WARNING = "News-optimistic: historical news replay is unavailable."

#: Module-level alias for the ``datetime.date`` class: ``size_for_replay``
#: names a parameter ``date``, which shadows the imported class inside that
#: function body, so date-indexed rate lookups use this alias instead.
_DATE = date

#: Closed bars of history the indicators need before a snapshot is
#: meaningful — mirrors ``strategy_replay.DEFAULT_WARMUP`` (EMA50/EMA100
#: need room to converge; gold needs EMA200 + Wilder ATR14).
REPLAY_WARMUP = 200


@dataclass(frozen=True)
class ReplayTrade:
    trade_id: str
    asset: str
    direction: str
    entry_date: date
    entry_price: float
    exit_date: date
    exit_price: float
    volume: float
    pnl_usd: float
    r_multiple: float
    exit_reason: str
    mae_r: float
    mfe_r: float
    ambiguous: bool


@dataclass(frozen=True)
class EquityPoint:
    date: date
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    drawdown_pct: float


@dataclass(frozen=True)
class ReplayMetrics:
    net_pnl: float = 0.0
    gross_pnl: float = 0.0
    costs: float = 0.0
    trades: int = 0
    wins: int = 0
    win_rate_pct: float = 0.0
    profit_factor: float = 0.0
    average_r: float = 0.0
    max_drawdown_pct: float = 0.0
    ambiguous_pct: float = 0.0


@dataclass
class ReplayResult:
    trades: list[ReplayTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    metrics: ReplayMetrics = field(default_factory=ReplayMetrics)
    warnings: list[str] = field(default_factory=list)


def _candle_date(candle: Candle, asset: str) -> date:
    if candle.t <= 0:
        raise ValueError(f"{asset}: candle is missing its UTC timestamp")
    return datetime.fromtimestamp(candle.t, tz=timezone.utc).date()


def build_timeline(
    series: dict[str, list[Candle]],
) -> list[tuple[date, dict[str, Candle]]]:
    """Merge per-asset bars onto one chronological date axis.

    Each asset's bars must be sorted oldest-first with unique dates;
    unsorted or duplicate bars raise ValueError. Missing bars are never
    invented: a date entry contains only assets that traded that date.
    """
    by_asset: dict[str, dict[date, Candle]] = {}
    for asset in sorted(series):
        bars = series[asset]
        dated: dict[date, Candle] = {}
        prev: Optional[date] = None
        for bar in bars:
            day = _candle_date(bar, asset)
            if prev is not None and day < prev:
                raise ValueError(f"{asset}: bars are not sorted oldest-first")
            if day == prev or day in dated:
                raise ValueError(f"{asset}: duplicate bars for date {day}")
            dated[day] = bar
            prev = day
        by_asset[asset] = dated

    days = sorted({day for dated in by_asset.values() for day in dated})
    timeline: list[tuple[date, dict[str, Candle]]] = []
    for day in days:
        row = {asset: by_asset[asset][day] for asset in sorted(by_asset) if day in by_asset[asset]}
        timeline.append((day, row))
    return timeline


@dataclass(frozen=True)
class ReplaySignal:
    """One candidate signal at a signal-bar close (reference level, not a fill).

    ``entry`` is the signal-bar close; Task 3 executes at the next bar open.
    ``snapshot`` is deterministic plain-Python indicator data (no live
    timestamps, DB objects, or shared mutable state) for later replay exits.
    """

    asset: str
    date: date
    direction: str
    entry: float
    stop_loss: float
    take_profit: float
    opportunity: float
    confidence: float
    snapshot: dict


def signal_at(
    asset: str,
    history: list[Candle],
    settings: AppSettings,
    date: date,
) -> Optional[ReplaySignal]:
    """Production-parity candidate signal for ``asset`` as of ``date``.

    Filters ``history`` to UTC dates ``<= date`` (never reads future bars),
    requires a bar on ``date`` and at least ``REPLAY_WARMUP`` usable closed
    bars, then runs the production path: ``snapshot_from_candles`` →
    ``StrategyEngine.opportunity_score`` (direction-aware) →
    ``evidence_confidence`` gates (``effective_min_confidence`` /
    ``effective_min_opportunity``) → gold entry policy
    (``gold_entry_block_reason``: ATR ceiling incl. the
    ``gold_max_atr_pct == 0`` disable, BUY-only breakout gate; SELL stays
    trend-following) → production SL/TP (``build_proposal`` +
    ``effective_sl_tp`` with ``apply_cap=False``, mirroring signal
    generation — the risk-budget cap is execution's gate).

    Returns ``None`` for insufficient history, a missing/invalid snapshot, a
    blocked gold entry, a failed confidence/opportunity gate, or no
    production direction. Never touches the network, DB, broker, or live
    settings writes. ``history`` must be oldest-first (as in
    ``build_timeline``); dateless candles raise ``ValueError`` like Task 1.
    """
    from app.core.gold_policy import gold_entry_block_reason
    from app.engine.strategy_engine import (
        IndicatorSnapshot,
        StrategyEngine,
        regime_of,
    )
    from app.models.schemas import (
        G as _G,
        effective_min_confidence,
        effective_min_opportunity,
        effective_sl_tp,
    )

    wanted = date.date() if isinstance(date, datetime) else date

    prefix: list[Candle] = []
    on_date: Optional[Candle] = None
    for bar in history or []:
        day = _candle_date(bar, asset)
        if day <= wanted:
            prefix.append(bar)
            if day == wanted:
                on_date = bar
    if on_date is None or len(prefix) < REPLAY_WARMUP:
        return None

    ordered = sorted(prefix, key=lambda c: c.t)
    signal_bar = on_date
    if ordered[-1].t != on_date.t or _candle_date(ordered[-1], asset) != wanted:
        # Duplicate bars share the date: the latest same-date bar is the close.
        for bar in reversed(ordered):
            if _candle_date(bar, asset) == wanted:
                signal_bar = bar
                break
        ordered = [b for b in ordered if b.t <= signal_bar.t]
        if len(ordered) < REPLAY_WARMUP:
            return None

    snap = snapshot_from_candles(
        asset, ordered, news_sentiment=0.0, high_impact_event=False
    )
    if not snap or not snap.get("price"):
        return None

    try:
        ind = IndicatorSnapshot(**snap)
    except TypeError:
        return None
    if not ind.price or not ind.atr_pct:
        return None

    # Per-asset volatility ceiling rides on the snapshot (scanner parity).
    try:
        from app.core.gold_policy import effective_max_atr_pct

        ind.atr_threshold_pct = effective_max_atr_pct(settings, asset)
    except Exception:
        pass

    # Direction exactly as market_scanner picks it (EMA spread, else Supertrend).
    if ind.ema_fast and ind.ema_slow:
        direction = "BUY" if ind.ema_fast > ind.ema_slow else "SELL"
    else:
        direction = "BUY" if (ind.supertrend_dir or 0) > 0 else "SELL"

    engine = StrategyEngine()
    try:
        opp = engine.opportunity_score(ind, direction=direction)
    except Exception:
        return None

    min_conf = effective_min_confidence(settings, asset)
    min_opp = effective_min_opportunity(settings, asset)
    if opp.score < min_opp or opp.confidence < min_conf:
        return None

    # Gold entry policy (ATR ceiling, degraded/short-history guards, SELL
    # ADX/trend checks, BUY-only breakout gate). Non-gold always passes.
    try:
        blocked = gold_entry_block_reason(
            settings,
            asset=asset,
            direction=direction,
            snapshot={
                "ema_fast": ind.ema_fast,
                "ema_slow": ind.ema_slow,
                "adx": ind.adx,
                "atr_pct": ind.atr_pct,
                "breakout_state": ind.breakout_state,
                "bar_count": len(ordered),
            },
        )
    except Exception:
        return None
    if blocked:
        return None

    # Production SL/TP: structural stop (gold breakout level when set) with
    # the Settings clamp band, then the shared tier/cap/spread-floor math.
    try:
        proposal = engine.build_proposal(
            ind,
            opp,
            risk_per_trade_pct=float(getattr(settings, "risk_per_trade_pct", 0) or 0),
            regime_bullish=(direction == "BUY"),
            rr_target=max(0.5, float(_G(settings, "rr_target"))),
            sl_min_pct=settings.sl_distance_min_pct,
            sl_max_pct=settings.sl_distance_max_pct,
            invalidation_level=(
                float(ind.breakout_level or 0)
                if str(asset or "").upper() == "XAUUSD"
                else 0.0
            ),
        )
        sl, tp, _dist, _tier, _capped = effective_sl_tp(
            settings,
            float(proposal.entry),
            float(proposal.stop_loss),
            float(proposal.take_profit or 0),
            asset,
            direction,
            apply_cap=False,
        )
    except Exception:
        return None
    if not sl or not tp:
        return None

    snapshot = {
        "price": float(ind.price),
        "ema_fast": float(ind.ema_fast or 0),
        "ema_slow": float(ind.ema_slow or 0),
        "adx": float(ind.adx or 0),
        "supertrend_dir": int(ind.supertrend_dir or 0),
        "rsi": float(ind.rsi or 0),
        "macd_hist": float(ind.macd_hist or 0),
        "atr_pct": float(ind.atr_pct or 0),
        "volatility_index": float(ind.volatility_index or 0),
        "price_change_pct_20": float(ind.price_change_pct_20 or 0),
        "breakout_state": float(ind.breakout_state or 0),
        "breakout_level": float(ind.breakout_level or 0),
        "regime": str(regime_of(ind) or ""),
    }
    return ReplaySignal(
        asset=asset,
        date=wanted,
        direction=direction,
        entry=float(proposal.entry),
        stop_loss=float(sl),
        take_profit=float(tp),
        opportunity=float(opp.score),
        confidence=float(opp.confidence),
        snapshot=snapshot,
    )


def replay_portfolio(
    series: dict[str, list[Candle]],
    settings: AppSettings,
    *,
    starting_capital: float = 1000.0,
    cost_config: CostConfig | None = None,
    warnings: list[str] | None = None,
) -> ReplayResult:
    """Minimal flat skeleton (Task 1): no signals or exits yet.

    Validates the shared timeline, then returns no trades, zero metrics,
    and an equity curve starting at the user-supplied capital. Later tasks
    replace the flat body with the full replay loop.
    """
    _ = settings
    _ = cost_config
    timeline = build_timeline(series)

    merged = list(warnings) if warnings else []
    if NEWS_WARNING not in merged:
        merged.append(NEWS_WARNING)

    if not timeline:
        return ReplayResult(
            trades=[], equity_curve=[], metrics=ReplayMetrics(), warnings=merged
        )

    first_day = timeline[0][0]
    curve = [
        EquityPoint(
            date=first_day,
            equity=starting_capital,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            drawdown_pct=0.0,
        )
    ]
    return ReplayResult(
        trades=[], equity_curve=curve, metrics=ReplayMetrics(), warnings=merged
    )


# ---------------------------------------------------------------------------
# Task 3: sizing, conversion, entry, and costs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostConfig:
    """Static replay cost table (offline mirror of production paper costs).

    ``spreads`` maps UPPER-case asset → full bid-ask width in price units.
    Assets absent from ``spreads`` fall back to ``default_spread`` (the
    legacy global ``paper_spread``), so an unknown symbol still fills with
    a configured width instead of silently trading at mid.
    ``commission_per_lot_per_side`` is USD per 1.00 lot, charged once per
    side (entry commission at fill; the exit side is aggregated later).
    ``exit_spread_mult`` scales the full exit spread cost during later PnL
    aggregation (0.5 completes the entry half-spread into one full
    round-trip spread).
    """

    spreads: dict[str, float]
    commission_per_lot_per_side: float
    exit_spread_mult: float
    default_spread: float = 0.0


def _static_spread(settings: AppSettings, asset: str) -> float:
    """Per-symbol spread for replay fills, without any live quote branch.

    Offline mirror of ``effective_spread`` (production ``schemas`` helper):
    finite non-negative ``settings.spread_overrides[ASSET]`` first, then
    ``DEFAULT_SPREADS[ASSET]``, then the legacy global ``paper_spread``.
    The live XAUUSD quote-cache branch is deliberately excluded — replay
    must be deterministic and offline, so every test and run sees the same
    static width for an asset.
    """
    a = str(asset or "").upper()
    overrides = getattr(settings, "spread_overrides", None) or {}
    if a in overrides and overrides[a] is not None:
        try:
            v = float(overrides[a])
        except (TypeError, ValueError):
            v = None
        if v is not None and math.isfinite(v) and v >= 0:
            return v
    if a in DEFAULT_SPREADS:
        return float(DEFAULT_SPREADS[a])
    try:
        return max(0.0, float(getattr(settings, "paper_spread", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _nonneg_finite(value, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v) or v < 0:
        return default
    return v


def default_cost_config(settings: AppSettings) -> CostConfig:
    """Build the static replay cost table from ``settings``.

    Commission and exit-spread multiplier come from
    ``paper_commission_per_lot`` / ``paper_exit_spread_mult`` with the
    production canonical defaults (3.5 / 0.5) when absent; an explicit 0 is
    kept (commission-free / no exit spread). Spreads resolve every
    ``DEFAULT_SPREADS`` symbol plus any valid override via ``_static_spread``;
    ``default_spread`` carries the finite non-negative legacy global
    ``paper_spread`` for symbols outside that table (invalid values → 0.0).
    """
    spreads = {
        a: _static_spread(settings, a)
        for a in set(DEFAULT_SPREADS) | {
            str(k).upper()
            for k in (getattr(settings, "spread_overrides", None) or {})
        }
    }
    commission = _nonneg_finite(
        G(settings, "paper_commission_per_lot", zero_as_missing=False), 3.5
    )
    exit_mult = _nonneg_finite(
        G(settings, "paper_exit_spread_mult", zero_as_missing=False), 0.5
    )
    return CostConfig(
        spreads=spreads,
        commission_per_lot_per_side=commission,
        exit_spread_mult=exit_mult,
        default_spread=_nonneg_finite(
            G(settings, "paper_spread", zero_as_missing=False), 0.0
        ),
    )


def build_rate_map(series: dict[str, list[Candle]]) -> dict[date, dict[str, float]]:
    """Per-date snapshot of each asset's last known close (carry-forward).

    Walks the shared timeline oldest-first; each date's map holds only
    closes known on or before that date (never a future bar). Missing bars
    are never invented — an asset simply keeps its previous close.
    Non-finite / non-positive closes are omitted.
    """
    timeline = build_timeline(series)
    rate_map: dict[date, dict[str, float]] = {}
    last: dict[str, float] = {}
    for day, row in timeline:
        for asset in sorted(row):
            try:
                close = float(row[asset].c)
            except (TypeError, ValueError):
                continue
            if math.isfinite(close) and close > 0:
                last[str(asset).upper()] = close
        rate_map[day] = dict(last)
    return rate_map


def _resolve_rates_snapshot(
    rates: dict | None, wanted: date
) -> dict | None:
    """Resolve ``rates`` to the flat snapshot valid on ``wanted``.

    Accepts a flat ``{ASSET: price}`` unit-test snapshot (returned as-is)
    or a date-indexed ``{date: {ASSET: price}}`` map from ``build_rate_map``
    (exact date, else latest prior date — never a future date). Returns an
    empty snapshot when no prior date exists, and None when ``rates`` is
    not a usable map (production legacy conversion applies).
    """
    if rates is None or not isinstance(rates, dict):
        return None if rates is None else {}
    if rates and all(isinstance(k, _DATE) for k in rates):
        norm = {k.date() if isinstance(k, datetime) else k: v for k, v in rates.items()}
        best: Optional[date] = None
        for day in norm:
            if day <= wanted and (best is None or day > best):
                best = day
        if best is None:
            return {}
        snap = norm[best]
        return dict(snap) if isinstance(snap, dict) else {}
    return dict(rates)


def size_for_replay(
    settings: AppSettings,
    signal: ReplaySignal,
    equity: float,
    rates: dict | None,
    date: date,
) -> float:
    """Replay lots for ``signal`` against simulated ``equity`` (USD).

    Uses the simulated ``equity`` — not ``settings.capital`` — by sizing on
    an equity-adjusted settings copy through the production path:
    ``size_position`` (risk_to_lot + effective min-lot floor + per-asset
    contract value + quote→USD conversion), then ``min_lot_budget_breach``
    on the same copy — a breach returns 0.0 (no valid size). ``rates`` is a
    flat snapshot or a date-indexed map (exact date or latest prior, never
    future). Returns 0.0 for invalid inputs, zero stop distance, a cross
    that cannot derive its quote-to-USD rate (fail closed), non-positive
    equity, or non-finite results.
    """
    from app.services.execution import min_lot_budget_breach, size_position

    try:
        eq = float(equity)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(eq) or eq <= 0:
        return 0.0
    try:
        entry = float(signal.entry)
        stop = float(signal.stop_loss)
    except (TypeError, ValueError, AttributeError):
        return 0.0
    if not (math.isfinite(entry) and math.isfinite(stop)):
        return 0.0
    stop_distance = abs(entry - stop)
    if not math.isfinite(stop_distance) or stop_distance <= 0:
        return 0.0
    asset = str(getattr(signal, "asset", "") or "").upper()
    if not asset:
        return 0.0
    wanted = date.date() if isinstance(date, datetime) else date
    snapshot = _resolve_rates_snapshot(rates, wanted)
    if snapshot is None:
        rate = usd_per_quote_unit(asset, entry)
    else:
        rate = quote_usd_rate(asset, entry, snapshot)
    if rate is None:
        return 0.0
    try:
        scoped = settings.model_copy(update={"capital": eq})
    except Exception:
        return 0.0
    try:
        lots = float(size_position(scoped, entry, stop, asset, rates=snapshot))
    except Exception:
        return 0.0
    if not math.isfinite(lots) or lots <= 0:
        return 0.0
    try:
        breach = min_lot_budget_breach(scoped, entry, stop, asset, rates=snapshot)
    except Exception:
        breach = None
    if breach:
        return 0.0
    return lots


def entry_fill(
    signal: ReplaySignal,
    next_bar_open: float,
    direction: str,
    costs: CostConfig,
    *,
    volume: float,
) -> tuple[float, float]:
    """Next-bar-open execution fill plus entry commission cost (USD).

    BUY pays ``open + full_spread/2``, SELL receives ``open - full_spread/2``
    — the fill never uses the signal close as a price. The half-spread cost
    is embodied in the fill exactly once; the returned cost is entry
    commission only (``commission_per_lot_per_side × volume``), so later
    aggregation must not subtract the spread again. Raises ValueError for
    unknown direction, negative volume, or non-finite prices/costs.
    """
    try:
        px = float(next_bar_open)
    except (TypeError, ValueError):
        raise ValueError("next_bar_open must be a finite price")
    try:
        vol = float(volume)
    except (TypeError, ValueError):
        raise ValueError("volume must be a finite non-negative lot size")
    if not math.isfinite(px) or not math.isfinite(vol) or vol < 0:
        raise ValueError("non-finite price/volume or negative volume")
    side = str(direction or "").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"unknown direction: {direction!r}")
    asset = str(getattr(signal, "asset", "") or "").upper()
    try:
        table = costs.spreads or {}
    except AttributeError:
        raise ValueError("unusable cost spreads")
    _MISSING = object()
    raw = table.get(asset, _MISSING)
    if raw is _MISSING:
        try:
            raw = float(getattr(costs, "default_spread", 0.0))
        except (TypeError, ValueError, AttributeError):
            raise ValueError(f"unusable spread for {asset or '?'}")
    try:
        spread = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"unusable spread for {asset or '?'}")
    try:
        commission = float(costs.commission_per_lot_per_side)
    except (TypeError, ValueError, AttributeError):
        raise ValueError("unusable commission_per_lot_per_side")
    if not math.isfinite(spread) or spread < 0:
        raise ValueError(f"unusable spread for {asset or '?'}")
    if not math.isfinite(commission) or commission < 0:
        raise ValueError("unusable commission_per_lot_per_side")
    half = spread / 2.0
    fill = round(px + half, 5) if side == "BUY" else round(px - half, 5)
    return fill, commission * vol
