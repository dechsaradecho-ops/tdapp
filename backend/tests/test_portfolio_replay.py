"""Task 1: replay models and shared timeline (TDD)."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.engine.portfolio_replay import (
    CostConfig,
    EquityPoint,
    ReplayMetrics,
    ReplayResult,
    ReplaySignal,
    ReplayTrade,
    build_rate_map,
    build_timeline,
    default_cost_config,
    entry_fill,
    replay_portfolio,
    signal_at,
    size_for_replay,
)
from app.integrations.quotes import Candle
from app.models.schemas import (
    AppSettings,
    DEFAULT_SPREADS,
    effective_sl_tp,
    effective_spread,
    risk_usd_of_distance,
)


def _candle(day: int, close: float = 100.0) -> Candle:
    ts = int(datetime(2026, 1, day, tzinfo=timezone.utc).timestamp())
    return Candle(o=close, h=close + 1, l=close - 1, c=close, t=ts)


def test_timeline_merges_assets_without_future_bars():
    c1 = _candle(1)
    c3 = _candle(3)
    c2 = _candle(2)
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    timeline = build_timeline({"EURUSD": [c1, c3], "USDJPY": [c2]})
    assert [d for d, _ in timeline] == sorted([d1, d2, d3])
    assert set(timeline[0][1]) <= {"EURUSD", "USDJPY"}


def test_replay_result_starts_at_user_capital():
    flat_series = {"EURUSD": [_candle(1), _candle(2), _candle(3)]}
    result = replay_portfolio(flat_series, AppSettings(), starting_capital=1000)
    assert result.equity_curve[0].equity == 1000


def test_timeline_rejects_unsorted_bars():
    with pytest.raises(ValueError):
        build_timeline({"EURUSD": [_candle(2), _candle(1)]})


def test_timeline_rejects_duplicate_dates():
    with pytest.raises(ValueError):
        build_timeline({"EURUSD": [_candle(1), _candle(1)]})


def test_timeline_does_not_invent_missing_bars():
    timeline = build_timeline({"EURUSD": [_candle(1)], "USDJPY": [_candle(2)]})
    assert set(timeline[0][1]) == {"EURUSD"}
    assert set(timeline[1][1]) == {"USDJPY"}


def test_flat_replay_has_no_trades_zero_metrics_and_news_warning():
    flat_series = {"EURUSD": [_candle(1), _candle(2)]}
    result = replay_portfolio(flat_series, AppSettings(), starting_capital=1000)
    assert result.trades == []
    assert result.metrics.net_pnl == 0
    assert result.metrics.trades == 0
    assert result.metrics.profit_factor == 0
    assert result.warnings.count(
        "News-optimistic: historical news replay is unavailable."
    ) == 1


def test_empty_series_returns_empty_equity_curve_with_warning():
    result = replay_portfolio({}, AppSettings(), starting_capital=1000)
    assert result.equity_curve == []
    assert (
        "News-optimistic: historical news replay is unavailable." in result.warnings
    )


def test_caller_warnings_are_copied_and_deduplicated():
    caller = ["custom", "News-optimistic: historical news replay is unavailable."]
    flat_series = {"EURUSD": [_candle(1)]}
    result = replay_portfolio(
        flat_series, AppSettings(), starting_capital=1000, warnings=caller
    )
    assert caller == ["custom", "News-optimistic: historical news replay is unavailable."]
    assert result.warnings.count(
        "News-optimistic: historical news replay is unavailable."
    ) == 1
    assert "custom" in result.warnings


# ---------------------------------------------------------------------------
# Task 2: production-parity signal generation (TDD — RED before signal_at exists)
# ---------------------------------------------------------------------------

_SIGNAL_BASE_TS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
_DAY = 86400


def _signal_candles(closes: list[float]) -> list[Candle]:
    """Daily candles with real positive UTC timestamps (one bar per day)."""
    out: list[Candle] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        rng = abs(c - o) * 0.5 + abs(c) * 0.0008
        out.append(
            Candle(
                o=round(o, 5),
                h=round(c + rng, 5),
                l=round(c - rng, 5),
                c=round(c, 5),
                t=_SIGNAL_BASE_TS + i * _DAY,
            )
        )
    return out


def _fx_trend(n: int = 220, start: float = 1.0800, step: float = 0.0008) -> list[Candle]:
    closes = [round(start + step * i, 5) for i in range(n)]
    out: list[Candle] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        pad = abs(step) * 0.6 + 0.0002
        out.append(
            Candle(
                o=round(o, 5),
                h=round(c + pad, 5),
                l=round(c - pad, 5),
                c=c,
                t=_SIGNAL_BASE_TS + i * _DAY,
            )
        )
    return out


def _gold_history(kind: str) -> list[Candle]:
    if kind == "sell":
        return _signal_candles([round(2600.0 - 2.0 * i, 2) for i in range(230)])
    if kind == "bullish_no_breakout":
        prices = [2400.0 + 2.0 * i for i in range(220)]
        p = prices[-1]
        for _ in range(8):
            p -= 1.0
            prices.append(p)
        return _signal_candles([round(v, 2) for v in prices])
    if kind == "buy_breakout":
        return _signal_candles([round(2400.0 + 2.0 * i, 2) for i in range(230)])
    raise ValueError(f"unknown gold history kind: {kind}")


def _day_of(history: list[Candle], index: int) -> date:
    return datetime.fromtimestamp(history[index].t, tz=timezone.utc).date()


def test_signal_uses_production_indicators_and_candidate_gates():
    settings = AppSettings(min_confidence=70, min_opportunity=60)
    history = _fx_trend()
    sig = signal_at("EURUSD", history, settings, _day_of(history, len(history) - 1))
    assert sig is not None
    assert sig.confidence >= 70 and sig.opportunity >= 60
    assert sig.entry == history[-1].c


def test_signal_gold_buy_requires_breakout_but_sell_does_not():
    settings = AppSettings(gold_breakout_only=True, gold_max_atr_pct=3.5)
    bearish = _gold_history("sell")
    sig_sell = signal_at("XAUUSD", bearish, settings, _day_of(bearish, len(bearish) - 1))
    assert sig_sell is not None
    assert sig_sell.direction == "SELL"
    bullish = _gold_history("bullish_no_breakout")
    sig_buy = signal_at("XAUUSD", bullish, settings, _day_of(bullish, len(bullish) - 1))
    assert sig_buy is None


def test_signal_never_reads_future_candles():
    settings = AppSettings(min_confidence=70, min_opportunity=60)
    history = _fx_trend(n=230)
    t_index = 209
    date_t = _day_of(history, t_index)
    through_t = history[: t_index + 1]
    extreme = Candle(
        o=through_t[-1].c,
        h=through_t[-1].c * 3.0,
        l=through_t[-1].c,
        c=through_t[-1].c * 3.0,
        t=history[t_index + 1].t,
    )
    with_future = through_t + [extreme]
    sig_t = signal_at("EURUSD", through_t, settings, date_t)
    sig_future = signal_at("EURUSD", with_future, settings, date_t)
    assert sig_t is not None
    assert sig_t == sig_future


def test_signal_candidate_gates_change_trade_eligibility():
    history = _fx_trend()
    day = _day_of(history, len(history) - 1)
    base = AppSettings(min_confidence=70, min_opportunity=60)
    sig = signal_at("EURUSD", history, base, day)
    assert sig is not None
    # Raising either gate above the signal's own scores blocks the trade.
    assert signal_at("EURUSD", history, AppSettings(min_confidence=70, min_opportunity=99), day) is None
    assert signal_at("EURUSD", history, AppSettings(min_confidence=99, min_opportunity=60), day) is None


def test_signal_gold_atr_zero_blocks_every_entry():
    settings = AppSettings(gold_breakout_only=True, gold_max_atr_pct=0)
    for kind in ("sell", "buy_breakout"):
        history = _gold_history(kind)
        day = _day_of(history, len(history) - 1)
        assert signal_at("XAUUSD", history, settings, day) is None


def test_signal_returns_none_without_enough_history_or_bar_on_date():
    settings = AppSettings(min_confidence=70, min_opportunity=60)
    short = _fx_trend(n=50)
    assert signal_at("EURUSD", short, settings, _day_of(short, len(short) - 1)) is None
    full = _fx_trend()
    assert signal_at("EURUSD", full, settings, date(2025, 12, 31)) is None
    assert signal_at("EURUSD", full, settings, date(2027, 6, 1)) is None


def test_signal_snapshot_is_deterministic_plain_data():
    settings = AppSettings(min_confidence=70, min_opportunity=60)
    history = _fx_trend()
    day = _day_of(history, len(history) - 1)
    sig = signal_at("EURUSD", history, settings, day)
    assert sig is not None
    assert isinstance(sig, ReplaySignal)
    assert isinstance(sig.snapshot, dict)
    assert "atr_pct" in sig.snapshot and "breakout_state" in sig.snapshot
    for value in sig.snapshot.values():
        assert isinstance(value, (str, int, float, bool)) or value is None
    assert signal_at("EURUSD", history, settings, day) == sig


# ---------------------------------------------------------------------------
# Task 3: sizing, conversion, entry, and costs (TDD — RED before they exist)
# ---------------------------------------------------------------------------

def _replay_signal(
    asset: str = "GBPJPY",
    entry: float = 200.0,
    stop_loss: float = 201.5,
    day: date = date(2026, 1, 5),
    direction: str = "SELL",
) -> ReplaySignal:
    return ReplaySignal(
        asset=asset,
        date=day,
        direction=direction,
        entry=entry,
        stop_loss=stop_loss,
        take_profit=entry,
        opportunity=0.0,
        confidence=0.0,
        snapshot={},
    )


def test_gbpjpy_risk_and_pnl_are_usd():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal("GBPJPY", entry=200.0, stop_loss=201.5)
    lots = size_for_replay(
        settings, signal, equity=1000, rates={"USDJPY": 160.0},
        date=date(2026, 1, 5),
    )
    assert lots > 0
    assert risk_usd_of_distance(
        1.5, lots, "GBPJPY", 200.0, rates={"USDJPY": 160.0}
    ) <= 10.01


def test_entry_uses_next_open_and_deducts_cost_once():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    costs = default_cost_config(settings)
    signal = _replay_signal("GBPJPY", entry=200.0, stop_loss=201.5)
    lots = size_for_replay(
        settings, signal, equity=1000, rates={"USDJPY": 160.0},
        date=date(2026, 1, 5),
    )
    assert lots > 0
    spread = costs.spreads["GBPJPY"]
    commission = costs.commission_per_lot_per_side
    fill, cost = entry_fill(
        signal, next_bar_open=201.0, direction="BUY", costs=costs, volume=lots
    )
    assert fill == pytest.approx(201.0 + spread / 2)
    assert cost == pytest.approx(commission * lots)


def test_sizing_direct_usd_quote_needs_no_rates():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    lots = size_for_replay(
        settings, signal, equity=1000, rates={}, date=date(2026, 1, 5)
    )
    assert lots == pytest.approx(0.01)
    assert risk_usd_of_distance(0.01, lots, "EURUSD", 1.10, rates={}) <= 10.01


def test_sizing_usd_base_uses_inverse_conversion():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal(
        "USDJPY", entry=160.0, stop_loss=158.5, direction="SELL"
    )
    lots = size_for_replay(
        settings, signal, equity=1000, rates={}, date=date(2026, 1, 5)
    )
    assert lots == pytest.approx(0.01)
    # Without the 1/price conversion the raw quote figure would read ~1500.
    assert risk_usd_of_distance(
        1.5, lots, "USDJPY", 160.0, rates={}
    ) <= 10.01


def test_sizing_cross_without_usd_leg_fails_closed():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal("GBPJPY", entry=200.0, stop_loss=201.5)
    assert size_for_replay(
        settings, signal, equity=1000, rates={}, date=date(2026, 1, 5)
    ) == 0.0


def test_sizing_never_selects_a_future_rate():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal("GBPJPY", entry=200.0, stop_loss=201.5)
    by_date = {date(2026, 1, 10): {"USDJPY": 160.0}}
    # Querying before any known rate: the cross cannot convert → 0.0,
    # even though a future snapshot could price it.
    assert size_for_replay(
        settings, signal, equity=1000, rates=by_date, date=date(2026, 1, 5)
    ) == 0.0
    # Exact date and carry-forward from the latest prior date both resolve.
    assert size_for_replay(
        settings, signal, equity=1000, rates=by_date, date=date(2026, 1, 10)
    ) > 0
    assert size_for_replay(
        settings, signal, equity=1000, rates=by_date, date=date(2026, 1, 20)
    ) > 0


def test_sizing_min_lot_breach_returns_zero():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0000)
    assert size_for_replay(
        settings, signal, equity=1000, rates={}, date=date(2026, 1, 5)
    ) == 0.0


def test_sizing_uses_simulated_equity_not_settings_capital():
    rich = AppSettings(capital=1_000_000, risk_per_trade_pct=1)
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    lots = size_for_replay(
        rich, signal, equity=1000, rates={}, date=date(2026, 1, 5)
    )
    assert lots == pytest.approx(0.01)


def test_sizing_rejects_invalid_inputs():
    settings = AppSettings(capital=1000, risk_per_trade_pct=1)
    good = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    assert size_for_replay(
        settings, good, equity=0, rates={}, date=date(2026, 1, 5)
    ) == 0.0
    assert size_for_replay(
        settings, good, equity=float("nan"), rates={}, date=date(2026, 1, 5)
    ) == 0.0
    zero_dist = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.1000)
    assert size_for_replay(
        settings, zero_dist, equity=1000, rates={}, date=date(2026, 1, 5)
    ) == 0.0


def test_sell_entry_fills_below_next_open_and_charges_once():
    costs = CostConfig(
        spreads={"EURUSD": 0.0002},
        commission_per_lot_per_side=3.5,
        exit_spread_mult=0.5,
    )
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    fill, cost = entry_fill(
        signal, next_bar_open=1.1000, direction="SELL", costs=costs, volume=0.01
    )
    assert fill == pytest.approx(1.1000 - 0.0001)
    assert cost == pytest.approx(3.5 * 0.01)


def test_entry_fill_rejects_bad_inputs():
    costs = default_cost_config(AppSettings())
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    with pytest.raises(ValueError):
        entry_fill(signal, next_bar_open=1.1, direction="HOLD",
                   costs=costs, volume=0.01)
    with pytest.raises(ValueError):
        entry_fill(signal, next_bar_open=1.1, direction="BUY",
                   costs=costs, volume=-0.01)
    with pytest.raises(ValueError):
        entry_fill(signal, next_bar_open=float("nan"), direction="BUY",
                   costs=costs, volume=0.01)


def test_entry_fill_never_uses_signal_close_as_fill():
    costs = CostConfig(
        spreads={"EURUSD": 0.0002},
        commission_per_lot_per_side=3.5,
        exit_spread_mult=0.5,
    )
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    fill, _ = entry_fill(
        signal, next_bar_open=1.1050, direction="BUY", costs=costs, volume=0.01
    )
    assert fill == pytest.approx(1.1050 + 0.0001)
    assert fill != pytest.approx(1.1000 + 0.0001)


def test_static_spread_honors_overrides_then_defaults():
    settings = default_cost_config(
        AppSettings(spread_overrides={"EURUSD": 0.0002})
    )
    assert settings.spreads["EURUSD"] == pytest.approx(0.0002)
    assert settings.spreads["GBPJPY"] == pytest.approx(DEFAULT_SPREADS["GBPJPY"])
    assert settings.commission_per_lot_per_side == pytest.approx(3.5)
    assert settings.exit_spread_mult == pytest.approx(0.5)


def test_static_spread_matches_production_without_live_gold():
    settings = AppSettings()
    costs = default_cost_config(settings)
    for asset in ("EURUSD", "GBPJPY", "USDJPY", "XAUUSD"):
        assert costs.spreads[asset] == pytest.approx(
            effective_spread(settings, asset)
        )


def test_default_cost_config_keeps_explicit_zero_commission():
    costs = default_cost_config(AppSettings(paper_commission_per_lot=0))
    assert costs.commission_per_lot_per_side == 0.0


def test_unknown_asset_entry_uses_global_default_spread():
    costs = CostConfig(
        spreads={"EURUSD": 0.0002},
        commission_per_lot_per_side=3.5,
        exit_spread_mult=0.5,
        default_spread=0.0004,
    )
    signal = _replay_signal("USDSEK", entry=10.0000, stop_loss=9.9000)
    fill, cost = entry_fill(
        signal, next_bar_open=10.0000, direction="BUY", costs=costs, volume=0.01
    )
    assert fill == pytest.approx(10.0000 + 0.0004 / 2)
    assert cost == pytest.approx(3.5 * 0.01)


def test_per_asset_spread_wins_over_global_default():
    costs = CostConfig(
        spreads={"EURUSD": 0.0002},
        commission_per_lot_per_side=3.5,
        exit_spread_mult=0.5,
        default_spread=0.0004,
    )
    signal = _replay_signal("EURUSD", entry=1.1000, stop_loss=1.0900)
    fill, _ = entry_fill(
        signal, next_bar_open=1.1000, direction="BUY", costs=costs, volume=0.01
    )
    assert fill == pytest.approx(1.1000 + 0.0002 / 2)


def test_default_cost_config_carries_legacy_global_spread():
    costs = default_cost_config(AppSettings(paper_spread=0.0005))
    assert costs.default_spread == pytest.approx(0.0005)
    signal = _replay_signal("USDSEK", entry=10.0000, stop_loss=9.9000)
    fill, _ = entry_fill(
        signal, next_bar_open=10.0000, direction="SELL", costs=costs, volume=0.01
    )
    assert fill == pytest.approx(10.0000 - 0.0005 / 2)


def test_default_cost_config_sanitizes_bad_global_spread():
    assert default_cost_config(AppSettings()).default_spread == 0.0
    assert default_cost_config(AppSettings(paper_spread=-0.1)).default_spread == 0.0


def test_entry_fill_rejects_bad_fallback_spread():
    signal = _replay_signal("USDSEK", entry=10.0000, stop_loss=9.9000)
    for bad in (-0.0001, float("nan"), float("inf")):
        costs = CostConfig(
            spreads={},
            commission_per_lot_per_side=3.5,
            exit_spread_mult=0.5,
            default_spread=bad,
        )
        with pytest.raises(ValueError):
            entry_fill(signal, next_bar_open=10.0, direction="BUY",
                       costs=costs, volume=0.01)


def test_build_rate_map_carries_forward_without_future_leakage():
    d1, d2, d3 = date(2026, 1, 1), date(2026, 1, 2), date(2026, 1, 3)
    eurusd = [_candle(1, close=1.10), _candle(3, close=1.12)]
    usdjpy = [_candle(2, close=160.0), _candle(3, close=161.0)]
    rate_map = build_rate_map({"EURUSD": eurusd, "USDJPY": usdjpy})
    assert rate_map[d1] == {"EURUSD": pytest.approx(1.10)}
    assert rate_map[d2] == {
        "EURUSD": pytest.approx(1.10),
        "USDJPY": pytest.approx(160.0),
    }
    assert rate_map[d3]["EURUSD"] == pytest.approx(1.12)
    assert rate_map[d3]["USDJPY"] == pytest.approx(161.0)


def test_build_rate_map_omits_bad_closes():
    bad = Candle(o=1.0, h=1.0, l=1.0, c=float("nan"),
                 t=int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()))
    rate_map = build_rate_map({"EURUSD": [bad, _candle(2, close=1.10)]})
    assert rate_map[date(2026, 1, 1)] == {}
    assert rate_map[date(2026, 1, 2)] == {"EURUSD": pytest.approx(1.10)}


# ---------------------------------------------------------------------------
# Task 4: portfolio constraints and exit stack (TDD — RED before the loop exists)
# ---------------------------------------------------------------------------

_T4_BASE_TS = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
_T4_DAY = 86400


def _t4_ts(day_index: int) -> int:
    return _T4_BASE_TS + day_index * _T4_DAY


def _t4_date(day_index: int) -> date:
    return datetime.fromtimestamp(_t4_ts(day_index), tz=timezone.utc).date()


def _t4_bars(closes: list[float], first_index: int = 0, pad: float = 0.0002) -> list[Candle]:
    out: list[Candle] = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i else c
        out.append(Candle(o=round(o, 5), h=round(max(o, c) + pad, 5),
                          l=round(min(o, c) - pad, 5), c=round(c, 5),
                          t=_t4_ts(first_index + i)))
    return out


def _t4_trend(n: int = 220, start: float = 1.0800, step: float = 0.0008,
              first_index: int = 0) -> list[Candle]:
    closes = [round(start + step * i, 5) for i in range(n)]
    return _t4_bars(closes, first_index=first_index,
                    pad=abs(step) * 0.6 + 0.0002)


def _t4_signal_levels(asset: str, history: list[Candle], settings: AppSettings,
                      day_index: int):
    sig = signal_at(asset, history, settings, _t4_date(day_index))
    assert sig is not None
    return sig


def _t4_open_spans(trades):
    spans = []
    for t in trades:
        spans.append((t.asset, t.entry_date, t.exit_date))
    return spans


def _t4_max_concurrent(trades) -> int:
    events = []
    for t in trades:
        events.append((t.entry_date, 1))
        events.append((t.exit_date, -1))
    # Exits settle before entries on the same shared date (entries run first
    # but a closing position still counts for that date's cap check).
    events.sort(key=lambda e: (e[0], e[1]))
    cur = best = 0
    for _, delta in events:
        if delta > 0:
            cur += delta
            best = max(best, cur)
        else:
            cur += delta
    return best


def test_max_open_positions_is_enforced_on_shared_timeline():
    settings = AppSettings(max_open_positions=1)
    series = {"EURUSD": _t4_trend(), "GBPUSD": _t4_trend(start=1.2700)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    assert result.trades, "expected chained entries on the shared timeline"
    assert _t4_max_concurrent(result.trades) <= 1
    assert {t.asset for t in result.trades} == {"EURUSD"}


def test_ambiguous_bar_is_stop_first_and_counted():
    settings = AppSettings()
    history = _t4_trend()
    sig = _t4_signal_levels("EURUSD", history, settings, 199)
    entry_mid = history[200].o if len(history) > 200 else sig.entry
    sl, tp = sig.stop_loss, sig.take_profit
    tail = [
        history[200],
        Candle(o=round(entry_mid, 5), h=round(tp + 0.005, 5),
               l=round(sl - 0.005, 5), c=round(entry_mid + 0.001, 5),
               t=_t4_ts(201)),
    ]
    series = {"EURUSD": history[:200] + tail + _t4_bars([tail[-1].c] * 4, first_index=202)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    assert result.trades[0].exit_reason == "sl"
    assert result.trades[0].ambiguous is True
    assert result.metrics.ambiguous_pct > 0


def test_partial_close_deducts_costs_once_per_closed_slice():
    settings = AppSettings(partial_close_pct=50, partial_trigger_r=1.0,
                           smart_exit_enabled=False, max_hold_days=0,
                           rr_target=10.0)
    closes = [round(1.0800 + 0.0008 * i, 5) for i in range(200)]
    closes += [round(closes[-1] + 0.0015 * i, 5) for i in range(1, 5)]
    closes += [closes[-1]] * 6
    result = replay_portfolio({"EURUSD": _t4_bars(closes)}, settings,
                              starting_capital=1000)
    assert len(result.trades) == 2
    assert result.trades[0].exit_reason == "partial"
    assert result.trades[1].exit_reason == "end_of_data"
    spread = DEFAULT_SPREADS["EURUSD"]
    total_vol = sum(t.volume for t in result.trades)
    expected = (0.5 * spread * 100000.0 * total_vol
                + 3.5 * total_vol
                + 0.5 * spread * 100000.0 * total_vol
                + 3.5 * total_vol)
    assert result.metrics.costs == pytest.approx(expected)
    assert result.metrics.gross_pnl - result.metrics.costs == pytest.approx(
        result.metrics.net_pnl)
    assert sum(t.pnl_usd for t in result.trades) == pytest.approx(
        result.metrics.net_pnl)


def test_next_open_entry_uses_next_bar_open_plus_half_spread():
    settings = AppSettings()
    history = _t4_trend(n=225)
    result = replay_portfolio({"EURUSD": history}, settings, starting_capital=1000)
    assert result.trades
    first = result.trades[0]
    # First signal needs 200 bars (index 199); entry is the next bar open.
    assert first.entry_date == _t4_date(200)
    spread = DEFAULT_SPREADS["EURUSD"]
    assert first.entry_price == pytest.approx(history[200].o + spread / 2)


def test_duplicate_asset_position_is_blocked_while_open():
    settings = AppSettings()
    result = replay_portfolio({"EURUSD": _t4_trend(n=260)}, settings,
                              starting_capital=1000)
    assert len(result.trades) >= 2
    eurusd = sorted((t.entry_date, t.exit_date) for t in result.trades
                    if t.asset == "EURUSD")
    for (e1, x1), (e2, x2) in zip(eurusd, eurusd[1:]):
        assert not (e2 < x1 and e1 < x2), "same-asset positions must not overlap"


def test_daily_entry_limit_is_enforced():
    settings = AppSettings(max_trades_daily=1, max_open_positions=12)
    series = {"EURUSD": _t4_trend(n=230), "GBPUSD": _t4_trend(n=230, start=1.2700)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    first_day = min(t.entry_date for t in result.trades)
    assert sum(1 for t in result.trades if t.entry_date == first_day) == 1


def test_weekly_entry_limit_is_enforced():
    settings = AppSettings(max_trades_weekly=1, max_open_positions=12)
    series = {"EURUSD": _t4_trend(n=240), "GBPUSD": _t4_trend(n=240, start=1.2700)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    first = min(t.entry_date for t in result.trades)
    iso = first.isocalendar()[:2]
    assert sum(1 for t in result.trades
               if t.entry_date.isocalendar()[:2] == iso) == 1


def test_cooldown_blocks_reentry_after_close():
    base = _t4_trend()
    sig = _t4_signal_levels("EURUSD", base, AppSettings(), 199)
    entry_mid = base[200].o
    sl, tp = sig.stop_loss, sig.take_profit
    crash = [round(entry_mid - 0.002 * i, 5) for i in range(1, 4)]
    recovery = [round(crash[-1] + 0.0008 * i, 5) for i in range(1, 21)]
    series = {"EURUSD": base[:201] + _t4_bars(crash, first_index=201)
              + _t4_bars(recovery, first_index=204)}
    plain = replay_portfolio(series, AppSettings(), starting_capital=1000)
    cooled = replay_portfolio(series, AppSettings(reentry_cooldown_min=30 * 1440),
                              starting_capital=1000)
    assert len(plain.trades) >= 2
    assert len(cooled.trades) == 1


def test_allowed_assets_restricts_universe():
    settings = AppSettings(allowed_assets=["EURUSD"])
    series = {"EURUSD": _t4_trend(n=230), "GBPUSD": _t4_trend(n=230, start=1.2700)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    assert result.trades
    assert {t.asset for t in result.trades} == {"EURUSD"}


def test_correlation_cap_rejects_second_forex_leg():
    series = {"EURUSD": _t4_trend(n=235), "GBPUSD": _t4_trend(n=235, start=1.2700)}
    capped = replay_portfolio(series, AppSettings(correlation_cap=50.0),
                              starting_capital=1000)
    assert capped.trades
    assert _t4_max_concurrent(capped.trades) <= 1
    control = replay_portfolio(series, AppSettings(), starting_capital=1000)
    assert _t4_max_concurrent(control.trades) == 2


def test_currency_exposure_cap_blocks_over_concentration():
    series = {"EURUSD": _t4_trend(n=230)}
    blocked = replay_portfolio(
        series, AppSettings(max_currency_exposure_pct=1.0),
        starting_capital=1000)
    assert blocked.trades == []
    control = replay_portfolio(series, AppSettings(), starting_capital=1000)
    assert control.trades


def test_spread_guard_blocks_tight_stop():
    series = {"EURUSD": _t4_trend(n=230)}
    blocked = replay_portfolio(series, AppSettings(spread_guard_max_pct=1.0),
                               starting_capital=1000)
    assert blocked.trades == []
    control = replay_portfolio(series, AppSettings(), starting_capital=1000)
    assert control.trades


def test_sl_cap_tightens_stop_at_execution():
    settings = AppSettings(risk_per_trade_pct=0.1)
    history = _t4_trend(n=206)
    sig = _t4_signal_levels("EURUSD", history, settings, 199)
    entry_mid = history[200].o
    dip = round(entry_mid - 0.0015, 5)
    bars = history[:201] + [
        Candle(o=round(entry_mid + 0.0002, 5), h=round(entry_mid + 0.0004, 5),
               l=dip, c=dip, t=_t4_ts(202)),
    ]
    bars += _t4_bars([dip + 0.0008 * i for i in range(1, 7)], first_index=203)
    result = replay_portfolio({"EURUSD": bars}, settings, starting_capital=1000)
    assert result.trades[0].exit_reason == "sl"
    # Risk budget $1 at the 0.01 floor caps the stop 0.001 below entry; the
    # signal stop (~0.0023 away) would have survived this dip.
    assert result.trades[0].entry_price - result.trades[0].exit_price == pytest.approx(
        0.001 + DEFAULT_SPREADS["EURUSD"], abs=2e-4)
    control = replay_portfolio({"EURUSD": bars}, AppSettings(), starting_capital=1000)
    assert all(t.exit_date != _t4_date(202) or t.exit_reason != "sl"
               for t in control.trades if t.entry_date == _t4_date(201))


def test_gap_down_through_sl_fills_at_open():
    settings = AppSettings(smart_exit_enabled=False)
    history = _t4_trend(n=206)
    sig = _t4_signal_levels("EURUSD", history, settings, 199)
    entry_mid = history[200].o
    gap_open = round(sig.stop_loss - 0.001, 5)
    bars = history[:201] + [
        Candle(o=gap_open, h=round(gap_open + 0.0005, 5),
               l=round(gap_open - 0.0005, 5), c=gap_open, t=_t4_ts(202)),
    ]
    bars += _t4_bars([gap_open] * 4, first_index=203)
    result = replay_portfolio({"EURUSD": bars}, settings, starting_capital=1000)
    assert result.trades[0].exit_reason == "sl"
    assert result.trades[0].exit_date == _t4_date(202)
    assert result.trades[0].ambiguous is False
    assert result.trades[0].exit_price == pytest.approx(
        gap_open - 0.5 * DEFAULT_SPREADS["EURUSD"])


def test_gap_up_through_tp_fills_at_open():
    settings = AppSettings(smart_exit_enabled=False)
    history = _t4_trend(n=206)
    sig = _t4_signal_levels("EURUSD", history, settings, 199)
    entry_mid = history[200].o
    gap_open = round(sig.take_profit + 0.001, 5)
    bars = history[:201] + [
        Candle(o=gap_open, h=round(gap_open + 0.0005, 5),
               l=round(gap_open - 0.0005, 5), c=gap_open, t=_t4_ts(202)),
    ]
    bars += _t4_bars([gap_open] * 4, first_index=203)
    result = replay_portfolio({"EURUSD": bars}, settings, starting_capital=1000)
    assert result.trades[0].exit_reason == "tp"
    assert result.trades[0].exit_date == _t4_date(202)
    assert result.trades[0].exit_price == pytest.approx(
        gap_open - 0.5 * DEFAULT_SPREADS["EURUSD"])


def _t4_crash_series():
    history = _t4_trend(n=201)
    entry_mid = history[200].o if len(history) > 200 else history[-1].o
    rise = [round(history[-1].c + 0.0015 * i, 5) for i in range(1, 5)]
    top = rise[-1]
    crash_low = round(entry_mid - 0.008, 5)
    bars = history + _t4_bars(rise, first_index=201)
    bars.append(Candle(o=top, h=round(top + 0.0002, 5), l=crash_low,
                       c=round(entry_mid - 0.006, 5), t=_t4_ts(205)))
    bars += _t4_bars([bars[-1].c] * 2, first_index=206)
    return bars


def test_breakeven_and_trailing_lock_profit_vs_control():
    bars = _t4_crash_series()
    armed = AppSettings(smart_exit_enabled=False, partial_close_pct=0,
                        max_hold_days=0, rr_target=10.0)
    naked = AppSettings(smart_exit_enabled=False, partial_close_pct=0,
                        max_hold_days=0, rr_target=10.0,
                        breakeven_trigger_r=0, trail_atr_mult=0)
    with_guard = replay_portfolio({"EURUSD": bars}, armed, starting_capital=1000)
    without_guard = replay_portfolio({"EURUSD": bars}, naked, starting_capital=1000)
    assert with_guard.trades[0].exit_reason == "sl"
    assert without_guard.trades[0].exit_reason == "sl"
    assert with_guard.trades[0].r_multiple > 0.3
    assert without_guard.trades[0].r_multiple < -0.8


def test_hard_stop_has_precedence_over_management():
    settings = AppSettings(partial_close_pct=50, partial_trigger_r=1.0)
    history = _t4_trend()
    sig = _t4_signal_levels("EURUSD", history, settings, 199)
    entry_mid = history[200].o
    sl, tp = sig.stop_loss, sig.take_profit
    monster = Candle(o=round(entry_mid, 5), h=round(tp + 0.005, 5),
                     l=round(sl - 0.005, 5), c=round(entry_mid + 0.001, 5),
                     t=_t4_ts(201))
    series = {"EURUSD": history[:201] + [monster]
              + _t4_bars([monster.c] * 4, first_index=202)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    monster_trades = [t for t in result.trades if t.exit_date == _t4_date(201)]
    assert len(monster_trades) == 1
    assert monster_trades[0].exit_reason == "sl"
    assert monster_trades[0].ambiguous is True
    assert all(t.exit_reason != "partial" for t in result.trades)


def _t4_volatile_downtrend(n: int = 220, seed: int = 7):
    import random
    rng = random.Random(seed)
    closes, c = [], 1.2000
    for _ in range(n):
        o = c
        c = round(c - 0.0012 + rng.uniform(-0.006, 0.006), 5)
        closes.append((o, c))
    out = []
    for i, (o, c) in enumerate(closes):
        out.append(Candle(o=round(o, 5), h=round(max(o, c) * 1.0018, 5),
                          l=round(min(o, c) * 0.9982, 5), c=c,
                          t=_t4_ts(i)))
    return out


def _t4_sell_rally_series(flat_n: int = 4):
    base = _t4_volatile_downtrend()
    last = base[-1].c
    rally = [round(last + 0.0015 * i, 5) for i in range(1, 7)]
    flat = [rally[-1]] * flat_n
    return base + _t4_bars(rally, first_index=220) + _t4_bars(flat, first_index=226)


def _t4_post_rally_trades(result):
    post = [t for t in result.trades if t.entry_date >= _t4_date(220)]
    assert post, "expected entries once the rally phase starts"
    first_entry = min(t.entry_date for t in post)
    return sorted((t for t in post if t.entry_date == first_entry),
                  key=lambda t: t.exit_date)


def test_smart_exit_close_on_reversal():
    settings = AppSettings(reversal_opp_min=55)
    result = replay_portfolio({"EURUSD": _t4_sell_rally_series()}, settings,
                              starting_capital=1000)
    assert _t4_post_rally_trades(result)[0].exit_reason == "smart_exit:reversal"


def test_smart_exit_partial_on_volatility():
    settings = AppSettings(reversal_opp_min=20, volatility_exit_atr=0.5,
                           risk_per_trade_pct=4.0, max_hold_days=30)
    result = replay_portfolio({"EURUSD": _t4_sell_rally_series(flat_n=12)},
                              settings, starting_capital=1000)
    by_entry: dict = {}
    for t in result.trades:
        by_entry.setdefault((t.asset, t.entry_date), []).append(t)
    scaled = [sorted(v, key=lambda t: t.exit_date) for v in by_entry.values()
              if len(v) == 2 and v[0].exit_reason == "smart_exit:volatility"]
    assert scaled, "expected a volatility partial followed by its remainder"
    assert scaled[0][0].volume == pytest.approx(0.01)
    assert scaled[0][1].exit_reason in ("smart_exit:left_behind", "sl", "tp",
                                        "time", "end_of_data")
    assert any(pair[1].exit_reason == "smart_exit:left_behind"
               for pair in scaled)


def test_smart_exit_close_on_exit_score():
    settings = AppSettings(reversal_opp_min=20, exit_score_close=70.0)
    result = replay_portfolio({"EURUSD": _t4_sell_rally_series()}, settings,
                              starting_capital=1000)
    assert _t4_post_rally_trades(result)[0].exit_reason == "smart_exit:exit_score"


def test_smart_exit_close_on_left_behind():
    settings = AppSettings(no_behind_hold_mult=0.5, no_behind_min_days=0.0,
                           no_behind_min_r=5.0, max_hold_days=10)
    history = _t4_trend(n=220)
    flat = [history[-1].c] * 8
    series = {"EURUSD": history + _t4_bars(flat, first_index=220)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    assert result.trades[0].exit_reason == "smart_exit:left_behind"


def test_time_stop_closes_aged_position():
    settings = AppSettings(max_hold_days=4)
    closes = [round(1.0800 + 0.0008 * i, 5) for i in range(220)]
    closes += [round(closes[-1] + 0.0005 * i, 5) for i in range(1, 4)]
    closes += [closes[-1]] * 10
    result = replay_portfolio({"EURUSD": _t4_bars(closes)}, settings,
                              starting_capital=1000)
    timed = [t for t in result.trades if t.exit_reason == "time"]
    assert timed
    assert (timed[0].exit_date - timed[0].entry_date).days == 4


def test_time_stop_defers_when_stop_locks_profit():
    closes = [round(1.0800 + 0.0008 * i, 5) for i in range(220)]
    closes += [round(closes[-1] + 0.0015 * i, 5) for i in range(1, 7)]
    closes += [closes[-1]] * 12
    bars = _t4_bars(closes)
    armed = AppSettings(smart_exit_enabled=False, partial_close_pct=0,
                        max_hold_days=8, rr_target=30.0, time_stop_min_r=0)
    naked = AppSettings(smart_exit_enabled=False, partial_close_pct=0,
                        max_hold_days=8, rr_target=30.0, time_stop_min_r=0,
                        breakeven_trigger_r=0, trail_atr_mult=0)
    with_lock = replay_portfolio({"EURUSD": bars}, armed, starting_capital=1000)
    without_lock = replay_portfolio({"EURUSD": bars}, naked, starting_capital=1000)
    assert with_lock.trades[0].exit_reason == "end_of_data"
    assert without_lock.trades[0].exit_reason == "time"


def test_end_of_data_force_closes_open_positions():
    settings = AppSettings(max_hold_days=0, smart_exit_enabled=False)
    history = _t4_trend(n=220)
    flat = [history[-1].c] * 10
    series = {"EURUSD": history + _t4_bars(flat, first_index=220)}
    result = replay_portfolio(series, settings, starting_capital=1000)
    assert result.trades
    assert result.trades[-1].exit_reason == "end_of_data"
    assert result.metrics.net_pnl == pytest.approx(sum(t.pnl_usd for t in result.trades))
    assert result.equity_curve[-1].equity == pytest.approx(
        1000 + result.metrics.net_pnl)


def test_entry_exit_costs_across_partial_slices():
    settings = AppSettings(partial_close_pct=50, partial_trigger_r=1.0,
                           smart_exit_enabled=False, max_hold_days=0,
                           rr_target=10.0)
    closes = [round(1.0800 + 0.0008 * i, 5) for i in range(200)]
    closes += [round(closes[-1] + 0.0015 * i, 5) for i in range(1, 5)]
    closes += [closes[-1]] * 6
    result = replay_portfolio({"EURUSD": _t4_bars(closes)}, settings,
                              starting_capital=1000)
    assert len(result.trades) == 2
    first, second = result.trades
    assert first.exit_reason == "partial"
    assert first.volume > 0 and second.volume > 0
    assert abs(first.volume - second.volume) <= 0.01
    assert result.metrics.costs > 0
    assert result.metrics.gross_pnl - result.metrics.costs == pytest.approx(
        result.metrics.net_pnl)
