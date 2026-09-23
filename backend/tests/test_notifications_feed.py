"""Tests for GET /api/system/notifications — the bell feed in the top-right.

The bell reads the SAME `notifications` table the LINE/Web-Push transports
write to. These tests pin the response shape the frontend depends on:
items[] (id/type/message/status/channel/created_at/sent_at/error), total,
unread (created after the client's last-seen `since`, or the 7-day window
when absent — the schema has no read flag), has_more, server-side paging +
type filter, and the 7-day retention window.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_notifications_feed.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_workers import FakeDatabase


def _row(hours_old: float, ntype: str = "trade_opened",
         channel: str = "line", status: str = "sent") -> dict:
    created = (datetime.now(timezone.utc)
               - timedelta(hours=hours_old)).isoformat()
    return {
        "id": f"n-{hours_old}-{ntype}-{channel}",
        "type": ntype,
        "message": f"🔔 {ntype} test message",
        "status": status,
        "channel": channel,
        "created_at": created,
        "sent_at": created,
        "error": None,
    }


class TestNotificationsFeedEndpoint:
    @pytest.mark.asyncio
    async def test_returns_items_and_counts(self):
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"notifications": [
            _row(0.5, "trade_opened"),
            _row(2, "sl_moved", channel="both"),
            _row(30, "stop_loss"),
        ]})
        set_state(db)
        res = await call("GET", "/api/system/notifications")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "ok"
        assert len(body["items"]) == 3
        assert body["total"] == 3
        # No `since` → unread falls back to the whole 7-day window.
        assert body["unread"] == 3
        assert body["has_more"] is False
        first = body["items"][0]
        assert set(first) == {"id", "type", "message", "status", "channel",
                              "created_at", "sent_at", "error"}

    @pytest.mark.asyncio
    async def test_unread_uses_since_timestamp(self):
        """`since` = the client's last-seen time → only newer rows are unread."""
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"notifications": [
            _row(0.5, "trade_opened"),
            _row(2, "sl_moved"),
            _row(30, "stop_loss"),
        ]})
        set_state(db)
        since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        body = (await call(
            "GET", f"/api/system/notifications?since={since}")).json()
        assert body["unread"] == 1, "only the 0.5h row is newer than `since`"
        assert body["total"] == 3, "`since` must not filter the feed itself"

    @pytest.mark.asyncio
    async def test_rows_older_than_7_days_are_hidden(self):
        """Retention: the feed never returns rows past the 7-day window."""
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"notifications": [
            _row(1, "trade_opened"),
            _row(24 * 8, "stop_loss"),      # 8 days old → hidden
        ]})
        set_state(db)
        body = (await call("GET", "/api/system/notifications")).json()
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["type"] == "trade_opened"

    @pytest.mark.asyncio
    async def test_server_paging_does_not_repeat_rows(self):
        from tests.test_api_routes import call, set_state

        rows = [{**_row(0.1, "trade_opened"), "id": f"n-{i}"} for i in range(7)]
        db = FakeDatabase(rows={"notifications": rows})
        set_state(db)
        page1 = (await call("GET", "/api/system/notifications?limit=5&offset=0")).json()
        assert len(page1["items"]) == 5
        assert page1["total"] == 7
        assert page1["has_more"] is True
        page2 = (await call("GET", "/api/system/notifications?limit=5&offset=5")).json()
        assert len(page2["items"]) == 2
        assert page2["has_more"] is False
        ids1 = {r["id"] for r in page1["items"]}
        ids2 = {r["id"] for r in page2["items"]}
        assert not ids1 & ids2

    @pytest.mark.asyncio
    async def test_type_filter(self):
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"notifications": [
            _row(0.1, "trade_opened"),
            _row(0.1, "sl_moved"),
            _row(0.1, "sl_moved"),
        ]})
        set_state(db)
        body = (await call("GET", "/api/system/notifications?type=sl_moved")).json()
        assert body["total"] == 2
        assert all(r["type"] == "sl_moved" for r in body["items"])

    @pytest.mark.asyncio
    async def test_limit_capped_at_200(self):
        from tests.test_api_routes import call, set_state

        db = FakeDatabase(rows={"notifications": [
            {**_row(0.1), "id": f"n-{i}"} for i in range(250)]})
        set_state(db)
        body = (await call("GET", "/api/system/notifications?limit=9999")).json()
        assert body["limit"] == 200
        assert len(body["items"]) == 200

    @pytest.mark.asyncio
    async def test_empty_table_is_ok(self):
        from tests.test_api_routes import call, set_state

        set_state(FakeDatabase())
        body = (await call("GET", "/api/system/notifications")).json()
        assert body["verdict"] == "ok"
        assert body["items"] == []
        assert body["total"] == 0
        assert body["unread"] == 0

    @pytest.mark.asyncio
    async def test_unavailable_db_reports_fail(self):
        from app.main import app
        from tests.test_api_routes import call

        class Dead:
            available = False
            init_error = "no env"
        app.state.db = Dead()
        res = await call("GET", "/api/system/notifications")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "fail"
        assert body["items"] == []
        assert body["unread"] == 0


class TestNotificationRetention:
    """purge_old_notifications — the 7-day TTL for the bell feed."""

    def test_purges_rows_older_than_7_days(self):
        from app.services import notification_service as ns

        db = FakeDatabase(rows={"notifications": [
            _row(1, "trade_opened"),
            _row(24 * 8, "stop_loss"),
            _row(24 * 30, "risk_warning"),
        ]})
        deleted = ns.purge_old_notifications(db, force=True)
        assert deleted == 2
        remaining = db.rows["notifications"]
        assert len(remaining) == 1
        assert remaining[0]["type"] == "trade_opened"

    def test_keeps_rows_inside_the_window(self):
        from app.services import notification_service as ns

        db = FakeDatabase(rows={"notifications": [
            _row(1, "trade_opened"),
            _row(24 * 6, "sl_moved"),
        ]})
        assert ns.purge_old_notifications(db, force=True) == 0
        assert len(db.rows["notifications"]) == 2

    def test_throttled_without_force(self):
        from app.services import notification_service as ns

        db = FakeDatabase(rows={"notifications": [_row(24 * 30)]})
        ns._last_purge = 0.0
        assert ns.purge_old_notifications(db) == 1, "first call runs"
        # Second call within the interval is skipped (row already gone).
        db.rows["notifications"] = [_row(24 * 30)]
        assert ns.purge_old_notifications(db) == 0, "throttled"
        assert len(db.rows["notifications"]) == 1

    def test_unavailable_db_is_safe(self):
        from app.services import notification_service as ns

        class Dead:
            available = False
        assert ns.purge_old_notifications(Dead(), force=True) == 0

    @pytest.mark.asyncio
    async def test_log_maintenance_purges_notifications(self):
        from app.workers import log_maintenance

        db = FakeDatabase(rows={"notifications": [
            _row(1, "trade_opened"),
            _row(24 * 9, "stop_loss"),
        ]})
        out = await log_maintenance.run_once(db)
        assert out["purged"]["notifications"] == 1
        assert len(db.rows["notifications"]) == 1
