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
    assert s.no_behind_hold_mult == 5.0
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
    """
    from app.services import execution
    rows = [_closed(10, 0.5, 10.0), _closed(9, 0.5, -5.0),
            _closed(8, 30.0, None)]  # open/legacy row, no realized pnl
    db = _FakeDb(rows)
    via_guard = execution.avg_hold_days(db)
    via_monitor = execution.avg_hold_days(
        db, [r for r in rows if r.get("pnl") is not None])
    assert via_guard == via_monitor == 0.5


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
