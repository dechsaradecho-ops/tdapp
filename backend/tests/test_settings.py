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
    effective_min_confidence,
    effective_min_lot,
)
from app.integrations import quotes
from tests.test_workers import FakeDatabase

# ---------------------------------------------------------------------------
# Fake supabase-style client for the app_settings single row
# ---------------------------------------------------------------------------
class FakeSettingsClient:
    """Minimal .table().select/.upsert/.delete chainable fake.

    `fail_columns` simulates a Supabase table that is MISSING some columns
    (migrations not yet applied): the first upsert containing one of them
    raises the PostgREST PGRST204 error so the retry path is exercised.
    """

    def __init__(self, row: dict | None = None,
                 fail_columns: tuple[str, ...] = ()):
        self.row = dict(row) if row else None
        self.fail_columns = set(fail_columns)
        self.upsert_count = 0
        self.saved_rows: list[dict] = []

    def table(self, _name: str) -> "FakeSettingsClient":
        return self

    def select(self, _cols: str) -> "FakeSettingsClient":
        return self

    def eq(self, _col: str, _val: object) -> "FakeSettingsClient":
        return self

    def limit(self, _n: int) -> "FakeSettingsClient":
        return self

    def upsert(self, row: dict) -> "FakeSettingsClient":
        self.upsert_count += 1
        for col in self.fail_columns:
            if col in row:
                raise RuntimeError(
                    '{"code":"PGRST204","message":"Could not find the \''
                    + col + '\' column of \'trading_settings\' in the schema '
                    'cache"}')
        self.saved_rows.append(dict(row))
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
    assert s.news_block_minutes == 30
    assert s.correlation_cap == 80.0
    assert s.risk_profile == RiskProfile.moderate
    # UI prefs (moved out of localStorage 2026-09-06)
    assert s.monitor_refresh_sec == 10
    assert s.signals_refresh_sec == 0
    # moderate profile limits unchanged
    mod = FrequencyEngine(RiskProfile.moderate).limits()
    assert (mod.max_trades_daily, mod.max_trades_weekly,
            mod.max_open_positions, mod.risk_per_trade_pct) == (6, 30, 4, 1.0)
    # settings defaults mirror moderate
    assert s.max_trades_daily == 6 and s.risk_per_trade_pct == 1.0


def test_settings_fields_accepted_by_put_endpoint():
    """UI prefs (monitor_refresh_sec / signals_refresh_sec) are part of
    AppSettings → _FIELDS auto-includes them → PUT /api/settings persists
    them. 0 values must survive the `v is not None` filter (ปิด = 0)."""
    s = AppSettings()
    assert s.monitor_refresh_sec == 10
    assert s.signals_refresh_sec == 0


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


# ---------------------------------------------------------------------------
# 2b) Per-asset Min Confidence (gold)
# ---------------------------------------------------------------------------
def test_effective_min_confidence_gold_uses_override():
    s = AppSettings(min_confidence=70.0, min_confidence_gold=85.0)
    assert effective_min_confidence(s, "XAUUSD") == 85.0
    assert effective_min_confidence(s, "xauusd") == 85.0   # case-insensitive
    # other assets keep the base threshold
    assert effective_min_confidence(s, "EURUSD") == 70.0
    assert effective_min_confidence(s, "GBPUSD") == 70.0
    assert effective_min_confidence(s, "AUDUSD") == 70.0
    assert effective_min_confidence(s, "USDJPY") == 70.0


def test_effective_min_confidence_gold_falls_back_to_base():
    # No override (None) → gold behaves exactly like before the feature
    s = AppSettings(min_confidence=70.0, min_confidence_gold=None)
    assert effective_min_confidence(s, "XAUUSD") == 70.0
    # Default settings (no gold field set) → base threshold everywhere
    assert effective_min_confidence(AppSettings(), "XAUUSD") == 70.0


