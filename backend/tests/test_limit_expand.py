"""Owner-confirmed risk-limit expansion (migration 036).

Incident these tests guard (prod 2026-09-14): drawdown 10.06% > 10.00% fired the
kill switch, the guard closed all 5 open positions and Gate 1 blocked every new
order — correct, but trading could never resume and nothing was allowed to
widen a risk limit by itself. Expansion is now an explicit owner decision:
breach → pending request → LINE Approve/Reject (or /dd_ok, /dd_no) → write the
named limits + resume + re-evaluate.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_limit_expand.py -v
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.models.schemas import AppSettings
from app.services import execution, limit_expand
from app.workers import portfolio_monitor
from tests.test_auto_trader import db_with_client
from tests.test_workers import FakeDatabase


def _minutes_ago(mins: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=mins)).isoformat()


class RecordingNotifier:
    """NotificationService stand-in that also accepts the quick_reply kwarg."""

    def __init__(self):
        self.sent: list[dict] = []

    async def notify(self, user_id, ntype, message, critical=None,
                     quick_reply=None) -> None:
        self.sent.append({"user_id": user_id, "type": ntype, "message": message,
                          "quick_reply": quick_reply})

    def of(self, ntype: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == ntype]


def _dd_db(equity: float = 8000.0, peak: float = 10000.0) -> FakeDatabase:
    """FakeDatabase with a peak→current equity drop and a working KV client.

    Default 8000 vs a 10000 peak = 20% drawdown (> the 10% limit).
    8900 = 11% → clears after ONE +5pp expansion.
    """
    return db_with_client({
        "equity_snapshots": [
            {"id": "eq-2", "snapshot_date": "2026-09-14", "equity": equity},
            {"id": "eq-1", "snapshot_date": "2026-09-01", "equity": peak},
        ],
    })


def _daily_loss_db(pnl: float = -300.0, capital: float = 10000.0) -> FakeDatabase:
    """Db whose only breach is a DAILY-LOSS breach (3% vs a 2% limit).

    The risk engine and the kill switch both read ``kill_daily_loss_pct``, so a
    +5pp expansion (2 → 7) clears the breach for BOTH in the same cycle —
    unlike a drawdown the monitor computes from the live broker book.
    """
    db = db_with_client({
        "equity_snapshots": [
            {"id": "eq-2", "snapshot_date": "2026-09-14", "equity": capital},
            {"id": "eq-1", "snapshot_date": "2026-09-01", "equity": capital},
        ],
    })
    db.rows["paper_trades"] = [{
        "id": "t1", "status": "closed", "pnl": pnl,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }]
    return db


# ---------------------------------------------------------------------------
# breach detection
# ---------------------------------------------------------------------------
def test_breached_triggers_match_the_kill_switch():
    db, s = _dd_db(), AppSettings()
    triggers = limit_expand.breached_triggers(db, s)
    assert [t["trigger"] for t in triggers] == ["drawdown"]
    t = triggers[0]
    assert t["value"] == pytest.approx(20.0)
    assert t["limit"] == 10.0
    assert t["new_limit"] == 15.0            # +5 percentage points
    # the prompt and the switch must never disagree about the numbers
    kill = execution.evaluate_kill(db, s)
    assert kill.engaged
    daily, weekly, monthly, dd = execution.kill_metrics(db, float(s.capital))
    assert dd == pytest.approx(t["value"])


def test_no_breach_creates_no_request():
    db, s = _dd_db(equity=10000.0, peak=10000.0), AppSettings()
    res = limit_expand.request_expand(db, s)
    assert res == {"requested": False, "reason": "no_breach", "triggers": []}
    assert not db.rows.get("kill_expand_requests")


# ---------------------------------------------------------------------------
# request creation / dedupe
# ---------------------------------------------------------------------------
def test_request_expand_creates_pending_row():
    db, s = _dd_db(), AppSettings()
    res = limit_expand.request_expand(db, s)
    assert res["requested"] and res["reason"] == "created"
    rows = db.rows["kill_expand_requests"]
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["trigger_type"] == "drawdown"
    assert rows[0]["limit_before"] == 10.0
    assert rows[0]["limit_after"] == 15.0


def test_request_expand_dedupes_while_pending():
    db, s = _dd_db(), AppSettings()
    assert limit_expand.request_expand(db, s)["requested"]
    second = limit_expand.request_expand(db, s)
    assert second["requested"] is False
    assert second["reason"] == "already_pending"
    assert len(db.rows["kill_expand_requests"]) == 1


def test_rejected_request_is_in_cooldown():
    db, s = _dd_db(), AppSettings()
    limit_expand.request_expand(db, s)
    limit_expand.decide(db, "/dd_no", decided_by="line")
    assert db.rows["kill_expand_requests"][0]["status"] == "rejected"
    assert limit_expand.request_expand(db, s)["reason"] == "cooldown"


def test_a_lapsed_request_is_no_longer_answerable_but_waits_for_the_timeout():
    """Reads stay side-effect free: the timeout path (not a poll) settles it."""
    db = _dd_db()
    db.insert("kill_expand_requests", {
        "status": "pending", "trigger_type": "drawdown",
        "requested_at": _minutes_ago(limit_expand.PENDING_TTL_MIN + 30),
    })
    assert limit_expand.pending_request(db) is None       # ตอบไม่ได้แล้ว
    assert limit_expand.stale_pending(db) is not None     # แต่ยังค้างอยู่
    row = db.rows["kill_expand_requests"][0]
    assert row["status"] == "pending"
    assert not row.get("decided_by")
    # …และสองการอ่านข้างบนไม่เขียนอะไรเลย
    assert "trading_settings" not in db._client.store


def test_the_timeout_settles_a_lapsed_request_even_with_no_breach_left():
    db = _dd_db(equity=8900.0)
    limit_expand.request_and_notify(db, AppSettings(), RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)
    db.rows["equity_snapshots"][0]["equity"] = 10000.0   # กลับเข้ากรอบ

    report = limit_expand.auto_apply_expired(db, AppSettings())

    assert report and "ไม่มีลิมิตที่เกินอยู่แล้ว" in report
    row = db.rows["kill_expand_requests"][0]
    assert row["status"] == "expired"
    assert row["decided_by"] == limit_expand.AUTO_NO_BREACH_BY
    assert "trading_settings" not in db._client.store     # ไม่ขยายอะไรเลย


# ---------------------------------------------------------------------------
# confirmation window is a SETTING ("เวลาในการรอ", default 180 นาที)
# ---------------------------------------------------------------------------
def test_confirmation_window_defaults_to_180_minutes_everywhere():
    """AppSettings default == the service fallback == what both channels say."""
    from app.integrations.line_client import build_limit_expand_prompt
    assert AppSettings().kill_expand_ttl_min == 180
    assert limit_expand.PENDING_TTL_MIN == float(
        AppSettings().kill_expand_ttl_min)
    assert limit_expand.ttl_minutes(None) == 180.0
    assert limit_expand.ttl_minutes(AppSettings()) == 180.0
    assert "180 นาที" in build_limit_expand_prompt(
        [{"label": "Drawdown", "value": 11.0, "limit": 10.0,
          "new_limit": 15.0}],
        ttl_min=limit_expand.ttl_minutes(AppSettings()))


def test_confirmation_window_setting_decides_which_requests_are_late():
    """A shorter window (Settings) makes an older request LATE — not closed."""
    db = _dd_db()
    db.insert("kill_expand_requests", {
        "status": "pending", "trigger_type": "drawdown",
        "requested_at": _minutes_ago(45.0),
    })
    short = AppSettings(kill_expand_ttl_min=30)
    # 45 min old: fine under the 180 default, late under a 30-minute window
    assert limit_expand.pending_request(db, settings=AppSettings()) is not None
    assert limit_expand.pending_request(db, settings=short) is None
    assert limit_expand.stale_pending(db, short) is not None
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"


def test_confirmation_window_is_used_by_state_and_the_timeout_report():
    """Popup ttl_min + expires_at + the timeout report follow the setting."""
    db, s = _dd_db(equity=8900.0), AppSettings(kill_expand_ttl_min=30)
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    body = limit_expand.state(db, s)
    assert body["ttl_min"] == 30.0
    requested = body["request"]["requested_at"]
    assert body["request"]["expires_at"] > requested
    # just under the window the request is still answerable…
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(29.0)
    assert limit_expand.pending_request(db, settings=s) is not None
    assert limit_expand.stale_pending(db, s) is None
    # …and past it the timeout applies it and quotes the CONFIGURED window
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(31.0)
    report = limit_expand.auto_apply_expired(db, s)
    assert report and "ไม่มีการยืนยันภายใน 30 นาที" in report
    assert "180 นาที" not in report


def test_the_no_request_message_quotes_the_configured_window():
    db = _dd_db(equity=8900.0)
    reply = limit_expand.decide(db, "/dd_ok", decided_by="line",
                                settings=AppSettings(kill_expand_ttl_min=30))
    assert "ไม่มีคำขอขยายลิมิต" in reply
    assert "30 นาที" in reply
    assert "trading_settings" not in db._client.store


def test_confirmation_window_from_the_db_when_caller_has_no_settings():
    """LINE postback / bare decide(db, ...) read the stored row."""
    from app.api.routes.settings import persist_settings

    db, s = _dd_db(equity=8900.0), AppSettings(kill_expand_ttl_min=45)
    execution.set_pause(db, True, "kill switch: drawdown")
    assert persist_settings(db, s)
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    # no settings argument → the service loads trading_settings itself
    assert limit_expand.ttl_minutes(execution.get_app_settings(db)) == 45.0
    assert limit_expand.pending_request(db) is not None
    assert limit_expand.decide(db, "/dd_ok", decided_by="line") \
        .startswith("✅")


def test_confirmation_window_is_clamped_into_a_sane_range():
    """0/ติดลบ → 5 นาที (ไม่ใช่ "ไม่มีวันหมดอายุ"), ค่ามหาศาล → 7 วัน."""
    assert limit_expand.ttl_minutes(AppSettings(kill_expand_ttl_min=0)) == \
        limit_expand.MIN_TTL_MIN
    assert limit_expand.ttl_minutes(AppSettings(kill_expand_ttl_min=-30)) == \
        limit_expand.MIN_TTL_MIN
    assert limit_expand.ttl_minutes(
        AppSettings(kill_expand_ttl_min=999_999)) == limit_expand.MAX_TTL_MIN
    assert limit_expand.ttl_minutes(AppSettings(kill_expand_ttl_min=999_999)) \
        < 30 * 24 * 60          # never "unbounded" in practice


def test_line_prompt_quotes_the_configured_window():
    db, s = _dd_db(equity=8900.0), AppSettings(kill_expand_ttl_min=30)
    notifier = RecordingNotifier()
    limit_expand.request_and_notify(db, s, notifier)
    msg = notifier.of("limit_expand")[0]["message"]
    assert "คำขอมีอายุ 30 นาที" in msg
    assert "60 นาที" not in msg


def test_insert_failure_is_reported_instead_of_pretending():
    """No 036 table → DB.insert swallows the error; the flow must say so."""
    db, s = _dd_db(), AppSettings()
    db.fail_tables = {"kill_expand_requests"}
    res = limit_expand.request_expand(db, s)
    assert res["requested"] is False and res["reason"] == "insert_failed"

    notifier = RecordingNotifier()
    limit_expand.request_and_notify(db, s, notifier)
    msg = notifier.of("limit_expand")[0]["message"]
    assert "036_kill_expand_confirm.sql" in msg
    assert notifier.of("limit_expand")[0]["quick_reply"] is None


# ---------------------------------------------------------------------------
# the LINE prompt
# ---------------------------------------------------------------------------
def test_request_and_notify_pushes_actionable_prompt():
    db, s = _dd_db(), AppSettings()
    notifier = RecordingNotifier()
    res = limit_expand.request_and_notify(db, s, notifier)
    assert res["notified"] is True
    msg = notifier.of("limit_expand")[0]
    assert "ต้องยืนยันจากเจ้าของบัญชี" in msg["message"]
    assert "10.00%" in msg["message"] and "15.00%" in msg["message"]
    assert "/dd_ok" in msg["message"] and "/dd_no" in msg["message"]
    assert [i["action"]["data"] for i in msg["quick_reply"]] == ["dd_ok", "dd_no"]
    assert msg["quick_reply"][0]["action"]["type"] == "postback"


def test_second_breach_cycle_does_not_reask():
    db, s = _dd_db(), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    notifier = RecordingNotifier()
    res = limit_expand.request_and_notify(db, s, notifier)
    assert res["requested"] is False
    assert notifier.sent == []


# ---------------------------------------------------------------------------
# timeout → APPLY the expansion ("ถ้า confirm หมดอายุให้ดำเนินการขยาย limit เลย")
# the owner is ASKED first, the WINDOW decides last — but never silently
# ---------------------------------------------------------------------------
def test_a_lapsed_window_is_applied_and_reported():
    """No answer inside the window → the requested limits ARE written."""
    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd → clears at 15%
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    report = limit_expand.auto_apply_expired(db, s)

    assert report and "หมดเวลายืนยัน" in report
    assert "• Drawdown: 10.00% → 15.00%" in report
    assert "เปิดเทรดต่อทันที" in report
    assert "ไม่มีการยืนยันภายใน 180 นาที" in report
    stored = db._client.store["trading_settings"][1]
    assert stored["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is False
    row = db.rows["kill_expand_requests"][0]
    assert row["status"] == "approved"
    assert row["decided_by"] == limit_expand.AUTO_DECIDED_BY
    assert "limit_expanded" in [
        e["event_type"] for e in db.rows.get("risk_events", [])]


def test_a_lapsed_window_is_applied_once_not_every_cycle():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    assert limit_expand.auto_apply_expired(db, s)
    assert limit_expand.auto_apply_expired(db, s) is None   # ปิดคำขอแล้ว
    assert len(db.rows["kill_expand_requests"]) == 1


def test_the_monitor_cycle_applies_then_does_not_ask_again():
    """The 1-minute loop reports the widening instead of re-asking."""
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(200.0)

    notifier = RecordingNotifier()
    res = limit_expand.request_and_notify(db, s, notifier)

    assert res["requested"] is False and res["reason"] == "auto_applied"
    assert res["auto_applied"] is True and res["notified"] is True
    sent = notifier.of("limit_expand")
    assert len(sent) == 1                   # the REPORT, not a new question
    assert "หมดเวลายืนยัน" in sent[0]["message"]
    assert sent[0]["quick_reply"] is None    # ไม่มีปุ่ม Approve/Reject ให้กดซ้ำ
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert len(db.rows["kill_expand_requests"]) == 1


def test_a_late_button_press_still_settles_the_lapsed_window():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    reply = limit_expand.decide(db, "/dd_ok", decided_by="line:user")

    assert "หมดเวลายืนยัน" in reply
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    # the widening is credited to the timeout, NOT to the late press
    assert db.rows["kill_expand_requests"][0]["decided_by"] == \
        limit_expand.AUTO_DECIDED_BY
    # pressing again explains what the system already did
    again = limit_expand.decide(db, "/dd_no", decided_by="line:user")
    assert "ไปอัตโนมัติแล้ว" in again
    assert "10.0% → 15.0%" in again


def test_a_lapsed_window_never_lowers_a_limit_the_owner_raised():
    """The request is a snapshot — a hand-raised limit wins over a stale +5pp.

    Two triggers (drawdown + daily) and the owner raised the DAILY limit to 8%
    while the request proposed 7%: that field must not be written back down,
    and the report must not claim it was.
    """
    from app.api.routes.settings import persist_settings

    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd; pnl adds 3% daily
    db.rows["paper_trades"] = [{
        "id": "t1", "status": "closed", "pnl": -300.0,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }]
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    row = db.rows["kill_expand_requests"][0]
    assert {t["trigger"] for t in row["detail"]["triggers"]} == {
        "drawdown", "daily"}
    row["requested_at"] = _minutes_ago(181.0)
    # the owner raised the daily limit by hand (> the 7% the request proposed)
    assert persist_settings(db, AppSettings(kill_daily_loss_pct=8.0))
    live = execution.get_app_settings(db)

    report = limit_expand.auto_apply_expired(db, live)

    stored = db._client.store["trading_settings"][1]
    assert stored["max_drawdown_pct"] == 15.0        # ที่เกินอยู่ → ขยาย
    assert stored["kill_daily_loss_pct"] == 8.0       # ไม่เขียนลดลง
    assert report and "ไม่เขียนทับ: Daily loss" in report
    assert "Daily loss: 2.00% → 7.00%" not in report  # ไม่รายงานสิ่งที่ไม่ได้เขียน
    assert "• Drawdown: 10.00% → 15.00%" in report
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"


def test_a_failed_timeout_write_is_retried_and_never_reported():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    class _Boom:
        def table(self, _name):
            raise RuntimeError("PostgREST down")

    real = db._client
    db._client = _Boom()
    assert limit_expand.auto_apply_expired(db, s) is None   # ไม่มีรายงานหลอก
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"

    db._client = real                        # เขียนได้อีกครั้ง → รอบถัดไปสำเร็จ
    report = limit_expand.auto_apply_expired(db, s)
    assert report and "หมดเวลายืนยัน" in report
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"


def test_timeout_apply_can_be_switched_off(monkeypatch):
    """AUTO_APPLY_ON_EXPIRY=False keeps the old "wait for the owner" behaviour."""
    monkeypatch.setattr(limit_expand, "AUTO_APPLY_ON_EXPIRY", False)
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    assert limit_expand.auto_apply_expired(db, s) is None
    assert "trading_settings" not in db._client.store
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"


def test_only_the_newest_pending_request_can_be_applied():
    """A row left behind by a failed write must not stack another +5pp."""
    db, s = _dd_db(equity=8900.0), AppSettings()
    # oldest first: the fake returns the newest-inserted row first, exactly like
    # ORDER BY requested_at DESC does on the real table
    db.insert("kill_expand_requests", {
        "status": "pending", "trigger_type": "drawdown",
        "requested_at": _minutes_ago(600.0)})
    db.insert("kill_expand_requests", {
        "status": "pending", "trigger_type": "drawdown",
        "requested_at": _minutes_ago(5.0)})

    assert limit_expand.stale_pending(db, s) is None     # ตัวใหม่ยังไม่หมดเวลา
    assert limit_expand.auto_apply_expired(db, s) is None
    assert {r["status"] for r in db.rows["kill_expand_requests"]} == {"pending"}
    assert "trading_settings" not in db._client.store


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------
def test_approve_writes_limit_resumes_and_reports():
    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd → clears with 15%
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = limit_expand.decide(db, "/dd_ok", decided_by="line:user")

    stored = db._client.store["trading_settings"][1]
    assert stored["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is False
    request = db.rows["kill_expand_requests"][0]
    assert request["status"] == "approved"
    assert request["decided_by"] == "line:user"
    assert "ขยายลิมิตเรียบร้อย" in reply
    assert "15.00%" in reply
    assert "เปิดเทรดต่อทันที" in reply
    # audit trail
    assert "limit_expanded" in [e["event_type"] for e in db.rows.get("risk_events", [])]


def test_approve_still_breached_repauses_and_says_so():
    db, s = _dd_db(equity=8000.0), AppSettings()   # 20% dd, 15% is not enough
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = limit_expand.decide(db, "/dd_ok", decided_by="line")

    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert "ยังไม่เปิดเทรด" in reply
    assert "Drawdown 20.00% > 15.00%" in reply


def test_reject_keeps_limits_and_pause():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = limit_expand.decide(db, "/dd_no", decided_by="line:group")

    assert "trading_settings" not in db._client.store      # nothing widened
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert db.rows["kill_expand_requests"][0]["status"] == "rejected"
    assert "ลิมิตเดิม" in reply
    assert "limit_expand_rejected" in [
        e["event_type"] for e in db.rows.get("risk_events", [])]


def test_decide_without_a_pending_request():
    db = _dd_db()
    assert "ไม่มีคำขอ" in limit_expand.decide(db, "/dd_ok")
    assert "ไม่เข้าใจคำสั่ง" in limit_expand.decide(db, "maybe")


def test_handle_postback_maps_buttons_only():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    assert limit_expand.handle_postback(db, "something_else", "line:group") is None
    reply = limit_expand.handle_postback(db, "dd_no", "line:group")
    assert reply and "ลิมิตเดิม" in reply
    assert db.rows["kill_expand_requests"][0]["decided_by"] == "line:group"


def test_approve_failure_keeps_everything_as_is():
    """A settings write that does not land must NOT lift the pause."""
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    # make the upsert fail: KV client loses its store
    db._client.store = {}
    db._client.table("trading_settings")  # noqa: B018 - keep the fake alive
    orig = db._client.store

    class _Boom:
        def table(self, _name):
            raise RuntimeError("PostgREST down")

    db._client = _Boom()
    reply = limit_expand.decide(db, "/dd_ok", decided_by="line")
    assert "บันทึกลิมิตใหม่ไม่สำเร็จ" in reply
    assert db._client is not None and orig is not None
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"
    assert "trading_pause" not in db.rows


# ---------------------------------------------------------------------------
# LINE webhook commands
# ---------------------------------------------------------------------------
def test_webhook_dd_ok_command_approves():
    from app.api.routes.webhook import handle_command

    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = asyncio.run(handle_command("/dd_ok", db))
    assert "ขยายลิมิตเรียบร้อย" in reply
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0


def test_webhook_dd_ok_without_request_never_widens_anything():
    from app.api.routes.webhook import handle_command

    db = _dd_db(equity=8900.0)
    reply = asyncio.run(handle_command("/dd_ok", db))
    assert "ไม่มีคำขอ" in reply
    assert "trading_settings" not in db._client.store


def test_webhook_approve_label_still_handles_semi_auto():
    from app.api.routes.webhook import handle_command

    db = _dd_db(equity=8900.0)
    assert "SEMI-AUTO" in asyncio.run(handle_command("[Approve]", db))
    limit_expand.request_and_notify(db, AppSettings(), RecordingNotifier())
    assert "ขยายลิมิตเรียบร้อย" in asyncio.run(handle_command("[Approve]", db))


def test_webhook_answer_after_the_window_applies_the_request():
    """A late /dd_ok or Approve card settles the window instead of no-op'ing."""
    from app.api.routes.webhook import handle_command

    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    reply = asyncio.run(handle_command("/dd_ok", db))
    assert "หมดเวลายืนยัน" in reply
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0


