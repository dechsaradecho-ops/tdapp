"""Smart Exit Engine tests — pure exit evaluation (no DB/network/broker).

Run from backend/: C:/Python314/python.exe -m pytest tests/test_smart_exit.py -v
"""
from __future__ import annotations

from app.engine import smart_exit
from app.models.schemas import AppSettings


def healthy_snap() -> dict:
    return {
        "ema_fast": 105.0, "ema_slow": 95.0, "adx": 40.0,
        "supertrend_dir": 1, "rsi": 62.0, "macd_hist": 2.0,
        "atr_pct": 0.8, "volatility_index": 8.0,
        "opportunity_score": 70.0, "regime": "bull_trend",
    }


def dead_snap() -> dict:
    return {
        "ema_fast": 95.0, "ema_slow": 105.0, "adx": 12.0,
        "supertrend_dir": -1, "rsi": 38.0, "macd_hist": -1.5,
        "atr_pct": 0.8, "volatility_index": 8.0,
        "opportunity_score": 30.0, "regime": "sideway",
    }


def test_quality_thresholds():
    assert smart_exit.quality_of(65) == "High"
    assert smart_exit.quality_of(64.9) == "Medium"
    assert smart_exit.quality_of(45) == "Medium"
    assert smart_exit.quality_of(44.9) == "Low"
    # spec example: 42 → Low → Close
    assert smart_exit.quality_of(42) == "Low"


def test_healthy_trend_holds():
    s = AppSettings()
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=1.1000,
        price=1.1050, stop_loss=1.0950, age_days=1.0,
        settings=s, snapshot=healthy_snap(),
        news_status="SAFE", avg_hold_days=4.0)
    assert d.quality == "High"
    assert d.recommendation == "HOLD"
    assert d.final == "CONTINUE"
    assert d.reasoning  # Thai reasoning ALWAYS


def test_reversal_closes():
    s = AppSettings()
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=1.1000,
        price=1.1010, stop_loss=1.0950, age_days=2.0,
        settings=s, snapshot=dead_snap(),
        news_status="SAFE", avg_hold_days=4.0)
    assert d.signals.reversal is True
    assert d.recommendation == "CLOSE"
    assert d.trigger == "reversal"


def test_news_exit_with_profit():
    s = AppSettings()
    snap = healthy_snap()
    # +1.5R profit: entry 100, SL 99 → R dist 1, price 101.5
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=101.5, stop_loss=99.0, age_days=1.0,
        settings=s, snapshot=snap,
        news_status="DANGER", news_event="NFP", avg_hold_days=4.0)
    assert d.signals.news is True
    assert d.recommendation == "CLOSE"
    assert d.trigger == "news"


def test_news_without_profit_holds():
    s = AppSettings()
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=100.2, stop_loss=99.0, age_days=1.0,
        settings=s, snapshot=healthy_snap(),
        news_status="DANGER", news_event="NFP", avg_hold_days=4.0)
    # +0.2R < news_exit_min_r 1.0 → no news exit
    assert d.trigger != "news"


def test_volatility_scales_out():
    s = AppSettings()
    snap = healthy_snap()
    snap["atr_pct"] = 4.0  # above volatility_exit_atr 2.5
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=101.5, stop_loss=99.0, age_days=1.0,
        settings=s, snapshot=snap,
        news_status="SAFE", avg_hold_days=4.0)
    assert d.trigger == "volatility"
    assert d.recommendation in ("PARTIAL_25", "PARTIAL_50")


def test_left_behind_closes_stale():
    s = AppSettings()
    snap = healthy_snap()
    snap["opportunity_score"] = 60.0  # avoid reversal vote
    # +0.2R profit, held 30 days vs avg 4 × 5 = 20 → left behind
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=100.2, stop_loss=99.0, age_days=30.0,
        settings=s, snapshot=snap,
        news_status="SAFE", avg_hold_days=4.0)
    assert d.trigger == "left_behind"
    assert d.recommendation == "CLOSE"


def test_profit_protect_scales_out():
    s = AppSettings(profit_protect_r=2.0)
    snap = healthy_snap()
    snap["adx"] = 22.0  # weaker trend → Medium quality, no reversal
    snap["rsi"] = 58.0
    snap["macd_hist"] = 0.5
    snap["regime"] = "sideway"
    snap["opportunity_score"] = 55.0
    # +2.5R profit with non-High quality → profit protect
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=102.5, stop_loss=99.0, age_days=2.0,
        settings=s, snapshot=snap,
        news_status="SAFE", avg_hold_days=4.0)
    assert d.r_multiple >= 2.0
    if d.quality != "High":
        assert d.trigger == "profit_protect"
        assert d.recommendation == "PARTIAL_50"