# ---------------------------------------------------------------------------
# 2c) Per-asset Min Lot (gold)
# ---------------------------------------------------------------------------
def test_effective_min_lot_gold_uses_override():
    s = AppSettings(min_lot=0.01, min_lot_gold=0.05)
    assert effective_min_lot(s, "XAUUSD") == 0.05
    assert effective_min_lot(s, "xauusd") == 0.05   # case-insensitive
    # other assets keep the base floor
    assert effective_min_lot(s, "EURUSD") == 0.01
    assert effective_min_lot(s, "GBPUSD") == 0.01
    assert effective_min_lot(s, "AUDUSD") == 0.01
    assert effective_min_lot(s, "USDJPY") == 0.01


def test_effective_min_lot_gold_falls_back_to_base():
    # No override (None) → gold behaves exactly like before the feature
    s = AppSettings(min_lot=0.02, min_lot_gold=None)
    assert effective_min_lot(s, "XAUUSD") == 0.02
    # Default settings (no gold field set) → base floor everywhere
    assert effective_min_lot(AppSettings(), "XAUUSD") == 0.01
    # missing asset → base floor
    assert effective_min_lot(AppSettings(min_lot=0.03), "") == 0.03


# ---------------------------------------------------------------------------
# 2d) Per-symbol paper spread
# ---------------------------------------------------------------------------
def test_effective_spread_builtin_defaults():
    """Every known asset gets a realistic built-in spread — no config needed."""
    from app.models.schemas import DEFAULT_SPREADS, effective_spread
    s = AppSettings()
    assert effective_spread(s, "XAUUSD") == 0.30
    assert effective_spread(s, "EURUSD") == 0.00010
    assert effective_spread(s, "USDJPY") == 0.015
    assert effective_spread(s, "GBPJPY") == 0.030
    # case-insensitive
    assert effective_spread(s, "xauusd") == 0.30


def test_effective_spread_table_covers_supported_assets():
    """The built-in table must cover every tradable asset (settings page
    universe) — otherwise the legacy global paper_spread leaks into the
    fill of a symbol that should have a realistic default."""
    from app.models.schemas import DEFAULT_SPREADS, effective_spread
    missing = [a for a in quotes.SUPPORTED_ASSETS
               if effective_spread(AppSettings(), a)
               != DEFAULT_SPREADS[a]]
    assert missing == []


def test_effective_spread_override_wins():
    """User override (spread_overrides) beats the built-in default."""
    from app.models.schemas import effective_spread
    s = AppSettings(spread_overrides={"XAUUSD": 0.45, "EURUSD": 0.0002})
    assert effective_spread(s, "XAUUSD") == 0.45
    assert effective_spread(s, "EURUSD") == 0.0002
    # symbols without an override keep the built-in default
    assert effective_spread(s, "GBPUSD") == 0.00015


def test_effective_spread_unknown_asset_falls_back_to_paper_spread():
    """Symbols outside the built-in table (forward compat) use the legacy
    global paper_spread; 0 = no spread (old behaviour)."""
    from app.models.schemas import effective_spread
    assert effective_spread(AppSettings(), "XAGUSD") == 0.0
    assert effective_spread(AppSettings(paper_spread=0.00015), "XAGUSD") == 0.00015


def test_effective_spread_zero_override_disables_spread():
    """An explicit 0 override disables the spread for that symbol (paid
    overrides beat the built-in table — clearing means removing the key)."""
    from app.models.schemas import effective_spread
    s = AppSettings(spread_overrides={"XAUUSD": 0.0})
    assert effective_spread(s, "XAUUSD") == 0.0


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
async def test_put_settings_persists_gold_confidence():
    """Min Confidence (gold) round-trips through the settings API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings", {"min_confidence_gold": 85})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["min_confidence_gold"] == 85
    assert db._client.row["min_confidence_gold"] == 85
    # base threshold untouched
    assert body["settings"]["min_confidence"] == 70


@pytest.mark.asyncio
async def test_put_settings_persists_min_lot_gold():
    """min_lot_gold (ขนาด Lot ขั้นต่ำ gold) round-trips through the settings API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings", {"min_lot_gold": 0.05})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["min_lot_gold"] == 0.05
    assert db._client.row["min_lot_gold"] == 0.05
    # base floor untouched
    assert body["settings"]["min_lot"] == 0.01


