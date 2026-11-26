"""Portfolio replay engine — Task 1: models, shared timeline, flat skeleton.

Pure replay code: no network, no live database, no broker calls.
Later tasks (signals, sizing, full loop) extend the `replay_portfolio`
skeleton without changing these model field names.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Optional

from app.integrations.quotes import Candle, snapshot_from_candles
from app.models.schemas import AppSettings

NEWS_WARNING = "News-optimistic: historical news replay is unavailable."

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
    cost_config: Any | None = None,
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
