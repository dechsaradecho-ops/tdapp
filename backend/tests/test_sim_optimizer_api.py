"""Optimizer background job + persistence + APIs (Task 4).

Background optimization service with single-worker guard, evidence
persistence, and six routes. No live network: the search loop is
monkeypatched with a fast fake; the dataset is seeded in-memory.

Run from backend/:
  D:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_sim_optimizer_api.py -v
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.main import app
from app.services import sim_market_data, sim_optimizer
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# Harness (mirrors tests/test_sim_data_api.py)
# ---------------------------------------------------------------------------
class OptFakeDatabase(FakeDatabase):
    """Stock fake + id-preservation shim + live-settings write tripwire.

    DIVERGENCE FROM STOCK (deliberate, documented): the stock
    ``FakeDatabase.insert`` overwrites every row's ``id`` with a generated
    ``fake-N`` value. Production Supabase preserves a client-supplied text
    PK (insert returns the row with the given id), and the optimizer reads
    and patches ``sim_opt_runs`` by run id — so under the stock fake every
    run-row lookup would miss and every test using ``sim_opt_runs`` would
    fail for harness reasons, not code reasons. This shim therefore keeps a
    supplied ``id`` and only mints one when absent. Stock-fake users must
    preserve ids the same way for ``sim_opt_runs`` reads/writes to work;
    every other table behaves exactly like the stock fake.

    Any insert/update/delete touching ``trading_settings`` is recorded in
    ``saved_settings`` — the optimizer must never write live settings
    (reads via ``_client`` are untouched and stay free).
    """

    def __init__(self, rows=None, fail_tables=None):
        super().__init__(rows=rows, fail_tables=fail_tables)
        self.saved_settings: dict[str, dict] = {}

    def insert(self, table: str, row: dict | list[dict]):
        if table == "trading_settings":
            if isinstance(row, list):
                for single in row:
                    self.saved_settings[str(single.get("id", "1"))] = dict(
                        single)
            else:
                self.saved_settings[str(row.get("id", "1"))] = dict(row)
        if table in (self.fail_tables or set()):
            return None
        if isinstance(row, list):
            # Bulk path mirrors production Database.insert(list): one call,
            # one list back. Ids supplied by the caller are preserved (this
            # fake's sim_opt_runs shim); otherwise a fake id is minted.
            stored: list[dict] = []
            for single in row:
                self.inserted.append((table, dict(single)))
                rec = dict(single)
                if "id" not in rec:
                    rec["id"] = f"fake-{len(self.inserted)}"
                self.rows.setdefault(table, []).insert(0, rec)
                stored.append(rec)
            return stored
        stored_row = dict(row)
        if "id" not in stored_row:
            stored_row["id"] = f"fake-{len(self.inserted)}"
        self.inserted.append((table, dict(stored_row)))
        self.rows.setdefault(table, []).insert(0, stored_row)
        return stored_row

    def update(self, table: str, row_id: str, changes: dict) -> bool:
        if table == "trading_settings":
            self.saved_settings.setdefault(str(row_id), {}).update(dict(changes))
        for r in self.rows.get(table, []):
            if r.get("id") == row_id:
                r.update(changes)
                return True
        return False

    def select(self, table: str, filters: dict | None = None,
               order: str = "created_at", desc: bool = True, limit: int = 50,
               offset: int = 0, columns: str = "*",
               after: dict | None = None) -> list[dict]:
        # Production Database.select orders server-side before slicing, so
        # seq/iteration pages are chronological. The stock fake ignores
        # `order` (returning newest-first storage order), which would slice
        # the wrong window for offset > 0 — honor `order`/`desc` here.
        # `after` mirrors the production gap-safe keyset filter (strict >).
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        for col, val in (after or {}).items():
            rows = [r for r in rows
                    if r.get(col) is not None and r.get(col) > val]
        try:
            rows = sorted(rows,
                          key=lambda r: (r.get(order) is None,
                                         r.get(order)),
                          reverse=bool(desc))
        except TypeError:
            pass
        return rows[offset:offset + limit]

    def delete(self, table: str, filters: dict) -> bool:
        if table == "trading_settings":
            self.saved_settings["_deleted"] = dict(filters)
        rows = self.rows.get(table, [])
        kept = [r for r in rows
                if not all(r.get(c) == v for c, v in filters.items())]
        self.rows[table] = kept
        return len(kept) < len(rows)


def set_state(db) -> None:
    app.state.db = db


async def call(method: str, path: str,
               json_body: dict | None = None,
               params: dict | None = None) -> dict:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://test") as client:
        resp = await client.request(method, path, json=json_body,
                                    params=params)
        return resp.json()


@pytest.fixture
def fake_db() -> OptFakeDatabase:
    db = OptFakeDatabase()
    set_state(db)
    return db


@pytest.fixture(autouse=True)
def _clean_opt_slot():
    sim_optimizer._set_active(None)
    sim_optimizer._CANCEL.clear()
    yield
    sim_optimizer._set_active(None)
    sim_optimizer._CANCEL.clear()
    set_state(FakeDatabase())


class _InlinePool:
    """Run the worker synchronously so the run reaches a terminal verdict
    deterministically inside the test (production uses the real pool)."""

    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None


@pytest.fixture
def inline_worker(monkeypatch):
    monkeypatch.setattr(sim_optimizer, "_POOL", _InlinePool())


def _seed_dataset(db, version: str, assets=("EURUSD",), n: int = 40) -> None:
    """One complete immutable dataset version with valid bars."""
    db.insert(sim_market_data.META_TABLE, {
        "dataset_version": version,
        "status": "complete",
        "years": 1,
        "assets": list(assets),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "report": {"stage": "done", "complete": True, "version": version},
        "data_hash": "hash-seed",
        "code_version": sim_market_data.CODE_VERSION,
    })
    base = date(2024, 1, 2)
    for k, asset in enumerate(assets):
        for i in range(n):
            day = (base + timedelta(days=i)).isoformat()
            px = 1.1000 + i * 0.0001 + k * 0.5
            db.insert(sim_market_data.BARS_TABLE, {
                "dataset_version": version,
                "asset": asset,
                "bar_date": day,
                "open": px, "high": px + 0.0003, "low": px - 0.0004,
                "close": px + 0.0001,
                "source": "biquote",
                "source_timestamp": None,
            })


def fast_fake_optimizer(series, baseline, *, starting_capital=1000.0,
                         search_config=None, progress_callback=None,
                         cancel_event=None):
    """Deterministic stand-in for optimize_settings keyed like the Task 3 fake.

    Emits a baseline + two candidates through progress_callback and returns a
    done OptimizationResult with a validation leaderboard, so the worker's
    persistence path (iterations, baseline/best/top-five details, verdict) is
    exercised without replaying a real portfolio.
    """
    from tests.test_sim_optimizer import fake_replay

    cfg = search_config or sim_optimizer.SearchConfig(max_iterations=3)
    rng = random.Random(cfg.seed)
    entries = []
    base_train = fake_replay(series, baseline,
                             starting_capital=starting_capital)
    base_entry = sim_optimizer.IterationResult(
        iteration=0, config=baseline, config_diff={},
        train_result=base_train, validation_result=None, improved=False)
    entries.append(base_entry)
    if progress_callback is not None:
        progress_callback(base_entry)
    for i in range(1, min(3, cfg.max_iterations)):
        cand = sim_optimizer.candidate_from_baseline(baseline, rng, i)
        train = fake_replay(series, cand, starting_capital=starting_capital)
        entry = sim_optimizer.IterationResult(
            iteration=i, config=cand,
            config_diff=sim_optimizer.config_diff(baseline, cand),
            train_result=train, validation_result=None,
            improved=(i == 1))
        entries.append(entry)
        if progress_callback is not None:
            progress_callback(entry)
    _, cutoff = sim_optimizer.split_series(series,
                                           train_ratio=cfg.train_ratio)
    board = []
    for src in entries[:max(1, cfg.top_n)]:
        full = fake_replay(series, src.config,
                           starting_capital=starting_capital)
        _, val = sim_optimizer.slice_replay_result(full, cutoff)
        board.append(sim_optimizer.IterationResult(
            iteration=src.iteration, config=src.config,
            config_diff=dict(src.config_diff),
            train_result=src.train_result, validation_result=val,
            improved=src.improved))
    board.sort(key=lambda e: e.validation_result.metrics.net_pnl,
               reverse=True)
    best = board[0] if board else None
    return sim_optimizer.OptimizationResult(
        best=best, baseline=entries[0], iterations=entries,
        validation_leaderboard=board, warnings=[])


def load_migration(name: str) -> str:
    path = Path(__file__).resolve().parents[2] / "database" / name
    return path.read_text(encoding="utf-8").lower()


# ---------------------------------------------------------------------------
# Task 3: per-run event log + bulk detail persistence (RED first)
# ---------------------------------------------------------------------------
class TestRunEventLog:
    def test_run_event_log_schema_is_service_role_only(self):
        schema = load_migration("064_sim_opt_run_events.sql")
        assert "create table if not exists sim_opt_run_events" in schema
        assert "revoke all on sim_opt_run_events from anon, authenticated" \
            in schema

    async def test_run_logs_are_ordered_and_paged(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        logs = await call(
            "GET", f"/api/system/optimize/{res['run_id']}/logs?limit=100")
        assert logs["ok"] is True
        seqs = [r["seq"] for r in logs["rows"]]
        assert seqs == sorted(seqs)
        assert any(r["event"] == "iteration_completed"
                   for r in logs["rows"])
        assert any(r["event"] == "run_completed" for r in logs["rows"])

    async def test_run_logs_page_shape_and_unknown_run(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        run_id = res["run_id"]
        first = await call(
            "GET", f"/api/system/optimize/{run_id}/logs",
            params={"offset": 0, "limit": 2})
        assert first["ok"] is True
        assert first["run_id"] == run_id
        assert len(first["rows"]) == 2
        assert first["more"] is True
        assert first["last_seq"] == first["rows"][-1]["seq"]
        second = await call(
            "GET", f"/api/system/optimize/{run_id}/logs",
            params={"offset": 2, "limit": 2})
        assert second["ok"] is True
        assert second["rows"]
        assert second["rows"][0]["seq"] > first["rows"][-1]["seq"]
        missing = await call("GET", "/api/system/optimize/nope/logs")
        assert missing["ok"] is False

    async def test_run_logs_after_seq_is_gap_safe(self, fake_db):
        """after_seq=1 over seqs 0,1,3,4 returns 3,4 — never skips.

        Event seq advances even when an insert fails (see
        ``_emit_run_event``), so a positional offset overshoots after a
        gap: offset=2 lands on row *position* 2 = seq 3, silently
        dropping seq 3 from the client's view. ``seq > after_seq``
        filtering cannot skip.
        """
        run_id = "gap-run"
        fake_db.insert(sim_optimizer.RUNS_TABLE,
                       {"id": run_id, "status": "complete"})
        for seq in (0, 1, 3, 4):
            fake_db.insert(sim_optimizer.EVENTS_TABLE, {
                "run_id": run_id, "seq": seq, "level": "info",
                "stage": "searching", "iteration": None,
                "event": f"ev{seq}", "message": "", "data": {},
                "created_at": None,
            })
        gap_safe = sim_optimizer.optimization_logs(
            fake_db, run_id, after_seq=1)
        assert gap_safe["ok"] is True
        assert [r["seq"] for r in gap_safe["rows"]] == [3, 4]
        # Legacy positional paging is retained unchanged.
        legacy = sim_optimizer.optimization_logs(
            fake_db, run_id, offset=2, limit=2)
        assert [r["seq"] for r in legacy["rows"]] == [3, 4]
        # Route passthrough: after_seq reaches the service.
        via_route = await call(
            "GET", f"/api/system/optimize/{run_id}/logs",
            params={"after_seq": 1, "limit": 100})
        assert via_route["ok"] is True
        assert [r["seq"] for r in via_route["rows"]] == [3, 4]


# ---------------------------------------------------------------------------
# Task 3 review round 1: regression pins for bulk details + early status.
# Source behavior is approved as-is; these tests pin it.
# ---------------------------------------------------------------------------
class TestTask3RegressionPins:
    @staticmethod
    def _series():
        from types import SimpleNamespace
        base = datetime(2024, 1, 2, tzinfo=timezone.utc).timestamp()
        return {"EURUSD": [SimpleNamespace(t=base + 86400 * i)
                           for i in range(10)]}

    def test_store_details_uses_bulk_arrays(self, fake_db, monkeypatch):
        """Details persist via at most two bulk inserts (trade array +
        equity array), never one insert per row."""
        from tests.test_sim_optimizer import fake_replay
        from app.models.schemas import AppSettings
        replay = fake_replay(self._series(), AppSettings())
        assert replay.trades and replay.equity_curve
        calls: list = []
        real = fake_db.insert

        def spy(table, row):
            calls.append((table, row))
            return real(table, row)

        monkeypatch.setattr(fake_db, "insert", spy)
        assert sim_optimizer._store_details(
            fake_db, "run-x", 1, replay) is True
        detail = [c for c in calls if c[0] in (
            sim_optimizer.TRADES_TABLE, sim_optimizer.EQUITY_TABLE)]
        assert len(detail) <= 2
        assert {t for t, _ in detail} == {
            sim_optimizer.TRADES_TABLE, sim_optimizer.EQUITY_TABLE}
        assert all(isinstance(rows, list) and rows for _, rows in detail)
        assert fake_db.select(
            sim_optimizer.TRADES_TABLE, {"run_id": "run-x"}) != []
        assert fake_db.select(
            sim_optimizer.EQUITY_TABLE, {"run_id": "run-x"}) != []

    def test_store_details_skips_empty_arrays(self, fake_db, monkeypatch):
        """Empty trade/equity arrays are skipped, not inserted as junk;
        an empty replay (or None) still reports success with zero calls."""
        from types import SimpleNamespace
        point = SimpleNamespace(date="2024-01-03", equity=1100.0,
                                realized_pnl=100.0, unrealized_pnl=0.0,
                                drawdown_pct=0.0)
        calls: list = []
        real = fake_db.insert

        def spy(table, row):
            calls.append((table, row))
            return real(table, row)

        monkeypatch.setattr(fake_db, "insert", spy)
        replay = SimpleNamespace(trades=[], equity_curve=[point])
        assert sim_optimizer._store_details(
            fake_db, "run-y", 1, replay) is True
        tables = [t for t, _ in calls]
        assert sim_optimizer.TRADES_TABLE not in tables
        assert tables.count(sim_optimizer.EQUITY_TABLE) == 1
        before = len(calls)
        assert sim_optimizer._store_details(
            fake_db, "run-y", 2,
            SimpleNamespace(trades=[], equity_curve=[])) is True
        assert sim_optimizer._store_details(
            fake_db, "run-y", 3, None) is True
        assert len(calls) == before

    def test_store_details_false_on_short_bulk_response(self):
        """A short/mismatched bulk response (or None) is a logged failure,
        never silently accepted."""
        from types import SimpleNamespace

        class _ShortBulkDb:
            available = True

            def __init__(self):
                self.calls: list = []

            def insert(self, table, rows):
                self.calls.append((table, rows))
                return [{"partial": True}]

        replay = SimpleNamespace(
            trades=[SimpleNamespace(), SimpleNamespace()],
            equity_curve=[])
        short_db = _ShortBulkDb()
        assert sim_optimizer._store_details(
            short_db, "run-x", 1, replay) is False
        assert len(short_db.calls) == 1
        assert isinstance(short_db.calls[0][1], list)
        failed_db = OptFakeDatabase(
            fail_tables={sim_optimizer.TRADES_TABLE})
        assert sim_optimizer._store_details(
            failed_db, "run-x", 1, replay) is False

    async def test_failed_detail_write_marks_nothing_detailed(
            self, fake_db, inline_worker, monkeypatch):
        """When `_store_details` returns False the run still completes and
        no iteration is left marked detailed (no trade/equity rows land)."""
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        seen: list = []

        def failing_store(db, run_id, iteration, replay):
            seen.append(iteration)
            return False

        monkeypatch.setattr(sim_optimizer, "_store_details", failing_store)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        run_id = res["run_id"]
        st = await call("GET", f"/api/system/optimize/{run_id}")
        assert st["ok"] is True
        assert st["status"] == "complete"
        assert seen, "detail persistence must have been attempted"
        assert fake_db.select("sim_opt_trades", {"run_id": run_id},
                              limit=100000) == []
        assert fake_db.select("sim_opt_equity", {"run_id": run_id},
                              limit=100000) == []
        assert len(fake_db.select("sim_opt_iterations", {"run_id": run_id},
                                  limit=100000)) == 3

    async def test_status_patch_precedes_detail_persistence(
            self, fake_db, inline_worker, monkeypatch):
        """The live run row must already show the improved best BEFORE
        detail persistence begins — fails if `_progress_patch` is moved
        after `_store_details`."""
        from types import SimpleNamespace

        def _res(net):
            metrics = SimpleNamespace(
                net_pnl=net, gross_pnl=net, costs=0.0, trades=10, wins=10,
                win_rate_pct=100.0, profit_factor=None, average_r=1.0,
                max_drawdown_pct=0.0, ambiguous_pct=0.0)
            return SimpleNamespace(trades=[], equity_curve=[],
                                   metrics=metrics, warnings=[])

        def scripted_optimizer(series, baseline, *, starting_capital=1000.0,
                               search_config=None, progress_callback=None,
                               cancel_event=None):
            script = [(0, 100.0, False), (1, 50.0, False),
                      (2, 200.0, True)]
            iters = []
            for it, net, improved in script:
                entry = sim_optimizer.IterationResult(
                    iteration=it, config=baseline, config_diff={},
                    train_result=_res(net), validation_result=None,
                    improved=improved)
                iters.append(entry)
                if progress_callback is not None:
                    progress_callback(entry)
            base_board = sim_optimizer.IterationResult(
                iteration=0, config=baseline, config_diff={},
                train_result=_res(100.0),
                validation_result=_res(150.0), improved=False)
            return sim_optimizer.OptimizationResult(
                best=None, baseline=iters[0], iterations=iters,
                validation_leaderboard=[base_board], warnings=[])

        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            scripted_optimizer)
        real_store = sim_optimizer._store_details
        probes: list = []

        def spy_store(db, run_id, iteration, replay):
            rows = db.select(sim_optimizer.RUNS_TABLE,
                             filters={"id": run_id}, limit=1)
            assert rows, "run row must be readable while details persist"
            probes.append({
                "iteration": iteration,
                "best_train_pnl": (rows[0].get("result") or {}).get(
                    "best_train_pnl"),
            })
            return real_store(db, run_id, iteration, replay)

        monkeypatch.setattr(sim_optimizer, "_store_details", spy_store)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET",
                        f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["status"] == "complete"
        by_iter = {p["iteration"]: p["best_train_pnl"] for p in probes}
        # Iteration 1 is worse (no store); 0 and 2 store details, each
        # AFTER the live row already shows that iteration's best.
        assert by_iter == {0: 100.0, 2: 200.0}


class TestBriefStep1:
    async def test_start_uses_user_capital_and_never_writes_live_settings(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"],
                          "starting_capital": 2500})
        assert res["ok"] is True
        assert fake_db.saved_settings == {}

    async def test_cancel_preserves_completed_iterations(
            self, fake_db):
        now = datetime.now(timezone.utc).isoformat()
        fake_db.insert(sim_optimizer.RUNS_TABLE, {
            "id": "run-1", "status": "running", "stage": "searching",
            "progress": 0.5, "seed": 0, "starting_capital": 1000.0,
            "search_config": {}, "baseline_settings": {},
            "dataset_version": "v1", "data_hash": None,
            "result": {"best_train_pnl": 10.0, "best": None,
                       "recommendation": None, "warnings": []},
            "error": None, "created_at": now, "started_at": now,
            "finished_at": None,
        })
        for i in range(3):
            fake_db.insert(sim_optimizer.ITER_TABLE, {
                "run_id": "run-1", "iteration": i, "config": {},
                "config_diff": {},
                "train_metrics": {"net_pnl": float(i), "trades": 10},
                "validation_metrics": None, "improved": i == 1,
            })
        res = await call("POST", "/api/system/optimize/run-1/cancel", {})
        assert res["ok"] is True
        assert fake_db.select("sim_opt_iterations", {"run_id": "run-1"})


# ---------------------------------------------------------------------------
# Safety: no live writes, no broker/notifier coupling
# ---------------------------------------------------------------------------
class TestOptimizerSafety:
    async def test_full_run_writes_no_settings_or_broker_tables(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"],
                          "starting_capital": 2500})
        assert res["ok"] is True
        touched = {t for t, _ in fake_db.inserted}
        assert "trading_settings" not in touched
        assert fake_db.saved_settings == {}
        assert fake_db.select("sim_opt_iterations",
                              {"run_id": res["run_id"]})

    def test_module_has_no_broker_or_notifier_imports(self):
        src = Path(sim_optimizer.__file__).read_text(encoding="utf-8")
        imports = "\n".join(
            line for line in src.lower().splitlines()
            if line.strip().startswith(("import ", "from ")))
        assert "broker" not in imports
        assert "notif" not in imports
        assert "trading_settings" not in src.lower()

    async def test_starting_capital_recorded_per_run(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"],
                          "starting_capital": 2500})
        assert res["ok"] is True
        st = await call("GET",
                        f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["starting_capital"] == 2500
        assert st["status"] == "complete"


# ---------------------------------------------------------------------------
# Guards: single worker, input validation, terminal cancel
# ---------------------------------------------------------------------------
class TestOptimizerGuards:
    async def test_second_start_refused_while_active(self, fake_db):
        sim_optimizer._set_active("opt-live")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is False
        assert res["run_id"] == "opt-live"
        assert "already live" in str(res.get("error", ""))

    async def test_start_rejects_bad_capital(self, fake_db):
        for bad in (0, -5, "lots", float("nan"), float("inf")):
            res = await call("POST", "/api/system/optimize",
                             {"dataset_version": "v1", "assets": ["EURUSD"],
                              "starting_capital": bad})
            assert res["ok"] is False, bad

    async def test_start_rejects_oversize_search_config(self, fake_db):
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"],
                          "search_config": {"max_iterations": 501}})
        assert res["ok"] is False

    async def test_start_rejects_missing_dataset(self, fake_db):
        res = await call("POST", "/api/system/optimize",
                         {"assets": ["EURUSD"]})
        assert res["ok"] is False

    async def test_cancel_unknown_run_refused(self, fake_db):
        res = await call("POST", "/api/system/optimize/nope/cancel", {})
        assert res["ok"] is False

    async def test_cancel_terminal_returns_unchanged(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        first = await call("GET",
                           f"/api/system/optimize/{res['run_id']}")
        assert first["status"] == "complete"
        again = await call("POST",
                           f"/api/system/optimize/{res['run_id']}/cancel",
                           {})
        assert again["ok"] is True
        assert again["status"] == "complete"


# ---------------------------------------------------------------------------
# Shapes: status / list / iterations / equity
# ---------------------------------------------------------------------------
class TestOptimizerShapes:
    async def _done_run(self, fake_db, monkeypatch, **kw):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        body = {"dataset_version": "v1", "assets": ["EURUSD"]}
        body.update(kw)
        res = await call("POST", "/api/system/optimize", body)
        assert res["ok"] is True
        return res["run_id"]

    async def test_status_shape(self, fake_db, inline_worker, monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        st = await call("GET", f"/api/system/optimize/{run_id}")
        assert st["ok"] is True
        for key in ("run_id", "status", "stage", "progress", "seed",
                    "starting_capital", "dataset_version", "best_train_pnl",
                    "best", "recommendation", "warnings", "error"):
            assert key in st, key
        assert st["best"] is not None
        assert st["best"]["train_metrics"]["profit_factor"] is None
        assert st["recommendation"]["iteration"] == \
            st["best"]["iteration"]

    async def test_list_shape(self, fake_db, inline_worker, monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        lst = await call("GET", "/api/system/optimize")
        assert lst["ok"] is True
        assert lst["total"] >= 1
        assert any(r["run_id"] == run_id for r in lst["runs"])

    async def test_iterations_paged(self, fake_db, inline_worker,
                                    monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        page = await call("GET",
                          f"/api/system/optimize/{run_id}/iterations",
                          params={"offset": 0, "limit": 2})
        assert page["ok"] is True
        assert page["run_id"] == run_id
        assert page["total"] == 3
        assert len(page["rows"]) == 2
        row = page["rows"][0]
        for key in ("iteration", "config", "config_diff",
                    "train_metrics", "validation_metrics", "improved"):
            assert key in row, key

    async def test_equity_defaults_to_best_else_baseline(
            self, fake_db, inline_worker, monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        st = await call("GET", f"/api/system/optimize/{run_id}")
        default = await call("GET",
                             f"/api/system/optimize/{run_id}/equity")
        assert default["ok"] is True
        assert default["iteration"] == st["best"]["iteration"]
        assert default["rows"]
        base = await call("GET",
                          f"/api/system/optimize/{run_id}/equity",
                          params={"iteration": 0})
        assert base["ok"] is True
        assert base["iteration"] == 0
        assert base["rows"]

    async def test_equity_unknown_iteration_refused(
            self, fake_db, inline_worker, monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        res = await call("GET",
                         f"/api/system/optimize/{run_id}/equity",
                         params={"iteration": 999})
        assert res["ok"] is False

    async def test_status_unknown_run_refused(self, fake_db):
        res = await call("GET", "/api/system/optimize/nope")
        assert res["ok"] is False

    async def test_detail_tables_hold_baseline_evidence(
            self, fake_db, inline_worker, monkeypatch):
        run_id = await self._done_run(fake_db, monkeypatch)
        trades = fake_db.select("sim_opt_trades", {"run_id": run_id})
        equity = fake_db.select("sim_opt_equity", {"run_id": run_id})
        assert trades and equity
        assert {t["direction"] for t in trades} <= {"long", "short"}

    @staticmethod
    def _train_cutoff_iso(fake_db, version: str = "v1") -> str:
        bars = fake_db.select(sim_market_data.BARS_TABLE,
                              {"dataset_version": version}, limit=100000)
        days = sorted({r["bar_date"] for r in bars})
        assert len(days) >= 2
        return days[int(len(days) * 0.7)]

    async def test_topn_validation_replaces_best_train_details(
            self, fake_db, inline_worker, monkeypatch):
        """Phase rule: baseline (iter 0) keeps train-phase details; every
        other detailed iteration ends with validation-phase details only."""
        run_id = await self._done_run(fake_db, monkeypatch)
        st = await call("GET", f"/api/system/optimize/{run_id}")
        assert st["ok"] is True and st["status"] == "complete"
        best_iter = st["best"]["iteration"]
        cutoff = self._train_cutoff_iso(fake_db)
        trades = fake_db.select("sim_opt_trades", {"run_id": run_id},
                                limit=100000)
        by_iter: dict[int, list] = {}
        for t in trades:
            by_iter.setdefault(t["iteration"], []).append(t)
        # All search iterations are in the top-N board, so all are detailed.
        assert set(by_iter) == {0, 1, 2}
        # Pinned baseline: full train-slice replay, incl. pre-cutoff dates.
        assert any(t["exit_date"] < cutoff for t in by_iter[0])
        # Non-baseline: validation-phase only, pre-cutoff rows replaced.
        for it, rows in by_iter.items():
            if it == 0:
                continue
            assert rows, it
            assert all(t["exit_date"] >= cutoff for t in rows), it
        if best_iter != 0:
            assert all(t["exit_date"] >= cutoff
                       for t in by_iter[best_iter])

    async def test_cancel_during_topn_validation_preserves_rows(
            self, fake_db, inline_worker, monkeypatch):
        """Cancel landing mid-validation-persist stops further replays and
        marks the run cancelled with all completed rows intact."""
        state = {"search_done": False}

        def fake_then_flag(series, baseline, *, starting_capital=1000.0,
                           search_config=None, progress_callback=None,
                           cancel_event=None):
            out = fast_fake_optimizer(
                series, baseline, starting_capital=starting_capital,
                search_config=search_config,
                progress_callback=progress_callback,
                cancel_event=cancel_event)
            state["search_done"] = True
            return out

        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fake_then_flag)
        real_store = sim_optimizer._store_details

        def flaky_store(db, run_id, iteration, replay):
            out = real_store(db, run_id, iteration, replay)
            if state["search_done"]:
                ev = sim_optimizer._CANCEL.get(run_id)
                if ev is not None:
                    ev.set()
            return out

        monkeypatch.setattr(sim_optimizer, "_store_details", flaky_store)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET",
                        f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["status"] == "cancelled"
        assert st["error"] == "cancelled"
        iters = fake_db.select("sim_opt_iterations",
                               {"run_id": res["run_id"]}, limit=100000)
        assert len(iters) == 3

    async def test_cancel_just_before_terminal_write_wins(
            self, fake_db, inline_worker, monkeypatch):
        """Race guard: a cancel landing during best-summary computation —
        after the last validation check, just before the terminal
        ``complete`` write — must end the run ``cancelled`` (completed
        rows preserved), never silently overwritten to ``complete``."""
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        real_best = sim_optimizer._best_summary

        def hooked_best(entry):
            for ev in list(sim_optimizer._CANCEL.values()):
                try:
                    ev.set()
                except Exception:
                    pass
            return real_best(entry)

        monkeypatch.setattr(sim_optimizer, "_best_summary", hooked_best)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET",
                        f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["status"] == "cancelled"
        assert st["stage"] == "cancelled"
        assert st["error"] == "cancelled"
        iters = fake_db.select("sim_opt_iterations",
                               {"run_id": res["run_id"]}, limit=100000)
        assert len(iters) == 3

    async def test_finish_run_refuses_cancelled_to_complete(
            self, fake_db, inline_worker, monkeypatch):
        """Unit guard: _finish_run(complete) after cancel_optimization
        patched the row (event set + row cancelled) must write
        ``cancelled``, preserving completed rows."""
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        real_finish = sim_optimizer._finish_run

        def racing_finish(db, run_id, status, **kw):
            if status == "complete":
                ev = sim_optimizer._CANCEL.get(run_id)
                if ev is not None:
                    try:
                        ev.set()
                    except Exception:
                        pass
                sim_optimizer._patch_run(db, run_id, {
                    "status": "cancelled", "stage": "cancelled",
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "error": "cancelled"})
            return real_finish(db, run_id, status, **kw)

        monkeypatch.setattr(sim_optimizer, "_finish_run", racing_finish)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET",
                        f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["status"] == "cancelled"
        assert st["error"] == "cancelled"
        iters = fake_db.select("sim_opt_iterations",
                               {"run_id": res["run_id"]}, limit=100000)
        assert len(iters) == 3


# ---------------------------------------------------------------------------
# Task 4: post-run meta-filter report (RED first).
# ---------------------------------------------------------------------------
class TestMetaFilterReport:
    @staticmethod
    def _series():
        from app.integrations.quotes import Candle
        base = datetime(2024, 1, 2, tzinfo=timezone.utc)
        bars = []
        for i in range(10):
            t = int((base + timedelta(days=i)).timestamp())
            px = 1.1000 + i * 0.001
            bars.append(Candle(o=px, h=px + 0.0003, l=px - 0.0004,
                               c=px + 0.0001, t=t))
        return {"EURUSD": bars}

    def test_meta_filter_summary_is_post_run_and_does_not_change_best(
            self, monkeypatch):
        """Summary tallies every trade, never fabricates, never ranks."""
        from types import SimpleNamespace
        from app.models.schemas import AppSettings

        series = self._series()
        trades = [
            SimpleNamespace(asset="EURUSD", direction="BUY",
                            entry_date=date(2024, 1, 5)),
            SimpleNamespace(asset="EURUSD", direction="SELL",
                            entry_date=date(2024, 1, 5)),
            SimpleNamespace(asset="MISSING", direction="BUY",
                            entry_date=date(2024, 1, 5)),
        ]
        before = [(t.asset, t.direction, t.entry_date) for t in trades]
        seen: list = []

        def fake_verdict(settings, asset, direction, snapshot):
            seen.append((asset, direction, snapshot))
            if asset == "MISSING":
                return SimpleNamespace(
                    status="unavailable", probability=None,
                    threshold=None, reason="",
                    artifact_run_id=None, trained_at=None)
            ok = direction == "BUY"
            return SimpleNamespace(
                status="take" if ok else "skip",
                probability=0.9 if ok else 0.1, threshold=0.65,
                reason="" if ok else "meta_filter skip",
                artifact_run_id="abc123", trained_at="2024-01-01")

        monkeypatch.setattr(sim_optimizer, "evaluate_meta_filter",
                            fake_verdict)
        summary = sim_optimizer.summarize_meta_filter(
            series, AppSettings(), trades)
        assert summary["evaluated"] == len(trades)
        assert (summary["take"] + summary["skip"]
                + summary["unavailable"]) == len(trades)
        assert summary["ranking_effect"] == "none"
        # Prior-close picture only: the entry-date bar itself is excluded.
        eur_snaps = [s for a, _, s in seen if a == "EURUSD"]
        assert eur_snaps and all(s is not None for s in eur_snaps)
        assert {s["bar_count"] for s in eur_snaps} == {3}
        assert {s["price"] for s in eur_snaps} == {round(1.1000 + 2 * 0.001
                                                         + 0.0001, 5)}
        assert [s for a, _, s in seen if a == "MISSING"] == [None]
        # Post-run purity: inputs untouched, summary carries the artifact.
        assert [(t.asset, t.direction, t.entry_date)
                for t in trades] == before
        assert summary["threshold"] == 0.65
        assert summary["artifact_run_id"] == "abc123"

    async def test_completed_run_reports_meta_filter(
            self, fake_db, inline_worker, monkeypatch):
        """A finished run stores the report on result + recommendation and
        emits meta_filter_reported — without changing the recommendation."""
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        run_id = res["run_id"]
        st = await call("GET", f"/api/system/optimize/{run_id}")
        assert st["ok"] is True
        assert st["status"] == "complete"
        report = st.get("meta_filter") or {}
        assert report.get("ranking_effect") == "none"
        assert report.get("evaluated", 0) > 0
        assert (report.get("take", 0) + report.get("skip", 0)
                + report.get("unavailable", 0)) == report["evaluated"]
        reco = st.get("recommendation") or {}
        assert (reco.get("meta_filter") or {}).get("ranking_effect") \
            == "none"
        logs = await call(
            "GET", f"/api/system/optimize/{run_id}/logs",
            params={"offset": 0, "limit": 500})
        assert logs["ok"] is True
        assert any(r["event"] == "meta_filter_reported"
                   for r in logs["rows"])

    async def test_failed_best_store_leaves_no_orphan_details(
            self, fake_db, inline_worker, monkeypatch):
        """A failed `_store_details` for a new best must not advance the
        best-detail pointer: the next improvement deletes the last
        *successful* best's rows, leaving no orphan rows behind."""
        from tests.test_sim_optimizer import fake_replay

        series = self._series()

        def scripted_optimizer(s, baseline, *, starting_capital=1000.0,
                               search_config=None, progress_callback=None,
                               cancel_event=None):
            from types import SimpleNamespace

            template = fake_replay(series, baseline,
                                   starting_capital=starting_capital)
            script = [(0, 100.0, False), (1, 200.0, True),
                      (2, 300.0, True), (3, 400.0, True)]
            iters = []
            for it, net, improved in script:
                metrics = SimpleNamespace(
                    net_pnl=net, gross_pnl=net, costs=0.0,
                    trades=len(template.trades), wins=len(template.trades),
                    win_rate_pct=100.0, profit_factor=None, average_r=1.0,
                    max_drawdown_pct=0.0, ambiguous_pct=0.0)
                train = SimpleNamespace(trades=list(template.trades),
                                        equity_curve=list(
                                            template.equity_curve),
                                        metrics=metrics, warnings=[])
                entry = sim_optimizer.IterationResult(
                    iteration=it, config=baseline, config_diff={},
                    train_result=train, validation_result=None,
                    improved=improved)
                iters.append(entry)
                if progress_callback is not None:
                    progress_callback(entry)
            base_board = sim_optimizer.IterationResult(
                iteration=0, config=baseline, config_diff={},
                train_result=iters[0].train_result,
                validation_result=template, improved=False)
            return sim_optimizer.OptimizationResult(
                best=None, baseline=iters[0], iterations=iters,
                validation_leaderboard=[base_board], warnings=[])

        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            scripted_optimizer)
        real_store = sim_optimizer._store_details

        def flaky_store(db, run_id, iteration, replay):
            if iteration == 2:
                # Persist junk-free failure: land rows, then report False.
                return False
            return real_store(db, run_id, iteration, replay)

        monkeypatch.setattr(sim_optimizer, "_store_details", flaky_store)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        run_id = res["run_id"]
        by_iter: dict = {}
        for row in fake_db.select(sim_optimizer.TRADES_TABLE,
                                  {"run_id": run_id}, limit=100000):
            by_iter.setdefault(row.get("iteration"), 0)
            by_iter[row.get("iteration")] += 1
        # Iteration 1 (last successful best before the failure) must have
        # been deleted when iteration 3 improved; iteration 2 never
        # landed. Only pinned baseline 0 and final best 3 remain.
        assert 2 not in by_iter, f"orphan rows for failed iter 2: {by_iter}"
        assert 1 not in by_iter, f"stale best rows leaked: {by_iter}"
        assert 0 in by_iter and 3 in by_iter
        eq_iters = {r.get("iteration") for r in fake_db.select(
            sim_optimizer.EQUITY_TABLE, {"run_id": run_id}, limit=100000)}
        assert 2 not in eq_iters and 1 not in eq_iters
        assert {0, 3} <= eq_iters

    async def test_no_recommendation_meta_filter_has_zero_counts(
            self, fake_db, inline_worker, monkeypatch):
        """No-recommendation payload keeps the success shape: zero counts
        plus status/reason/ranking_effect."""
        def no_best_optimizer(series, baseline, *, starting_capital=1000.0,
                              search_config=None, progress_callback=None,
                              cancel_event=None):
            from tests.test_sim_optimizer import fake_replay
            train = fake_replay(series, baseline,
                                starting_capital=starting_capital)
            entry = sim_optimizer.IterationResult(
                iteration=0, config=baseline, config_diff={},
                train_result=train, validation_result=None, improved=False)
            if progress_callback is not None:
                progress_callback(entry)
            return sim_optimizer.OptimizationResult(
                best=None, baseline=entry, iterations=[entry],
                validation_leaderboard=[], warnings=[])

        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            no_best_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET", f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        report = st.get("meta_filter") or {}
        assert report["status"] == "unavailable"
        assert report["reason"] == "no recommendation"
        assert report["ranking_effect"] == "none"
        assert report["evaluated"] == 0
        assert report["take"] == 0
        assert report["skip"] == 0
        assert report["unavailable"] == 0
        assert report["skip_rate_pct"] == 0.0

    async def test_summary_failure_meta_filter_has_zero_counts(
            self, fake_db, inline_worker, monkeypatch):
        """A raised `summarize_meta_filter` still stores a shape-stable
        zero-count payload instead of a bare status dict."""
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)

        def _boom(series, config, trades):
            raise RuntimeError("meta store down")

        monkeypatch.setattr(sim_optimizer, "summarize_meta_filter", _boom)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1", "assets": ["EURUSD"]})
        assert res["ok"] is True
        st = await call("GET", f"/api/system/optimize/{res['run_id']}")
        assert st["ok"] is True
        assert st["status"] == "complete"
        report = st.get("meta_filter") or {}
        assert report["status"] == "unavailable"
        assert report["reason"] == "summary failed"
        assert report["ranking_effect"] == "none"
        assert report["evaluated"] == 0
        assert report["take"] == 0
        assert report["skip"] == 0
        assert report["unavailable"] == 0
        assert report["skip_rate_pct"] == 0.0
        reco = st.get("recommendation") or {}
        assert (reco.get("meta_filter") or {})["evaluated"] == 0


