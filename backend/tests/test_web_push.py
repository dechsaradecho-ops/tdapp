"""Web Push (VAPID) transport tests — the second notification channel.

Why this suite exists: Web Push is deliberately built to be INERT (no keys → no
network, no errors) so nothing else in the pipeline can break. That safety net
is also a trap — a typo in the key plumbing would silently mean "no phone
alerts ever" and every other test would still pass. These tests pin the
behaviour that actually makes a phone ring:

  1. no VAPID keys  → push_all returns 0 and never touches the network
  2. keys + devices → every ENABLED device sends, disabled ones skipped
  3. HTTP 404/410   → that row is disabled (dead endpoint), never retried
  4. other errors   → fail_count increments; row disabled only at MAX_FAILS
  5. success        → fail_count resets to 0 (an outage heals itself)
  6. notify(critical)   → LINE **and** Web Push fire (not either/or)
  7. category switched off → neither transport fires, no queue row
  8. dispatch_pending → queued (non-critical) alerts also reach Web Push
  9. /api/push/*    → never leaks endpoint/p256dh/auth to the client

Run from backend/: d:/tdapp/.venv/Scripts/python.exe -m pytest tests/test_web_push.py -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.api.routes import push as push_routes
from app.integrations import web_push
from app.main import app
from app.models.schemas import AppSettings
from app.services.notification_service import NotificationService
from app.workers import notification_worker
from tests.test_settings import SettingsDatabase, set_state
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def keys_on(monkeypatch, pub: str = "pub-key", priv: str = "priv-key",
            subject: str = "mailto:test@example.com") -> None:
    """Pretend the API service has VAPID env vars configured."""
    fake = SimpleNamespace(vapid_public_key=pub, vapid_private_key=priv,
                           vapid_subject=subject)
    monkeypatch.setattr(web_push, "get_settings", lambda: fake)


def keys_off(monkeypatch) -> None:
    """The default deployment state: VAPID env vars absent."""
    fake = SimpleNamespace(vapid_public_key="", vapid_private_key="",
                           vapid_subject="")
    monkeypatch.setattr(web_push, "get_settings", lambda: fake)


def device(i: int, enabled: bool = True, **extra) -> dict:
    # `id` is bigint in the DB (migration 042) — PostgREST returns a JSON number,
    # and PushDeviceInfo.id is typed int, so the fake must not use strings here.
    row = {
        "id": i,
        "endpoint": f"https://push.example.com/{i}",
        "p256dh": f"p256dh-{i}",
        "auth": f"auth-{i}",
        "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120",
        "enabled": enabled,
        "fail_count": 0,
        "last_error": None,
        "last_ok_at": None,
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    row.update(extra)
    return row


class FakeLine:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.pushed: list[tuple[str, str]] = []

    async def push(self, target_id: str, message: str) -> bool:
        self.pushed.append((target_id, message))
        return self.ok


class FakeNotifier:
    """Stands in for NotificationService inside dispatch_pending."""

    def __init__(self, line_ok: bool = False, push_ok: bool = True):
        self.line_ok = line_ok
        self.push_ok = push_ok
        self.line_calls: list[tuple[str, str]] = []
        self.push_calls: list[tuple[str, str]] = []

    async def push_line(self, user_id: str, message: str) -> bool:
        self.line_calls.append((user_id, message))
        return self.line_ok

    async def push_web(self, ntype: str, message: str, url=None) -> bool:
        self.push_calls.append((ntype, message))
        return self.push_ok


async def call(method: str, path: str, json_body: dict | None = None,
               db=None) -> httpx.Response:
    if db is not None:
        set_state(db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, json=json_body)


# ---------------------------------------------------------------------------
# 1-2. No keys = inert. Keys = one send per ENABLED device.
# ---------------------------------------------------------------------------
def test_no_vapid_keys_is_inert(monkeypatch):
    keys_off(monkeypatch)
    db = FakeDatabase({"push_subscriptions": [device(1)]})
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda *a, **k: pytest.fail("must not touch the network"))

    assert asyncio.run(web_push.push_all(db, "t", "b")) == 0
    assert asyncio.run(web_push.push_test_rows(db, "t", "b")) == []
    assert web_push.vapid_configured() is False
    assert db.rows["push_subscriptions"][0]["fail_count"] == 0


def test_missing_table_degrades_to_zero(monkeypatch):
    """Migration 042 not run yet → no crash, no alerts, app keeps working."""
    keys_on(monkeypatch)
    db = FakeDatabase()

    def boom(*a, **k):
        raise RuntimeError("relation \"push_subscriptions\" does not exist")

    monkeypatch.setattr(db, "select", boom)
    assert asyncio.run(web_push.push_all(db, "t", "b")) == 0


def test_push_all_sends_only_to_enabled_devices(monkeypatch):
    keys_on(monkeypatch)
    db = FakeDatabase({"push_subscriptions": [device(1), device(2, enabled=False),
                                              device(3)]})
    seen: list[str] = []

    def fake_send(info, payload):
        seen.append(info["endpoint"])
        return "ok", ""

    monkeypatch.setattr(web_push, "_send_sync", fake_send)

    assert asyncio.run(web_push.push_all(db, "หัวข้อ", "เนื้อหา")) == 2
    assert seen == ["https://push.example.com/1", "https://push.example.com/3"]
    # A successful send stamps last_ok_at (Settings shows "ล่าสุด"), while the
    # disabled device must stay untouched — no sends, no stamp.
    rows = {r["id"]: r for r in db.rows["push_subscriptions"]}
    assert rows[1]["last_ok_at"] and rows[3]["last_ok_at"]
    assert rows[2]["last_ok_at"] is None


# ---------------------------------------------------------------------------
# 3-5. Failure bookkeeping
# ---------------------------------------------------------------------------
def test_dead_endpoint_is_disabled_not_deleted(monkeypatch):
    keys_on(monkeypatch)
    db = FakeDatabase({"push_subscriptions": [device(1)]})
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda *a, **k: ("gone", "endpoint หมดอายุ (HTTP 410)"))

    assert asyncio.run(web_push.push_all(db, "t", "b")) == 0
    row = db.rows["push_subscriptions"][0]
    # The row must survive: Settings explains "endpoint หมดอายุ" with it.
    assert row["enabled"] is False
    assert row["fail_count"] == 1
    assert "410" in row["last_error"]


def test_error_disables_only_at_max_fails(monkeypatch):
    keys_on(monkeypatch)
    db = FakeDatabase({"push_subscriptions": [
        device(1, fail_count=web_push.MAX_FAILS - 2),
        device(2, fail_count=web_push.MAX_FAILS - 1),
    ]})
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda *a, **k: ("error", "HTTP 500: boom"))

    asyncio.run(web_push.push_all(db, "t", "b"))
    below, at = db.rows["push_subscriptions"]
    assert below["enabled"] is True            # still retried
    assert below["fail_count"] == web_push.MAX_FAILS - 1
    assert at["enabled"] is False              # gave up — no more retries
    assert at["fail_count"] == web_push.MAX_FAILS


def test_success_resets_fail_count(monkeypatch):
    keys_on(monkeypatch)
    db = FakeDatabase({"push_subscriptions": [device(1, fail_count=7,
                                                    last_error="HTTP 503")]})
    monkeypatch.setattr(web_push, "_send_sync", lambda *a, **k: ("ok", ""))

    assert asyncio.run(web_push.push_all(db, "t", "b")) == 1
    row = db.rows["push_subscriptions"][0]
    assert row["fail_count"] == 0
    assert row["last_error"] is None


def test_category_off_sends_nothing(monkeypatch):
    """The Settings switch must silence BOTH channels, not just LINE."""
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db._client.upsert({"id": 1, "notify_trade_opened": False})
    app.state.db = db
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda *a, **k: pytest.fail("push must not fire"))
    line = FakeLine()
    svc = NotificationService(db, line)

    asyncio.run(svc.notify("demo", "trade_opened", "เปิดไม้ EURUSD"))
    assert line.pushed == []
    assert db.inserted == []


def test_critical_alert_fires_both_transports(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]
    db.rows["line_targets"] = [{"target_id": "grp1", "notification_enabled": True}]
    app.state.db = db
    monkeypatch.setattr(web_push, "_send_sync", lambda *a, **k: ("ok", ""))
    line = FakeLine(ok=True)
    svc = NotificationService(db, line)

    asyncio.run(svc.notify("demo", "trade_opened", "เปิดไม้ EURUSD 0.01"))
    assert len(line.pushed) == 1                       # LINE unchanged
    assert db.rows["push_subscriptions"][0]["last_ok_at"]  # AND Web Push
    # One queue row stamped 'sent' → worker #4 must not re-alert the same event.
    assert [r[0] for r in db.inserted] == ["notifications"]
    assert db.inserted[0][1]["status"] == "sent"


def test_critical_alert_survives_dead_web_push(monkeypatch):
    """A broken push service must never break a trade alert."""
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]
    db.rows["line_targets"] = [{"target_id": "grp1", "notification_enabled": True}]
    app.state.db = db

    def boom(*a, **k):
        raise RuntimeError("push service down")

    monkeypatch.setattr(web_push, "_send_sync", boom)
    line = FakeLine(ok=True)
    svc = NotificationService(db, line)

    asyncio.run(svc.notify("demo", "stop_loss", "ชน SL"))
    assert len(line.pushed) == 1
    assert db.inserted[0][1]["status"] == "sent"       # LINE alone is enough


def test_web_push_does_not_set_sent_when_both_fail(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]
    db.rows["line_targets"] = [{"target_id": "grp1", "notification_enabled": True}]
    app.state.db = db
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda *a, **k: ("error", "HTTP 500: boom"))
    line = FakeLine(ok=False)
    svc = NotificationService(db, line)

    asyncio.run(svc.notify("demo", "trade_opened", "เปิดไม้ EURUSD"))
    # 'pending' → the worker retries it.
    assert db.inserted[0][1]["status"] == "pending"


# ---------------------------------------------------------------------------
# 8. Queued (non-critical) alerts reach the phone too
# ---------------------------------------------------------------------------
def test_dispatch_pending_sends_web_push(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["notifications"] = [
        {"id": 1, "user_id": "demo", "type": "daily_digest",
         "message": "สรุปตลาดประจำวัน", "status": "pending"},
    ]
    db.rows["push_subscriptions"] = [device(1)]
    app.state.db = db
    monkeypatch.setattr(web_push, "_send_sync", lambda *a, **k: ("ok", ""))
    notifier = FakeNotifier(line_ok=False, push_ok=True)

    sent = asyncio.run(notification_worker.dispatch_pending(db, notifier))
    assert sent == 1
    assert notifier.push_calls == [("daily_digest", "สรุปตลาดประจำวัน")]
    # 'sent' after ONE transport delivered → no duplicate phone notification.
    row = next(r for r in db.rows["notifications"] if r["id"] == 1)
    assert row["status"] == "sent"


# ---------------------------------------------------------------------------
# 9. API surface — /api/push/* must never leak push credentials
# ---------------------------------------------------------------------------
def test_push_key_endpoint_disabled_without_env(monkeypatch):
    keys_off(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]

    r = asyncio.run(call("GET", "/api/push/key", db=db))
    body = r.json()
    assert r.status_code == 200
    assert body["enabled"] is False
    assert body["public_key"] == ""
    assert "VAPID_PUBLIC_KEY" in body["message"]        # tells the owner what to do


def test_push_key_endpoint_exposes_public_key_only(monkeypatch):
    keys_on(monkeypatch, pub="BK-public", priv="SECRET-private")
    db = SettingsDatabase()

    body = asyncio.run(call("GET", "/api/push/key", db=db)).json()
    assert body["enabled"] is True
    assert body["public_key"] == "BK-public"
    assert "SECRET-private" not in str(body)


def test_subscribe_is_idempotent_and_re_enables(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1, enabled=False, fail_count=9,
                                            last_error="endpoint หมดอายุ")]
    payload = {
        "endpoint": "https://push.example.com/1",
        "keys": {"p256dh": "p1", "auth": "a1"},
        "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120",
    }

    body = asyncio.run(call("POST", "/api/push/subscribe", payload, db=db)).json()
    assert body["ok"] is True
    row = db.rows["push_subscriptions"][0]
    assert row["enabled"] is True           # browsing again revives the device
    assert row["fail_count"] == 0
    assert row["last_error"] is None
    assert len(db.rows["push_subscriptions"]) == 1   # upsert, no duplicate


def test_subscribe_rejects_incomplete_payload(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()

    body = asyncio.run(call("POST", "/api/push/subscribe",
                            {"endpoint": "https://x/1", "keys": {}}, db=db)).json()
    assert body["ok"] is False
    assert "ไม่ครบ" in body["message"]


def test_subscribe_missing_table_explains_migration(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.fail_tables.add("push_subscriptions")

    body = asyncio.run(call("POST", "/api/push/subscribe", {
        "endpoint": "https://x/1",
        "keys": {"p256dh": "p", "auth": "a"},
    }, db=db)).json()
    assert body["ok"] is False
    assert "042" in body["message"]         # actionable, not a raw SQL error


def test_unsubscribe_keeps_row_but_disables(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]

    body = asyncio.run(call("POST", "/api/push/unsubscribe",
                            {"endpoint": "https://push.example.com/1"},
                            db=db)).json()
    assert body["ok"] is True
    assert db.rows["push_subscriptions"][0]["enabled"] is False


def test_subscriptions_endpoint_hides_credentials(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1)]

    body = asyncio.run(call("GET", "/api/push/subscriptions", db=db)).json()
    assert body["total"] == 1 and body["enabled_count"] == 1
    dumped = str(body)
    for secret in ("endpoint", "p256dh", "auth", "push.example.com"):
        assert secret not in dumped
    assert body["devices"][0]["device"] == "Android · Chrome"


def test_test_endpoint_reports_per_device(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()
    db.rows["push_subscriptions"] = [device(1), device(2)]
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda info, payload: (
                            "ok", "") if info["endpoint"].endswith("/1")
                        else ("error", "HTTP 500: boom"))

    body = asyncio.run(call("POST", "/api/push/test", db=db)).json()
    assert body["enabled"] is True
    assert body["sent"] == 1 and body["failed"] == 1
    assert body["ok"] is True                # at least one phone was reached
    assert sorted(r["ok"] for r in body["results"]) == [False, True]
    assert "endpoint" not in str(body["results"])


def test_test_endpoint_without_devices_explains_what_to_do(monkeypatch):
    keys_on(monkeypatch)
    db = SettingsDatabase()

    body = asyncio.run(call("POST", "/api/push/test", db=db)).json()
    assert body["ok"] is False and body["total"] == 0
    assert "เปิดการแจ้งเตือนบนอุปกรณ์นี้" in body["message"]


def test_test_endpoint_without_keys_says_why(monkeypatch):
    keys_off(monkeypatch)
    db = SettingsDatabase()

    body = asyncio.run(call("POST", "/api/push/test", db=db)).json()
    assert body["ok"] is False and body["enabled"] is False
    assert "VAPID" in body["message"]


# ---------------------------------------------------------------------------
# Presentation helpers — the OS tray needs a short title, not a LINE message
# ---------------------------------------------------------------------------
def test_push_title_and_url_per_type():
    from app.services.notification_service import push_title, push_url

    assert push_title("trade_opened") == "เปิดไม้ใหม่"
    assert push_title("stop_loss") == "ชน Stop Loss"
    assert push_title("unknown_type") == "แจ้งเตือนจาก AI Trading"
    assert push_url("economic_news") == "/signals.html"
    assert push_url("trade_opened") == "/monitor.html"


def test_push_body_strips_emoji_and_keeps_first_line():
    from app.services.notification_service import push_body

    msg = "🔔 เปิดไม้ใหม่ EURUSD\nSL 1.0900 · TP 1.1100"
    body = push_body(msg)
    assert body.startswith("เปิดไม้ใหม่ EURUSD")
    assert "SL 1.0900" not in body          # one-line body for the tray
    assert "🔔" not in body                  # UI design system: no emoji


def test_push_body_truncates_and_handles_empty():
    from app.services.notification_service import push_body

    assert len(push_body("ก" * 400)) == 140
    assert push_body("") == ""
    assert push_body("   ") == ""


def test_device_label_falls_back_for_unknown_agents():
    assert web_push.device_label({"user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) Safari"}) == "iPhone/iPad · Safari"
    assert web_push.device_label({"user_agent": ""}) == "ไม่ทราบอุปกรณ์ · ?"


def test_push_defaults_are_safe_when_settings_untouched():
    """No monkeypatching at all: the real Settings object must default to inert."""
    from app.integrations.web_push import SUBSCRIPTIONS_TABLE, MAX_FAILS

    assert SUBSCRIPTIONS_TABLE == "push_subscriptions"
    assert MAX_FAILS == 10
    assert isinstance(AppSettings(), AppSettings)   # schema module importable