def test_webhook_approve_card_late_does_not_fall_into_semi_auto():
    from app.api.routes.webhook import handle_command

    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)

    reply = asyncio.run(handle_command("[Approve]", db))
    assert "SEMI-AUTO" not in reply
    assert "หมดเวลายืนยัน" in reply


# ---------------------------------------------------------------------------
# monitor integration (the path that actually fires)
# ---------------------------------------------------------------------------
def test_monitor_breach_asks_the_owner():
    db = _dd_db(equity=10000.0, peak=10000.0)
    db.rows["paper_trades"] = [{
        "id": "t1", "status": "closed", "pnl": -2500.0,
        "closed_at": datetime.now(timezone.utc).isoformat(),
    }]
    notifier = RecordingNotifier()

    out = portfolio_monitor.monitor_once(db, None, notifier)

    assert out["breach"] is True and out["paused"] is True
    requests = db.rows.get("kill_expand_requests") or []
    assert len(requests) == 1 and requests[0]["status"] == "pending"
    asked = notifier.of("limit_expand")
    assert len(asked) == 1
    assert asked[0]["quick_reply"][0]["action"]["data"] == "dd_ok"
    # the breach notice still goes out alongside the request
    assert notifier.of("risk_warning")

    # and the 1-minute loop must not repeat the question
    quiet = RecordingNotifier()
    portfolio_monitor.monitor_once(db, None, quiet)
    assert quiet.of("limit_expand") == []