# ---------------------------------------------------------------------------
# Task 1: warm-start from a previous recommendation (RED first)
# ---------------------------------------------------------------------------
def _seed_completed_run_with_recommendation(fake_db, run_id: str) -> None:
    """One terminal run row whose recommendation sets min_opportunity=85."""
    from app.models.schemas import AppSettings

    now = datetime.now(timezone.utc).isoformat()
    config = AppSettings(min_opportunity=85).model_dump(mode="json")
    fake_db.insert(sim_optimizer.RUNS_TABLE, {
        "id": run_id,
        "status": "complete",
        "stage": "done",
        "progress": 1.0,
        "seed": 0,
        "starting_capital": 1000.0,
        "search_config": {},
        "baseline_settings": {},
        "dataset_version": "v1",
        "data_hash": None,
        "result": {
            "stage": "done",
            "best_train_pnl": 10.0,
            "best": {"iteration": 1, "train_metrics": None,
                     "validation_metrics": None, "config_diff": {}},
            "recommendation": {"iteration": 1, "config": config,
                               "validation_metrics": None,
                               "note": "seeded warm-start base"},
            "warnings": [],
        },
        "error": None,
        "created_at": now,
        "started_at": now,
        "finished_at": now,
    })


class TestWarmStart:
    async def test_start_from_previous_recommendation_uses_search_baseline(
            self, fake_db, inline_worker, monkeypatch):
        from app.models.schemas import AppSettings

        captured: dict = {}

        def fake_opt(series, baseline, **kwargs):
            captured["baseline"] = baseline
            return fast_fake_optimizer(series, baseline, **kwargs)

        monkeypatch.setattr(sim_optimizer, "optimize_settings", fake_opt)
        monkeypatch.setattr(sim_optimizer, "capture_live_baseline",
                            lambda db: AppSettings(min_opportunity=0))
        _seed_completed_run_with_recommendation(fake_db, "old-run")
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize", {
            "dataset_version": "v1", "assets": ["EURUSD"],
            "base_run_id": "old-run",
        })
        assert res["ok"] is True
        assert captured["baseline"].min_opportunity == 85
        row = fake_db.select("sim_opt_runs", {"id": res["run_id"]})[0]
        assert row["baseline_settings"]["min_opportunity"] == 0
        assert row["search_config"]["warm_start_run_id"] == "old-run"
        assert (row["search_config"]["warm_start_settings"]
                ["min_opportunity"] == 85)

    @pytest.mark.parametrize("row", [None, {"status": "running"},
                                     {"status": "complete", "result": {}}])
    async def test_warm_start_refuses_unusable_base_run(
            self, fake_db, inline_worker, row):
        if row:
            fake_db.insert("sim_opt_runs", {"id": "old-run", **row})
        before = len(fake_db.select("sim_opt_runs", limit=100))
        res = await call("POST", "/api/system/optimize", {
            "dataset_version": "v1", "assets": ["EURUSD"],
            "base_run_id": "old-run",
        })
        assert res["ok"] is False
        assert len(fake_db.select("sim_opt_runs", limit=100)) == before

    async def test_normal_start_without_base_run_id_is_unchanged(
            self, fake_db, inline_worker, monkeypatch):
        """No `base_run_id` means a pure live-baseline run: the worker
        receives the captured live baseline itself, the stored
        `baseline_settings` equals it, `search_config` carries no warm
        keys, and a prior completed run is neither read nor mutated."""
        from app.models.schemas import AppSettings

        live = AppSettings(min_opportunity=0)
        captured: dict = {}

        def fake_opt(series, baseline, **kwargs):
            captured["baseline"] = baseline
            return fast_fake_optimizer(series, baseline, **kwargs)

        monkeypatch.setattr(sim_optimizer, "optimize_settings", fake_opt)
        monkeypatch.setattr(sim_optimizer, "capture_live_baseline",
                            lambda db: live)
        _seed_completed_run_with_recommendation(fake_db, "old-run")
        _seed_dataset(fake_db, "v1")
        before_old = dict(
            fake_db.select("sim_opt_runs", {"id": "old-run"})[0])
        touched: list = []
        real_select = fake_db.select

        def spy_select(table, filters=None, **kw):
            if (table == sim_optimizer.RUNS_TABLE
                    and (filters or {}).get("id") == "old-run"):
                touched.append((table, dict(filters or {})))
            return real_select(table, filters=filters, **kw)

        monkeypatch.setattr(fake_db, "select", spy_select)
        res = await call("POST", "/api/system/optimize", {
            "dataset_version": "v1", "assets": ["EURUSD"],
        })
        assert res["ok"] is True
        assert captured["baseline"] is live
        row = real_select("sim_opt_runs", {"id": res["run_id"]})[0]
        assert row["baseline_settings"] == live.model_dump(mode="json")
        assert "warm_start_run_id" not in row["search_config"]
        assert "warm_start_settings" not in row["search_config"]
        assert touched == []
        assert real_select("sim_opt_runs", {"id": "old-run"})[0] \
            == before_old


