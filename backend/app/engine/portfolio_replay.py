"""Portfolio replay engine — Task 1: models, shared timeline, flat skeleton.

Pure replay code: no network, no live database, no broker calls.
Later tasks (signals, sizing, full loop) extend the `replay_portfolio`
skeleton without changing these model field names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from app.integrations.quotes import Candle, snapshot_from_candles
from app.models.schemas import (
    DEFAULT_SPREADS,
    G,
    AppSettings,
    CorrelationEngine,
    ExposureEngine,
    contract_value_for,
    effective_sl_tp,
    quote_usd_rate,
    sl_cap_below_spread,
    spread_sl_floor,
    usd_per_quote_unit,
)

NEWS_WARNING = "News-optimistic: historical news replay is unavailable."

DAILY_BAR_WARNING = (
    "Daily-bar replay: no intrabar tick ordering; "
    "bars touching both SL and TP resolve stop-first."
)

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
    gross_pnl_usd: float = 0.0
    costs_usd: float = 0.0


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
    profit_factor: float | None = None
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
    snapshot_cache: dict | None = None,
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

    ``snapshot_cache`` is an optional caller-owned per-(asset, date) cache
    for the ``snapshot_from_candles`` result: on a hit the cached snapshot
    is reused (identical inputs → identical signal, no recompute); on a
    miss the computed snapshot is stored. The cached snapshot is treated
    as read-only. ``None`` (default) preserves the uncached behavior.
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

    snap: dict
    cache_key = (str(asset), wanted)
    if snapshot_cache is not None and cache_key in snapshot_cache:
        cached = snapshot_cache[cache_key]
        snap = cached if isinstance(cached, dict) else {}
    else:
        snap = snapshot_from_candles(
            asset, ordered, news_sentiment=0.0, high_impact_event=False
        )
        if snapshot_cache is not None:
            snap = snap if isinstance(snap, dict) else {}
            snapshot_cache[cache_key] = snap
    if not snap or not snap.get("price"):
        return None

    try:
        ind = IndicatorSnapshot(**snap)
    except TypeError:
        return None
    if not ind.price or not ind.atr_pct:
        return None

    # Per-asset volatility ceiling rides on the snapshot (scanner parity).
    # Offline degradation (explicit, not silent): when the production
    # gold-policy helper is unavailable or fails offline, the snapshot keeps
    # its own default ceiling instead of blocking the signal — replay never
    # touches live/DB-coupled gold policy paths.
    try:
        from app.core.gold_policy import effective_max_atr_pct

        ceiling = effective_max_atr_pct(settings, asset)
    except Exception:
        ceiling = None
    if ceiling is not None:
        ind.atr_threshold_pct = ceiling

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
            spread=_static_spread(settings, asset),
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
    use_snapshot_cache: bool = True,
) -> ReplayResult:
    """Deterministic daily portfolio replay (Task 4: full constraint/exit loop).

    Walks the shared timeline oldest-first. At each date: executes pending
    prior signals at that asset's bar open (next-open entry, never the signal
    close), marks open positions, evaluates hard SL/TP (gap-aware,
    stop-first on ambiguity), then discretionary management / smart exits /
    time stops (bar-close decisions), then generates new close-date signals
    for the next available bar. Remaining positions are force-closed at their
    last available close (``end_of_data``).

    Entry gates mirror production execution: one open position per asset,
    max-open / daily / ISO-weekly counts, per-asset cooldown from the final
    close, allowed-asset universe, portfolio correlation cap, currency
    exposure cap, and the spread-guard. Before sizing, the signal SL/TP is
    re-run through ``effective_sl_tp`` with ``apply_cap=True`` on an
    equity-adjusted settings copy — this is where ``sl_cap_enabled`` takes
    effect (``signal_at`` deliberately mirrors generation with
    ``apply_cap=False``).

    Costs follow the Task 4 clarification: entry spread converts at the
    entry-date quote rate, gross PnL and exit spread convert at the exit-date
    quote rate, open marks use the current date's rate; commissions are USD.
    Pure function: no network, DB, broker, notifications, or settings writes.

    ``use_snapshot_cache`` (default True) shares one per-(asset, date)
    snapshot cache between signal generation and exits: the same
    ``snapshot_from_candles`` inputs computed for a signal are reused by
    the exit stack instead of being recomputed, and repeated
    ``signal_at`` calls for the same bar hit the cache. ``False`` bypasses
    the cache (recomputes every snapshot) with identical outputs — the
    slow path used to prove production-parity equality.
    """
    from app.engine.smart_exit import evaluate_exit

    timeline = build_timeline(series)
    costs = cost_config if cost_config is not None else default_cost_config(settings)
    rate_map = build_rate_map(series)

    merged = list(warnings) if warnings else []
    if NEWS_WARNING not in merged:
        merged.append(NEWS_WARNING)
    if DAILY_BAR_WARNING not in merged:
        merged.append(DAILY_BAR_WARNING)

    if not timeline:
        return ReplayResult(
            trades=[], equity_curve=[], metrics=ReplayMetrics(), warnings=merged
        )

    try:
        capital = float(starting_capital)
    except (TypeError, ValueError):
        capital = 1000.0
    if not math.isfinite(capital) or capital <= 0:
        capital = 1000.0

    allowed = set(settings.effective_assets())
    discretionary = bool(settings.management_allows_discretionary())
    smart_on = bool(G(settings, "smart_exit_enabled"))
    try:
        avg_hold = float(G(settings, "avg_hold_fallback_days") or 4.0)
    except (TypeError, ValueError):
        avg_hold = 4.0
    if not math.isfinite(avg_hold) or avg_hold <= 0:
        avg_hold = 4.0

    last_date_of: dict[str, date] = {}
    for day, row in timeline:
        for asset in row:
            last_date_of[str(asset).upper()] = day

    histories: dict[str, list[Candle]] = {}
    last_mid: dict[str, float] = {}
    pending: dict[str, ReplaySignal] = {}
    opens: list[_OpenPosition] = []
    slices: list[tuple[date, date, str, int, ReplayTrade]] = []
    seq = 0
    last_close: dict[str, date] = {}
    entries_by_date: dict[date, int] = {}
    entries_by_week: dict[tuple[int, int], int] = {}
    realized = 0.0
    curve: list[EquityPoint] = []
    peak = capital
    prev_drawdown = 0.0
    # ONE per-(asset, date) snapshot cache shared by signal generation
    # (``signal_at``) and the exit stack (``snapshot_for``): both call
    # ``snapshot_from_candles`` on the same last-``REPLAY_WARMUP`` window
    # for the same bar, so the second caller reuses the first caller's
    # result instead of recomputing. Keyed ``(asset, date)`` — never
    # wall-clock/random content — so replay stays deterministic.
    snap_cache: dict[tuple[str, date], dict] = {}

    def _compute_snapshot(asset_u: str, day: date) -> dict:
        try:
            hist = histories.get(asset_u, [])
            window = (hist[-REPLAY_WARMUP:]
                      if len(hist) > REPLAY_WARMUP else hist)
            snap = snapshot_from_candles(
                asset_u, window,
                news_sentiment=0.0, high_impact_event=False) or {}
        except Exception:
            snap = {}
        if not isinstance(snap, dict):
            snap = {}
        return snap

    def snapshot_for(asset_u: str, day: date) -> dict:
        if not use_snapshot_cache:
            return _compute_snapshot(asset_u, day)
        key = (asset_u, day)
        if key not in snap_cache:
            snap_cache[key] = _compute_snapshot(asset_u, day)
        return snap_cache[key]

    for day, row in timeline:
        for asset in sorted(row):
            asset_u = str(asset).upper()
            histories.setdefault(asset_u, []).append(row[asset])

        rates_today = _resolve_rates_snapshot(rate_map, day) or {}
        # Strictly prior-date snapshot (never the current bar): the only rate
        # source an open-known entry decision may use.
        prior_rates = _resolve_rates_snapshot(
            rate_map, day - timedelta(days=1)) or {}

        # Entry marks are open-known: today's bar open where traded, else the
        # last known mark. Current closes must not influence entries.
        entry_marks = dict(last_mid)
        for asset in sorted(row):
            try:
                open_px = float(row[asset].o)
            except (TypeError, ValueError):
                continue
            if math.isfinite(open_px) and open_px > 0:
                entry_marks[str(asset).upper()] = open_px

        # ---- 1. prior-signal entries at this bar's open -------------------
        for asset in sorted(row):
            asset_u = str(asset).upper()
            sig = pending.pop(asset_u, None)
            if sig is None:
                continue
            if asset_u not in allowed:
                continue
            # Current marked equity, recomputed before each same-day entry so
            # earlier entries' marks affect later ones. Missing post-entry
            # rates fail the replay instead of booking raw quote PnL.
            equity_now = (capital + realized + _unrealized_total(
                opens, entry_marks, prior_rates, costs))
            opened = _try_enter(
                settings, sig, asset_u, row[asset], day, opens, costs,
                prior_rates, equity_now,
                entries_by_date, entries_by_week, last_close,
            )
            if opened is not None:
                opens.append(opened)
                entries_by_date[day] = entries_by_date.get(day, 0) + 1
                iso = day.isocalendar()
                week_key = (int(iso[0]), int(iso[1]))
                entries_by_week[week_key] = entries_by_week.get(week_key, 0) + 1

        # ---- 2. mark + exits (close-known from here on) ---------------------
        for asset in sorted(row):
            asset_u = str(asset).upper()
            try:
                close_px = float(row[asset].c)
            except (TypeError, ValueError):
                continue
            if math.isfinite(close_px) and close_px > 0:
                last_mid[asset_u] = close_px
        realized_equity = capital + realized
        marked_now = realized_equity + _unrealized_total(
            opens, last_mid, rates_today, costs)
        drawdown_in = prev_drawdown
        if marked_now > peak:
            drawdown_in = 0.0
        elif peak > 0:
            drawdown_in = max(0.0, (peak - marked_now) / peak * 100.0)

        survivors: list[_OpenPosition] = []
        for pos in opens:
            asset_u = pos.asset
            if asset_u not in row:
                survivors.append(pos)
                continue
            bar = row[asset_u]
            outcome = _process_bar(
                settings, pos, bar, day, costs, prior_rates, rates_today,
                discretionary, smart_on, avg_hold, drawdown_in,
                snapshot_for, evaluate_exit,
            )
            if outcome is None:
                if day == last_date_of.get(asset_u):
                    fs = _close_slice(pos, pos.volume_remaining, float(bar.c),
                                      day, "end_of_data", costs, rates_today)
                    if fs is not None:
                        trade, d_real, _d_gross, _d_cost = fs
                        slices.append((trade.exit_date, trade.entry_date,
                                       trade.asset, seq, trade))
                        seq += 1
                        realized += d_real
                        last_close[asset_u] = day
                    else:
                        survivors.append(pos)
                else:
                    survivors.append(pos)
                continue
            new_slices, closed_all, new_realized, _new_gross, _new_costs = outcome
            for sl in new_slices:
                slices.append((sl.exit_date, sl.entry_date, sl.asset, seq, sl))
                seq += 1
            realized += new_realized
            if closed_all:
                last_close[asset_u] = day
            else:
                survivors.append(pos)
            # A stop moved by management applies from the next bar: the hard
            # SL/TP check above already ran against the pre-management stop.
            if day == last_date_of.get(asset_u) and not closed_all:
                fs = _close_slice(pos, pos.volume_remaining, float(bar.c),
                                  day, "end_of_data", costs, rates_today)
                if fs is not None:
                    trade, d_real, _d_gross, _d_cost = fs
                    slices.append((trade.exit_date, trade.entry_date,
                                   trade.asset, seq, trade))
                    seq += 1
                    realized += d_real
                    last_close[asset_u] = day
                else:
                    survivors.append(pos)
        opens = survivors

        # ---- 3. new close-date signals for the next available bar ---------
        for asset in sorted(row):
            asset_u = str(asset).upper()
            if asset_u not in allowed:
                continue
            hist = histories.get(asset_u, [])
            window = hist[-REPLAY_WARMUP:] if len(hist) > REPLAY_WARMUP else hist
            try:
                sig = signal_at(asset_u, window, settings, day,
                                snapshot_cache=(snap_cache
                                                if use_snapshot_cache else None))
            except Exception:
                sig = None
            if sig is not None:
                pending[asset_u] = sig

        # ---- 4. daily equity point -----------------------------------------
        realized_pnl = realized
        unrealized = _unrealized_total(opens, last_mid, rates_today, costs)
        equity = capital + realized_pnl + unrealized
        if equity > peak:
            peak = equity
        if peak > 0:
            drawdown_pct = max(0.0, (peak - equity) / peak * 100.0)
        else:
            drawdown_pct = 0.0
        prev_drawdown = drawdown_pct
        curve.append(EquityPoint(date=day, equity=equity,
                                 realized_pnl=realized_pnl,
                                 unrealized_pnl=unrealized,
                                 drawdown_pct=drawdown_pct))

    ordered = sorted(slices, key=lambda s: (s[0], s[1], s[2], s[3]))
    trades = [
        ReplayTrade(trade_id=f"RT-{i + 1:06d}", asset=t.asset,
                    direction=t.direction, entry_date=t.entry_date,
                    entry_price=t.entry_price, exit_date=t.exit_date,
                    exit_price=t.exit_price, volume=t.volume,
                    pnl_usd=t.pnl_usd, r_multiple=t.r_multiple,
                    exit_reason=t.exit_reason, mae_r=t.mae_r,
                    mfe_r=t.mfe_r, ambiguous=t.ambiguous,
                    gross_pnl_usd=t.gross_pnl_usd, costs_usd=t.costs_usd)
        for i, (_, _, _, _, t) in enumerate(ordered)
    ]
    metrics = _build_metrics(trades, curve)
    return ReplayResult(trades=trades, equity_curve=curve,
                        metrics=metrics, warnings=merged)


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
    *,
    rates_snapshot: dict | None = None,
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

    ``rates_snapshot`` is an already-resolved flat per-date snapshot (what
    the replay loop holds as its once-per-date ``prior_rates``): when given
    it is used directly and the date-indexed re-resolution is skipped —
    same conversion math, no per-position recompute. ``None`` (default)
    preserves the resolving behavior for existing callers.
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
    # Fail closed on a malformed date: date-indexed rate lookup compares
    # dates, so a non-date (e.g. a string) would raise TypeError. A signal
    # that cannot be dated cannot be sized — return 0.0, never raise.
    try:
        wanted = date.date() if isinstance(date, datetime) else date
    except Exception:
        return 0.0
    if not isinstance(wanted, _DATE):
        return 0.0
    # Hoisted fast path: the replay loop already resolved the once-per-date
    # open-known snapshot — reuse it instead of re-resolving per position.
    if rates_snapshot is not None:
        snapshot = rates_snapshot if isinstance(rates_snapshot, dict) else {}
    else:
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
    # A malformed spreads table (e.g. a string or list) has no usable
    # per-asset lookup: raise the documented ValueError, never a raw
    # AttributeError from `.get`.
    _MISSING = object()
    try:
        get = table.get
    except AttributeError:
        raise ValueError(f"unusable spread for {asset or '?'}")
    raw = get(asset, _MISSING)
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


# ---------------------------------------------------------------------------
# Task 4: portfolio constraints and exit stack (internal state + helpers)
# ---------------------------------------------------------------------------

@dataclass
class _OpenPosition:
    """Mutable position state for one open replay leg.

    ``entry_mid`` is the next-bar-open mid the signal executed at;
    ``entry_fill`` is the spread-adjusted fill. ``risk_price`` is the original
    entry-fill-to-initial-stop distance (price units) — every R computation
    (partial trigger, breakeven/trailing, smart-exit R, time-stop R, trade R,
    MAE/MFE) divides by this, never by the trailed stop. Entry costs are
    stored as run totals and allocated to each closed slice in proportion to
    its volume so partial slices plus the final remainder sum to exactly one
    entry side and one exit side.
    """

    asset: str
    direction: str
    signal_date: date
    entry_date: date
    entry_mid: float
    entry_fill: float
    volume_original: float
    volume_remaining: float
    entry_spread_cost: float
    entry_comm_cost: float
    entry_rate: float
    full_spread: float
    initial_sl: float
    initial_tp: float
    current_sl: float
    current_tp: float
    risk_price: float
    partial_done: bool = False
    adv_exc: float = 0.0
    fav_exc: float = 0.0
    snapshot: dict = field(default_factory=dict)


def _full_spread_of(costs: CostConfig, asset_u: str) -> float:
    try:
        table = costs.spreads or {}
    except AttributeError:
        table = {}
    if asset_u in table:
        try:
            return max(0.0, float(table[asset_u]))
        except (TypeError, ValueError):
            return 0.0
    try:
        return max(0.0, float(getattr(costs, "default_spread", 0.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _required_rate(asset_u: str, price: float, snapshot: dict | None) -> float:
    """Quote-currency → USD rate, failing the replay when underivable.

    Never substitutes ``1.0`` for an underivable cross: entry fails closed on
    a missing open-known rate, and an unexpected missing rate after entry
    raises instead of silently booking raw quote-currency PnL. A quote that
    IS USD still resolves to exactly ``1.0`` through production math.
    """
    try:
        rate = quote_usd_rate(asset_u, price, snapshot)
    except Exception as exc:
        raise ValueError(f"replay: quote-USD rate failed for {asset_u}: {exc}")
    if rate is None or not math.isfinite(rate) or rate <= 0:
        raise ValueError(f"replay: no quote-USD rate for {asset_u}")
    return rate


def _try_enter(
    settings: AppSettings,
    signal: ReplaySignal,
    asset_u: str,
    bar: Candle,
    day: date,
    opens: list[_OpenPosition],
    costs: CostConfig,
    prior_rates: dict,
    equity_now: float,
    entries_by_date: dict[date, int],
    entries_by_week: dict[tuple[int, int], int],
    last_close: dict[str, date],
) -> _OpenPosition | None:
    """Next-open execution attempt for one pending signal.

    Every decision input is open-known: the bar open for marks and levels,
    the latest prior-date rate snapshot for conversion/sizing/exposure, and
    current marked equity (starting capital + realized PnL + open marked
    PnL). The entry fails closed when the open-known rate is unavailable.
    Gate order: duplicate asset → max open → daily/weekly counts → cooldown
    → open-known rate required → SL/TP cap + sizing → correlation →
    currency exposure → sub-spread-stop guard → spread guard.

    Production parity for zero limits: ``schemas.frequency_decision`` blocks
    via ``trades_today >= max_trades_daily`` (and the weekly analogue), so a
    configured 0 blocks every entry (0 >= 0) — it never means "unlimited".
    The ``>=`` comparisons below mirror that exactly. The Plan 2 search space
    starts at 1 precisely because 0 disables entries.
    """
    direction = str(getattr(signal, "direction", "") or "").upper()
    if direction not in ("BUY", "SELL"):
        return None
    for pos in opens:
        if pos.asset == asset_u:
            return None
    try:
        max_open = int(getattr(settings, "max_open_positions", 0) or 0)
    except (TypeError, ValueError):
        max_open = 0
    if len(opens) >= max_open:
        return None
    try:
        max_daily = int(getattr(settings, "max_trades_daily", 0) or 0)
    except (TypeError, ValueError):
        max_daily = 0
    if entries_by_date.get(day, 0) >= max_daily:
        return None
    iso = day.isocalendar()
    week_key = (int(iso[0]), int(iso[1]))
    try:
        max_weekly = int(getattr(settings, "max_trades_weekly", 0) or 0)
    except (TypeError, ValueError):
        max_weekly = 0
    if entries_by_week.get(week_key, 0) >= max_weekly:
        return None
    try:
        cooldown_min = float(getattr(settings, "reentry_cooldown_min", 0) or 0)
    except (TypeError, ValueError):
        cooldown_min = 0.0
    if cooldown_min > 0 and asset_u in last_close:
        elapsed_min = (day - last_close[asset_u]).days * 1440.0
        if elapsed_min < cooldown_min:
            return None

    try:
        next_open = float(bar.o)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(next_open) or next_open <= 0:
        return None

    # Open-known quote rate is required BEFORE any cap/sizing math: the
    # production helpers fall back to 1.0 for an underivable cross, so the
    # entry fails closed here and replay cap math never runs on the fallback.
    static_spread = _full_spread_of(costs, asset_u)
    try:
        open_rate = quote_usd_rate(asset_u, next_open, prior_rates)
    except Exception:
        open_rate = None
    if open_rate is None or not math.isfinite(open_rate) or open_rate <= 0:
        return None

    # Simulated equity for this run is supplied by the caller (current
    # marked equity, recomputed before each same-day entry).
    equity = equity_now
    if not math.isfinite(equity) or equity <= 0:
        return None
    try:
        scoped = settings.model_copy(update={"capital": equity})
    except Exception:
        return None
    try:
        sig_sl = float(signal.stop_loss)
        sig_tp = float(signal.take_profit or 0)
    except (TypeError, ValueError):
        return None
    try:
        sl2, tp2, _dist, _tier, _capped = effective_sl_tp(
            scoped, next_open, sig_sl, sig_tp, asset_u, direction,
            apply_cap=True, rates=prior_rates,
            spread=static_spread)
    except Exception:
        return None
    if not sl2 or not tp2:
        return None
    try:
        sl2 = float(sl2)
        tp2 = float(tp2)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(sl2) and math.isfinite(tp2)) or sl2 <= 0 or tp2 <= 0:
        return None

    adjusted = ReplaySignal(
        asset=asset_u, date=signal.date, direction=direction,
        entry=next_open, stop_loss=sl2, take_profit=tp2,
        opportunity=float(getattr(signal, "opportunity", 0.0) or 0.0),
        confidence=float(getattr(signal, "confidence", 0.0) or 0.0),
        snapshot=dict(getattr(signal, "snapshot", None) or {}),
    )
    lots = size_for_replay(settings, adjusted, equity, prior_rates, day,
                             rates_snapshot=prior_rates)
    if not math.isfinite(lots) or lots <= 0:
        return None

    try:
        fill, entry_comm = entry_fill(
            adjusted, next_bar_open=next_open, direction=direction,
            costs=costs, volume=lots)
    except ValueError:
        return None

    # Correlation gate (production portfolio_correlation over open + candidate).
    try:
        corr_cap = float(G(settings, "correlation_cap",
                           zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        corr_cap = 0.0
    if corr_cap > 0:
        try:
            book = sorted({p.asset for p in opens} | {asset_u})
            if CorrelationEngine.portfolio_correlation(book) > corr_cap:
                return None
        except Exception:
            return None

    # Currency-exposure gate (risk-at-stop per currency/direction vs capital).
    try:
        exp_cap = float(G(settings, "max_currency_exposure_pct",
                          zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        exp_cap = 0.0
    if exp_cap > 0:
        try:
            legs = [{
                "asset": p.asset, "direction": p.direction,
                "volume": p.volume_remaining, "entry_price": p.entry_fill,
                "stop_loss": p.current_sl,
            } for p in opens]
            legs.append({"asset": asset_u, "direction": direction,
                         "volume": lots, "entry_price": fill,
                         "stop_loss": sl2})
            res = ExposureEngine.currency_risk(
                legs, equity, exp_cap, rates=prior_rates)
            if res.get("over_cap"):
                return None
        except Exception:
            return None

    # Sub-spread-stop guard (production P0-6): a stop that cannot clear
    # the deterministic static spread is not a stop. Block when the
    # equity-adjusted budget cannot fund a spread-wide min-lot stop, or when
    # the effective SL distance itself sits below the spread floor.
    try:
        if sl_cap_below_spread(scoped, asset_u, price=next_open,
                               rates=prior_rates, spread=static_spread):
            return None
        floor = spread_sl_floor(scoped, asset_u, spread=static_spread)
    except Exception:
        return None
    # Spread guard: full spread as % of the execution SL distance.
    try:
        spread_cap = float(G(settings, "spread_guard_max_pct",
                             zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        spread_cap = 0.0
    spread = static_spread
    exec_dist = abs(fill - sl2)
    if spread_cap > 0 and exec_dist > 0 and spread > 0:
        if spread / exec_dist * 100.0 > spread_cap:
            return None
    if exec_dist <= 0:
        return None
    if floor > 0 and exec_dist < floor - 1e-12:
        return None

    contract = contract_value_for(asset_u)
    try:
        rate_entry = _required_rate(asset_u, next_open, prior_rates)
    except ValueError:
        # Open-known rate unavailable: fail closed, no position.
        return None
    entry_spread_cost = 0.5 * spread * contract * lots * rate_entry
    risk_price = abs(fill - sl2)
    return _OpenPosition(
        asset=asset_u, direction=direction, signal_date=signal.date,
        entry_date=day, entry_mid=next_open, entry_fill=fill,
        volume_original=lots, volume_remaining=lots,
        entry_spread_cost=entry_spread_cost, entry_comm_cost=entry_comm,
        entry_rate=rate_entry, full_spread=spread,
        initial_sl=sl2, initial_tp=tp2, current_sl=sl2, current_tp=tp2,
        risk_price=risk_price,
        snapshot=dict(getattr(signal, "snapshot", None) or {}),
    )


def _close_slice(
    pos: _OpenPosition,
    slice_vol: float,
    exit_mid: float,
    day: date,
    reason: str,
    costs: CostConfig,
    rates: dict,
    ambiguous: bool = False,
) -> tuple[ReplayTrade, float, float, float] | None:
    """Close one slice of ``pos`` at ``exit_mid`` (mid-scale).

    Returns (trade, delta_realized_net, delta_gross, delta_costs). Gross PnL
    is mid-to-mid converted at the caller-supplied rate snapshot (prior-date
    for intrabar exits, close-date for bar-close exits); entry spread converts
    at the entry-date rate; commissions are already USD. R divides the slice
    net by the original fill-to-initial-stop USD risk scaled to the slice.
    A missing quote rate raises instead of booking raw quote-currency PnL.
    """
    try:
        vol = float(slice_vol)
        mid = float(exit_mid)
    except (TypeError, ValueError):
        return None
    if (not math.isfinite(vol) or not math.isfinite(mid)
            or vol <= 0 or mid <= 0):
        return None
    vol = min(vol, pos.volume_remaining)
    if vol <= 0:
        return None
    if pos.volume_original <= 0:
        return None
    sign = 1 if pos.direction == "BUY" else -1
    contract = contract_value_for(pos.asset)
    rate_exit = _required_rate(pos.asset, mid, rates)

    gross = sign * (mid - pos.entry_mid) * vol * contract * rate_exit
    frac = vol / pos.volume_original
    alloc_entry_spread = pos.entry_spread_cost * frac
    alloc_entry_comm = pos.entry_comm_cost * frac
    try:
        exit_mult = float(costs.exit_spread_mult)
    except (TypeError, ValueError, AttributeError):
        exit_mult = 0.5
    if not math.isfinite(exit_mult) or exit_mult < 0:
        exit_mult = 0.5
    try:
        commission = float(costs.commission_per_lot_per_side)
    except (TypeError, ValueError, AttributeError):
        commission = 0.0
    if not math.isfinite(commission) or commission < 0:
        commission = 0.0
    exit_spread = exit_mult * pos.full_spread * contract * vol * rate_exit
    exit_comm = commission * vol
    costs_slice = alloc_entry_spread + alloc_entry_comm + exit_spread + exit_comm
    pnl = gross - costs_slice

    risk_slice = pos.risk_price * vol * contract * pos.entry_rate
    if risk_slice and risk_slice > 0 and math.isfinite(risk_slice):
        r_mult = pnl / risk_slice
    else:
        r_mult = 0.0
    if not math.isfinite(r_mult):
        r_mult = 0.0
    if pos.risk_price > 0 and math.isfinite(pos.risk_price):
        mae_r = min(0.0, pos.adv_exc) / pos.risk_price
        mfe_r = max(0.0, pos.fav_exc) / pos.risk_price
    else:
        mae_r, mfe_r = 0.0, 0.0

    exit_fill = mid - sign * exit_mult * pos.full_spread
    trade = ReplayTrade(
        trade_id="", asset=pos.asset, direction=pos.direction,
        entry_date=pos.entry_date, entry_price=pos.entry_fill,
        exit_date=day, exit_price=round(exit_fill, 5),
        volume=round(vol, 8), pnl_usd=pnl, r_multiple=r_mult,
        exit_reason=reason, mae_r=mae_r, mfe_r=mfe_r, ambiguous=ambiguous,
        gross_pnl_usd=gross, costs_usd=costs_slice,
    )
    pos.volume_remaining = round(pos.volume_remaining - vol, 8)
    if pos.volume_remaining < 1e-9:
        pos.volume_remaining = 0.0
    return trade, pnl, gross, costs_slice


def _update_excursions(pos: _OpenPosition, bar: Candle) -> None:
    try:
        high, low = float(bar.h), float(bar.l)
    except (TypeError, ValueError):
        return
    if not (math.isfinite(high) and math.isfinite(low)):
        return
    if pos.direction == "BUY":
        pos.adv_exc = min(pos.adv_exc, low - pos.entry_mid)
        pos.fav_exc = max(pos.fav_exc, high - pos.entry_mid)
    else:
        pos.adv_exc = min(pos.adv_exc, pos.entry_mid - high)
        pos.fav_exc = max(pos.fav_exc, pos.entry_mid - low)


def _process_bar(
    settings: AppSettings,
    pos: _OpenPosition,
    bar: Candle,
    day: date,
    costs: CostConfig,
    prior_rates: dict,
    close_rates: dict,
    discretionary: bool,
    smart_on: bool,
    avg_hold: float,
    drawdown_pct: float,
    snapshot_for,
    evaluate_exit,
) -> tuple[list[ReplayTrade], bool, float, float, float] | None:
    """Evaluate one daily bar for one open position.

    Returns None when the position stays open, else (slices, closed_all,
    delta_net, delta_gross, delta_costs). Sequencing mirrors position_guard:
    hard SL/TP (pre-management, stop-first) → partial close → breakeven /
    trailing (+ ``smart_exit.ladder_sl``) → smart exit → time stop.
    Intrabar exits (hard, partial) convert at the prior-date open-known
    snapshot; bar-close exits (smart, time, end-of-data) at the close snapshot.
    """
    try:
        o, h, l, c = float(bar.o), float(bar.h), float(bar.l), float(bar.c)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) and v > 0 for v in (o, h, l, c)):
        return None
    if pos.volume_remaining <= 0 or pos.risk_price <= 0:
        return None

    _update_excursions(pos, bar)
    sign = 1 if pos.direction == "BUY" else -1
    sl, tp = pos.current_sl, pos.current_tp

    # ---- hard SL/TP (adverse gap fills at the open; stop-first) ------------
    if pos.direction == "BUY":
        sl_gap = o <= sl
        tp_gap = o >= tp
        sl_touch = sl_gap or l <= sl
        tp_touch = tp_gap or h >= tp
    else:
        sl_gap = o >= sl
        tp_gap = o <= tp
        sl_touch = sl_gap or h >= sl
        tp_touch = tp_gap or l <= tp
    if sl_touch and tp_touch:
        # Both levels inside one bar (neither gapped through at the open, or
        # gapped): stop-first is mandatory and the bar counts as ambiguous.
        fill_mid = o if sl_gap else sl
        done = _close_slice(pos, pos.volume_remaining, fill_mid, day, "sl",
                            costs, prior_rates, ambiguous=True)
        if done is None:
            return None
        trade, d_net, d_gross, d_cost = done
        return [trade], True, d_net, d_gross, d_cost
    if sl_touch:
        fill_mid = o if sl_gap else sl
        done = _close_slice(pos, pos.volume_remaining, fill_mid, day, "sl",
                            costs, prior_rates)
        if done is None:
            return None
        trade, d_net, d_gross, d_cost = done
        return [trade], True, d_net, d_gross, d_cost
    if tp_touch:
        fill_mid = o if tp_gap else tp
        done = _close_slice(pos, pos.volume_remaining, fill_mid, day, "tp",
                            costs, prior_rates)
        if done is None:
            return None
        trade, d_net, d_gross, d_cost = done
        return [trade], True, d_net, d_gross, d_cost

    if not discretionary:
        return None

    risk = pos.risk_price
    r_close = sign * (c - pos.entry_fill) / risk
    out: list[ReplayTrade] = []
    d_net = d_gross = d_cost = 0.0

    # ---- partial close (TP1) — favorable extreme, trigger-or-open fill -----
    try:
        partial_pct = float(G(settings, "partial_close_pct",
                              zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        partial_pct = 0.0
    try:
        partial_trigger = float(G(settings, "partial_trigger_r",
                                  zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        partial_trigger = 0.0
    if (partial_pct > 0 and partial_trigger > 0 and not pos.partial_done):
        trigger = pos.entry_fill + sign * partial_trigger * risk
        touched = (h >= trigger) if pos.direction == "BUY" else (l <= trigger)
        if touched:
            if pos.direction == "BUY":
                exit_mid = o if o > trigger else trigger
            else:
                exit_mid = o if o < trigger else trigger
            slice_vol = round(pos.volume_remaining * partial_pct / 100.0, 2)
            if slice_vol >= pos.volume_remaining and pos.volume_remaining > 0:
                done = _close_slice(pos, pos.volume_remaining, exit_mid, day,
                                    "tp", costs, prior_rates)
                if done is not None:
                    trade, n, g, co = done
                    return [trade], True, n, g, co
            elif slice_vol > 0:
                done = _close_slice(pos, slice_vol, exit_mid, day, "partial",
                                    costs, prior_rates)
                if done is not None:
                    trade, n, g, co = done
                    out.append(trade)
                    d_net += n
                    d_gross += g
                    d_cost += co
                    pos.partial_done = True
            if pos.volume_remaining <= 0:
                return out, True, d_net, d_gross, d_cost

    # ---- breakeven / trailing (+ R-ladder floor) ---------------------------
    try:
        be_trigger = float(G(settings, "breakeven_trigger_r",
                             zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        be_trigger = 0.0
    try:
        trail_mult = float(G(settings, "trail_atr_mult",
                             zero_as_missing=False) or 0.0)
    except (TypeError, ValueError):
        trail_mult = 0.0
    if be_trigger > 0 and r_close >= be_trigger:
        snap = snapshot_for(pos.asset, day)
        atr_price = 0.0
        try:
            atr_pct = float((snap or {}).get("atr_pct") or 0.0)
        except (TypeError, ValueError):
            atr_pct = 0.0
        if atr_pct > 0 and math.isfinite(atr_pct):
            atr_price = atr_pct / 100.0 * c
        if not (atr_price > 0 and math.isfinite(atr_price)):
            try:
                proxy_mult = float(G(settings, "guard_atr_proxy_mult",
                                     zero_as_missing=False) or 0.0)
            except (TypeError, ValueError):
                proxy_mult = 0.0
            if not (proxy_mult > 0):
                proxy_mult = 0.2
            atr_price = risk * proxy_mult
        new_sl: float | None = None
        if trail_mult > 0:
            trail_price = c - sign * trail_mult * atr_price
            if pos.direction == "BUY":
                new_sl = max(pos.entry_fill, trail_price)
            else:
                new_sl = min(pos.entry_fill, trail_price)
            try:
                if bool(G(settings, "trailing_ladder")):
                    from app.engine.smart_exit import ladder_sl
                    ladder = ladder_sl(
                        entry_price=pos.entry_fill, direction=pos.direction,
                        r_distance=risk, r_multiple=r_close)
                    if ladder is not None:
                        if pos.direction == "BUY":
                            new_sl = max(new_sl, ladder)
                        else:
                            new_sl = min(new_sl, ladder)
            except Exception:
                pass
        else:
            new_sl = pos.entry_fill
        if new_sl is not None and math.isfinite(new_sl):
            improves = (new_sl > pos.current_sl) if sign == 1 else (
                new_sl < pos.current_sl)
            if improves and abs(new_sl - pos.current_sl) > 1e-9:
                pos.current_sl = round(new_sl, 5)

    # ---- smart exit (production evaluate_exit, bar-close decisions) --------
    if smart_on:
        snap = snapshot_for(pos.asset, day)
        if snap:
            try:
                age_days = float((day - pos.entry_date).days)
            except Exception:
                age_days = 0.0
            try:
                decision = evaluate_exit(
                    asset=pos.asset, direction=pos.direction,
                    entry_price=pos.entry_fill, price=c,
                    stop_loss=pos.current_sl, age_days=age_days,
                    settings=settings, snapshot=dict(snap),
                    news_status="SAFE", news_event="",
                    avg_hold_days=avg_hold, drawdown_pct=drawdown_pct)
            except Exception:
                decision = None
            if decision is not None:
                rec = str(getattr(decision, "recommendation", "HOLD") or "HOLD")
                trigger = str(getattr(decision, "trigger", "") or "")
                reason = f"smart_exit:{trigger}" if trigger else "smart_exit"
                if rec == "CLOSE":
                    done = _close_slice(pos, pos.volume_remaining, c, day,
                                        reason, costs, close_rates)
                    if done is not None:
                        trade, n, g, co = done
                        out.append(trade)
                        return out, True, d_net + n, d_gross + g, d_cost + co
                elif rec in ("PARTIAL_25", "PARTIAL_50"):
                    if not pos.partial_done:
                        pct = 25.0 if rec == "PARTIAL_25" else 50.0
                        slice_vol = round(pos.volume_remaining * pct / 100.0, 2)
                        if (slice_vol >= pos.volume_remaining
                                and pos.volume_remaining > 0):
                            done = _close_slice(pos, pos.volume_remaining, c,
                                                day, reason, costs, close_rates)
                            if done is not None:
                                trade, n, g, co = done
                                out.append(trade)
                                return (out, True, d_net + n,
                                        d_gross + g, d_cost + co)
                        elif slice_vol > 0:
                            done = _close_slice(pos, slice_vol, c, day, reason,
                                                costs, close_rates)
                            if done is not None:
                                trade, n, g, co = done
                                out.append(trade)
                                d_net += n
                                d_gross += g
                                d_cost += co
                                pos.partial_done = True
                # MOVE_SL belongs to the breakeven/trailing pass above.
            if pos.volume_remaining <= 0:
                return out, True, d_net, d_gross, d_cost

    # ---- time stop (original-stop R; locked-profit exemption) --------------
    try:
        max_hold = int(G(settings, "max_hold_days",
                         zero_as_missing=False) or 0)
    except (TypeError, ValueError):
        max_hold = 0
    if max_hold > 0:
        try:
            age_days = float((day - pos.entry_date).days)
        except Exception:
            age_days = 0.0
        if age_days >= max_hold:
            try:
                ts_min_r = float(G(settings, "time_stop_min_r",
                                   zero_as_missing=False) or 0.0)
            except (TypeError, ValueError):
                ts_min_r = 0.0
            r_now = sign * (c - pos.entry_fill) / risk
            locked = sign * (pos.current_sl - pos.entry_fill) / risk
            # Locked-profit exemption mirrors production
            # `workers/position_guard.py` (locked-profit check): a trailed SL
            # that already locks >= +0.25R means the downside is capped and
            # the position is a winner-pending, so the rigid time stop must
            # not cut it. The 0.25 threshold is hardcoded in production too —
            # documented there as sim evidence (expired trades averaged
            # +0.34R), not tuned — and is intentionally NOT
            # `profit_protect_r` (the separate smart-exit R-guard). The
            # boundary is inclusive (`>=`) on both sides.
            if locked >= 0.25:
                pass  # deferred — the trailed stop already locks profit
            elif ts_min_r <= 0 or r_now < ts_min_r:
                done = _close_slice(pos, pos.volume_remaining, c, day, "time",
                                    costs, close_rates)
                if done is not None:
                    trade, n, g, co = done
                    out.append(trade)
                    return out, True, d_net + n, d_gross + g, d_cost + co
    if out:
        return out, False, d_net, d_gross, d_cost
    return None


def _unrealized_total(
    opens: list[_OpenPosition],
    marks: dict[str, float],
    rates: dict,
    costs: CostConfig,
) -> float:
    """Marked net PnL of open positions (mid PnL minus accrued costs).

    Raises on a missing quote rate instead of booking raw quote-currency PnL.
    """
    try:
        exit_mult = float(costs.exit_spread_mult)
    except (TypeError, ValueError, AttributeError):
        exit_mult = 0.5
    if not math.isfinite(exit_mult) or exit_mult < 0:
        exit_mult = 0.5
    try:
        commission = float(costs.commission_per_lot_per_side)
    except (TypeError, ValueError, AttributeError):
        commission = 0.0
    if not math.isfinite(commission) or commission < 0:
        commission = 0.0
    snapshot = rates if isinstance(rates, dict) else {}
    total = 0.0
    for pos in opens:
        if pos.volume_remaining <= 0:
            continue
        mid = marks.get(pos.asset, pos.entry_mid)
        try:
            mid = float(mid)
        except (TypeError, ValueError):
            mid = pos.entry_mid
        if not math.isfinite(mid) or mid <= 0:
            mid = pos.entry_mid
        rate = _required_rate(pos.asset, mid, snapshot)
        contract = contract_value_for(pos.asset)
        sign = 1 if pos.direction == "BUY" else -1
        rem = pos.volume_remaining
        mid_pnl = sign * (mid - pos.entry_mid) * rem * contract * rate
        frac = rem / pos.volume_original if pos.volume_original > 0 else 0.0
        entry_costs = (pos.entry_spread_cost + pos.entry_comm_cost) * frac
        exit_costs = exit_mult * pos.full_spread * contract * rem * rate
        exit_costs += commission * rem
        total += mid_pnl - entry_costs - exit_costs
    return total


def _build_metrics(trades: list[ReplayTrade],
                   curve: list[EquityPoint]) -> ReplayMetrics:
    """Aggregate run metrics purely from closed slices and the equity curve.

    Every identity is by construction: ``net_pnl`` is the sum of slice net
    PnL, ``gross_pnl``/``costs`` are the sums of the per-slice gross/cost
    legs (each slice satisfies ``pnl = gross - costs``), and
    ``max_drawdown_pct`` is the peak of the curve drawdowns (whose running
    peak is seeded at starting capital in ``replay_portfolio``).
    ``profit_factor`` is total winning net PnL over abs(total losing net
    PnL), and is None when undefined (no trades or no losing PnL); no field
    is ever non-finite.
    """
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
        trades=len(trades), wins=wins,
        win_rate_pct=win_rate if math.isfinite(win_rate) else 0.0,
        profit_factor=profit_factor,
        average_r=average_r if math.isfinite(average_r) else 0.0,
        max_drawdown_pct=max_dd if math.isfinite(max_dd) else 0.0,
        ambiguous_pct=ambiguous_pct if math.isfinite(ambiguous_pct) else 0.0,
    )
