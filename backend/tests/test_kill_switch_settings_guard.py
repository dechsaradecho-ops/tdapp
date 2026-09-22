"""Regression tests for the 2026-09-22 Emergency-Exit incident.

Incident (prod 2026-09-22 02:12 UTC): the owner had configured
``max_drawdown_pct = 15`` but the Emergency Exit (kill switch) closed 6
positions quoting ``Drawdown 13.25% > 10%`` — the SCHEMA DEFAULT, not the
owner's setting — and it closed WITHOUT first notifying and waiting for
confirmation. Two bugs, one shared root cause:

  A. ``settings._load_settings`` / the guard fell back to ``AppSettings()``
     defaults on ANY read error, and ``AppSettings.max_drawdown_pct`` defaults
     to 10.0. A swallowed settings read therefore gave ``evaluate_kill`` the
     default 10% instead of 15% → a breach that should not exist.
  B. The confirmation prompt was created ONLY by the monitor's breach branch,
     which uses a DIFFERENT drawdown definition (PortfolioSnapshot/RiskEngine)
     than the guard (evaluate_kill/equity_snapshots). The monitor reported
     "no breach" (13.25% < 15%) while the guard was "engaged" (13.25% > 10%),
     so no request ever existed, ``emergency_hold`` returned None, and the
     guard closed unprompted.

Owner's rule to preserve: "1.ตั้ง drawdown ไว้ 15  2.ต้องแจ้งเตือนแล้วรอ user
confirm ก่อนตามระบบก่อนหน้านี้".

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_kill_switch_settings_guard.py -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.api.routes import settings as settings_router
from app.models.schemas import AppSettings
from app.services import execution, limit_expand
from app.workers import portfolio_monitor, position_guard
from tests.test_limit_expand import (
    RecordingNotifier,
    _GUARD_SETTINGS,
    _ask_the_owner,
    _book,
    _daily_loss_db,
    _guard,
    _marks,          # noqa: F401  (pytest fixture)
    _open_trade_row,
)


@pytest.fixture(autouse=True)
def _fresh_fail_notices():
    limit_expand._FAIL_NOTICES.clear()
    yield
    limit_expand._FAIL_NOTICES.clear()


class _SettingsReadFails:
    """Database shim that RAISES on the trading_settings read.

    Simulates the 2026-09-22 condition: the settings row could not be read
    (client hiccup / transient error), which used to silently degrade to the
    schema defaults and hand the kill switch the default 10% drawdown limit.

    ``_load_settings`` reads through ``db._client.table(...)`` directly, so the
    shim must make THAT path raise while every other table keeps working.
    """

    def __init__(self, db):
        self._db = db
        self._client = _TableBreakingClient(db._client)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def select(self, table, **kw):
        if table == settings_router.SETTINGS_TABLE:
            raise RuntimeError("transient settings read failure")
        return self._db.select(table, **kw)


class _TableBreakingClient:
    """Wraps a PostgREST client so table('trading_settings') raises."""

    def __init__(self, client):
        self._client = client

    def table(self, name, *a, **k):
        if name == settings_router.SETTINGS_TABLE:
            raise RuntimeError("transient settings read failure")
        return self._client.table(name, *a, **k)

    def __getattr__(self, name):
        return getattr(self._client, name)


# ---------------------------------------------------------------------------
# Bug A — the configured limit must be authoritative; a failed read must NOT
# degrade to the 10% default.
# ---------------------------------------------------------------------------
def test_try_load_returns_none_when_the_read_fails():
    """Strict loader distinguishes 'no row' (defaults) from 'read FAILED'."""
    db = _daily_loss_db()
    assert settings_router.try_load_settings(db) is not None   # healthy read

    class Boom:
        available = True
        _client = None                       # .table raises AttributeError

    assert settings_router.try_load_settings(Boom()) is None


def test_strict_loader_still_returns_defaults_for_an_absent_row():
    """A reachable DB with no settings row is legitimate first-run → defaults."""
    db = _daily_loss_db()

    class NoRow:
        available = True

        class _Table:
            def select(self, *a, **k):
                return self

            def eq(self, *a, **k):
                return self

            def limit(self, *a, **k):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        _client = SimpleNamespace(table=lambda *a, **k: NoRow._Table())

    loaded = settings_router.try_load_settings(NoRow())
    assert loaded is not None
    assert loaded.max_drawdown_pct == 10.0     # documented default, not None


def test_unconfirmed_settings_refuse_to_quote_a_breach():
    """evaluate_kill(settings_confirmed=False) must not compare against 10%."""
    from tests.test_limit_expand import _dd_db

    # 13.25% drawdown — the incident figure: above the default 10%, below the
    # owner's configured 15%.
    db = _dd_db(equity=8675.0, peak=10000.0)
    dd = execution.equity_drawdown_pct(db, 10000.0)
    assert 13.0 < dd < 13.5

    # With the owner's real limit the account is NOT breached (13.25 < 15).
    assert execution.evaluate_kill(
        db, AppSettings(max_drawdown_pct=15.0)).engaged is False

    # With the SCHEMA DEFAULT it WOULD be (13.25 > 10) — the bug's comparison.
    assert execution.evaluate_kill(db, AppSettings()).engaged is True

    # The fail-loud path refuses even to make that comparison: it returns an
    # explicit "cannot confirm" status instead of quoting the default.
    unconfirmed = execution.evaluate_kill(
        db, AppSettings(), settings_confirmed=False)
    assert unconfirmed.engaged is True         # fail-safe: still "engaged"
    assert any("settings unreadable" in t for t in unconfirmed.triggers)


def test_guard_holds_instead_of_closing_when_settings_are_unreadable(_marks):
    """The incident condition: settings read fails + kill engaged + no request.

    Before the fix the guard evaluated against the DEFAULT 10% and closed.
    Now it refuses to act on a guessed limit and DEFERS (no close, no prompt
    quoting a number it cannot vouch for).
    """
    db = _daily_loss_db()
    db.rows["paper_trades"].append(_open_trade_row())
    broker, closed = _book()
    notifier = RecordingNotifier()

    out = asyncio.run(position_guard.guard_once(
        _SettingsReadFails(db), broker, notifier, settings=None))

    assert out["emergency_closed"] == 0
    assert out["emergency_held"] == 1
    assert closed == []
    assert notifier.of("trade_closed") == []
    # No prompt either: quoting the default 10% to the owner would be a lie.
    assert notifier.of("limit_expand") == []


def test_guard_uses_the_configured_limit_and_stays_open_at_13_25(_marks):
    """13.25% drawdown with 15% configured → no breach → no close (the case)."""
    from tests.test_limit_expand import _dd_db

    # 13.25% dd: peak 10000, equity 8675 (matches the incident's 13.25%).
    # _dd_db carries NO daily-loss row, so drawdown is the ONLY trigger.
    db = _dd_db(equity=8675.0, peak=10000.0)
    db.rows.setdefault("paper_trades", []).append(_open_trade_row())
    broker, closed = _book()
    notifier = RecordingNotifier()
    s = AppSettings(max_drawdown_pct=15.0, smart_exit_enabled=False)

    out = asyncio.run(position_guard.guard_once(
        db, broker, notifier, settings=s))

    val = execution.equity_drawdown_pct(db, float(s.capital))
    assert 13.0 < val < 13.5                   # the incident's drawdown
    assert out["emergency_closed"] == 0        # 13.25% < 15% → never closed
    assert out["emergency_held"] == 0
    assert closed == []


# ---------------------------------------------------------------------------
# Bug B — the guard raises its OWN prompt when it is engaged but no request
# exists, so the owner is ALWAYS asked before any emergency close.
# ---------------------------------------------------------------------------
def test_guard_raises_a_prompt_and_defers_when_none_exists(_marks):
    """kill engaged + NO pending request → ask the owner, do NOT close.

    This is the missing step on 2026-09-22: the monitor never created a request
    (it saw no breach), so the guard closed unprompted. Now the guard creates
    the request itself and holds.
    """
    db = _daily_loss_db()                      # daily loss breach (3% > 2%)
    db.rows["paper_trades"].append(_open_trade_row())
    broker, closed = _book()
    notifier = RecordingNotifier()

    out = asyncio.run(_guard(db, broker, notifier))

    assert out["emergency_closed"] == 0 and out["emergency_held"] == 1
    assert closed == []
    # a request now exists in the DB, and the owner got the Approve/Reject push
    rows = db.rows.get("kill_expand_requests") or []
    assert len(rows) == 1 and rows[0]["status"] == "pending"
    prompt = notifier.of("limit_expand")
    assert len(prompt) == 1
    assert prompt[0]["quick_reply"]           # actionable, not just a warning


def test_guard_prompt_quotes_the_same_numbers_as_the_close(_marks):
    """The prompt's numbers come from kill_metrics — the switch's own math."""
    db = _daily_loss_db()
    db.rows["paper_trades"].append(_open_trade_row())
    broker, closed = _book()
    notifier = RecordingNotifier()

    asyncio.run(_guard(db, broker, notifier))

    row = db.rows["kill_expand_requests"][0]
    daily, _w, _m, _dd = execution.kill_metrics(
        db, float(_GUARD_SETTINGS.capital))
    assert row["metric_value"] == pytest.approx(round(daily, 4))
    assert row["limit_before"] == 2.0
    assert row["limit_after"] == 7.0           # +5pp


def test_guard_does_not_double_prompt_when_the_monitor_already_asked(_marks):
    """A request already pending (monitor raised it) → guard holds, no 2nd push."""
    db = _daily_loss_db()
    db.rows["paper_trades"].append(_open_trade_row())
    _ask_the_owner(db)                         # the monitor got there first
    broker, closed = _book()
    notifier = RecordingNotifier()

    out = asyncio.run(position_guard.guard_once(
        db, broker, notifier, settings=_GUARD_SETTINGS))

    assert out["emergency_closed"] == 0 and out["emergency_held"] == 1
    assert closed == []
    assert notifier.sent == []                 # no duplicate prompt
    assert len(db.rows["kill_expand_requests"]) == 1


# ---------------------------------------------------------------------------
# Bug C — the monitor bridges to the shared kill evaluation, so a monitor
# "no breach" can no longer coexist with an engaged guard.
# ---------------------------------------------------------------------------
def test_monitor_bridge_pauses_when_the_kill_switch_is_engaged(monkeypatch):
    """Merge point: a monitor snapshot that looks FINE + an engaged kill
    switch → the monitor still pauses and raises the prompt.

    On 2026-09-22 the monitor's PortfolioSnapshot said "no breach" while the
    guard's ``evaluate_kill`` was engaged, so no prompt was ever created. The
    bridge makes the monitor consult the SAME evaluation the guard uses. Here
    the monitor's own snapshot is forced healthy so the ONLY reason to pause
    is the bridge.
    """
    from tests.test_limit_expand import _dd_db
    from app.services import execution as _exec
    from app.api.routes.settings import persist_settings

    db = _dd_db(equity=8000.0, peak=10000.0)   # REAL 20% dd for the prompt path
    # The monitor writes today's equity as `capital + realized pnl` (it does
    # NOT read the broker book for the total), so pin capital to 8000 — the
    # snapshot write then preserves the 20% drawdown instead of erasing it.
    assert persist_settings(db, AppSettings(capital=8000.0))

    # Make the monitor's own RiskEngine agree there is nothing to pause for,
    # so `status.trading_paused` is False and the bridge is the sole trigger.
    class _CalmStatus:
        trading_paused = False
        message = "ok"
        current_drawdown_pct = 0.0
        max_drawdown_pct = 10.0
        expected_drawdown_pct = 10.0
        expected_daily_loss_pct = 2.0
        open_risk_pct = 0.0

        def model_dump(self):
            return {}

    class _CalmEngine:
        def check(self, _snap):
            return _CalmStatus()

    monkeypatch.setattr(portfolio_monitor, "risk_engine_for_settings",
                        lambda _s: _CalmEngine())

    # The kill switch (shared definition) IS engaged — this is the divergence.
    def _engaged(db_, s_, **kw):
        return _exec.KillSwitchStatus(
            engaged=True, triggers=["Drawdown 20.00% > 10%"])

    monkeypatch.setattr(_exec, "evaluate_kill", _engaged)

    class _Broker:
        # monitor_once is SYNC and calls these synchronously.
        def all_positions(self):
            return []

        def account_summary(self):
            # Book equity matches the drawdown, so the monitor's own snapshot
            # write cannot erase it before the prompt path reads the table.
            return SimpleNamespace(equity=8000.0)

    # monitor_once is a plain (non-async) function.
    out = portfolio_monitor.monitor_once(db, _Broker(), RecordingNotifier())

    assert out["breach"] is True, "the bridge must turn an engaged switch into a breach"
    assert out["kill_bridge"] is True
    rows = db.rows.get("kill_expand_requests") or []
    assert len(rows) == 1 and rows[0]["status"] == "pending"
