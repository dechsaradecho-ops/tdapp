"""Settings feature tests — user-configurable trading configuration.

Covers:
  1. AppSettings defaults are byte-identical to the pre-settings engine defaults
  2. FrequencyEngine / KillSwitchEngine / EconomicCalendarEngine accept overrides
  3. GET /api/settings returns defaults when the DB has no row
  4. PUT /api/settings persists (merge-patch) and engines observe it
  5. POST /api/settings/reset reverts to defaults

Run from backend/: C:/Python314/python.exe -m pytest tests/test_settings.py -v
"""
from __future__ import annotations

import httpx
import pytest

from app.main import app
from app.models.schemas import (
    AppSettings,
    EconomicCalendarEngine,
    EconomicEvent,
    FrequencyEngine,
    KillSwitchEngine,
    RiskProfile,
    TradeLimits,
)
from tests.test_workers import FakeDatabase

# ---------------------------------------------------------------------------
# Fake supabase-style client for the app_settings single row
# ---------------------------------------------------------------------------
class FakeSettingsClient:
    """Minimal .table().select/.upsert/.delete chainable fake."""

    def __init__(self, row: dict | None = None):
        self.row = dict(row) if row else None

    def table(self, _name: str) -> "FakeSettingsClient":
        return self

    def select(self, _cols: str) -> "FakeSettingsClient":
        return self

    def eq(self, _col: str, _val: object) -> "FakeSettingsClient":
        return self

    def limit(self, _n: int) -> "FakeSettingsClient":
        return self

    def upsert(self, row: dict) -> "FakeSettingsClient":
        self.row = dict(row)
        return self

    def delete(self) -> "FakeSettingsClient":
        self.row = None
        return self

    def execute(self):
        data = [self.row] if self.row else []
        return SimpleResult(data)


class SimpleResult:
    def __init__(self, data):
        self.data = data


class SettingsDatabase(FakeDatabase):
    """FakeDatabase + a supabase _client for app_settings."""

    def __init__(self, settings_row: dict | None = None):
        super().__init__()
        self._client = FakeSettingsClient(settings_row)


def set_state(db) -> None:
    app.state.db = db
    app.state.line = type("L", (), {"push": staticmethod(lambda *a, **k: None)})()
    app.state.broker = type("B", (), {"connected": True})()


async def call(method: str, path: str, json_body: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, json=json_body)


# ---------------------------------------------------------------------------
# 1) Defaults identical to pre-settings behavior
# ---------------------------------------------------------------------------
def test_app_settings_defaults_match_engine_defaults():
    s = AppSettings()
    assert s.min_confidence == 70.0
    assert s.min_opportunity == 60.0
    assert s.kill_daily_loss_pct == 2.0
    assert s.kill_weekly_loss_pct == 5.0
    assert s.kill_monthly_loss_pct == 8.0
    assert s.max_drawdown_pct == 10.0
    assert s.drawdown_throttle_pct == 5.0
    assert s.news_block_minutes == 30.0
    assert s.correlation_cap == 80.0
    assert s.risk_profile == RiskProfile.moderate
    # moderate profile limits unchanged
    mod = FrequencyEngine(RiskProfile.moderate).limits()
    assert (mod.max_trades_daily, mod.max_trades_weekly,
            mod.max_open_positions, mod.risk_per_trade_pct) == (6, 30, 4, 1.0)
    # settings defaults mirror moderate
    assert s.max_trades_daily == 6 and s.risk_per_trade_pct == 1.0


# ---------------------------------------------------------------------------
# 2) Engines honor overrides
# ---------------------------------------------------------------------------
def test_frequency_engine_min_confidence_override():
    lo = FrequencyEngine(RiskProfile.moderate, min_confidence=50.0)
    hi = FrequencyEngine(RiskProfile.moderate, min_confidence=90.0)
    assert lo.evaluate(confidence=60, regime="bull_trend").allowed is True
    d = hi.evaluate(confidence=60, regime="bull_trend")
    assert d.allowed is False
    assert "90" in d.reason