# ---------------------------------------------------------------------------
# Max-search plan Task 1 (RED): realistic run-level costs over the API.
# ---------------------------------------------------------------------------
class TestRunLevelCosts:
    async def test_start_persists_custom_run_costs(
            self, fake_db, inline_worker, monkeypatch):
        captured: dict = {}

        def fake_opt(series, baseline, **kwargs):
            captured["search_config"] = kwargs.get("search_config")
            return fast_fake_optimizer(series, baseline, **kwargs)

        monkeypatch.setattr(sim_optimizer, "optimize_settings", fake_opt)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize", {
            "dataset_version": "v1", "assets": ["EURUSD"],
            "search_config": {"max_iterations": 3,
                              "commission_per_lot_per_side": 9.0,
                              "exit_spread_mult": 0.75},
        })
        assert res["ok"] is True
        row = fake_db.select("sim_opt_runs", {"id": res["run_id"]})[0]
        assert row["search_config"]["commission_per_lot_per_side"] == 9.0
        assert row["search_config"]["exit_spread_mult"] == 0.75
        assert captured["search_config"].commission_per_lot_per_side == 9.0
        assert captured["search_config"].exit_spread_mult == 0.75

    async def test_start_defaults_run_costs_to_realistic(
            self, fake_db, inline_worker, monkeypatch):
        monkeypatch.setattr(sim_optimizer, "optimize_settings",
                            fast_fake_optimizer)
        _seed_dataset(fake_db, "v1")
        res = await call("POST", "/api/system/optimize",
                         {"dataset_version": "v1",
                          "assets": ["EURUSD"]})
        assert res["ok"] is True
        row = fake_db.select("sim_opt_runs", {"id": res["run_id"]})[0]
        assert row["search_config"]["commission_per_lot_per_side"] == 3.5
        assert row["search_config"]["exit_spread_mult"] == 0.5

    async def test_start_rejects_bad_run_costs(self, fake_db):
        before = len(fake_db.select("sim_opt_runs", limit=100))
        res = await call("POST", "/api/system/optimize", {
            "dataset_version": "v1", "assets": ["EURUSD"],
            "search_config": {"commission_per_lot_per_side": -1.0},
        })
        assert res["ok"] is False
        assert len(fake_db.select("sim_opt_runs", limit=100)) == before