def test_ladder_sl():
    # below 1R → None
    assert smart_exit.ladder_sl(
        entry_price=100.0, direction="BUY",
        r_distance=1.0, r_multiple=0.5) is None
    # 1R → breakeven
    assert smart_exit.ladder_sl(
        entry_price=100.0, direction="BUY",
        r_distance=1.0, r_multiple=1.5) == 100.0
    # 2R → +1R
    assert smart_exit.ladder_sl(
        entry_price=100.0, direction="BUY",
        r_distance=1.0, r_multiple=2.5) == 101.0
    # 3R → +2R
    assert smart_exit.ladder_sl(
        entry_price=100.0, direction="BUY",
        r_distance=1.0, r_multiple=3.5) == 102.0
    # SELL mirrors
    assert smart_exit.ladder_sl(
        entry_price=100.0, direction="SELL",
        r_distance=1.0, r_multiple=2.5) == 99.0


def test_fail_safe_never_raises():
    s = AppSettings()
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=0.0,
        price=0.0, stop_loss=None, age_days=0.0,
        settings=None, snapshot=None)
    assert d.recommendation == "HOLD"


def test_smart_exit_defaults():
    s = AppSettings()
    assert s.smart_exit_enabled is True
    assert s.exit_score_close == 45.0
    assert s.profit_protect_r == 2.0
    assert s.reversal_opp_min == 50.0
    assert s.news_exit_enabled is True
    assert s.news_exit_min_r == 1.0
    assert s.volatility_exit_atr == 2.5
    assert s.no_behind_min_r == 0.5
    assert s.no_behind_hold_mult == 1.75
    assert s.no_behind_min_days == 2.0
    assert s.time_stop_min_r == 1.0
    assert s.trailing_ladder is True