def test_frequency_engine_limits_override():
    tight = FrequencyEngine(
        RiskProfile.moderate,
        limits_override=TradeLimits(max_trades_daily=1, max_trades_weekly=5,
                                    max_open_positions=1, risk_per_trade_pct=0.25))
    d = tight.evaluate(confidence=90, trades_today=1)
    assert d.allowed is False
    assert d.limits.max_trades_daily == 1
    assert tight.limits() is tight._override


def test_frequency_engine_drawdown_throttle_override():
    eng = FrequencyEngine(RiskProfile.moderate, drawdown_throttle_pct=1.0)
    d = eng.evaluate(confidence=90, current_drawdown_pct=1.5)
    assert d.allowed is False
    assert "throttled" in d.reason


def test_kill_switch_threshold_overrides():
    loose = KillSwitchEngine(daily_loss_limit=10.0, weekly_loss_limit=20.0,
                             monthly_loss_limit=30.0, drawdown_limit=40.0)
    st = loose.evaluate(daily_loss_pct=5.0, weekly_loss_pct=8.0,
                        monthly_loss_pct=12.0, drawdown_pct=15.0)
    assert st.engaged is False and st.triggers == []

    strict = KillSwitchEngine(daily_loss_limit=1.0)
    st2 = strict.evaluate(daily_loss_pct=1.5)
    assert st2.engaged is True
    assert any("Daily loss" in t for t in st2.triggers)


def test_calendar_engine_block_minutes_override():
    now = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    soon = now + __import__("datetime").timedelta(minutes=45)
    ev = EconomicEvent(event="NFP", currency="USD", time_utc=soon, impact="high")
    default = EconomicCalendarEngine().news_risk([ev], now)
    custom = EconomicCalendarEngine(block_minutes=60.0).news_risk([ev], now)
    assert default.status == "CAUTION"   # 45 min: safe under default 30 block
    assert custom.status == "DANGER"     # 45 min: blocked under custom 60 block


# ---------------------------------------------------------------------------
# 3-5) API endpoints with DB-backed settings
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_settings_defaults_when_no_row():
    set_state(SettingsDatabase(None))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["min_confidence"] == 70
    assert body["kill_daily_loss_pct"] == 2.0
    assert body["risk_profile"] == "moderate"


@pytest.mark.asyncio
async def test_put_settings_persists_and_merges():
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings",
                     {"min_confidence": 85, "kill_daily_loss_pct": 1.0})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["min_confidence"] == 85
    assert body["settings"]["kill_daily_loss_pct"] == 1.0
    # untouched fields keep defaults (merge, not replace)
    assert body["settings"]["news_block_minutes"] == 30
    # row landed in the fake client
    assert db._client.row is not None
    assert db._client.row["id"] == 1
    assert db._client.row["min_confidence"] == 85


@pytest.mark.asyncio
async def test_put_settings_ignores_unknown_and_none_fields():
    set_state(SettingsDatabase(None))
    res = await call("PUT", "/api/settings",
                     {"hacker_field": "x", "min_confidence": None,
                      "correlation_cap": 70})
    assert res.status_code == 200
    body = res.json()
    assert body["settings"]["correlation_cap"] == 70
    assert body["settings"]["min_confidence"] == 70  # None ignored → default kept


@pytest.mark.asyncio
async def test_saved_settings_flow_into_frequency_endpoint():
    row = AppSettings(min_confidence=90, max_trades_daily=2).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("GET", "/api/trading/frequency?profile=moderate")
    assert res.status_code == 200
    body = res.json()
    # confidence passed to evaluate == s.min_confidence (90) → passes its own gate,
    # but the daily limit override (2) with 0 trades today keeps it allowed.
    assert body["limits"]["max_trades_daily"] == 2


@pytest.mark.asyncio
async def test_reset_settings_reverts_to_defaults():
    db = SettingsDatabase(AppSettings(min_confidence=85).model_dump(mode="json"))
    set_state(db)
    res = await call("POST", "/api/settings/reset")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["min_confidence"] == 70
    assert db._client.row is None
    # GET now returns defaults again
    res2 = await call("GET", "/api/settings")
    assert res2.json()["min_confidence"] == 70
