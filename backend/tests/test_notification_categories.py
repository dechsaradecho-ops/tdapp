"""Notification category toggle tests.

Covers:
  1. category_enabled() mapping — each ntype resolves to its settings field
  2. NotificationService.notify skips queue + push for disabled categories
  3. dispatch_pending marks queued rows of disabled categories as 'skipped'
  4. PUT /api/settings accepts and persists notify_* booleans (merge-patch)

Run from backend/: C:/Python314/python.exe -m pytest tests/test_notification_categories.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.main import app
from app.models.schemas import AppSettings
from app.services.notification_service import (
    NotificationService,
    category_enabled,
)
from tests.test_workers import FakeDatabase
from tests.test_settings import SettingsDatabase, set_state


# ---------------------------------------------------------------------------
# 1. category_enabled mapping
# ---------------------------------------------------------------------------
def test_category_enabled_defaults_all_on():
    s = AppSettings()
    for ntype in ("trade_opened", "trade_closed", "stop_loss", "risk_warning",
                  "daily_digest", "daily_portfolio_summary",
                  "daily_market_summary", "weekly_report", "monthly_report"):
        assert category_enabled(s, ntype) is True


def test_category_enabled_unknown_type_defaults_on():
    assert category_enabled(AppSettings(), "semi_auto_approval") is True
    assert category_enabled(None, "trade_opened") is True


def test_category_enabled_respects_each_field():
    s = AppSettings(notify_trade_opened=False)
    assert category_enabled(s, "trade_opened") is False
    assert category_enabled(s, "trade_closed") is True

    s = AppSettings(notify_daily_summary=False)
    assert category_enabled(s, "daily_portfolio_summary") is False
    assert category_enabled(s, "daily_market_summary") is False
    assert category_enabled(s, "weekly_report") is False
    assert category_enabled(s, "monthly_report") is False
    assert category_enabled(s, "trade_opened") is True


# ---------------------------------------------------------------------------
# 2. NotificationService.notify honours the switch
# ---------------------------------------------------------------------------
class FakeLine:
    def __init__(self):
        self.pushed: list[tuple[str, str]] = []

    async def push(self, target_id: str, message: str) -> bool:
        self.pushed.append((target_id, message))
        return True


def _service(db, line):
    return NotificationService(db, line)


def test_notify_skips_disabled_category():
    db = SettingsDatabase()  # no settings row → defaults (all on)
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)

    # Turn trade_opened off via the settings row
    db._client.upsert({"id": 1, "notify_trade_opened": False})

    asyncio.run(svc.notify("u1", "trade_opened", "opened!"))
    assert line.pushed == []                      # no push
    assert db.inserted == []                      # no queue row either


def test_notify_still_works_for_enabled_category():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "trade_closed", "closed!", critical=True))
    assert len(line.pushed) == 1                  # immediate critical push
    assert db.inserted and db.inserted[0][0] == "notifications"


def test_notify_critical_type_respects_category_switch():
    """stop_loss is CRITICAL (immediate push) but must still honour the switch."""
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]
    db._client.upsert({"id": 1, "notify_stop_loss": False})

    asyncio.run(svc.notify("u1", "stop_loss", "SL hit", critical=True))
    assert line.pushed == []
    assert db.inserted == []


# ---------------------------------------------------------------------------
# 2b. risk_warning cooldown — monitor runs every 1 min; a standing breach
# used to push an identical LINE alert every minute.
# ---------------------------------------------------------------------------
def test_risk_warning_cooldown_blocks_second_alert_within_30min():
    from app.services.notification_service import RISK_WARNING_COOLDOWN_MIN
    assert RISK_WARNING_COOLDOWN_MIN == 30.0

    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "risk_warning", "breach #1", critical=True))
    assert len(line.pushed) == 1                    # first alert goes out

    asyncio.run(svc.notify("u1", "risk_warning", "breach #2", critical=True))
    assert len(line.pushed) == 1                    # second within 30 min → held
    # only ONE queue row: the suppressed notify never inserts
    assert len([r for t, r in db.inserted
                if t == "notifications"]) == 1


def test_risk_warning_resumes_after_cooldown_window():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    # seed an old risk_warning row (31 min ago) → outside the cooldown
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=31)).isoformat()
    db.rows["notifications"] = [{
        "id": "old-1", "user_id": "u1", "channel": "line",
        "type": "risk_warning", "message": "earlier",
        "status": "sent", "created_at": old,
    }]

    asyncio.run(svc.notify("u1", "risk_warning", "breach now", critical=True))
    assert len(line.pushed) == 1                    # window passed → delivered


def test_risk_warning_cooldown_fail_open_on_db_error():
    """Cooldown lookup breaks → send anyway (a repeated risk alert beats a
    swallowed one). The failure is scoped to the notifications table so the
    rest of the notify path still works, mirroring a partial DB issue."""

    class BoomDB(SettingsDatabase):
        def select(self, table, *a, **k):
            if table == "notifications":
                raise RuntimeError("db down")
            return super().select(table, *a, **k)

    db = BoomDB()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "risk_warning", "breach", critical=True))
    assert len(line.pushed) == 1


# ---------------------------------------------------------------------------
# 2c. sl_moved cooldown — a trailing stop ratchets every guard cycle, so one
# trending pair used to push an identical "SL ขยับ" alert every ~1 minute
# (prod 2026-09-23: GBPCHF/EURCHF moved SL 7× in a row). Scoped PER ASSET.
# ---------------------------------------------------------------------------
def test_sl_moved_cooldown_blocks_same_asset_within_window():
    from app.services.notification_service import SL_MOVE_COOLDOWN_MIN
    assert SL_MOVE_COOLDOWN_MIN == 20.0

    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    msg = "🔔 SL ขยับ (trailing)\nAsset: GBPCHF\nDirection: long\nSL 1 → 2"
    asyncio.run(svc.notify("u1", "sl_moved", msg, asset="GBPCHF"))
    assert len(line.pushed) == 1                    # first move goes out

    asyncio.run(svc.notify("u1", "sl_moved", msg, asset="GBPCHF"))
    assert len(line.pushed) == 1                    # same asset within 20 min → held
    assert len([r for t, r in db.inserted
                if t == "notifications"]) == 1


def test_sl_moved_cooldown_is_per_asset():
    """A move on GBPCHF must NOT silence a move on EURCHF."""
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify(
        "u1", "sl_moved", "🔔 SL ขยับ\nAsset: GBPCHF\nSL 1 → 2", asset="GBPCHF"))
    asyncio.run(svc.notify(
        "u1", "sl_moved", "🔔 SL ขยับ\nAsset: EURCHF\nSL 1 → 2", asset="EURCHF"))
    assert len(line.pushed) == 2                    # different asset → delivered


def test_sl_moved_resumes_after_cooldown_window():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    old = (datetime.now(timezone.utc) - timedelta(minutes=21)).isoformat()
    db.rows["notifications"] = [{
        "id": "old-sl", "user_id": "u1", "channel": "line",
        "type": "sl_moved", "message": "🔔 SL ขยับ\nAsset: GBPCHF\nSL 1 → 2",
        "status": "sent", "created_at": old,
    }]

    asyncio.run(svc.notify(
        "u1", "sl_moved", "🔔 SL ขยับ\nAsset: GBPCHF\nSL 2 → 3", asset="GBPCHF"))
    assert len(line.pushed) == 1                    # window passed → delivered


def test_sl_moved_maps_to_notify_stop_loss_switch():
    """sl_moved shares the notify_stop_loss switch — turning it off silences
    both real stop-outs and SL moves."""
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]
    db._client.upsert({"id": 1, "notify_stop_loss": False})

    asyncio.run(svc.notify(
        "u1", "sl_moved", "🔔 SL ขยับ\nAsset: GBPCHF", asset="GBPCHF"))
    assert line.pushed == []
    assert db.inserted == []


# ---------------------------------------------------------------------------
# 2d. truthful channel — the column used to be hardcoded 'line' even when
# only Web Push reached the user.
# ---------------------------------------------------------------------------
class FakeWebPush:
    """push_web returns a fixed bool; records the calls."""

    def __init__(self, ok: bool):
        self.ok = ok
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, ntype: str, message: str) -> bool:
        self.calls.append((ntype, message))
        return self.ok


def _channel_of(db) -> str:
    rows = [r for t, r in db.inserted if t == "notifications"]
    assert rows, "no notification row queued"
    return rows[-1].get("channel")


def test_channel_both_when_line_and_push_deliver():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    svc.push_web = FakeWebPush(True)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "trade_closed", "closed!", critical=True))
    assert _channel_of(db) == "both"


def test_channel_web_push_when_only_push_delivers():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    svc.push_web = FakeWebPush(True)
    # no line_targets → push_line finds nothing → ok=False
    asyncio.run(svc.notify("u1", "trade_closed", "closed!", critical=True))
    assert _channel_of(db) == "web_push"


def test_channel_line_when_only_line_delivers():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    svc.push_web = FakeWebPush(False)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "trade_closed", "closed!", critical=True))
    assert _channel_of(db) == "line"


# ---------------------------------------------------------------------------
# 3. dispatch_pending marks disabled-category rows as skipped
# ---------------------------------------------------------------------------
def test_dispatch_pending_skips_disabled_category():
    from app.workers.notification_worker import dispatch_pending

    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["notifications"] = [
        {"id": "n1", "user_id": "u1", "type": "trade_opened",
         "message": "opened", "status": "pending"},
        {"id": "n2", "user_id": "u1", "type": "trade_closed",
         "message": "closed", "status": "pending"},
    ]
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]
    db._client.upsert({"id": 1, "notify_trade_opened": False})

    sent = asyncio.run(dispatch_pending(db, svc))
    assert sent == 1                              # only trade_closed delivered
    rows = {r["id"]: r for r in db.rows["notifications"]}
    assert rows["n1"]["status"] == "skipped"
    assert rows["n2"]["status"] == "sent"


# ---------------------------------------------------------------------------
# 3b. LINE delivery fix (prod 2026-09-07): trade_opened / trade_closed were
# never delivered — the queue rejected the pseudo-user "demo" (uuid cast),
# dispatch skipped empty-user rows, and push_line filtered line_users by the
# same bogus uuid.
# ---------------------------------------------------------------------------
class UuidCastDB(SettingsDatabase):
    """Mimics Postgres: notifications.user_id is a uuid FK — any non-uuid
    string ('demo', 'u1') makes the insert fail; NULL passes."""

    def insert(self, table, row):
        if table == "notifications" and row.get("user_id") not in (None, ""):
            return None  # Database.insert swallows the error
        return super().insert(table, row)


def test_trade_opened_pushes_immediately_and_marks_queue_sent():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "trade_opened", "opened!"))
    assert len(line.pushed) == 1                  # immediate push, no worker wait
    rows = [r for t, r in db.inserted if t == "notifications"]
    assert rows and rows[0]["status"] == "sent"   # worker must NOT re-deliver


def test_trade_closed_pushes_immediately():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    asyncio.run(svc.notify("u1", "trade_closed", "closed!"))
    assert len(line.pushed) == 1


def test_failed_immediate_push_stays_pending_for_worker_retry():
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    # no line_targets / line_users → push reaches nobody → ok=False

    asyncio.run(svc.notify("u1", "trade_opened", "opened!"))
    assert line.pushed == []
    rows = [r for t, r in db.inserted if t == "notifications"]
    assert rows and rows[0]["status"] == "pending"  # worker retries later


def test_queue_notification_retries_without_bogus_user_id():
    """The uuid-cast failure must not kill the row — retry once with
    user_id stripped (NULL = broadcast to every enabled chat)."""
    db = UuidCastDB()
    app.state.db = db

    from app.services.database import queue_notification
    queue_notification(db, "demo", "trade_opened", "opened!")

    rows = [r for t, r in db.inserted if t == "notifications"]
    assert len(rows) == 1                         # retry succeeded
    assert rows[0]["user_id"] is None             # bogus uuid stripped
    assert rows[0]["status"] == "pending"


def test_dispatch_pending_delivers_broadcast_rows():
    """Rows with a NULL user_id are delivered, not skipped."""
    from app.workers.notification_worker import dispatch_pending

    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["notifications"] = [
        {"id": "n1", "user_id": None, "type": "trade_opened",
         "message": "opened", "status": "pending"},
    ]
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    sent = asyncio.run(dispatch_pending(db, svc))
    assert sent == 1
    assert db.rows["notifications"][0]["status"] == "sent"


def test_push_line_ignores_user_id_filter():
    """push_line must reach enabled chats even with the pseudo-user id —
    the old eq-filter on line_users.user_id silently matched nothing."""
    db = SettingsDatabase()
    app.state.db = db
    line = FakeLine()
    svc = _service(db, line)
    db.rows["line_users"] = [
        {"id": "lu1", "user_id": "not-a-uuid", "line_user_id": "U-me",
         "notification_enabled": True},
        {"id": "lu2", "user_id": "not-a-uuid", "line_user_id": "U-off",
         "notification_enabled": False},
    ]
    db.rows["line_targets"] = [
        {"target_id": "grp1", "notification_enabled": True}]

    ok = asyncio.run(svc.push_line("demo", "hello"))
    assert ok is True
    assert {t for t, _ in line.pushed} == {"U-me", "grp1"}  # disabled skipped


# ---------------------------------------------------------------------------
# 4. Settings API round-trip for notify_* booleans
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_settings_api_roundtrip_notify_fields():
    from tests.test_settings import FakeSettingsClient, SettingsDatabase

    db = SettingsDatabase()
    set_state(db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.put("/api/settings", json={"notify_trade_opened": False,
                                               "notify_daily_digest": False})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert body["settings"]["notify_trade_opened"] is False
        assert body["settings"]["notify_daily_digest"] is False
        assert body["settings"]["notify_trade_closed"] is True  # untouched

        # GET reflects the saved row
        r2 = await c.get("/api/settings")
        assert r2.json()["notify_trade_opened"] is False

        # merge-patch: absent keys keep stored value
        r3 = await c.put("/api/settings", json={"capital": 12_000})
        s3 = r3.json()["settings"]
        assert s3["capital"] == 12_000
        assert s3["notify_trade_opened"] is False


@pytest.mark.asyncio
async def test_settings_api_rejects_unknown_notify_field():
    db = SettingsDatabase()
    set_state(db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.put("/api/settings", json={"notify_bogus": False})
        assert r.status_code == 200
        # unknown key filtered out by _FIELDS — stored value unchanged
        assert r.json()["settings"]["notify_trade_opened"] is True