def test_monitor_applies_a_lapsed_window_before_judging_the_account():
    """Settled at the TOP of the cycle → the risk check sees the new limits."""
    db = _daily_loss_db()                     # 3% daily vs a 2% limit
    first = RecordingNotifier()
    asked = portfolio_monitor.monitor_once(db, None, first)
    assert asked["breach"] is True
    row = db.rows["kill_expand_requests"][0]
    assert row["status"] == "pending" and row["limit_after"] == 7.0
    row["requested_at"] = _minutes_ago(181.0)   # ไม่มีใครกดยืนยัน

    notifier = RecordingNotifier()
    out = portfolio_monitor.monitor_once(db, None, notifier)

    assert db._client.store["trading_settings"][1]["kill_daily_loss_pct"] == 7.0
    # widened FIRST → THIS cycle already evaluates against 7%: no breach, so
    # no pause churn and no second prompt
    assert out["breach"] is False
    assert db._client.store["trading_pause"][1]["paused"] is False
    assert notifier.of("risk_warning") == []
    sent = notifier.of("limit_expand")
    assert len(sent) == 1 and "หมดเวลายืนยัน" in sent[0]["message"]
    assert len(db.rows["kill_expand_requests"]) == 1   # ไม่สร้างคำขอใหม่


def test_monitor_still_asks_when_the_window_is_open():
    """A fresh request must NOT be applied early — the owner still decides."""
    db = _daily_loss_db()
    s = AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    notifier = RecordingNotifier()
    out = portfolio_monitor.monitor_once(db, None, notifier)

    assert out["breach"] is True
    assert "trading_settings" not in db._client.store
    assert db.rows["kill_expand_requests"][0]["status"] == "pending"
    assert notifier.of("limit_expand") == []            # ไม่ถามซ้ำในหน้าต่างเดิม