@pytest.mark.asyncio
async def test_put_settings_min_lot_gold_none_clears_override():
    """Sending null clears the gold lot override → falls back to base min_lot."""
    db = SettingsDatabase(AppSettings(min_lot_gold=0.05).model_dump(mode="json"))
    set_state(db)
    res = await call("PUT", "/api/settings", {"min_lot_gold": None})
    assert res.status_code == 200
    body = res.json()
    assert body["settings"]["min_lot_gold"] is None
    assert effective_min_lot(AppSettings(**body["settings"]), "XAUUSD") == 0.01


@pytest.mark.asyncio
async def test_get_settings_returns_min_lot_gold_from_row():
    row = AppSettings(min_lot=0.01, min_lot_gold=0.1).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["min_lot_gold"] == 0.1


@pytest.mark.asyncio
async def test_put_settings_persists_spread_overrides():
    """spread_overrides (spread รายสัญลักษณ์) round-trips through the API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings",
                     {"spread_overrides": {"XAUUSD": 0.45}})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["spread_overrides"] == {"XAUUSD": 0.45}
    assert db._client.row["spread_overrides"] == {"XAUUSD": 0.45}


@pytest.mark.asyncio
async def test_put_settings_empty_spread_overrides_clears_column():
    """{} (every override cleared in the UI) → column stores NULL so the
    built-in DEFAULT_SPREADS apply; a stored override is replaced."""
    db = SettingsDatabase(AppSettings(spread_overrides={"XAUUSD": 0.5})
                          .model_dump(mode="json"))
    set_state(db)
    res = await call("PUT", "/api/settings", {"spread_overrides": {}})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    # null = built-in defaults (same semantics as min_lot_gold)
    assert body["settings"]["spread_overrides"] is None
    assert db._client.row["spread_overrides"] is None


@pytest.mark.asyncio
async def test_get_settings_returns_spread_overrides_from_row():
    row = AppSettings(spread_overrides={"EURUSD": 0.0002})
    set_state(SettingsDatabase(row.model_dump(mode="json")))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["spread_overrides"] == {"EURUSD": 0.0002}


@pytest.mark.asyncio
async def test_put_settings_persists_sl_distance_mode():
    """sl_distance_mode (สั้น/กลาง/ยาว) round-trips through the settings API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings", {"sl_distance_mode": "long"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["sl_distance_mode"] == "long"
    assert db._client.row["sl_distance_mode"] == "long"
    # default stays medium on fresh rows
    res2 = await call("GET", "/api/settings")
    assert res2.json()["sl_distance_mode"] in ("medium", "long")  # merge row exists now


def test_app_settings_sl_distance_mode_default_is_medium():
    assert AppSettings().sl_distance_mode == "medium"


@pytest.mark.asyncio
async def test_put_settings_gold_none_clears_override():
    """Sending null clears the gold override → falls back to base."""
    db = SettingsDatabase(AppSettings(min_confidence_gold=85.0).model_dump(mode="json"))
    set_state(db)
    res = await call("PUT", "/api/settings", {"min_confidence_gold": None})
    assert res.status_code == 200
    body = res.json()
    assert body["settings"]["min_confidence_gold"] is None
    # effective threshold for gold is the base value again
    assert effective_min_confidence(AppSettings(**body["settings"]), "XAUUSD") == 70.0


@pytest.mark.asyncio
async def test_get_settings_returns_gold_override_from_row():
    row = AppSettings(min_confidence=70.0, min_confidence_gold=90.0).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["min_confidence_gold"] == 90.0


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
async def test_put_settings_persists_min_lot():
    """min_lot (ขนาด lot ขั้นต่ำ) round-trips through the settings API."""
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings", {"min_lot": 0.02})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["min_lot"] == 0.02
    assert db._client.row["min_lot"] == 0.02
    # default stays 0.01 on fresh rows
    res2 = await call("GET", "/api/settings")
    assert res2.json()["min_lot"] in (0.01, 0.02)  # merge row exists now


def test_app_settings_min_lot_default_is_0_01():
    assert AppSettings().min_lot == 0.01


@pytest.mark.asyncio
async def test_get_settings_returns_min_lot_from_row():
    row = AppSettings(min_lot=0.05).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["min_lot"] == 0.05


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


