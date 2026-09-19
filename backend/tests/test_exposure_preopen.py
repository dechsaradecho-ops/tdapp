"""Currency-exposure cap (Gate 4b) + pre-open guards (Gate 3b) tests.

Pro-trader survival guards #1 + #3, integrated into the ONE gate pipeline:

  Gate 4b — currency exposure cap. Correlation (Gate 4) averages PAIRWISE
  correlation, so AUDNZD + AUDCHF can pass while the book is one AUD bet.
  This sums risk-at-stop per currency/direction (open book + candidate) and
  blocks when the largest bucket exceeds max_currency_exposure_pct of capital.

  Gate 3b — pre-open guards: spread as a % of SL distance, pre-news flatten
  window, and the session/weekend filter.

Run from backend/: python -m pytest tests/test_exposure_preopen.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import AppSettings, ExposureEngine
from app.services import execution
from tests.test_workers import FakeDatabase


class FakeBroker:
    def __init__(self):
        self.orders: list = []
        self._next = 1

    async def place_order(self, order):
        self.orders.append(order)
        ticket = f"PAPER-{self._next:06d}"
        self._next += 1
        return SimpleNamespace(ok=True, broker_order_id=ticket, message="opened")

    async def all_positions(self):
        return []


class FakeNotifier:
    def __init__(self):
        self.sent: list = []

    async def notify(self, user_id, ntype, message):
        self.sent.append((ntype, message))


def clean_settings(**over) -> AppSettings:
    """Guards OFF by default so each test opts in to exactly what it checks."""
    base = dict(
        capital=10_000.0, min_confidence=70.0,
        kill_daily_loss_pct=2.0, kill_weekly_loss_pct=5.0,
        kill_monthly_loss_pct=8.0, max_drawdown_pct=10.0,
        max_trades_daily=6, max_trades_weekly=30, max_open_positions=4,
        risk_per_trade_pct=1.0, correlation_cap=80.0,
        news_block_minutes=30, order_mode="auto",
        max_currency_exposure_pct=0.0, spread_guard_max_pct=0.0,
        pre_news_flatten_min=0, session_filter_enabled=False,
    )
    base.update(over)
    return AppSettings(**base)


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture(autouse=True)
def _no_network_spot(monkeypatch):
    async def fake_spot(assets, **_kw):
        return {}, {}
    monkeypatch.setattr(execution.quotes, "fetch_spot_prices", fake_spot)
    monkeypatch.setattr(execution.quotes, "fetch_trusted_spot", fake_spot)


@pytest.fixture(autouse=True)
def _open_market(monkeypatch):
    """Pin the session filter to an open, liquid market unless a test overrides."""
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    monkeypatch.setattr(
        execution.SessionEngine, "active",
        staticmethod(lambda *a, **k: SimpleNamespace(
            active_sessions=["London", "New York"], overlapping=True,
            volatility_hint="high", market_closed=False, next_open_utc=None)))


def _open_row(asset: str, direction: str, volume: float,
              entry: float, sl: float) -> dict:
    return {
        "id": f"open-{asset}", "asset": asset, "direction": direction,
        "volume": volume, "entry_price": entry, "stop_loss": sl,
        "status": "open", "created_at": datetime.now(timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# ExposureEngine.currency_risk — the engine behind Gate 4b
# ---------------------------------------------------------------------------
def test_currency_risk_sums_both_legs_of_a_pair():
    """AUDNZD BUY = long AUD + short NZD; risk lands in BOTH buckets."""
    res = ExposureEngine.currency_risk(
        [{"asset": "AUDNZD", "direction": "BUY", "volume": 0.10,
          "entry_price": 1.10000, "stop_loss": 1.09000}],
        capital=10_000.0, cap_pct=50.0)
    buckets = {(b["currency"], b["direction"]): b for b in res["buckets"]}
    # 0.0010 × 0.10 lots × 100_000 contract = $100 risk per leg.
    assert buckets[("AUD", "long")]["risk_usd"] == pytest.approx(100.0)
    assert buckets[("NZD", "short")]["risk_usd"] == pytest.approx(100.0)
    assert res["over_cap"] is False


def test_currency_risk_flags_over_cap():
    """Two AUD-long pairs stack the AUD bucket past the cap."""
    positions = [
        {"asset": "AUDNZD", "direction": "BUY", "volume": 0.30,
         "entry_price": 1.10000, "stop_loss": 1.09000},
        {"asset": "AUDCHF", "direction": "BUY", "volume": 0.30,
         "entry_price": 0.58000, "stop_loss": 0.57000},
    ]
    res = ExposureEngine.currency_risk(positions, capital=10_000.0, cap_pct=50.0)
    # AUD bucket = 2 × (0.0010 × 0.30 × 100_000) = $600 = 6% — under 50%.
    assert res["over_cap"] is False
    # Tighten the cap to 5% → the AUD bucket now breaches it.
    res2 = ExposureEngine.currency_risk(positions, capital=10_000.0, cap_pct=5.0)
    assert res2["over_cap"] is True
    assert res2["currency"] == "AUD" and res2["direction"] == "long"
    assert res2["pct"] == pytest.approx(6.0, abs=0.01)


def test_currency_risk_opposite_directions_do_not_stack():
    """Long AUD (AUDNZD) + short AUD (AUDUSD) are separate buckets."""
    positions = [
        {"asset": "AUDNZD", "direction": "BUY", "volume": 0.50,
         "entry_price": 1.10000, "stop_loss": 1.09000},
        {"asset": "AUDUSD", "direction": "SELL", "volume": 0.50,
         "entry_price": 0.66000, "stop_loss": 0.67000},
    ]
    res = ExposureEngine.currency_risk(positions, capital=10_000.0, cap_pct=50.0)
    buckets = {(b["currency"], b["direction"]): b for b in res["buckets"]}
    assert ("AUD", "long") in buckets and ("AUD", "short") in buckets
    assert buckets[("AUD", "long")]["risk_usd"] == pytest.approx(500.0)
    assert buckets[("AUD", "short")]["risk_usd"] == pytest.approx(500.0)


def test_currency_risk_gold_and_crypto_bucket():
    """XAUUSD → Gold/USD; BTCUSD → Crypto/USD (explicit CURRENCY_MAP)."""
    res = ExposureEngine.currency_risk(
        [{"asset": "XAUUSD", "direction": "BUY", "volume": 0.10,
          "entry_price": 2400.0, "stop_loss": 2390.0}],
        capital=10_000.0, cap_pct=50.0)
    buckets = {(b["currency"], b["direction"]): b for b in res["buckets"]}
    # 10.0 × 0.10 × 100.0 (gold contract) = $100 risk.
    assert buckets[("Gold", "long")]["risk_usd"] == pytest.approx(100.0)
    assert buckets[("USD", "short")]["risk_usd"] == pytest.approx(100.0)


def test_base_quote_normalises_both_sources():
    """_base_quote must return (base, quote) for the map AND fx_parts paths."""
    # Explicit CURRENCY_MAP is stored [quote, base] — must be flipped.
    assert ExposureEngine._base_quote("EURUSD") == ("EUR", "USD")
    assert ExposureEngine._base_quote("USDJPY") == ("USD", "JPY")
    assert ExposureEngine._base_quote("XAUUSD") == ("Gold", "USD")
    # Generic FX derivation is already (base, quote).
    assert ExposureEngine._base_quote("AUDNZD") == ("AUD", "NZD")
    assert ExposureEngine._base_quote("GBPCAD") == ("GBP", "CAD")


def test_currency_risk_disabled_and_bad_input():
    """cap 0 / no capital / no SL → empty result, never raises."""
    pos = [{"asset": "AUDNZD", "direction": "BUY", "volume": 0.10,
            "entry_price": 1.10000, "stop_loss": 1.09000}]
    assert ExposureEngine.currency_risk(pos, 10_000.0, 0.0)["over_cap"] is False
    assert ExposureEngine.currency_risk(pos, 0.0, 50.0)["buckets"] == []
    assert ExposureEngine.currency_risk(
        [{"asset": "AUDNZD", "direction": "BUY", "volume": 0.10,
          "entry_price": 1.10000, "stop_loss": 0}], 10_000.0, 50.0)["buckets"] == []
    assert ExposureEngine.currency_risk(None, 10_000.0, 50.0)["buckets"] == []


# ---------------------------------------------------------------------------
# Gate 4b — currency exposure block through the real gate pipeline
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_gate4b_blocks_stacked_currency(broker, notifier):
    """Open AUDNZD long + a new AUDCHF long breaches a tight AUD cap.

    The candidate is sized by the risk budget (0.10 lots here), so the AUD
    bucket = $300 (open) + $100 (candidate) = $400 = 4% of capital.
    """
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("AUDNZD", "BUY", 0.30, 1.10000, 1.09000)]})
    s = clean_settings(max_currency_exposure_pct=3.0)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="AUDCHF", direction="BUY",
        entry=0.58000, stop_loss=0.57000, take_profit=0.60000,
        confidence=85.0, opportunity=80.0, signal_id="sig-exp-1", source="auto",
    )
    assert not report.allowed
    assert any("exposure=over" in c for c in report.checks)
    assert any("เกินเพดาน" in r and "AUD" in r for r in report.rejects)
    assert broker.orders == []
    blocked = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "order_blocked"]
    assert blocked and "เกินเพดาน" in blocked[0]["reason"]


@pytest.mark.asyncio
async def test_gate4b_allows_diversified_book(broker, notifier):
    """A EURUSD long does not stack the AUD bucket → allowed."""
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("AUDNZD", "BUY", 0.30, 1.10000, 1.09000)]})
    s = clean_settings(max_currency_exposure_pct=3.0, kill_daily_loss_pct=20.0)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="EURUSD", direction="BUY",
        entry=1.08500, stop_loss=1.08000, take_profit=1.09500,
        confidence=85.0, opportunity=80.0, signal_id="sig-exp-2", source="auto",
    )
    assert report.allowed, report.rejects
    assert any("exposure=ok" in c for c in report.checks)
    assert len(broker.orders) == 1


@pytest.mark.asyncio
async def test_gate4b_zero_disables(broker, notifier):
    """max_currency_exposure_pct 0 → the stacked book still opens."""
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("AUDNZD", "BUY", 0.30, 1.10000, 1.09000)]})
    s = clean_settings(max_currency_exposure_pct=0.0, kill_daily_loss_pct=20.0)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="AUDCHF", direction="BUY",
        entry=0.58000, stop_loss=0.57000, take_profit=0.60000,
        confidence=85.0, opportunity=80.0, signal_id="sig-exp-3", source="auto",
    )
    assert report.allowed, report.rejects
    assert any("exposure=ok" in c for c in report.checks)
    assert len(broker.orders) == 1


def test_exposure_helper_direct_unit():
    """Helper returns a Thai reason over cap and "" when clear."""
    db = FakeDatabase(rows={"paper_trades": [
        _open_row("AUDNZD", "BUY", 0.30, 1.10000, 1.09000)]})
    s = clean_settings(max_currency_exposure_pct=3.0)
    msg = execution.currency_exposure_block(
        db, s, "AUDCHF", entry=0.58000, stop_loss=0.57000, direction="BUY")
    assert "เกินเพดาน" in msg and "AUD" in msg
    # A different currency → clear.
    assert execution.currency_exposure_block(
        db, s, "EURUSD", entry=1.08500, stop_loss=1.08000, direction="BUY") == ""


# ---------------------------------------------------------------------------
# Gate 3b — spread guard
# ---------------------------------------------------------------------------
def test_spread_guard_blocks_wide_spread():
    """Spread 0.00035 on a 0.0007 SL = 50% > 25% cap → blocked."""
    db = FakeDatabase(rows={})
    s = clean_settings(spread_guard_max_pct=25.0,
                       spread_overrides={"EURUSD": 0.00035})
    msg = execution.pre_open_block(
        db, s, "EURUSD", entry=1.10000, stop_loss=1.09930)
    assert "สเปรด" in msg and "เกินเพดาน" in msg


def test_spread_guard_allows_tight_spread():
    """Spread 0.00005 on a 0.0010 SL = 5% < 25% cap → clear."""
    db = FakeDatabase(rows={})
    s = clean_settings(spread_guard_max_pct=25.0,
                       spread_overrides={"EURUSD": 0.00005})
    assert execution.pre_open_block(
        db, s, "EURUSD", entry=1.10000, stop_loss=1.09000) == ""


def test_spread_guard_zero_disables():
    """spread_guard_max_pct 0 → a terrible spread still passes."""
    db = FakeDatabase(rows={})
    s = clean_settings(spread_guard_max_pct=0.0,
                       spread_overrides={"EURUSD": 0.00500})
    assert execution.pre_open_block(
        db, s, "EURUSD", entry=1.10000, stop_loss=1.09930) == ""


# ---------------------------------------------------------------------------
# Gate 3b — pre-news flatten
# ---------------------------------------------------------------------------
def _news_row(currency: str, minutes_ahead: float) -> dict:
    t = datetime.now(timezone.utc) + timedelta(minutes=minutes_ahead)
    return {"event": "CPI", "currency": currency, "impact": "high",
            "event_time": t.isoformat()}


def test_pre_news_blocks_inside_window():
    """AUD CPI in 10 min + 30-min window → AUDNZD blocked."""
    db = FakeDatabase(rows={"economic_calendar": [_news_row("AUD", 10.0)]})
    s = clean_settings(pre_news_flatten_min=30, news_block_minutes=5)
    msg = execution.pre_open_block(
        db, s, "AUDNZD", entry=1.10000, stop_loss=1.09000)
    assert "ข่าว" in msg and "งดเปิดไม้ใหม่ก่อนข่าว" in msg


def test_pre_news_ignores_other_currency():
    """USD CPI in 10 min must NOT block AUDNZD (no USD leg)."""
    db = FakeDatabase(rows={"economic_calendar": [_news_row("USD", 10.0)]})
    s = clean_settings(pre_news_flatten_min=30, news_block_minutes=5)
    assert execution.pre_open_block(
        db, s, "AUDNZD", entry=1.10000, stop_loss=1.09000) == ""


def test_pre_news_outside_window_allows():
    """AUD CPI in 90 min + 30-min window → clear."""
    db = FakeDatabase(rows={"economic_calendar": [_news_row("AUD", 90.0)]})
    s = clean_settings(pre_news_flatten_min=30, news_block_minutes=5)
    assert execution.pre_open_block(
        db, s, "AUDNZD", entry=1.10000, stop_loss=1.09000) == ""


def test_pre_news_zero_disables():
    """pre_news_flatten_min 0 → the imminent event does not block."""
    db = FakeDatabase(rows={"economic_calendar": [_news_row("AUD", 5.0)]})
    s = clean_settings(pre_news_flatten_min=0, news_block_minutes=5)
    assert execution.pre_open_block(
        db, s, "AUDNZD", entry=1.10000, stop_loss=1.09000) == ""


# ---------------------------------------------------------------------------
# Gate 3b — session filter
# ---------------------------------------------------------------------------
def test_session_filter_blocks_when_market_closed(monkeypatch):
    """Weekend close → blocked with the gap reason.

    The weekend close moved OUT of `session_filter_block` into the
    unconditional Gate 0b (`market_closed_block`) — see
    test_market_closed_block_is_unconditional below. `pre_open_block` no
    longer reports it, so this asserts the new home of the rule.
    """
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    msg = execution.market_closed_block()
    assert "ตลาดปิด" in msg


def test_market_closed_block_is_unconditional(monkeypatch):
    """Gate 0b blocks the weekend close even with session_filter_enabled=False.

    Owner rule 2026-09-19: "เวลาตลาดปิด ห้ามมีการซื้อขาย หรือเปิด order" —
    the close is a fact of the market, not a user preference, so it must
    NOT be behind the session-filter switch (which the aggressive preset
    ships as False).
    """
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    assert "ตลาดปิด" in execution.market_closed_block()
    # The session filter itself stays silent (its own switch is off)…
    s = clean_settings(session_filter_enabled=False)
    assert execution.session_filter_block(s) == ""
    # …but the hard gate still refuses the order through the full pipeline.
    db = FakeDatabase(rows={})
    report = execution._gate_blocked(
        db, s, "demo", "EURUSD", 85.0, 80.0,
        entry=1.10000, stop_loss=1.09000, direction="BUY")
    assert not report.allowed
    assert any("ตลาดปิด" in r for r in report.rejects)


def test_market_closed_block_fails_closed_on_clock_error(monkeypatch):
    """An unreadable clock must BLOCK (fail-closed), never wave the order in."""
    def boom(*a, **k):
        raise RuntimeError("clock down")
    monkeypatch.setattr(execution, "is_market_closed", boom)
    assert execution.market_closed_block() != ""


def test_market_open_block_is_empty(monkeypatch):
    """Open market → no block reason (the normal weekday path)."""
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    assert execution.market_closed_block() == ""


def test_market_closed_close_block_blocks(monkeypatch):
    """Owner rule 2026-09-19: "ตอนตลาดปิด ห้ามปิดไม้ด้วย".

    A manual close while the market is closed has no live mark to settle at,
    so it must be refused with a Thai reason.
    """
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    msg = execution.market_closed_close_block()
    assert "ตลาดปิด" in msg
    assert "ปิดไม้ไม่ได้" in msg


def test_market_closed_close_block_empty_when_open(monkeypatch):
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    assert execution.market_closed_close_block() == ""


def test_market_closed_close_block_fails_closed(monkeypatch):
    """Unreadable clock → refuse the close (fail-closed), same as opening."""
    def boom(*a, **k):
        raise RuntimeError("clock down")
    monkeypatch.setattr(execution, "is_market_closed", boom)
    assert execution.market_closed_close_block() != ""


# ---------------------------------------------------------------------------
# Discretionary closes (Smart Exit / time stop / TP1) while the market is
# closed. Owner rule 2026-09-19 (follow-up): "ยังมี smart exit close อยู่ซึ่ง
# ผิด เวลาตลาดปิดไม่สามารถ close ได้" — these are judgement calls, not stops,
# so they must not book a PnL against a stale weekend mark.
# ---------------------------------------------------------------------------
def test_discretionary_close_block_blocks(monkeypatch):
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    msg = execution.market_closed_discretionary_close_block()
    assert "ตลาดปิด" in msg
    assert "smart exit" in msg
    assert "time stop" in msg


def test_discretionary_close_block_empty_when_open(monkeypatch):
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    assert execution.market_closed_discretionary_close_block() == ""


def test_discretionary_close_block_fails_closed(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("clock down")
    monkeypatch.setattr(execution, "is_market_closed", boom)
    assert execution.market_closed_discretionary_close_block() != ""


def test_discretionary_block_is_separate_from_manual_block(monkeypatch):
    """The two guards must stay independent — the manual one is used by the
    API routes, the discretionary one by the position guard."""
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    manual = execution.market_closed_close_block()
    disc = execution.market_closed_discretionary_close_block()
    assert manual and disc and manual != disc


def test_session_filter_blocks_low_liquidity(monkeypatch):
    """Sydney-only (low volatility, no overlap) → blocked."""
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    monkeypatch.setattr(
        execution.SessionEngine, "active",
        staticmethod(lambda *a, **k: SimpleNamespace(
            active_sessions=["Sydney"], overlapping=False,
            volatility_hint="low", market_closed=False, next_open_utc=None)))
    db = FakeDatabase(rows={})
    s = clean_settings(session_filter_enabled=True)
    msg = execution.pre_open_block(
        db, s, "EURUSD", entry=1.10000, stop_loss=1.09000)
    assert "สภาพคล่องต่ำ" in msg and "Sydney" in msg


def test_session_filter_disabled_allows(monkeypatch):
    """session_filter_enabled False → the low-liquidity rule is off.

    (The weekend close is NOT part of this switch any more — it is the
    unconditional Gate 0b, covered above.)
    """
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)
    monkeypatch.setattr(
        execution.SessionEngine, "active",
        staticmethod(lambda *a, **k: SimpleNamespace(
            active_sessions=["Sydney"], overlapping=False,
            volatility_hint="low", market_closed=False, next_open_utc=None)))
    db = FakeDatabase(rows={})
    s = clean_settings(session_filter_enabled=False)
    assert execution.pre_open_block(
        db, s, "EURUSD", entry=1.10000, stop_loss=1.09000) == ""


@pytest.mark.asyncio
async def test_gate3b_blocks_through_pipeline(broker, notifier, monkeypatch):
    """The market-closed guard surfaces in checks[]/rejects[]/signal_logs."""
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: True)
    db = FakeDatabase(rows={})
    s = clean_settings(session_filter_enabled=True)
    report = await execution.execute_signal(
        db, broker, notifier, s,
        user_id="demo", asset="EURUSD", direction="BUY",
        entry=1.08500, stop_loss=1.08000, take_profit=1.09500,
        confidence=85.0, opportunity=80.0, signal_id="sig-pre-1", source="auto",
    )
    assert not report.allowed
    assert any("market=CLOSED" in c for c in report.checks)
    assert any("ตลาดปิด" in r for r in report.rejects)
    assert broker.orders == []
    blocked = [row for table, row in db.inserted
               if table == "signal_logs" and row.get("event") == "order_blocked"]
    assert blocked and "ตลาดปิด" in blocked[0]["reason"]


# ---------------------------------------------------------------------------
# Defaults — the guards ship ON (user decision 2026-09-15)
# ---------------------------------------------------------------------------
def test_new_guard_defaults_are_on():
    """AppSettings defaults enable all four guards (matches moderate preset)."""
    s = AppSettings()
    assert s.max_currency_exposure_pct == 50.0
    assert s.spread_guard_max_pct == 25.0
    assert s.pre_news_flatten_min == 30
    assert s.session_filter_enabled is True