# ---------------------------------------------------------------------------
# web popup API — GET /api/trading/limit-expand, POST .../limit-expand/decide
# (the popup must follow the SAME condition as the LINE prompt)
# ---------------------------------------------------------------------------
class RecordingLineClient:
    """LineClient stand-in that records pushes (mirror assertion)."""

    def __init__(self):
        self.pushes: list[tuple[str, str]] = []

    async def push(self, user_id, message, quick_reply=None) -> bool:
        self.pushes.append((user_id, message))
        return True

    async def reply(self, reply_token, message) -> None:
        return None


def _mount(db) -> RecordingLineClient:
    """Wire the API app to `db` and return the recording LINE client."""
    from app.main import app
    from tests.test_api_routes import set_state

    set_state(db)
    line = RecordingLineClient()
    app.state.line = line
    return line


def _popup_state():
    from tests.test_api_routes import call
    return asyncio.run(call("GET", "/api/trading/limit-expand"))


def _popup_decide(decision: str):
    from tests.test_api_routes import call
    return asyncio.run(call("POST", "/api/trading/limit-expand/decide",
                            {"decision": decision}))


def _raise_missing_table(*_a, **_k):
    raise RuntimeError('relation "kill_expand_requests" does not exist')


def test_api_popup_exposes_the_same_pending_request_as_line():
    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd > 10% limit
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    _mount(db)

    res = _popup_state()
    assert res.status_code == 200
    body = res.json()
    assert body["pending"] is True
    assert body["breach"] is True
    assert body["setup_required"] is False
    assert body["request"]["trigger_type"] == "drawdown"
    assert body["request"]["limit_before"] == 10.0
    assert body["request"]["limit_after"] == 15.0
    assert body["request"]["age_min"] is not None
    assert body["request"]["expires_at"]
    assert body["last"] is None            # pending row is reported as request
    assert [t["trigger"] for t in body["triggers"]] == ["drawdown"]
    # the popup renders the exact commands LINE accepts
    assert body["approve_command"] == "/dd_ok"
    assert body["reject_command"] == "/dd_no"
    assert body["step_pct"] == 5.0 and body["ttl_min"] == 180.0
    assert body["kill_engaged"] is True
    assert body["paused"] is False


