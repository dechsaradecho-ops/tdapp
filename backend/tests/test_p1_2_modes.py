"""P1-2 tests — split order_mode into entry_mode × position_management_mode.

Two independent axes:
  entry_mode:               auto | confirm | advisory   (who OPENS)
  position_management_mode: auto | protective_only | advisory (who MANAGES)

Backward compatibility: both default to "" which derives from the legacy
``order_mode`` (auto / semi_auto / manual) so a pre-P1-2 row is unchanged.

Invariants proven here:
  * entry_mode and management_mode are INDEPENDENT (3×3 matrix).
  * entry_mode=advisory blocks even a human Approve.
  * management_mode != auto turns OFF every discretionary guard step
    (partial / BE / trail / ladder / smart-exit / time stop).
  * Hard SL/TP + Emergency Exit REMAIN active in EVERY management mode —
    a hard stop is a safety invariant, never dropped.
  * changing the mode while a position is open never loosens the SL.

Run from backend/: d:/tdapp/.venv/Scripts/python.exe -m pytest tests/test_p1_2_modes.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import AppSettings
from app.services import execution
from app.workers import position_guard
from tests.test_auto_trader import (
    FakeBroker, FakeNotifier, clean_settings, db_with_client,
    _neutral_thesis_snapshot,
)
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# 1. Field derivation + legacy back-compat
# ---------------------------------------------------------------------------
class TestModeResolution:
    def test_explicit_values_win(self):
        s = AppSettings(entry_mode="confirm",
                        position_management_mode="protective_only")
        assert s.effective_entry_mode() == "confirm"
        assert s.effective_position_management_mode() == "protective_only"

    @pytest.mark.parametrize("legacy,entry,mgmt", [
        ("auto", "auto", "auto"),
        ("semi_auto", "confirm", "protective_only"),
        ("manual", "advisory", "advisory"),
    ])
    def test_legacy_order_mode_maps_to_both_axes(self, legacy, entry, mgmt):
        s = AppSettings(order_mode=legacy)  # both new fields default ""
        assert s.effective_entry_mode() == entry
        assert s.effective_position_management_mode() == mgmt

    def test_default_row_is_auto_everything(self):
        s = AppSettings()
        assert s.effective_entry_mode() == "auto"
        assert s.effective_position_management_mode() == "auto"
        assert s.entry_is_auto() is True
        assert s.management_allows_discretionary() is True

    @pytest.mark.parametrize("bad", ["", "AUTO ", "banana", "  ", None])
    def test_entry_unknown_falls_back_safe(self, bad):
        s = AppSettings(entry_mode=bad or "")
        # unknown → derive from legacy order_mode (default auto) → "auto"
        assert s.effective_entry_mode() in ("auto", "confirm")

    def test_entry_typo_never_silently_auto_opens(self):
        """A garbage entry_mode with legacy manual must NOT become auto."""
        s = AppSettings(order_mode="manual", entry_mode="nonsense")
        assert s.effective_entry_mode() == "advisory"

    def test_helpers(self):
        assert AppSettings(entry_mode="confirm").entry_is_auto() is False
        assert AppSettings(entry_mode="confirm").entry_accepts_manual_approve()
        assert not AppSettings(entry_mode="advisory").entry_accepts_manual_approve()
        assert AppSettings(position_management_mode="protective_only") \
            .management_allows_discretionary() is False
        assert AppSettings(position_management_mode="advisory") \
            .management_allows_discretionary() is False


# ---------------------------------------------------------------------------
# 2. entry_mode controls the auto-trader (who OPENS)
# ---------------------------------------------------------------------------
def _seed_pending(db, asset="EURUSD", direction="BUY", conf=85.0,
                  opp=85.0):
    db.insert("signals", {
        "id": "S1", "user_id": "u1", "asset": asset, "direction": direction,
        "entry": 1.1000, "stop_loss": 1.0900, "take_profit": 1.1200,
        "confidence": conf, "opportunity_score": opp, "approval": "pending",
        "expected_rr": 2.0, "created_at": datetime.now(timezone.utc).isoformat(),
    })


class _AutoHarness:
    """Minimal offline harness for auto_trader.trade_once."""

    def __init__(self, settings, pending=True):
        self.db = db_with_client({"signals": [], "paper_trades": [],
                                  "signal_logs": [], "notifications": []})
        # trading_settings row → get_app_settings reads it back
        self.db._client.store["trading_settings"] = {
            1: {**settings.model_dump(mode="json"), "id": 1}}
        if pending:
            _seed_pending(self.db)
        self.broker = FakeBroker()
        self.notifier = FakeNotifier()

    async def run(self, monkeypatch):
        async def fake_spot(assets, **_kw):
            return {}, {}
        async def fake_snaps(assets, **_kw):
            return {a: _neutral_thesis_snapshot(a) for a in (assets or [])}
        monkeypatch.setattr(execution.quotes, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(execution.quotes, "fetch_all_snapshots", fake_snaps)
        from app.workers import auto_trader
        return await auto_trader.trade_once(self.db, self.broker, self.notifier)


class TestEntryModeOpens:
    @pytest.mark.asyncio
    async def test_auto_opens(self, monkeypatch):
        h = _AutoHarness(clean_settings(entry_mode="auto"))
        out = await h.run(monkeypatch)
        assert out["fired"] == 1
        assert len(h.broker.orders) == 1

    @pytest.mark.asyncio
    async def test_confirm_does_not_open(self, monkeypatch):
        h = _AutoHarness(clean_settings(entry_mode="confirm"))
        out = await h.run(monkeypatch)
        assert out["fired"] == 0
        assert h.broker.orders == []
        assert out["mode"] == "confirm"

    @pytest.mark.asyncio
    async def test_advisory_does_not_open(self, monkeypatch):
        h = _AutoHarness(clean_settings(entry_mode="advisory"))
        out = await h.run(monkeypatch)
        assert out["fired"] == 0
        assert h.broker.orders == []

    @pytest.mark.asyncio
    async def test_legacy_semi_auto_does_not_open(self, monkeypatch):
        h = _AutoHarness(clean_settings(order_mode="semi_auto"))
        out = await h.run(monkeypatch)
        assert out["fired"] == 0
        assert out["mode"] == "confirm"


# ---------------------------------------------------------------------------
# 3. Independence: the 3×3 matrix (entry × management)
# ---------------------------------------------------------------------------
class TestModeMatrix:
    @pytest.mark.parametrize("entry", ["auto", "confirm", "advisory"])
    @pytest.mark.parametrize("mgmt", ["auto", "protective_only", "advisory"])
    def test_axes_are_independent(self, entry, mgmt):
        s = AppSettings(entry_mode=entry, position_management_mode=mgmt)
        assert s.effective_entry_mode() == entry
        assert s.effective_position_management_mode() == mgmt
        # cross products stay exactly what was set — no coupling
        assert s.management_allows_discretionary() == (mgmt == "auto")
        assert s.entry_is_auto() == (entry == "auto")


# ---------------------------------------------------------------------------
# 4. management_mode gates the guard's DISCRETIONARY steps
# ---------------------------------------------------------------------------
from tests.test_workers import _SilentNotifier  # noqa: E402
from tests.test_workers import _AsyncClosedWith  # noqa: E402


class _BrokerHarness:
    def __init__(self, entry=1.1000, sl=1.0900, tp=None, volume=0.04):
        from app.integrations.brokers import Position
        from tests.test_workers import (
            _AsyncList, _AsyncFloat, _AsyncClosed, _AsyncPartial,
        )
        b = SimpleNamespace()
        b._positions = {"T1": Position(
            ticket="T1", user_id="u1", asset="EURUSD", direction="BUY",
            volume=volume, entry_price=entry, stop_loss=sl, take_profit=tp,
            current_price=entry)}
        b.all_positions = lambda: _AsyncList(list(b._positions.values()))
        b.mark_price = lambda ticket: _AsyncFloat(b._positions.get(
            ticket, Position(ticket="", user_id="", asset="", direction="BUY",
                             volume=0, entry_price=0)).current_price)
        b.quote = lambda asset: _AsyncFloat(0.0)
        b.close_position = lambda ticket: _AsyncClosed()
        b.partial_close = lambda ticket, vol: _AsyncPartial([], vol)
        self.broker = b

    def db(self, **over):
        row = {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
               "direction": "buy", "volume": 0.04, "entry_price": 1.1000,
               "stop_loss": 1.0900, "partial_done": False}
        row.update(over)
        return FakeDatabase(rows={"paper_trades": [row]})


def _settings(**over):
    s = AppSettings(breakeven_trigger_r=1.0, trail_atr_mult=2.0,
                    partial_close_pct=50, partial_trigger_r=1.0,
                    trailing_ladder=True, max_hold_days=5, time_stop_min_r=0.0)
    for k, v in over.items():
        setattr(s, k, v)
    return s


@pytest.fixture(autouse=True)
def _open_market(monkeypatch):
    monkeypatch.setattr(execution, "is_market_closed", lambda *a, **k: False)


@pytest.fixture(autouse=True)
def _spot(monkeypatch):
    async def fake_spot(assets, **_kw):
        return {a: 1.2500 for a in assets}, {}
    monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)


@pytest.mark.asyncio
async def test_mgmt_advisory_skips_partial_be_trail():
    """advisory: no partial, no BE/trail — but the position stays open and
    the SL is untouched (hard protection still armed)."""
    h = _BrokerHarness(volume=0.04)  # SL far below live → no hard hit
    moved: list[float] = []
    partials: list[float] = []
    from tests.test_workers import _AsyncModifySL, _AsyncPartial
    h.broker.modify_stop_loss = lambda t, sl: _AsyncModifySL(moved, sl)
    h.broker.partial_close = lambda t, v: _AsyncPartial(partials, v)
    summary = await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="advisory"))
    assert moved == [] and partials == []
    assert summary["moved_sl"] == 0 and summary["partial_closed"] == 0


@pytest.mark.asyncio
async def test_mgmt_auto_still_runs_management():
    """auto: the same fixture DOES beat/trail — proves the gate is the mode,
    not a broken fixture."""
    h = _BrokerHarness(volume=0.04)
    moved: list[float] = []
    from tests.test_workers import _AsyncModifySL
    h.broker.modify_stop_loss = lambda t, sl: _AsyncModifySL(moved, sl)
    summary = await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="auto"))
    assert summary["moved_sl"] == 1 and moved


@pytest.mark.asyncio
async def test_mgmt_advisory_still_closes_on_hard_sl():
    """INVARIANT: even in advisory mode a HARD SL still closes the position."""
    h = _BrokerHarness(sl=1.3000)  # live 1.2500 ≤ SL → hard hit
    closed: list[str] = []
    h.broker.close_position = lambda t: _AsyncClosedWith(closed, t)
    summary = await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="advisory"))
    assert closed == ["T1"]
    assert summary["closed"] == 1


@pytest.mark.asyncio
async def test_mgmt_advisory_still_closes_on_hard_tp():
    """INVARIANT: a HARD TP also still closes in advisory mode."""
    h = _BrokerHarness(tp=1.2000)  # live 1.2500 ≥ TP → hard hit
    closed: list[str] = []
    h.broker.close_position = lambda t: _AsyncClosedWith(closed, t)
    summary = await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="advisory"))
    assert closed == ["T1"] and summary["closed"] == 1


@pytest.mark.asyncio
async def test_mgmt_advisory_still_fires_emergency_exit(monkeypatch):
    """INVARIANT: the kill switch (Emergency Exit) is NOT discretionary — it
    still fires in advisory mode."""
    monkeypatch.setattr(execution, "evaluate_kill",
                        lambda *a, **k: SimpleNamespace(engaged=True,
                                                        triggers=["dd"]))
    h = _BrokerHarness()
    closed: list[str] = []
    h.broker.close_position = lambda t: _AsyncClosedWith(closed, t)
    summary = await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="advisory"))
    assert closed == ["T1"]
    assert summary["emergency_closed"] == 1


@pytest.mark.asyncio
async def test_mgmt_protective_only_skips_time_stop():
    """protective_only: an aged position is NOT cut by the time stop."""
    h = _BrokerHarness(sl=1.0500)
    closed: list[str] = []
    h.broker.close_position = lambda t: _AsyncClosedWith(closed, t)
    h.broker._positions["T1"].opened_at = \
        datetime.now(timezone.utc) - timedelta(days=10)
    db = h.db(created_at=(datetime.now(timezone.utc)
                          - timedelta(days=10)).isoformat())
    summary = await position_guard.guard_once(
        db, h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="protective_only"))
    assert closed == [] and summary["closed"] == 0
    assert "EURUSD:time_mgmt_off" in summary["skip_assets"]


@pytest.mark.asyncio
async def test_mgmt_auto_time_stop_still_fires():
    """auto: the SAME aged fixture IS cut by the time stop."""
    h = _BrokerHarness(sl=1.0500)
    closed: list[str] = []
    h.broker.close_position = lambda t: _AsyncClosedWith(closed, t)
    h.broker._positions["T1"].opened_at = \
        datetime.now(timezone.utc) - timedelta(days=10)
    db = h.db(created_at=(datetime.now(timezone.utc)
                          - timedelta(days=10)).isoformat())
    summary = await position_guard.guard_once(
        db, h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="auto",
                           time_stop_min_r=0.0))
    assert closed == ["T1"] and summary["closed"] == 1


@pytest.mark.asyncio
async def test_changing_mode_never_loosens_sl():
    """Switching auto → advisory between cycles must NOT move the SL back.
    Auto beats first (SL → breakeven); advisory then does nothing."""
    h = _BrokerHarness()
    moved: list[float] = []
    from tests.test_workers import _AsyncModifySL
    h.broker.modify_stop_loss = lambda t, sl: _AsyncModifySL(moved, sl)
    await position_guard.guard_once(
        h.db(), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="auto"))
    first = moved[0]
    h.broker._positions["T1"].stop_loss = first
    # now advisory — the SL must stay put
    await position_guard.guard_once(
        h.db(stop_loss=first), h.broker, _SilentNotifier(),
        settings=_settings(position_management_mode="advisory"))
    assert all(m >= first - 1e-9 for m in moved)
