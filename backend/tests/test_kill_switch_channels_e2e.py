"""Confirm-before-kill across ALL THREE channels — LINE, web popup, Web Push.

Incident these tests guard (prod 2026-09-22): the kill switch closed 6 positions
against a 10% default while the owner had configured 15%, and it did so WITHOUT
asking. The fix makes a breach ASK first — but "ask" is only real if EVERY
channel actually carries the question:

  1. LINE       — one actionable push with the Approve/Reject quick-reply and the
                  typed fallback ``/dd_ok`` / ``/dd_no``
  2. web popup  — ``GET /api/trading/limit-expand`` exposes the SAME pending
                  request, and ``POST .../decide`` writes the SAME limits + mirrors
                  the outcome back to LINE
  3. Web Push   — the OS-notification transport fires for ``limit_expand`` too
                  (it is in CRITICAL_TYPES), with the right title/body, and the
                  popup does NOT depend on it (push is best-effort)

The whole point of the incident was a SILENT close. Each test therefore asserts
both halves: the owner is ASKED (all three channels), and — until they answer —
nothing is closed/widened.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_kill_switch_channels_e2e.py -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.integrations import web_push
from app.models.schemas import AppSettings
from app.services import execution, limit_expand
from app.services.notification_service import NotificationService
from tests.test_limit_expand import (RecordingNotifier, _dd_db, _mount,
                                     _popup_decide, _popup_state)


# ---------------------------------------------------------------------------
# channel 3 helpers — Web Push (VAPID)
# ---------------------------------------------------------------------------
def keys_on(monkeypatch) -> None:
    """Pretend the API service has VAPID env vars configured."""
    fake = SimpleNamespace(vapid_public_key="pub-key",
                           vapid_private_key="priv-key",
                           vapid_subject="mailto:test@example.com")
    monkeypatch.setattr(web_push, "get_settings", lambda: fake)


def device(i: int, enabled: bool = True) -> dict:
    """One registered browser/phone subscription row (migration 042 shape)."""
    return {"id": i, "endpoint": f"https://push.example.com/{i}",
            "p256dh": f"p256dh-{i}", "auth": f"auth-{i}",
            "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/120",
            "enabled": enabled, "fail_count": 0, "last_error": None,
            "last_ok_at": None, "created_at": "2026-01-01T00:00:00+00:00"}


class FakeLine:
    """LineClient stand-in for the real NotificationService."""

    def __init__(self):
        self.pushes: list[tuple[str, str]] = []

    async def push(self, target_id: str, message: str,
                   quick_reply=None) -> bool:
        self.pushes.append((target_id, message))
        return True

    async def reply(self, reply_token: str, message: str) -> None:
        return None


# ---------------------------------------------------------------------------
# 1. LINE — the breach produces ONE actionable prompt, nothing is closed
# ---------------------------------------------------------------------------
def test_line_prompt_carries_approve_and_reject():
    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd > 10% default limit
    notifier = RecordingNotifier()

    res = limit_expand.request_and_notify(db, s, notifier)

    assert res["requested"] is True
    prompts = notifier.of("limit_expand")
    assert len(prompts) == 1                        # ONE prompt per window
    prompt = prompts[0]
    # the owner's buttons, wired to the postback the webhook answers
    labels = [b["action"]["label"] for b in prompt["quick_reply"]]
    assert any("อนุมัติ" in l for l in labels)
    assert any("ไม่อนุมัติ" in l for l in labels)
    data = [b["action"]["data"] for b in prompt["quick_reply"]]
    assert data == ["dd_ok", "dd_no"]
    # the typed fallback is quoted in the text too
    assert "/dd_ok" in prompt["message"] and "/dd_no" in prompt["message"]
    # asked — but NOT yet decided: the request is pending and no limit moved
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"
    assert "trading_settings" not in getattr(db._client, "store", {})


def test_line_button_approve_writes_the_limits_and_reports_back():
    db, s = _dd_db(equity=8900.0), AppSettings()
    notifier = RecordingNotifier()
    limit_expand.request_and_notify(db, s, notifier)

    # the quick-reply POSTBACK data ("dd_ok") arrives via the webhook path
    reply = limit_expand.handle_postback(db, "dd_ok", decided_by="line:user")

    assert "ขยายลิมิตเรียบร้อย" in reply
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is False
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"


def test_line_typed_dd_no_keeps_limits_and_pause():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = limit_expand.decide(db, "/dd_no", decided_by="line")

    assert "ลิมิตเดิม" in reply
    assert "trading_settings" not in db._client.store          # nothing widened
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert db.rows["kill_expand_requests"][0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# 2. web popup — same request as LINE, and the decision mirrors back
# ---------------------------------------------------------------------------
def test_web_popup_shows_exactly_the_line_request():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    _mount(db)

    body = _popup_state().json()

    assert body["pending"] is True and body["breach"] is True
    assert body["request"]["trigger_type"] == "drawdown"
    assert body["request"]["limit_before"] == 10.0
    assert body["request"]["limit_after"] == 15.0
    # the popup renders the SAME commands LINE accepts
    assert body["approve_command"] == "/dd_ok"
    assert body["reject_command"] == "/dd_no"
    assert body["step_pct"] == 5.0 and body["ttl_min"] == 180.0


def test_web_popup_approve_widens_and_mirrors_to_line():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["line_users"] = [{"id": "lu-1", "line_user_id": "U-owner",
                              "notification_enabled": True}]
    line = _mount(db)
    # swap in a recorder that also captures quick_reply (mirror assertions below)
    line.pushes.clear()

    body = _popup_decide("approve").json()

    assert body["ok"] is True and body["applied_decision"] == "approve"
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is False
    assert body["state"]["pending"] is False
    # the SAME report was pushed to LINE (web answer ⇒ LINE stays in sync)
    assert [m for _u, m in line.pushes if body["reply"] in m]


def test_web_popup_reject_keeps_limits_and_pause():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    _mount(db)

    body = _popup_decide("reject").json()

    assert body["applied_decision"] == "reject"
    assert "trading_settings" not in db._client.store
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert db.rows["kill_expand_requests"][0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# 3. Web Push — the SAME alert reaches the OS notification tray
# ---------------------------------------------------------------------------
def test_limit_expand_is_a_critical_type_so_it_pushes():
    """Web Push only fires for CRITICAL_TYPES — limit_expand must be one."""
    from app.services.notification_service import CRITICAL_TYPES
    assert "limit_expand" in CRITICAL_TYPES


def test_web_push_fires_for_the_limit_expand_prompt(monkeypatch):
    keys_on(monkeypatch)
    db = _dd_db(equity=8900.0)
    db.rows["push_subscriptions"] = [device(1), device(2, enabled=False)]
    db.rows["line_targets"] = [{"target_id": "grp1",
                                "notification_enabled": True}]
    sent_payloads: list[dict] = []

    def fake_send(row, payload):
        sent_payloads.append({"endpoint": row["endpoint"], "payload": payload})
        return "ok", ""

    monkeypatch.setattr(web_push, "_send_sync", fake_send)

    svc = NotificationService(db, FakeLine())
    prompt = "⚠️ คำขอขยายลิมิตความเสี่ยง\nDrawdown 11.00% > 10.0%"
    asyncio.run(svc.notify("demo", "limit_expand", prompt))

    # ONE enabled device got the OS notification; the disabled one did not
    assert [p["endpoint"] for p in sent_payloads] == ["https://push.example.com/1"]
    assert db.rows["push_subscriptions"][0]["last_ok_at"]     # success recorded
    assert db.rows["push_subscriptions"][1]["last_ok_at"] is None


def test_web_push_payload_has_title_and_one_line_body(monkeypatch):
    keys_on(monkeypatch)
    db = _dd_db(equity=8900.0)
    db.rows["push_subscriptions"] = [device(1)]
    captured: dict = {}
    monkeypatch.setattr(web_push, "_send_sync",
                        lambda row, payload: (captured.update(payload=payload)
                                              or ("ok", "")))

    svc = NotificationService(db, FakeLine())
    message = ("⚠️ คำขอขยายลิมิตความเสี่ยง\n"
               "• Drawdown 11.00% > 10.0% → 15.0%\n"
               "กดอนุมัติหรือไม่อนุมัติภายใน 180 นาที")
    asyncio.run(svc.notify("demo", "limit_expand", message))

    import json
    push = json.loads(captured["payload"])
    assert push["title"] == "ขอขยายลิมิตความเสี่ยง"
    assert push["ntype"] == "limit_expand"
    assert push["body"] == "คำขอขยายลิมิตความเสี่ยง"   # first line, emoji stripped
    assert "\n" not in push["body"]
    assert push["url"] == "/monitor.html"


def test_a_dead_push_service_never_blocks_the_prompt(monkeypatch):
    """Web Push is best-effort: other transports + the popup must survive."""
    keys_on(monkeypatch)
    db, s = _dd_db(equity=8900.0), AppSettings()
    db.rows["push_subscriptions"] = [device(1)]

    def boom(*_a, **_k):
        raise RuntimeError("push service down")

    monkeypatch.setattr(web_push, "_send_sync", boom)

    # the guard/monitor prompt path still lands the pending request + LINE text
    notifier = RecordingNotifier()
    res = limit_expand.request_and_notify(db, s, notifier)
    assert res["requested"] is True
    assert notifier.of("limit_expand")
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"

    # and the web popup still exposes the request the owner must answer
    _mount(db)
    assert _popup_state().json()["pending"] is True
