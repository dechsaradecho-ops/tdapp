"""Tests for the risk audit trail (risk_events) + GET /api/system/risk-logs.

WHY this file exists — prod 2026-09-14: **no** audit row ever landed.
risk_events was created in 001 as `user_id uuid not null references users(id)`
and every caller passes the pseudo-user 'demo' → PostgREST 22P02
`invalid input syntax for type uuid: "demo"`, which `Database.insert` reduced
to a single debug log line (invisible). Both write paths were affected:

  * portfolio_monitor → event_type "limit_breach" (kill switch fired)
  * limit_expand      → "limit_expanded" / "limit_expand_rejected"

The fix is database/038_risk_events_user_text.sql (user_id → text) **plus**
making the write path loud instead of silent, so a missing migration shows up
as an error instead of an empty audit trail. The Logs page now has an "Audit"
tab that reads this table, so both the write and the read path are locked
here.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_risk_logs.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import limit_expand
from app.workers import log_maintenance
from tests.test_workers import FakeDatabase
from tests.test_api_routes import call, set_state


def _stamp(minutes_old: float = 1.0) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes_old)).isoformat()


def _event(minutes_old: float = 1.0, etype: str = "limit_expanded",
           **detail) -> dict:
    return {"id": f"ev-{minutes_old}-{etype}", "created_at": _stamp(minutes_old),
            "user_id": "demo", "event_type": etype,
            "resolved_at": None, "detail": detail or {"approved": True}}


def _request(status: str = "approved", **extra) -> dict:
    row = {"id": "req-1", "status": status, "trigger_type": "monthly_loss",
           "metric_value": 9.5, "limit_before": 8.0, "limit_after": 13.0,
           "requested_at": _stamp(120), "decided_at": _stamp(60),
           "decided_by": "line:group", "detail": {"source": "monitor"}}
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def _clear_audit_fail_memory():
    """`_AUDIT_FAIL` เป็นหน่วยความจำระดับ process — ต้องล้างก่อน/หลังทุกเทสต์.

    ถ้าไม่ล้าง เทสต์ที่จำลองการเขียนล้มเหลวจะไปทำให้เทสต์ถัดไปเห็น audit_hint
    ทั้งที่ในเทสต์นั้นไม่ได้มีอะไรพัง (และกลับกัน: เทสต์นี้จะจับได้เองถ้าลืมล้าง)
    """
    limit_expand._AUDIT_FAIL.clear()
    yield
    limit_expand._AUDIT_FAIL.clear()


# ---------------------------------------------------------------------------
# Write path — must NOT swallow the error any more
# ---------------------------------------------------------------------------
class TestWriteAudit:
    def test_writes_text_user_id_and_detail(self):
        """user_id ต้องเป็น text 'demo' (ตรงกับ migration 038) ไม่ใช่ uuid."""
        db = FakeDatabase()
        assert limit_expand.write_audit(db, "limit_expanded", {"approved": True}) is None
        table, row = db.inserted[0]
        assert table == "risk_events"
        assert row["user_id"] == "demo"
        assert row["event_type"] == "limit_expanded"
        assert row["detail"] == {"approved": True}

    def test_returns_raw_error_instead_of_raising(self):
        """ตารางปฏิเสธการเขียน (เช่นยังไม่รัน 038) → คืน error ดิบ ไม่ throw

        คืนค่าออกมาสำคัญ: ผู้เรียก (และเทสต์) ต้องแยกได้ว่า audit ลงหรือไม่
        แทนที่จะเห็น log.debug แล้วเข้าใจว่าสำเร็จ
        """
        db = FakeDatabase(fail_tables={"risk_events"})
        err = limit_expand.write_audit(db, "limit_breach", {"risk_level": "high"})
        assert err and "row-level security" in err
        assert not db.inserted

    def test_works_with_old_fake_without_insert_raw(self):
        class _Plain:
            def __init__(self):
                self.rows: list[tuple[str, dict]] = []

            def insert(self, table, row):
                self.rows.append((table, dict(row)))
                return row

        db = _Plain()
        assert limit_expand.write_audit(db, "limit_expanded", {}) is None
        assert db.rows[0][0] == "risk_events"

    def test_old_fake_insert_raising_does_not_propagate(self):
        class _Boom:
            def insert(self, table, row):
                raise RuntimeError("db down")

        err = limit_expand.write_audit(_Boom(), "limit_expanded", {})
        assert err and "db down" in err      # reported, not raised

    def test_audit_helper_keeps_decision_detail_shape(self):
        """_audit ยังเขียน key ชุดเดิม (request_id/approved/triggers/ลิมิต)."""
        db = FakeDatabase()
        limit_expand._audit(
            db, "limit_expand_rejected",
            {"id": "req-9", "requested_at": _stamp(90), "limit_before": 8.0,
             "limit_after": 13.0},
            [{"limit": "monthly_loss", "value": 9.5}],
            approved=False,
        )
        _table, row = db.inserted[0]
        assert row["event_type"] == "limit_expand_rejected"
        d = row["detail"]
        assert d["request_id"] == "req-9"
        assert d["approved"] is False
        assert d["limit_before"] == 8.0 and d["limit_after"] == 13.0
        assert d["triggers"][0]["limit"] == "monthly_loss"


class TestAuditPermanence:
    @pytest.mark.asyncio
    async def test_maintenance_never_purges_risk_events(self, monkeypatch):
        """risk_events = ประวัติถาวร: log_maintenance purge log อื่น แต่ไม่ลบตารางนี้.

        ถ้ามีใครเผลอเพิ่ม risk_events เข้า purge list หลักฐานการตัดสินใจของ
        เจ้าของจะหายไปตามอายุ — เทสต์นี้กันไว้
        """
        monkeypatch.setattr(log_maintenance, "_last_alert", 0.0)
        monkeypatch.setattr(log_maintenance, "ALERT_COOLDOWN_S", 0.0)
        old = _event(minutes_old=60 * 24 * 400)          # 400 วัน
        db = FakeDatabase(rows={"risk_events": [old]})
        await log_maintenance.run_once(db, notifier=None)
        assert db.rows.get("risk_events") == [old]


# ---------------------------------------------------------------------------
# "เขียนไม่ลง" ต้องเป็นข้อเท็จจริงที่จำไว้ ไม่ใช่การเดาจากตารางว่าง
# ---------------------------------------------------------------------------
class TestAuditFailMemory:
    def test_no_failure_recorded_is_none(self):
        assert limit_expand.audit_write_status() is None

    def test_failure_is_remembered_with_raw_error(self):
        db = FakeDatabase(fail_tables={"risk_events"})
        err = limit_expand.write_audit(db, "limit_breach", {"x": 1})
        st = limit_expand.audit_write_status()
        assert st is not None and err in st["error"]
        assert st["event_type"] == "limit_breach"
        assert st["age_min"] >= 0

    def test_success_after_failure_does_not_erase_the_evidence(self, monkeypatch):
        """ล้มเหลวแล้วสำเร็จทีหลัง: ยังต้องเห็นว่าก่อนหน้านี้มี error.

        WHY: หลักฐานคือ "เคยเขียนไม่ลงเมื่อไร" — ถ้าล้างทิ้งเมื่อเขียนสำเร็จ
        ผู้ใช้จะไม่เห็นสาเหตุของแถวที่หายไปก่อนหน้านี้เลย
        """
        limit_expand.write_audit(FakeDatabase(fail_tables={"risk_events"}),
                                 "limit_breach", {})
        limit_expand.write_audit(FakeDatabase(), "limit_breach", {})
        assert limit_expand.audit_write_status() is not None

    def test_stale_failure_is_forgotten(self, monkeypatch):
        """เกิน 24 ชม. = เก่าเกินกว่าจะเอามาเตือนหน้าบ้าน (ปัญหาเดิมแก้ไปแล้ว)."""
        db = FakeDatabase(fail_tables={"risk_events"})
        limit_expand.write_audit(db, "limit_breach", {})
        limit_expand._AUDIT_FAIL["at"] -= limit_expand.AUDIT_FAIL_TTL + 60
        assert limit_expand.audit_write_status() is None


class TestProbeAudit:
    """probe_audit = พิสูจน์ทางเขียน audit ด้วยเส้นทางเดียวกับของจริง."""

    def test_ok_writes_demo_user_and_deletes_the_row(self):
        db = FakeDatabase()
        res = limit_expand.probe_audit(db)
        table, row = db.inserted[0]
        assert table == "risk_events"
        assert row["user_id"] == "demo"              # pseudo-user ตาม 038
        assert row["event_type"] == limit_expand.AUDIT_PROBE_EVENT
        assert res["insert"] == "ok" and res["delete"] == "ok"
        # แถวทดสอบต้องไม่ค้างในตารางถาวร
        assert db.rows.get("risk_events") == []

    def test_missing_038_reports_uuid_hint(self):
        db = FakeDatabase(fail_tables={"risk_events"})
        res = limit_expand.probe_audit(db)
        assert res["insert"] == "FAIL"
        assert "row-level security" in res["error"]
        assert "038" in res["hint"]

    def test_old_fake_without_insert_raw(self):
        class _Plain:
            def __init__(self):
                self.rows: list[tuple[str, dict]] = []

            def insert(self, table, row):
                self.rows.append((table, dict(row)))
                return {**row, "id": "p1"}

            def delete(self, table, filters):
                return True

        db = _Plain()
        res = limit_expand.probe_audit(db)
        assert res["insert"] == "ok"
        assert db.rows[0][0] == "risk_events"


# ---------------------------------------------------------------------------
# Read path — GET /api/system/risk-logs (ที่หน้า Logs > Audit เรียก)
# ---------------------------------------------------------------------------
class TestRiskLogsEndpoint:
    @pytest.mark.asyncio
    async def test_returns_logs_requests_and_summary(self):
        db = FakeDatabase(rows={
            "risk_events": [
                _event(1, "limit_expanded", approved=True, limit_before=8.0,
                       limit_after=13.0),
                _event(2, "limit_breach", risk_level="high", breaches=["a", "b"]),
            ],
            "kill_expand_requests": [_request("approved")],
        })
        set_state(db)
        resp = await call("GET", "/api/system/risk-logs?limit=100&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "ok" and body["client"] == "ok"
        assert len(body["logs"]) == 2
        assert body["summary"]["by_event"]["limit_expanded"] == 1
        assert body["summary"]["by_event"]["limit_breach"] == 1
        assert body["summary"]["total"] == 2
        assert body["logs"][0]["detail"]["limit_after"] == 13.0
        # คำขอยืนยัน (ต้นทางของการตัดสินใจ) มากับ response เดียวกัน
        assert body["requests"][0]["status"] == "approved"
        assert body["requests"][0]["decided_by"] == "line:group"
        assert body["has_more"] is False
        assert "limit_breach" in body["event_types"]

    @pytest.mark.asyncio
    async def test_event_filter_and_paging_do_not_repeat_rows(self):
        db = FakeDatabase(rows={"risk_events": [
            _event(1, "limit_breach"), _event(2, "limit_expanded"),
            _event(3, "limit_breach"),
        ]})
        set_state(db)
        body = (await call("GET", "/api/system/risk-logs?limit=100&offset=0"
                                  "&event=limit_breach")).json()
        assert body["summary"]["total"] == 2
        assert all(r["event_type"] == "limit_breach" for r in body["logs"])
        # paging: หน้า 2 ของชุด 1 แถว/หน้า
        page1 = (await call("GET", "/api/system/risk-logs?limit=1&offset=0")).json()
        page2 = (await call("GET", "/api/system/risk-logs?limit=1&offset=1")).json()
        assert page1["has_more"] is True
        assert page1["logs"][0]["id"] != page2["logs"][0]["id"]

    @pytest.mark.asyncio
    async def test_empty_table_without_recorded_failure_does_not_blame_038(self):
        """ตารางว่าง + ไม่มี error ที่บันทึกไว้ → **ห้าม**บอกให้รัน 038 ซ้ำ.

        prod 2026-09-14: 038 รันไปแล้ว แต่ตารางยังว่างเพราะยังไม่มีเหตุการณ์ใหม่
        ข้อความเดิม ("ให้รัน 038") ทำให้เจ้าของไปตามหาปัญหาที่ไม่มีอยู่
        """
        db = FakeDatabase(rows={"kill_expand_requests": [_request("approved")]})
        set_state(db)
        body = (await call("GET", "/api/system/risk-logs")).json()
        assert body["logs"] == []
        assert body["audit_state"] == "empty"
        assert "risk_events" in body["audit_hint"]
        assert "รัน database/038" not in body["audit_hint"]
        # ต้องอธิบาย "แถวที่หายไป" ด้วยข้อเท็จจริง: การตัดสินใจที่อนุมัติแล้ว
        assert body["audit_missing"]["count"] == 1
        assert body["audit_missing"]["latest"]["id"] == "req-1"
        assert "audit_error" not in body

    @pytest.mark.asyncio
    async def test_empty_table_without_any_decision_points_at_db_check(self):
        """ยังไม่มีคำขอเลย → บอกกลาง ๆ ว่าแค่ยังไม่มีเหตุการณ์ + ชี้ทางพิสูจน์."""
        set_state(FakeDatabase())
        body = (await call("GET", "/api/system/risk-logs")).json()
        assert body["audit_state"] == "empty"
        assert "db-check" in body["audit_hint"]
        assert "audit_missing" not in body

    @pytest.mark.asyncio
    async def test_hint_reports_the_real_failure_when_write_actually_failed(self):
        """มีการเขียนที่ล้มเหลวจริง → hint ต้องมี error ดิบ + คำแนะนำ 038."""
        limit_expand.write_audit(FakeDatabase(fail_tables={"risk_events"}),
                                 "limit_breach", {"risk_level": "high"})
        db = FakeDatabase(rows={"kill_expand_requests": [_request("approved")]})
        set_state(db)
        body = (await call("GET", "/api/system/risk-logs")).json()
        assert body["audit_state"] == "write_failed"
        assert "row-level security" in body["audit_hint"]
        assert "038" in body["audit_hint"]
        assert "limit_breach" in body["audit_hint"]
        assert body["audit_error"]

    @pytest.mark.asyncio
    async def test_no_hint_once_audit_rows_exist(self):
        db = FakeDatabase(rows={
            "risk_events": [_event(1, "limit_expanded")],
            "kill_expand_requests": [_request("approved")],
        })
        set_state(db)
        body = (await call("GET", "/api/system/risk-logs")).json()
        assert "audit_hint" not in body
        assert body["audit_state"] == "ok"

    @pytest.mark.asyncio
    async def test_empty_table_is_ok_not_fail(self):
        """ยังไม่มีเหตุการณ์ ≠ ระบบพัง — verdict ต้องเป็น ok (ไม่ใช่ fail)."""
        db = FakeDatabase()
        set_state(db)
        body = (await call("GET", "/api/system/risk-logs")).json()
        assert body["verdict"] == "ok"
        assert body["logs"] == [] and body["requests"] == []
        assert body["summary"]["by_event"] == {}

    @pytest.mark.asyncio
    async def test_unavailable_db_reports_fail(self):
        class Dead:
            available = False
            init_error = "no env"

        from app.main import app
        app.state.db = Dead()
        res = await call("GET", "/api/system/risk-logs")
        assert res.status_code == 200
        body = res.json()
        assert body["verdict"] == "fail"
        assert body["client"] == "unavailable"


# ---------------------------------------------------------------------------
# GET /api/system/db-check — พิสูจน์ทางเขียน audit ได้ทันทีที่เรียก
# ---------------------------------------------------------------------------
class TestDbCheckRiskAudit:
    @pytest.mark.asyncio
    async def test_includes_risk_audit_probe_and_passes(self):
        """db-check ต้องทดสอบ risk_events ด้วย (038) ไม่ใช่แค่ market_analysis.

        ตาราง db_probe กับ market_analysis ไม่มี FK/ชนิดที่ทำให้ล้มแบบเดียวกับ
        audit trail — ถ้าไม่มีขั้นนี้ "db-check ผ่าน" จะยังไม่ยืนยันว่าหน้า Logs
        จะมีข้อมูล
        """
        db = FakeDatabase()
        set_state(db)
        body = (await call("GET", "/api/system/db-check")).json()
        assert body["risk_audit"]["table"] == "risk_events"
        assert body["risk_audit"]["insert"] == "ok"
        assert body["risk_audit"]["delete"] == "ok"
        assert body["verdict"] == "pass"
        assert "risk_audit_hint" not in body
        # แถวทดสอบต้องถูกเก็บกวาด (ไม่ค้างใน risk_events ถาวร)
        assert db.rows.get("risk_events") == []

    @pytest.mark.asyncio
    async def test_failing_risk_events_downgrades_verdict_and_explains(self):
        db = FakeDatabase(fail_tables={"risk_events"})
        set_state(db)
        body = (await call("GET", "/api/system/db-check")).json()
        assert body["risk_audit"]["insert"] == "FAIL"
        assert body["verdict"] == "partial"        # ไม่ใช่ fail — DB ยังใช้ได้
        assert "038" in body["risk_audit_hint"]

