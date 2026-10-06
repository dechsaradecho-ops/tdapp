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

    def test_log_maintenance_runs_it(self):
        """Wiring matters as much as the function — a purge nobody calls is
        the exact gap this worker was written to close."""
        import inspect

        from app.workers import log_maintenance
        src = inspect.getsource(log_maintenance.run_once)
        assert "simulation.purge_old_runs" in src
        assert "import simulation" in inspect.getsource(log_maintenance)
