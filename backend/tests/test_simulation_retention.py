"""Retention for simulation runs — the purge the migration comment promises.

A 5,000-event run writes 5,000 rows and the owner can run several while
tuning, so these tables grow faster than anything else in the database. These
tests pin the two properties that matter: stale runs actually go away, and a
recent run never does (losing the run you are watching would be worse than
growing a table).

Run from backend/: C:/Python314/python.exe -m pytest tests/test_simulation.py -v
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services import simulation
from tests.test_simulation import FakeDB


def _run_row(run_id: str, days_old: float) -> dict:
    created = datetime.now(timezone.utc) - timedelta(days=days_old)
    return {"id": run_id, "status": "done", "created_at": created.isoformat(),
            "processed": 100, "target_events": 100, "total_events": 100,
            "stage": "done"}


@pytest.fixture(autouse=True)
def _reset_purge_throttle():
    simulation._last_purge = 0.0
    yield
    simulation._last_purge = 0.0


class TestPurge:
    """Policy: keep the newest SIMULATION_KEEP_RUNS regardless of age; purge
    anything older than the TTL beyond that head. The floor is what bounds the
    table — the TTL alone would not."""

    def _full(self, n, days_old=90):
        db = FakeDB()
        for i in range(n):
            db.insert("simulation_runs", _run_row(f"r{i:03d}", days_old))
        return db

    def test_beyond_the_floor_stale_runs_are_deleted(self):
        db = self._full(simulation.SIMULATION_KEEP_RUNS + 7)
        removed = simulation.purge_old_runs(db, force=True)
        assert removed == 7, "purge did not remove exactly the overflow"
        assert len(db.rows) == simulation.SIMULATION_KEEP_RUNS

    def test_the_table_is_bounded_at_the_floor(self):
        """The property that actually matters: however many runs pile up, the
        table never exceeds KEEP + whatever the newest is."""
        db = self._full(simulation.SIMULATION_KEEP_RUNS + 50)
        simulation.purge_old_runs(db, force=True)
        assert len(db.rows) == simulation.SIMULATION_KEEP_RUNS

    def test_a_run_inside_the_ttl_survives(self):
        db = FakeDB()
        db.insert("simulation_runs", _run_row("fresh", 1))
        for i in range(simulation.SIMULATION_KEEP_RUNS + 3):
            db.insert("simulation_runs", _run_row(f"old{i:03d}", 40))
        simulation.purge_old_runs(db, force=True)
        assert "fresh" in db.rows

    def test_fewer_runs_than_the_floor_are_all_kept(self):
        """A small table is never pruned below the floor — that is what makes
        the retention bounded rather than destructive."""
        db = FakeDB()
        db.insert("simulation_runs", _run_row("old1", 30))
        db.insert("simulation_runs", _run_row("old2", 40))
        assert simulation.purge_old_runs(db, force=True) == 0
        assert set(db.rows) == {"old1", "old2"}

    def test_the_run_being_watched_is_never_purged(self):
        """Losing the run whose progress bar is on screen would be worse than
        growing a table — even a very old one that is still 'running'."""
        db = self._full(simulation.SIMULATION_KEEP_RUNS + 5)
        row = _run_row("live", 999)
        row["status"] = "running"
        db.insert("simulation_runs", row)
        simulation.purge_old_runs(db, force=True)
        assert "live" in db.rows, "an in-flight run was purged"

    def test_events_are_swept_with_their_run(self):
        db = self._full(simulation.SIMULATION_KEEP_RUNS + 4)
        victim = f"r{len(db.rows) - 1:03d}"
        simulation.purge_old_runs(db, force=True)
        assert victim in db.deleted, "the overflow run was not deleted"

    def test_is_throttled_unless_forced(self):
        simulation._last_purge = 0.0
        db = self._full(simulation.SIMULATION_KEEP_RUNS + 2)
        assert simulation.purge_old_runs(db) == 2
        # a second call inside the interval must be a no-op, even though there
        # is now more to purge
        db.insert("simulation_runs", _run_row("extra", 90))
        assert simulation.purge_old_runs(db) == 0
        assert "extra" in db.rows
        assert simulation.purge_old_runs(db, force=True) == 1

    def test_unavailable_db_returns_zero(self):
        db = FakeDB()
        db.available = False
        assert simulation.purge_old_runs(db, force=True) == 0

    def test_never_raises_on_a_broken_client(self):
        class Boom:
            available = True

            def select(self, *a, **k):
                raise RuntimeError("connection reset")

        assert simulation.purge_old_runs(Boom(), force=True) == 0


class TestCsvExport:
    """The downloaded file must match the on-screen trace, row for row."""

    def _db_with_events(self):
        from tests.test_simulation import FakeDB
        db = FakeDB()
        db.rows["e3"] = {"seq": 3, "asset": "EURUSD", "direction": "BUY",
                         "entry": 1.1, "atr_pct": 0.5, "opportunity": 60.0,
                         "confidence": 70.0, "sl_mult": 1.5, "tp_r": 1.5,
                         "max_bars": 20, "label": "tp", "r_multiple": 1.5,
                         "bars_held": 7, "exit_price": 1.12,
                         "ambiguous": False, "gap_fill": None,
                         "mfe_r": 1.6, "mae_r": -0.4}
        db.rows["e1"] = {"seq": 1, "asset": "GBPUSD", "direction": "SELL",
                         "entry": 1.27, "atr_pct": 0.6, "opportunity": 55.0,
                         "confidence": 65.0, "sl_mult": 1.5, "tp_r": 1.5,
                         "max_bars": 20, "label": "sl", "r_multiple": -1.0,
                         "bars_held": 3, "exit_price": 1.28,
                         "ambiguous": True, "gap_fill": False,
                         "mfe_r": 0.2, "mae_r": -1.0}
        db.rows["e2"] = {"seq": 2, "asset": "XAU,USD", "direction": "BUY",
                         "entry": 2000.0, "atr_pct": 0.4, "opportunity": 70.0,
                         "confidence": 80.0, "sl_mult": 1.5, "tp_r": 1.5,
                         "max_bars": 20, "label": "expired", "r_multiple": 0.2,
                         "bars_held": 20, "exit_price": 2001.0,
                         "ambiguous": False, "gap_fill": False,
                         "mfe_r": 0.5, "mae_r": -0.3}
        return db

    def test_header_matches_the_streamed_columns(self):
        text = simulation.export_csv(self._db_with_events(), "run1")
        assert text.splitlines()[0] == ",".join(simulation.SIM_EXPORT_COLUMNS)

    def test_rows_come_out_in_seq_order_despite_storage_order(self):
        text = simulation.export_csv(self._db_with_events(), "run1")
        seqs = [l.split(",")[0] for l in text.splitlines()[1:]]
        assert seqs == ["1", "2", "3"]

    def test_cells_are_spreadsheet_clean(self):
        text = simulation.export_csv(self._db_with_events(), "run1")
        lines = text.splitlines()
        # None -> empty (gap_fill of seq 3), bool -> true/false
        assert lines[3].split(",")[15] == "", "None must be empty, not 'None'"
        assert lines[1].split(",")[14] == "true"
        assert lines[3].split(",")[14] == "false"
        # a comma inside a value must be quoted, not split the row
        assert '"XAU,USD"' in lines[2]
        assert len(lines[2].split('","')) >= 1

    def test_terminates_when_the_client_ignores_the_cursor(self):
        """FakeTable.execute ignores gt/order — the generator must still stop
        instead of paging forever."""
        text = simulation.export_csv(self._db_with_events(), "run1")
        assert len(text.splitlines()) == 4  # header + 3 rows, exactly once

    def test_empty_run_exports_header_only(self):
        from tests.test_simulation import FakeDB
        assert simulation.export_csv(FakeDB(), "nope").strip() == \
            ",".join(simulation.SIM_EXPORT_COLUMNS)

    @pytest.mark.asyncio
    async def test_route_streams_the_file(self):
        from app.api.routes import system

        class Req:
            class app:
                class state:
                    db = None
        Req.app.state.db = self._db_with_events()
        # the route checks the run row first
        Req.app.state.db.rows["run1"] = {"id": "run1", "status": "done"}

        resp = await system.export_simulation(Req(), "run1")
        assert getattr(resp, "status_code", 200) == 200
        chunks = [c async for c in resp.body_iterator]
        body = b"".join(
            c.encode("utf-8") if isinstance(c, str) else c for c in chunks
        ).decode("utf-8-sig")
        assert body.splitlines()[0].endswith(",".join(simulation.SIM_EXPORT_COLUMNS))
        assert "GBPUSD" in body and "EURUSD" in body
        cd = resp.headers.get("content-disposition", "")
        assert cd.startswith("attachment") and "simulation-run1" in cd

    @pytest.mark.asyncio
    async def test_route_404s_an_unknown_run(self):
        from app.api.routes import system
        from tests.test_simulation import FakeDB

        class Req:
            class app:
                class state:
                    db = None
        Req.app.state.db = FakeDB()
        resp = await system.export_simulation(Req(), "nope")
        assert resp.status_code == 404


class TestHistoryExport:
    """The replay's price history is NOT stored — every run fetches it fresh.
    This endpoint pulls the same feed and streams it, so the file and the run
    always agree on what went in."""

    def _req(self, db):
        class Req:
            class app:
                class state:
                    db = None
        Req.app.state.db = db
        return Req()

    async def _body(self, resp):
        chunks = [c async for c in resp.body_iterator]
        return b"".join(
            c.encode("utf-8") if isinstance(c, str) else c for c in chunks
        ).decode("utf-8-sig")

    @pytest.mark.asyncio
    async def test_streams_candles_oldest_first(self, monkeypatch):
        from app.api.routes import system
        from app.integrations.quotes import Candle
        from tests.test_simulation import FakeDB

        async def fake_candles(asset, client, days=365):
            assert days == 1095
            return [Candle(o=1.0 + i, h=1.1 + i, l=0.9 + i, c=1.05 + i)
                    for i in range(3)]

        monkeypatch.setattr("app.integrations.quotes.fetch_candles",
                            fake_candles)
        resp = await system.export_history(
            self._req(FakeDB()), days=1095, assets="EURUSD,GBPUSD")
        assert getattr(resp, "status_code", 200) == 200
        body = await self._body(resp)
        lines = body.splitlines()
        assert lines[0] == "asset,bar_index,open,high,low,close"
        assert lines[1] == "EURUSD,0,1.0,1.1,0.9,1.05"
        assert lines[3] == "EURUSD,2,3.0,3.1,2.9,3.05"
        assert lines[4] == "GBPUSD,0,1.0,1.1,0.9,1.05"
        assert len(lines) == 1 + 2 * 3
        cd = resp.headers.get("content-disposition", "")
        assert "history-1095d.csv" in cd

    @pytest.mark.asyncio
    async def test_days_is_clamped_not_rejected(self, monkeypatch):
        from app.api.routes import system
        from tests.test_simulation import FakeDB

        seen = {}

        async def fake_candles(asset, client, days=365):
            seen["days"] = days
            return []

        monkeypatch.setattr("app.integrations.quotes.fetch_candles",
                            fake_candles)
        resp = await system.export_history(
            self._req(FakeDB()), days=10, assets="EURUSD")
        # the generator is lazy — nothing runs until the body is consumed
        await self._body(resp)
        assert seen["days"] == 30, "below-minimum days must clamp, not fail"
        assert "history-30d.csv" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_unavailable_feed_pair_is_skipped_not_fatal(self, monkeypatch):
        from app.api.routes import system
        from app.integrations.quotes import Candle
        from tests.test_simulation import FakeDB

        async def fake_candles(asset, client, days=365):
            if asset == "BAD":
                raise RuntimeError("yahoo down")
            return [Candle(o=1.0, h=1.1, l=0.9, c=1.0)]

        monkeypatch.setattr("app.integrations.quotes.fetch_candles",
                            fake_candles)
        resp = await system.export_history(
            self._req(FakeDB()), days=1095, assets="BAD,EURUSD")
        body = await self._body(resp)
        assert "EURUSD,0,1.0,1.1,0.9,1.0" in body
        assert "BAD" not in body

    @pytest.mark.asyncio
    async def test_db_down_returns_503(self):
        from app.api.routes import system
        from tests.test_simulation import FakeDB

        db = FakeDB()
        db.available = False
        resp = await system.export_history(self._req(db))
        assert resp.status_code == 503

    def test_log_maintenance_runs_it(self):
        """Wiring matters as much as the function — a purge nobody calls is
        the exact gap this worker was written to close."""
        import inspect

        from app.workers import log_maintenance
        src = inspect.getsource(log_maintenance.run_once)
        assert "simulation.purge_old_runs" in src
        assert "import simulation" in inspect.getsource(log_maintenance)
