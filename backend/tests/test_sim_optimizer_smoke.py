"""Offline end-to-end sandbox optimizer smoke test (Task 7).

Synthetic and offline: a handful of synthetic daily bars per asset (no
network), the real ``optimize_settings`` search (2-3 iterations) driven
through the real background job with an inline worker, and the real guarded
apply path. Asserts:

  1. the persisted run carries the user-supplied starting capital;
  2. zero writes to ``trading_settings`` and zero broker/notifier calls
     (tripwire fake DB + module import-surface scan);
  3. iteration rows accumulate with diffs and metrics;
  4. the recommendation carries both train and validation metrics;
  5. apply with ``confirm=False`` and with a drifted live baseline are both
     refused with zero writes, while ``confirm=True`` on a fresh baseline
     succeeds.

Run from backend/:
  D:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_sim_optimizer_smoke.py -v
"""
from __future__ import annotations

import ast
import inspect
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.main import app
from app.models.schemas import AppSettings
from app.services import sim_market_data, sim_optimizer
from tests.test_sim_optimizer import fake_replay
from tests.test_sim_optimizer_api import _seed_dataset, call
from tests.test_sim_optimizer_apply import (
    ApplyFakeDatabase,
    ApplySettingsClient,
    _live_row,
)


@pytest.fixture
def fake_db() -> ApplyFakeDatabase:
    db = ApplyFakeDatabase()
    app.state.db = db
    return db


@pytest.fixture(autouse=True)
def _clean_state():
    sim_optimizer._set_active(None)
    sim_optimizer._CANCEL.clear()
    yield
    sim_optimizer._set_active(None)
    sim_optimizer._CANCEL.clear()
    from tests.test_workers import FakeDatabase
    app.state.db = FakeDatabase()


class _InlinePool:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)
        return None


@pytest.fixture
def inline_worker(monkeypatch):
    monkeypatch.setattr(sim_optimizer, "_POOL", _InlinePool())


CAPITAL = 4321.0
SEEN_CAPITALS: list[float] = []


def _capital_recording_replay(series, settings, *, starting_capital=1000.0,
                              cost_config=None, warnings=None):
    SEEN_CAPITALS.append(float(starting_capital))
    return fake_replay(series, settings, starting_capital=starting_capital,
                       cost_config=cost_config, warnings=warnings)


async def test_offline_smoke_end_to_end(fake_db, inline_worker, monkeypatch):
    SEEN_CAPITALS.clear()
    monkeypatch.setattr(sim_optimizer, "replay_portfolio",
                        _capital_recording_replay)
    _seed_dataset(fake_db, "v1", assets=("EURUSD", "XAUUSD"), n=14)

    # --- start a 3-iteration run with user-supplied capital -----------------
    res = await call("POST", "/api/system/optimize", {
        "dataset_version": "v1",
        "starting_capital": CAPITAL,
        "search_config": {"max_iterations": 3, "seed": 7},
    })
    assert res["ok"] is True, res
    run_id = res["run_id"]

    # (1) persisted run carries the user-supplied starting capital ------------
    status = await call("GET", f"/api/system/optimize/{run_id}")
    assert status["ok"] is True, status
    assert status["starting_capital"] == pytest.approx(CAPITAL)
    assert status["status"] == "complete", status
    assert SEEN_CAPITALS, "replay never ran"
    assert all(c == pytest.approx(CAPITAL) for c in SEEN_CAPITALS)

    # (2) zero live writes and zero broker/notifier surface -------------------
    assert fake_db.saved_settings == {}
    import ast as _ast
    tree = _ast.parse(inspect.getsource(sim_optimizer))
    imported = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, _ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    called = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            f = node.func
            name = (f.attr if isinstance(f, _ast.Attribute)
                    else f.id if isinstance(f, _ast.Name) else "")
            called.add(name)
    import re as _re
    for token in ("broker", "notifier", "notify", "send_line", "line_push",
                  "place_order", "submit_order", "open_trade"):
        pat = _re.compile(r"(?<![a-z])" + token + r"(?![a-z])")
        hits = {m for m in imported if pat.search(m.lower())} | {
            c for c in called if pat.search(c.lower())}
        assert not hits, f"optimizer surface hits {token!r}: {sorted(hits)}"

    # (3) iteration rows accumulate with diffs and metrics ---------------------
    iters = await call("GET", f"/api/system/optimize/{run_id}/iterations")
    assert iters["ok"] is True, iters
    rows = sorted(iters["rows"], key=lambda r: r["iteration"])
    assert iters["total"] == 3 and len(rows) == 3, iters
    first, rest = rows[0], rows[1:]
    assert first["iteration"] == 0 and first["config_diff"] == {}
    assert [r["iteration"] for r in rest] == [1, 2], iters
    for row in rows:
        assert row["train_metrics"] and row["train_metrics"]["trades"] > 0
        assert row["config"] and row["config_diff"] is not None

    # (4) recommendation carries train AND validation metrics ----------------
    rec = status.get("recommendation")
    assert isinstance(rec, dict) and rec.get("config"), status
    detail = next((r for r in rows if r["iteration"] == rec.get("iteration")),
                  None)
    assert detail is not None
    train = detail["train_metrics"]
    val = rec.get("validation_metrics") or detail.get("validation_metrics")
    assert train and train.get("net_pnl") is not None
    assert val and val.get("net_pnl") is not None and val.get("trades", 0) >= 0

    # (5a) confirm=False refused with zero writes ------------------------------
    denied = await call("POST", f"/api/system/optimize/{run_id}/apply",
                        {"confirm": False})
    assert denied["ok"] is False, denied
    assert fake_db.saved_settings == {}

    # (5b) drifted live baseline refused with zero writes ----------------------
    live_now = AppSettings().model_copy(update={"min_confidence": 80.0})
    app.state.db._client = ApplySettingsClient(
        app.state.db, _live_row(live_now))
    stale = await call("POST", f"/api/system/optimize/{run_id}/apply",
                       {"confirm": True})
    assert stale["ok"] is False, stale
    assert "baseline" in stale["error"]
    assert app.state.db.saved_settings == {}
    assert app.state.db.settings_upserts == []

    # (5c) confirm=True on a fresh baseline succeeds ---------------------------
    row = next(r for r in app.state.db.rows[sim_optimizer.RUNS_TABLE]
               if r.get("id") == run_id)
    import json as _json
    baseline = AppSettings.model_validate(
        _json.loads(_json.dumps(row["baseline_settings"])))
    app.state.db._client = ApplySettingsClient(
        app.state.db, _live_row(baseline))
    before = len(app.state.db.settings_upserts)
    ok = await call("POST", f"/api/system/optimize/{run_id}/apply",
                    {"confirm": True})
    assert ok["ok"] is True, ok
    assert len(app.state.db.settings_upserts) == before + 1
    assert set(app.state.db.saved_settings) <= set(
        AppSettings.model_fields)