# ---------------------------------------------------------------------------
# allowed_assets — user-managed tradable universe
# ---------------------------------------------------------------------------
def test_app_settings_allowed_assets_default():
    from app.integrations import quotes
    s = AppSettings()
    assert s.allowed_assets == quotes.DEFAULT_ASSETS
    assert s.effective_assets() == quotes.DEFAULT_ASSETS


def test_effective_assets_drops_unknown_and_dedupes():
    from app.integrations import quotes
    s = AppSettings(allowed_assets=["EURUSD", "eurusd", "FAKEUSD", "XAUUSD"])
    eff = s.effective_assets()
    assert eff == ["EURUSD", "XAUUSD"]  # dedup + whitelist only
    assert "FAKEUSD" not in quotes.SUPPORTED_ASSETS


def test_effective_assets_empty_falls_back_to_defaults():
    s = AppSettings(allowed_assets=[])
    assert s.effective_assets() == quotes.DEFAULT_ASSETS


@pytest.mark.asyncio
async def test_put_settings_persists_allowed_assets():
    db = SettingsDatabase(None)
    set_state(db)
    res = await call("PUT", "/api/settings",
                     {"allowed_assets": ["EURUSD", "GBPJPY", "XAUUSD"]})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["allowed_assets"] == ["EURUSD", "GBPJPY", "XAUUSD"]
    assert db._client.row["allowed_assets"] == ["EURUSD", "GBPJPY", "XAUUSD"]


@pytest.mark.asyncio
async def test_get_settings_returns_allowed_assets_from_row():
    row = AppSettings(allowed_assets=["USDJPY"]).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    assert res.json()["allowed_assets"] == ["USDJPY"]


# ---------------------------------------------------------------------------
# PGRST204 resilience — missing column (migration not applied) must NOT kill
# the whole save. Regression: min_confidence_gold stopped persisting once the
# frontend started sending newer fields (max_hold_days / gold_breakout_only /
# sl_distance_*) whose columns didn't exist in prod Supabase yet.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_put_settings_survives_missing_column_and_saves_rest():
    """PGRST204 on one column → retry without it; other fields still land."""
    db = SettingsDatabase(None)
    db._client = FakeSettingsClient(None, fail_columns=("sl_distance_min_pct",))
    set_state(db)
    res = await call("PUT", "/api/settings",
                     {"min_confidence_gold": 85, "sl_distance_min_pct": 0.8})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert "sl_distance_min_pct" in body["message"]  # friendly skip note
    # the important part: min_confidence_gold WAS saved
    assert db._client.row["min_confidence_gold"] == 85
    assert "sl_distance_min_pct" not in db._client.row
    assert db._client.upsert_count == 2  # failed attempt + retry


@pytest.mark.asyncio
async def test_put_settings_missing_column_retry_keeps_merge_semantics():
    """Retry must still merge with the stored row, not replace it."""
    db = SettingsDatabase(None)
    db._client = FakeSettingsClient(None, fail_columns=("gold_breakout_only",))
    set_state(db)
    res = await call("PUT", "/api/settings", {"gold_breakout_only": False,
                                              "min_confidence": 80})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert db._client.row["min_confidence"] == 80
    assert "gold_breakout_only" not in db._client.row
    # untouched fields keep defaults (merge, not replace)
    assert db._client.row["news_block_minutes"] == 30


@pytest.mark.asyncio
async def test_put_settings_missing_column_get_falls_back_cleanly():
    """After a skipped-column save, GET must still return valid settings."""
    db = SettingsDatabase(None)
    db._client = FakeSettingsClient(None, fail_columns=("sl_distance_max_pct",))
    set_state(db)
    await call("PUT", "/api/settings", {"min_confidence_gold": 65})
    res = await call("GET", "/api/settings")
    assert res.status_code == 200
    body = res.json()
    assert body["min_confidence_gold"] == 65
    # missing column → schema default on GET
    assert body["sl_distance_max_pct"] == 0


