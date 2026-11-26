"""Guarded diff-only recommendation apply (Task 5).

Safety-critical: applying a run's recommendation is the single explicit
exception to "the optimizer never writes live settings". Every guard must
hold with ZERO writes on refusal:

  1. unknown run -> refuse
  2. run not terminal-complete, or no recommendation -> refuse
  3. confirm is not literally True (incl. truthy non-True) -> refuse
  4. live settings drifted from the run's captured baseline -> refuse
     with a baseline-mentioning error
  5. empty recommendation diff -> success, applied == {}, no row write
  6. else: write ONLY the diff fields through the shared settings
     merge/audit path, audited as source="simulation_apply" + run id

A successful apply inherently makes a second apply stale (live now differs
from the stored baseline) — pinned below.

Observation note (brief mapping): ``fake_db.saved_settings`` observes
trading_settings writes concretely — the fake's supabase-style upsert diffs
the incoming row against the stored row and records only the fields whose
persisted values actually changed (bookkeeping ``id``/``updated_at``
excluded). ``set(saved_settings) == {...}`` therefore proves diff-only in
effect, not just in patch construction. ``settings_upserts`` counts every
upsert call so refusal paths can assert zero writes.

Run from backend/:
  D:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_sim_optimizer_apply.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from app.main import app
from app.models.schemas import AppSettings
from app.services import sim_optimizer
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# Harness: FakeDatabase + id-preserving run rows + supabase-style settings row
# ---------------------------------------------------------------------------
_MISSING = object()


class ApplySettingsClient:
    """Supabase-style chainable client for the single trading_settings row.

    Reads return the stored row; upserts merge into it (partial-write
    semantics, like production PostgREST) and record, in
    ``db.saved_settings``, only the fields whose persisted values actually
    changed — the brief's ``saved_settings`` observation, implemented
    concretely. ``db.settings_upserts`` counts every upsert call (zero-write
    assertions on refusal paths).
    """

    def __init__(self, db: "ApplyFakeDatabase", row: dict | None = None):
        self._db = db
        self.row = dict(row) if row else None

    def table(self, _name: str) -> "ApplySettingsClient":
        return self

    def select(self, _cols: str) -> "ApplySettingsClient":
        return self

    def eq(self, _col: str, _val: object) -> "ApplySettingsClient":
        return self

    def limit(self, _n: int) -> "ApplySettingsClient":
        return self

    def upsert(self, row: dict) -> "ApplySettingsClient":
        incoming = dict(row)
        base = self.row or {}
        changed = {
            k: v for k, v in incoming.items()
            if k not in ("id", "updated_at") and base.get(k, _MISSING) != v
        }
        self._db.saved_settings = changed
        self._db.settings_upserts.append(incoming)
        if self.row is None:
            self.row = {}
        self.row.update(incoming)
        self._in_write = True
        return self

    def delete(self) -> "ApplySettingsClient":
        self.row = None
        return self

    def execute(self) -> SimpleNamespace:
        return SimpleNamespace(data=[dict(self.row)] if self.row else [])


class ApplyFakeDatabase(FakeDatabase):
    """Stock fake + id-preservation shim (run rows are read back by id,
    which the stock fake's ``fake-N`` overwrite would break — same reason as
    the Task 4 ``OptFakeDatabase``) + supabase-style settings client."""

    def __init__(self, settings_row: dict | None = None):
        super().__init__()
        self.saved_settings: dict = {}
        self.settings_upserts: list[dict] = []
        self._client = ApplySettingsClient(self, settings_row)

    def insert(self, table: str, row: dict):
        if table in (self.fail_tables or set()):
            return None
        stored = dict(row)
        if "id" not in stored:
            stored["id"] = f"fake-{len(self.inserted)}"
        self.inserted.append((table, dict(stored)))
        self.rows.setdefault(table, []).insert(0, stored)
        return stored


def set_state(db) -> None:
    app.state.db = db


async def call(method: str, path: str,
               json_body: dict | None = None) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                base_url="http://test") as client:
        resp = await client.request(method, path, json=json_body)
        return resp.json()


@pytest.fixture
def fake_db() -> ApplyFakeDatabase:
    db = ApplyFakeDatabase()
    set_state(db)
    return db


@pytest.fixture(autouse=True)
def _clean_state():
    yield
    set_state(FakeDatabase())


def _live_row(settings: AppSettings) -> dict:
    row = settings.model_dump(mode="json")
    row["id"] = 1
    return row


def _seed_run(db: ApplyFakeDatabase, run_id: str = "run-1", *,
              baseline: AppSettings | None = None,
              best: AppSettings | None = None,
              status: str = "complete", stage: str = "done",
              recommendation: object = _MISSING) -> tuple[AppSettings, AppSettings]:
    """Seed one run row. Returns (baseline, best).

    ``recommendation=_MISSING`` (default) builds a normal recommendation
    from ``best``; pass ``None`` explicitly for a run with no recommendation.
    """
    baseline = baseline or AppSettings()
    if best is None:
        best = baseline.model_copy(
            update={"min_confidence": 75.0, "rr_target": 2.0})
    rec = recommendation
    if rec is _MISSING:
        rec = {"iteration": 3,
               "config": best.model_dump(mode="json"),
               "validation_metrics": {"net_pnl": 42.0, "trades": 60},
               "note": "seeded recommendation"}
    now = datetime.now(timezone.utc).isoformat()
    db.insert(sim_optimizer.RUNS_TABLE, {
        "id": run_id, "status": status, "stage": stage,
        "progress": 1.0 if status == "complete" else 0.5,
        "seed": 0, "starting_capital": 1000.0,
        "search_config": {}, "baseline_settings": baseline.model_dump(mode="json"),
        "dataset_version": "v1", "data_hash": None,
        "result": {"stage": stage, "best_train_pnl": 42.0,
                   "best": ({"iteration": 3} if rec is not None else None),
                   "recommendation": rec, "warnings": []},
        "error": None, "created_at": now, "started_at": now,
        "finished_at": now if status == "complete" else None,
    })
    return baseline, best


def _audit_rows(db: ApplyFakeDatabase) -> list[dict]:
    from app.api.routes.settings import SETTINGS_CHANGE_TABLE
    return db.select(SETTINGS_CHANGE_TABLE)


# ---------------------------------------------------------------------------
# Brief Step 1 tests
# ---------------------------------------------------------------------------
class TestBriefStep1:
    async def test_apply_requires_explicit_confirmation(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": False})
        assert res["ok"] is False
        assert fake_db.saved_settings == {}

    async def test_apply_rejects_stale_live_baseline(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        live = baseline.model_copy(update={"min_confidence": 80.0})
        fake_db._client = ApplySettingsClient(fake_db, _live_row(live))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert "baseline" in res["error"]

    async def test_apply_writes_only_diff_fields(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is True
        assert set(fake_db.saved_settings) == {"min_confidence", "rr_target"}


# ---------------------------------------------------------------------------
# Guard ordering + zero writes on every refusal
# ---------------------------------------------------------------------------
class TestApplyGuards:
    @pytest.mark.parametrize("confirm", [False, None, 0, 1, "true", "yes", []])
    async def test_non_true_confirm_refuses_with_zero_writes(
            self, fake_db, confirm):
        baseline, _ = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        body = {} if confirm is None else {"confirm": confirm}
        res = await call("POST", "/api/system/optimize/run-1/apply", body)
        assert res["ok"] is False
        assert "confirm" in str(res.get("error", "")).lower()
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_unknown_run_refused_with_zero_writes(self, fake_db):
        fake_db._client = ApplySettingsClient(
            fake_db, _live_row(AppSettings()))
        res = await call("POST", "/api/system/optimize/nope/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []

    @pytest.mark.parametrize("status,stage", [
        ("running", "searching"), ("failed", "failed"),
        ("cancelled", "cancelled"), ("complete", "searching"),
    ])
    async def test_non_terminal_run_refused_with_zero_writes(
            self, fake_db, status, stage):
        baseline, _ = _seed_run(fake_db, status=status, stage=stage)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_missing_recommendation_refused_with_zero_writes(
            self, fake_db):
        baseline, _ = _seed_run(fake_db, recommendation=None)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_stale_baseline_reports_fields_and_writes_nothing(
            self, fake_db):
        baseline, _ = _seed_run(fake_db)
        live = baseline.model_copy(update={"min_confidence": 80.0})
        fake_db._client = ApplySettingsClient(fake_db, _live_row(live))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert "baseline" in res["error"]
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_confirm_checked_before_baseline(self, fake_db):
        """Guard order: a bad confirm on a stale run still reports the
        confirm problem (and writes nothing) — confirm is gate 1."""
        baseline, _ = _seed_run(fake_db)
        live = baseline.model_copy(update={"min_confidence": 80.0})
        fake_db._client = ApplySettingsClient(fake_db, _live_row(live))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": False})
        assert res["ok"] is False
        assert "confirm" in str(res.get("error", "")).lower()
        assert fake_db.settings_upserts == []


# ---------------------------------------------------------------------------
# Success shape, diff-only effect, audit, idempotence-turns-stale
# ---------------------------------------------------------------------------
class TestApplySuccess:
    async def test_success_shape_and_diff_only_effect(self, fake_db):
        baseline, best = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is True
        assert res["run_id"] == "run-1"
        assert res["applied"] == {
            "min_confidence": {"from": baseline.min_confidence,
                               "to": best.min_confidence},
            "rr_target": {"from": baseline.rr_target, "to": best.rr_target},
        }
        assert set(fake_db.saved_settings) == {"min_confidence", "rr_target"}
        stored = fake_db._client.row
        assert stored["min_confidence"] == best.min_confidence
        assert stored["rr_target"] == best.rr_target
        assert stored["capital"] == baseline.capital
        assert stored["allowed_assets"] == baseline.allowed_assets
        assert res["settings"]["min_confidence"] == best.min_confidence
        assert res["settings"]["rr_target"] == best.rr_target

    async def test_audit_carries_source_and_run_id(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is True
        audits = _audit_rows(fake_db)
        assert len(audits) == 1
        assert audits[0]["source"] == "simulation_apply"
        assert "run-1" in str(audits[0].get("summary", ""))

    async def test_empty_diff_succeeds_with_no_write(self, fake_db):
        baseline = AppSettings()
        _seed_run(fake_db, baseline=baseline, best=baseline.model_copy())
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is True
        assert res["applied"] == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_second_apply_is_stale(self, fake_db):
        """A successful apply moves live off the stored baseline, so the
        very next apply must refuse as stale — exactly one upsert total."""
        baseline, _ = _seed_run(fake_db)
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        first = await call("POST", "/api/system/optimize/run-1/apply",
                           {"confirm": True})
        assert first["ok"] is True
        second = await call("POST", "/api/system/optimize/run-1/apply",
                            {"confirm": True})
        assert second["ok"] is False
        assert "baseline" in second["error"]
        assert len(fake_db.settings_upserts) == 1


# ---------------------------------------------------------------------------
# Fail-closed pins (review round 1): corrupt/unreadable inputs refuse with
# zero trading_settings writes AND zero audit rows for the run.
# ---------------------------------------------------------------------------
class BrokenReadClient(ApplySettingsClient):
    """Supabase client whose reads fail — try_load_settings returns None."""

    def execute(self) -> SimpleNamespace:
        raise RuntimeError("connection reset by peer")


class FailingUpsertClient(ApplySettingsClient):
    """Supabase client whose writes fail with a non-PGRST204 error."""

    def upsert(self, row: dict) -> "FailingUpsertClient":
        raise RuntimeError("boom: upsert failed")


class UnavailableDb(ApplyFakeDatabase):
    """DB that reports unavailable — route and service must refuse."""

    @property
    def available(self) -> bool:
        return False


class TestFailClosedPins:
    def _zero_writes(self, fake_db: ApplyFakeDatabase) -> None:
        assert fake_db.saved_settings == {}
        assert fake_db.settings_upserts == []
        assert _audit_rows(fake_db) == []

    async def test_corrupt_stored_baseline_refused(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        row = fake_db.rows[sim_optimizer.RUNS_TABLE][0]
        row["baseline_settings"] = {"min_confidence": "not-a-number"}
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        self._zero_writes(fake_db)

    async def test_unreadable_recommendation_config_refused(self, fake_db):
        baseline, _ = _seed_run(fake_db)
        row = fake_db.rows[sim_optimizer.RUNS_TABLE][0]
        rec = dict(row["result"]["recommendation"])
        rec["config"] = {"rr_target": "not-a-number"}
        row["result"]["recommendation"] = rec
        fake_db._client = ApplySettingsClient(fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        self._zero_writes(fake_db)

    async def test_unreadable_live_settings_refused(self, fake_db):
        _seed_run(fake_db)
        fake_db._client = BrokenReadClient(fake_db, None)
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        self._zero_writes(fake_db)

    async def test_failed_settings_write_refused(self, fake_db):
        baseline, best = _seed_run(fake_db)
        assert best != baseline  # non-empty diff, so the write is attempted
        fake_db._client = FailingUpsertClient(
            fake_db, _live_row(baseline))
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        self._zero_writes(fake_db)

    async def test_db_unavailable_refused(self):
        db = UnavailableDb(_live_row(AppSettings()))
        set_state(db)
        res = await call("POST", "/api/system/optimize/run-1/apply",
                         {"confirm": True})
        assert res["ok"] is False
        assert db.saved_settings == {}
        assert db.settings_upserts == []