def test_unapplied_037_column_never_blocks_an_approved_expansion():
    """Without migration 037 the ttl column is missing from trading_settings.

    persist_settings must DROP it and still write the new limits — otherwise an
    owner approval would be refused because of one new optional column.
    """
    from tests.test_settings import FakeSettingsClient

    db, s = _dd_db(equity=8900.0), AppSettings()   # 11% dd → clears at 15%
    db._client = FakeSettingsClient(None, fail_columns=("kill_expand_ttl_min",))
    limit_expand.request_and_notify(db, s, RecordingNotifier())

    reply = limit_expand.decide(db, "/dd_ok", decided_by="line")

    # the fake client is shared with trading_pause writes → pick the settings row
    rows = [r for r in db._client.saved_rows if "max_drawdown_pct" in r]
    assert rows, db._client.saved_rows
    assert rows[-1]["max_drawdown_pct"] == 15.0
    assert "kill_expand_ttl_min" not in rows[-1]   # dropped, not fatal
    assert "ขยายลิมิตเรียบร้อย" in reply
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"


def test_api_popup_hidden_until_the_monitor_has_asked():
    """Breach without a stored request → popup stays down (same as LINE)."""
    db = _dd_db(equity=8900.0)
    _mount(db)
    body = _popup_state().json()
    assert body["pending"] is False
    assert body["breach"] is True           # a live breach is reported
    assert not db.rows.get("kill_expand_requests")


