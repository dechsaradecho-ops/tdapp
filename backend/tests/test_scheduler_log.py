"""Tests for SL-move LINE alert + scheduler run log.

Covers: scheduler_log.log_run/purge/summary, _safe_job run logging
(ok/error), GET /api/system/scheduler-logs, and the guard SL-move
notifier.notify(stop_loss) hook.

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_scheduler_log.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services import scheduler_log
from tests.test_workers import FakeDatabase
from tests.test_api_routes import call, set_state


def _row(days_old: float, job_id: str = "position_guard",
         status: str = "ok") -> dict:
    created = (datetime.now(timezone.utc) - timedelta(days=days_old)).isoformat()
    return {"id": f"row-{days_old}-{job_id}-{status}", "created_at": created,
            "job_id": job_id, "status": status, "duration_ms": 12,
            "detail": "checked=1", "error": ""}


class TestLogRun:
    def test_log_run_inserts_row(self):
        db = FakeDatabase()
        scheduler_log.log_run(db=db, job_id="position_guard", status="ok",
                              duration_ms=34, detail="checked=1, moved_sl=1")
        assert len(db.inserted) == 1
        table, row = db.inserted[0]
        assert table == "scheduler_runs"
        assert row["job_id"] == "position_guard"
        assert row["status"] == "ok"
        assert row["duration_ms"] == 34

    def test_log_run_skips_when_db_none(self):
        scheduler_log.log_run(db=None, job_id="x")  # must not raise

    def test_log_run_swallows_db_errors(self):
        class Boom:
            available = True

            def insert(self, *a, **k):
                raise RuntimeError("db down")
        scheduler_log.log_run(db=Boom(), job_id="x")  # must not raise

    def test_summarize_dict(self):
        s = scheduler_log._summarize({"checked": 1, "moved_sl": 1})
        assert "checked=1" in s and "moved_sl=1" in s

    def test_summarize_none(self):
        assert scheduler_log._summarize(None) == ""

    def test_summarize_keeps_symbol_audit_lists(self):
        """sl_assets/closed_assets ต้องรอดจากการตัดความยาว

        เดิม limit=300 และ guard detail ยาวกว่านั้น ทำให้ list ชื่อคู่เงิน
        (ซึ่งอยู่ท้ายสุด) หายไป → หน้า Logs > Guard ไม่รู้ว่าคู่ไหนถูกขยับ
        sl_assets รูปแบบปัจจุบัน = ASSET@SLเดิม>SLใหม่ (บอก "ขยับจากเท่าไร")
        """
        row = {
            "checked": 4, "closed": 1, "moved_sl": 2, "partial_closed": 0,
            "smart_closed": 1, "smart_partials": 0, "smart_skipped": 0,
            "emergency_closed": 0,
            "sl_assets": "EURCHF@0.93624>0.94337;AUDNZD@1.0810>1.0821",
            "closed_assets": "XAUUSD:tp@4463.2",
            "skip_assets": "GBPCHF:no_snapshot",
        }
        s = scheduler_log._summarize(row)
        assert "sl_assets=EURCHF@0.93624>0.94337;AUDNZD@1.0810>1.0821" in s
        assert "closed_assets=XAUUSD:tp@4463.2" in s
        assert "skip_assets=GBPCHF:no_snapshot" in s
        # ยังต้องอยู่ในเพดานคอลัมน์ 500 ตัวอักษร
        assert len(s) <= 500

    def test_summarize_marks_a_cut_line(self):
        """บรรทัดที่ยาวเกิน limit ต้องลงท้ายด้วย "…"

        list ใน guard ถูกคั่นด้วย "," — ถ้าตัดเงียบ token สุดท้ายจะเหลือ
        "EURCHF@0.94" ซึ่งหน้าบ้านอ่านเป็นราคาจริงได้ ต้องมี marker ให้ทิ้ง
        """
        row = {f"k{i}": "x" * 60 for i in range(12)}
        s = scheduler_log._summarize(row)
        assert len(s) <= 480
        assert s.endswith("…")

    def test_summarize_keeps_marker_off_a_short_line(self):
        s = scheduler_log._summarize({"checked": 4, "moved_sl": 0,
                                      "sl_assets": ""})
        assert not s.endswith("…")


class TestPurge:
    def test_purge_deletes_rows_older_than_7_days(self):
        db = FakeDatabase(rows={"scheduler_runs": [_row(8), _row(10), _row(3)]})
        scheduler_log._last_purge = 0.0
        deleted = scheduler_log.purge_old_logs(db, force=True)
        assert deleted == 2
        assert len(db.rows["scheduler_runs"]) == 1

    def test_purge_throttled(self):
        db = FakeDatabase(rows={"scheduler_runs": [_row(10)]})
        scheduler_log.purge_old_logs(db, force=True)
        assert scheduler_log.purge_old_logs(db) == 0


class TestSummary:
    def test_summary_counts_by_job(self):
        db = FakeDatabase(rows={"scheduler_runs": [
            _row(1, "position_guard", "ok"), _row(1, "position_guard", "error"),
            _row(1, "market_scanner", "ok"), _row(8, "position_guard", "ok"),
        ]})
        out = scheduler_log.summary(db)
        assert out["total"] == 3  # 8-day row excluded
        assert out["ok"] == 2 and out["error"] == 1
        assert out["by_job"]["position_guard"] == {"total": 2, "ok": 1, "error": 1}
        assert out["by_job"]["market_scanner"]["total"] == 1


class TestSafeJob:
    @pytest.mark.asyncio
    async def test_safe_job_logs_ok(self):
        from app import main as main_mod

        db = FakeDatabase()

        async def fake_coro():
            return {"checked": 2, "moved_sl": 1}

        await main_mod._safe_job(fake_coro(), db, "position_guard")
        assert len(db.inserted) == 1
        table, row = db.inserted[0]
        assert table == "scheduler_runs"
        assert row["status"] == "ok" and "checked=2" in row["detail"]

    @pytest.mark.asyncio
    async def test_safe_job_logs_error(self):
        from app import main as main_mod

        db = FakeDatabase()

        async def boom():
            raise RuntimeError("kaboom")

        await main_mod._safe_job(boom(), db, "auto_trader")  # must not raise
        assert len(db.inserted) == 1
        assert db.inserted[0][1]["status"] == "error"
        assert "kaboom" in db.inserted[0][1]["error"]

    @pytest.mark.asyncio
    async def test_safe_job_watchdog_logs_error(self):
        """A tick that overruns its budget must still write a row.

        Prod 2026-09-11: position_guard took ~55s/cycle, so APScheduler's
        max_instances=1 skipped every following tick and NO scheduler_runs
        row was ever written — the Logs page looked empty and wrongly blamed
        migration 030. The watchdog bounds the tick and logs a timeout row.
        """
        import asyncio as _asyncio

        from app import main as main_mod

        db = FakeDatabase()

        async def hang():
            await _asyncio.sleep(30)

        await main_mod._safe_job(hang(), db, "position_guard", timeout_s=0.05)
        assert len(db.inserted) == 1
        row = db.inserted[0][1]
        assert row["status"] == "error"
        assert "watchdog" in row["error"]

    @pytest.mark.asyncio
    async def test_safe_job_no_watchdog_when_unset(self):
        """timeout_s=0 (default) keeps the unbounded behaviour for long jobs."""
        from app import main as main_mod

        db = FakeDatabase()

        async def quick():
            return {"checked": 1}

        await main_mod._safe_job(quick(), db, "market_scanner")
        assert db.inserted[0][1]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_safe_wrapper_passes_timeout(self):
        """_safe(...) must forward timeout_s down to _safe_job."""
        import asyncio as _asyncio

        from app import main as main_mod

        db = FakeDatabase()

        async def hang():
            await _asyncio.sleep(30)

        await main_mod._safe(hang, db, "position_guard", timeout_s=0.05)()
        assert len(db.inserted) == 1
        assert db.inserted[0][1]["status"] == "error"
        assert "watchdog" in db.inserted[0][1]["error"]

    @pytest.mark.asyncio
    async def test_overlapping_tick_logs_visible_skip(self):
        """A tick that arrives while the previous one is still running must
        write a VISIBLE "skipped_prev_running=1" row.

        Prod 2026-09-11: max_instances=1 made APScheduler drop those ticks
        with nothing but a warning, so the Guard tab stayed empty even though
        the guard was demonstrably moving SLs all day. Invisible skips are
        the bug — the skip itself is acceptable.
        """
        import asyncio as _asyncio

        from app import main as main_mod

        db = FakeDatabase()
        gate = _asyncio.Event()

        async def slow():
            await gate.wait()
            return {"checked": 3}

        task = _asyncio.ensure_future(
            main_mod._safe_job(slow(), db, "position_guard"))
        await _asyncio.sleep(0)              # let the first tick enter flight
        assert "position_guard" in main_mod._JOB_IN_FLIGHT

        async def never_runs():
            raise AssertionError("overlapping tick must not be awaited")

        await main_mod._safe_job(never_runs(), db, "position_guard")

        gate.set()
        await task
        assert len(db.inserted) == 2
        skip_row = db.inserted[0][1]
        assert skip_row["status"] == "ok"
        assert "skipped_prev_running=1" in skip_row["detail"]
        # the real tick still logged its own result afterwards
        assert "checked=3" in db.inserted[1][1]["detail"]
        assert not main_mod._JOB_IN_FLIGHT

    @pytest.mark.asyncio
    async def test_job_stats_heartbeat(self):
        """job_stats() must prove invocation even when rows exist elsewhere."""
        from app import main as main_mod

        main_mod._JOB_STATS.pop("heartbeat_probe", None)
        db = FakeDatabase()

        async def ok():
            return {"checked": 1}

        await main_mod._safe_job(ok(), db, "heartbeat_probe")
        st = main_mod.job_stats()["heartbeat_probe"]
        assert st["ticks"] == 1 and st["ok"] == 1 and st["error"] == 0
        assert st["last_status"] == "ok" and "checked=1" in st["last_detail"]
        assert st["running_s"] is None     # not left marked as running
        main_mod._JOB_STATS.pop("heartbeat_probe", None)


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_scheduler_logs_endpoint(self):
        db = FakeDatabase(rows={"scheduler_runs": [
            _row(1, "position_guard", "ok"), _row(1, "auto_trader", "error"),
        ]})
        set_state(db)
        resp = await call("GET", "/api/system/scheduler-logs?limit=100&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["verdict"] == "ok"
        assert body["total"] == 2
        assert len(body["logs"]) == 2
        assert body["summary"]["total"] == 2

    @pytest.mark.asyncio
    async def test_scheduler_logs_job_filter(self):
        db = FakeDatabase(rows={"scheduler_runs": [
            _row(1, "position_guard", "ok"), _row(1, "auto_trader", "error"),
        ]})
        set_state(db)
        resp = await call("GET", "/api/system/scheduler-logs?limit=100&offset=0&job=position_guard")
        assert resp.status_code == 200
        body = resp.json()
        assert all(r["job_id"] == "position_guard" for r in body["logs"])


# ---------------------------------------------------------------------------
# SL-move LINE alert — guard must notify(stop_loss) with old → new on move
# ---------------------------------------------------------------------------
class _AsyncModifySL:
    def __init__(self, sink: list, value: float):
        self._sink, self._value = sink, value

    def __await__(self):
        async def _coro():
            self._sink.append(self._value)
            from app.integrations.brokers import OrderResult
            return OrderResult(ok=True, message="SL moved")
        return _coro().__await__()


class _AsyncList:
    def __init__(self, items):
        self._items = items

    def __await__(self):
        async def _coro():
            return self._items
        return _coro().__await__()


class _AsyncFloat:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value
        return _coro().__await__()


class _AsyncClosed:
    ok = True
    message = "closed"

    def __await__(self):
        async def _coro():
            return self
        return _coro().__await__()


class _Recorder:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, user_id, ntype, message, **_kw):
        self.sent.append((user_id, ntype, message))
        return None


class TestSlMoveNotify:
    @pytest.fixture(autouse=True)
    def _no_network_spot(self, monkeypatch):
        from app.workers import position_guard

        async def fake_spot(assets, **_kw):
            return {a: 1.2500 for a in assets}, {}
        monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)

    def _broker(self):
        from app.integrations.brokers import Position
        broker = SimpleNamespace()
        broker._positions = {"T1": Position(
            ticket="T1", user_id="u1", asset="EURUSD", direction="BUY",
            volume=0.02, entry_price=1.1000, stop_loss=1.0900,
            take_profit=None, current_price=1.1000)}
        broker.all_positions = lambda: _AsyncList(list(broker._positions.values()))
        broker.mark_price = lambda ticket: _AsyncFloat(1.1000)
        broker.quote = lambda asset: _AsyncFloat(0.0)
        broker.close_position = lambda ticket: _AsyncClosed()
        return broker

    @pytest.mark.asyncio
    async def test_sl_move_sends_stop_loss_notify(self):
        from app.models.schemas import AppSettings
        from app.workers import position_guard

        moved: list[float] = []
        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL(moved, sl)
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
             "direction": "buy", "volume": 0.02, "entry_price": 1.1000,
             "stop_loss": 1.0900, "partial_done": False}]})
        rec = _Recorder()
        summary = await position_guard.guard_once(
            db, broker, rec,
            settings=AppSettings(breakeven_trigger_r=1.0, trail_atr_mult=0))
        assert summary["moved_sl"] == 1
        # audit token ต้องบอก SL เดิม→ใหม่ (marker ที่หน้า Guard ใช้โชว์ chip)
        assert summary["sl_assets"] == "EURUSD@1.09>1.1"
        sl_notes = [m for (_, t, m) in rec.sent if t == "stop_loss"]
        assert sl_notes, "SL move must notify with stop_loss type"
        assert "1.09" in sl_notes[0] and "1.1" in sl_notes[0]

    @pytest.mark.asyncio
    async def test_no_move_no_notify(self):
        from app.models.schemas import AppSettings
        from app.workers import position_guard

        broker = self._broker()
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL([], sl)
        db = FakeDatabase(rows={"paper_trades": [
            {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
             "direction": "buy", "volume": 0.02, "entry_price": 1.1000,
             "stop_loss": 1.0900, "partial_done": False}]})
        rec = _Recorder()
        # trigger above any plausible profit → no move
        await position_guard.guard_once(
            db, broker, rec,
            settings=AppSettings(breakeven_trigger_r=500.0, trail_atr_mult=0))
        assert not [t for (_, t, _) in rec.sent if t == "stop_loss"]


# ---------------------------------------------------------------------------
# Guard cycle budget — a slow feed must degrade to SL/TP-only, never hang
# ---------------------------------------------------------------------------
class TestGuardContextBudget:
    """`guard_once` must finish well inside its 1-min scheduler interval.

    Prod 2026-09-11: spot(20s) + snapshot(35s) + AI pushed a cycle to ~55s.
    With max_instances=1 the overrun made APScheduler skip every following
    tick, so position_guard never completed-and-logged (zero rows). The
    feeds now run CONCURRENTLY and each has its own cap; the SL/TP safety
    path always runs.

    This test mirrors the drift guard for the parallel-feed refactor: an
    old implementation that awaited `fetch_all_snapshots` without a cap
    would block here for 30s and fail the outer 5s wait_for.
    """

    @pytest.mark.asyncio
    async def test_slow_snapshot_degrades_and_still_closes(self, monkeypatch):
        import asyncio as _asyncio

        from app.integrations.brokers import Position
        from app.models.schemas import AppSettings
        from app.workers import position_guard

        # tiny snapshot cap so the test does not wait 30s
        monkeypatch.setattr(position_guard, "_GUARD_SNAP_BUDGET", 0.05)

        async def fake_spot(assets, **_kw):
            return {a: 1.2500 for a in assets}, {}

        async def hang_snaps(assets, **_kw):
            await _asyncio.sleep(30)
            return {}

        async def fake_news(db, s):
            return "SAFE", ""

        monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)
        monkeypatch.setattr(position_guard.quotes, "fetch_all_snapshots", hang_snaps)
        monkeypatch.setattr(position_guard, "_smart_exit_news", fake_news)

        broker = SimpleNamespace()
        broker._positions = {"T1": Position(
            ticket="T1", user_id="u1", asset="EURUSD", direction="BUY",
            volume=0.02, entry_price=1.1000, stop_loss=1.0900,
            take_profit=1.2000, current_price=1.1000)}
        broker.all_positions = lambda: _AsyncList(list(broker._positions.values()))
        broker.mark_price = lambda ticket: _AsyncFloat(0.0)
        broker.quote = lambda asset: _AsyncFloat(0.0)
        broker.modify_stop_loss = lambda ticket, sl: _AsyncModifySL([], sl)
        closed: list[str] = []

        class _Close:
            ok = True
            message = "closed"

            def __await__(self):
                async def _c():
                    closed.append("T1")
                    return self
                return _c().__await__()

        broker.close_position = lambda ticket: _Close()

        db = FakeDatabase(rows={"paper_trades": []})
        rec = _Recorder()

        summary = await _asyncio.wait_for(
            position_guard.guard_once(db, broker, rec,
                                      settings=AppSettings()),
            timeout=5)

        # snapshot timed out → no AI eval, but the SL/TP safety path ran:
        # live mark 1.2500 ≥ TP 1.2000 on a BUY → closed
        assert summary["checked"] == 1
        assert summary["closed"] == 1
        assert closed == ["T1"]