class _FakeDb:
    """Minimal select() stand-in for avg_hold_days / _position_age_days."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def select(self, table: str, filters: dict | None = None,
               limit: int = 500, **_kw) -> list[dict]:
        rows = list(self._rows)
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return rows[:limit]


def _closed(created_days_ago: float, held_days: float, pnl: float | None):
    from datetime import datetime, timedelta, timezone
    created = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    closed = created + timedelta(days=held_days)
    return {"status": "closed", "pnl": pnl,
            "created_at": created.isoformat(), "closed_at": closed.isoformat()}


def test_avg_hold_ignores_null_pnl_rows():
    """Guard (own query) and monitor (pre-filtered closed_rows) must agree.

    Regression for the GBPUSD left_behind mismatch: rows without a realized
    pnl must never drag the average, whichever caller path computes it.
    The 30-day legacy row would push the mean to 8.25 if it leaked in.
    """
    from app.services import execution
    rows = [_closed(10, 0.5, 10.0), _closed(9, 0.5, -5.0),
            _closed(8, 30.0, None),  # open/legacy row, no realized pnl
            _closed(7, 2.0, 3.0)]
    db = _FakeDb(rows)
    via_guard = execution.avg_hold_days(db)
    via_monitor = execution.avg_hold_days(
        db, [r for r in rows if r.get("pnl") is not None])
    assert via_guard == via_monitor == 1.0  # (0.5 + 0.5 + 2.0) / 3


def test_avg_hold_thin_sample_uses_neutral_fallback():
    """Fewer than 3 usable holds → neutral 4.0 instead of a 1-2 trade guess.

    Regression (prod 2026-09-11): with only 2 closed trades ever, the mean
    was 0.43d → multiplied by no_behind_hold_mult=5 → a 2.5-day left_behind
    threshold, which closed THREE 2.8-day-old positions in one guard cycle.
    """
    from app.services import execution
    assert execution.avg_hold_days(_FakeDb([])) == 4.0
    assert execution.avg_hold_days(_FakeDb([_closed(10, 0.5, 5.0)])) == 4.0
    assert execution.avg_hold_days(
        _FakeDb([_closed(10, 0.5, 5.0), _closed(9, 0.5, -1.0)])) == 4.0
    # a third hold flips it over to the real mean
    assert execution.avg_hold_days(
        _FakeDb([_closed(10, 0.5, 5.0), _closed(9, 0.5, -1.0),
                 _closed(8, 1.0, 1.0)])) == 0.67


def test_avg_hold_ignores_degenerate_instant_spans():
    """A ~60-second hold is a stats artifact, not a holding period."""
    from app.services import execution
    instant = _closed(6, 0.0007, 0.10)   # 60 s — must be dropped
    rows = [instant, _closed(10, 2.0, 5.0), _closed(9, 3.0, -1.0),
            _closed(8, 4.0, 1.0)]
    assert execution.avg_hold_days(_FakeDb(rows)) == 3.0  # (2+3+4)/3


def test_left_behind_threshold_never_passes_time_stop():
    """threshold = max(avg × mult, floor) but clamped to max_hold_days.

    Regression for the dead-code bug: with mult=5 the threshold was
    avg 2.4 × 5 = 11.9 days against a 5-day time stop, so left_behind could
    NEVER fire and the R-blind time stop closed everything instead.
    """
    from app.engine import smart_exit as _se
    s = AppSettings()
    # generous sample → the multiplier would overshoot max_hold_days
    d = _se.left_behind_days(settings=s, avg_hold_days=8.0)
    assert d == float(s.max_hold_days) == 5.0
    # thin sample (4.0) → 7.0 days, still clamped to the 5-day stop
    assert _se.left_behind_days(settings=s, avg_hold_days=4.0) == 5.0
    # a short-but-real average → floor wins (2.0 > 1.75 × 0.8)
    assert _se.left_behind_days(settings=s, avg_hold_days=0.8) == 2.0
    # collapsed average still cannot go under the floor
    assert _se.left_behind_days(settings=s, avg_hold_days=0.51) == 2.0
    # 0.0 means "unknown" → neutral 4.0 fallback, then clamped as usual
    assert _se.left_behind_days(settings=s, avg_hold_days=0.0) == 5.0
    # mult=0 disables the rule entirely (existing semantics kept)
    assert _se.left_behind_days(
        settings=AppSettings(no_behind_hold_mult=0), avg_hold_days=4.0) == 0.0


def test_left_behind_threshold_respects_disabled_time_stop():
    """max_hold_days=0 (time stop off) must not clamp the threshold to 0."""
    from app.engine import smart_exit as _se
    s = AppSettings(max_hold_days=0, no_behind_min_days=0.0)
    assert _se.left_behind_days(settings=s, avg_hold_days=4.0) == 7.0


def test_evaluate_exit_reports_behind_days():
    """The decision object must expose the threshold it used — the popup and
    the guard read the same number instead of re-deriving the formula."""
    s = AppSettings()
    d = smart_exit.evaluate_exit(
        asset="EURUSD", direction="BUY", entry_price=100.0,
        price=100.2, stop_loss=99.0, age_days=30.0,
        settings=s, snapshot=healthy_snap(),
        news_status="SAFE", avg_hold_days=4.0)
    assert d.trigger == "left_behind"
    assert d.behind_days == 5.0  # clamped by max_hold_days


def test_position_age_prefers_journal_created_at():
    """Age comes from the journal row first — same value the monitor badge
    shows — so left_behind and time-stop can't drift from the badge."""
    from datetime import datetime, timedelta, timezone
    from app.integrations.brokers import Position
    from app.workers import position_guard
    created = datetime.now(timezone.utc) - timedelta(days=2.7)
    pos = Position(ticket="T1", user_id="u1", asset="GBPUSD",
                   direction="BUY", volume=0.02, entry_price=1.35557,
                   stop_loss=1.34940, take_profit=1.36770,
                   current_price=1.35040)
    pos.opened_at = datetime.now(timezone.utc)  # stale in-memory clock (deploy reset)
    db = _FakeDb([{"ticket": "T1", "created_at": created.isoformat()}])
    age = position_guard._position_age_days(pos, db)
    assert age == _in_range(2.6, 2.8)


def _in_range(lo: float, hi: float):
    class _Between:
        def __eq__(self, other):
            return lo <= float(other) <= hi
    return _Between()


def test_left_behind_ignores_exit_score_close():
    """exit_score_close=54 must NOT gate the left_behind branch: a Medium
    score (57) with a stale losing hold still closes."""
    from app.engine import smart_exit as _se
    s = AppSettings(exit_score_close=54.0)
    snap = healthy_snap()
    snap["opportunity_score"] = 60.0  # avoid reversal vote
    d = _se.evaluate_exit(
        asset="GBPUSD", direction="BUY", entry_price=100.0,
        price=99.1, stop_loss=99.0, age_days=3.0,
        settings=s, snapshot=snap,
        news_status="SAFE", avg_hold_days=0.5)
    assert d.trigger == "left_behind"
    assert d.recommendation == "CLOSE"