def test_api_popup_flags_a_missing_migration():
    db = _dd_db(equity=8900.0)
    db.select_ex = _raise_missing_table     # 036 not applied on prod
    _mount(db)
    body = _popup_state().json()
    assert body["pending"] is False
    assert body["setup_required"] is True   # → popup shows the SQL instruction


def test_api_popup_approve_widens_resumes_and_mirrors_to_line():
    db, s = _dd_db(equity=8900.0), AppSettings()   # clears once widened to 15%
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["line_users"] = [{"id": "lu-1", "line_user_id": "U-owner",
                              "notification_enabled": True}]
    line = _mount(db)
    line.pushes.clear()

    res = _popup_decide("approve")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["applied_decision"] == "approve"
    assert "ขยายลิมิตเรียบร้อย" in body["reply"]
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db._client.store["trading_pause"][1]["paused"] is False
    assert db.rows["kill_expand_requests"][0]["status"] == "approved"
    assert db.rows["kill_expand_requests"][0]["decided_by"] == "ui"
    # fresh state → popup closes itself
    assert body["state"]["pending"] is False
    assert body["state"]["paused"] is False
    assert body["state"]["kill_engaged"] is False
    # the SAME report was mirrored into LINE
    assert [m for _u, m in line.pushes if body["reply"] in m]