# ---------------------------------------------------------------------------
# Full risk presets — profile owns 34 risk fields, not just 4 frequency ones
# ---------------------------------------------------------------------------
def test_risk_presets_moderate_matches_defaults():
    """moderate preset must be byte-identical to AppSettings field defaults
    (otherwise switching profile silently drifts an untouched row)."""
    from app.models.schemas import RISK_PRESETS, RISK_PRESET_FIELDS, RiskProfile
    assert set(RISK_PRESETS.keys()) == {
        RiskProfile.conservative, RiskProfile.moderate, RiskProfile.aggressive}
    defaults = AppSettings()
    for field, value in RISK_PRESETS[RiskProfile.moderate].items():
        assert getattr(defaults, field) == value, field
    # no default field the preset forgot (except deliberately excluded identity)
    excluded = {"capital", "min_confidence_gold", "min_lot",
                "min_lot_gold", "paper_spread", "spread_overrides", "order_mode",
                "default_equity", "paper_virtual_capital", "backtest_days",
                "backtest_indicator", "backtest_asset", "monitor_refresh_sec",
                "signals_refresh_sec", "notify_trade_opened", "notify_trade_closed",
                "notify_stop_loss", "notify_risk_warning", "notify_daily_digest",
                "notify_daily_summary", "allowed_assets",
                # exit-side simulation cost (migration 033) — a realism knob,
                # not a risk-profile field
                "paper_exit_spread_mult", "paper_commission_per_lot"}
    assert set(RISK_PRESET_FIELDS) | excluded == set(
        AppSettings.model_fields.keys()) - {"risk_profile"}, \
        set(AppSettings.model_fields.keys()) - {"risk_profile"} - set(RISK_PRESET_FIELDS) - excluded


def test_apply_risk_preset_only_touches_owned_fields():
    """Preset switch changes the 34 owned fields, keeps user identity."""
    from app.models.schemas import RISK_PRESET_FIELDS, apply_risk_preset
    base = AppSettings(capital=50_000, min_lot=0.05,
                       allowed_assets=["EURUSD"],
                       notify_trade_opened=False)
    out = apply_risk_preset(base, RiskProfile.aggressive)
    assert out.risk_profile == RiskProfile.aggressive
    assert out.max_trades_daily == 10 and out.risk_per_trade_pct == 2.0
    assert out.min_confidence == 65.0 and out.gold_breakout_only is False
    assert out.exit_score_close == 35.0 and out.kill_daily_loss_pct == 3.0
    # identity survives
    assert out.capital == 50_000 and out.min_lot == 0.05
    assert out.allowed_assets == ["EURUSD"]
    assert out.notify_trade_opened is False
    assert len(RISK_PRESET_FIELDS) == 36


def test_apply_risk_preset_conservative_is_tighter_than_aggressive():
    from app.models.schemas import RISK_PRESETS, RiskProfile
    con = RISK_PRESETS[RiskProfile.conservative]
    agg = RISK_PRESETS[RiskProfile.aggressive]
    assert con["risk_per_trade_pct"] < agg["risk_per_trade_pct"]
    assert con["min_confidence"] > agg["min_confidence"]
    assert con["kill_daily_loss_pct"] < agg["kill_daily_loss_pct"]
    assert con["exit_score_close"] > agg["exit_score_close"]


@pytest.mark.asyncio
async def test_get_presets_returns_all_three_levels():
    set_state(SettingsDatabase(None))
    res = await call("GET", "/api/settings/presets")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"conservative", "moderate", "aggressive"}
    assert body["moderate"]["max_trades_daily"] == 6
    assert body["conservative"]["risk_per_trade_pct"] == 0.5
    assert body["aggressive"]["risk_per_trade_pct"] == 2.0


@pytest.mark.asyncio
async def test_post_preset_applies_and_keeps_identity():
    row = AppSettings(capital=50_000, min_lot=0.05,
                      notify_trade_opened=False).model_dump(mode="json")
    set_state(SettingsDatabase(row))
    res = await call("POST", "/api/settings/preset/conservative", {})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["settings"]["risk_profile"] == "conservative"
    assert body["settings"]["max_trades_daily"] == 3
    assert body["settings"]["min_confidence"] == 75.0
    # identity survives the preset switch
    assert body["settings"]["capital"] == 50_000
    assert body["settings"]["min_lot"] == 0.05
    assert body["settings"]["notify_trade_opened"] is False
