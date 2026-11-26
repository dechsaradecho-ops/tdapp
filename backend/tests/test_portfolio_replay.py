"""Task 1: replay models and shared timeline (TDD)."""

from datetime import date, datetime, timezone

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