def test_api_popup_reject_keeps_limits_and_pause():
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    _mount(db)

    body = _popup_decide("reject").json()
    assert body["applied_decision"] == "reject"
    assert "ลิมิตเดิม" in body["reply"]
    assert "trading_settings" not in db._client.store   # nothing widened
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert db.rows["kill_expand_requests"][0]["status"] == "rejected"
    assert body["state"]["pending"] is False
    assert body["state"]["paused"] is True


def test_api_popup_rejects_an_unknown_decision():
    db = _dd_db(equity=8900.0)
    _mount(db)
    res = _popup_decide("maybe")
    assert res.status_code == 422
    assert not db.rows.get("kill_expand_requests")


def test_api_popup_approve_without_a_request_never_widens_anything():
    db = _dd_db(equity=8900.0)
    execution.set_pause(db, True, "manual")
    line = _mount(db)
    line.pushes.clear()

    body = _popup_decide("approve").json()
    assert body["applied_decision"] == ""      # nothing was pending
    assert "ไม่มีคำขอ" in body["reply"]
    assert "trading_settings" not in db._client.store
    # the manual pause is untouched — a bare approve is a no-op
    assert db._client.store["trading_pause"][1]["paused"] is True
    assert db._client.store["trading_pause"][1]["reason"] == "manual"
    assert line.pushes == []                   # nothing mirrored


def test_api_popup_reports_a_lapsed_window_as_auto_applied():
    """กดปุ่มหลังหมดเวลา → ระบบขยายให้เอง และยัง mirror รายงานเข้า LINE."""
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    db.rows["kill_expand_requests"][0]["requested_at"] = _minutes_ago(181.0)
    db.rows["line_users"] = [{"id": "lu-1", "line_user_id": "U-owner",
                              "notification_enabled": True}]
    line = _mount(db)
    line.pushes.clear()

    body = _popup_decide("approve").json()

    assert body["applied_decision"] == "auto"     # ไม่ใช่ผลจากการกดปุ่ม
    assert "หมดเวลายืนยัน" in body["reply"]
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
    assert db.rows["kill_expand_requests"][0]["decided_by"] == \
        limit_expand.AUTO_DECIDED_BY
    assert body["state"]["pending"] is False
    assert [m for _u, m in line.pushes if body["reply"] in m]


def test_api_popup_approve_survives_a_line_push_failure():
    """A dead LINE channel must not undo an already-applied decision."""
    db, s = _dd_db(equity=8900.0), AppSettings()
    limit_expand.request_and_notify(db, s, RecordingNotifier())
    _mount(db)

    from app.main import app

    class _DeadLine:
        async def push(self, *a, **k):
            raise RuntimeError("LINE 400")

        async def reply(self, *a, **k):
            raise RuntimeError("LINE 400")

    app.state.line = _DeadLine()
    res = _popup_decide("approve")
    assert res.status_code == 200
    assert res.json()["applied_decision"] == "approve"
    assert db._client.store["trading_settings"][1]["max_drawdown_pct"] == 15.0
