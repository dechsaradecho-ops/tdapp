"""Barrier simulation as a re-runnable, observable job.

WHY A JOB AND NOT A REQUEST
---------------------------
The owner asked (2026-10-06) for a "run 5,000 samples" button with a live
progress bar and a running TP/SL/PnL trace. A 5,000-event replay across 28
pairs is CPU-bound for tens of seconds — several minutes with the network
fetches — so it cannot live inside a request/response cycle. It runs on a
single-worker thread pool and the UI polls:

    POST /api/system/simulate              -> {"run_id": ...}
    GET  /api/system/simulate/{id}         -> status + stage + partial verdict
    GET  /api/system/simulate/{id}/events  -> rows after ?after=seq (the trace)
    POST /api/system/simulate/{id}/cancel  -> cooperative stop

WHY EVERY EVENT IS PERSISTED
----------------------------
The verdict is a summary; the events are the evidence. Persisting each label
means any surprising number can be traced back to the trades behind it, and
the whole sweep is recomputable in SQL. A run whose aggregate cannot be
re-derived from its own rows is a number nobody should act on.

SAFETY
------
* ONE run at a time. The pool has a single worker and a start is refused
  while another is live — a second 28-pair fetch would double the API load
  and produce interleaved, unreadable progress.
* Progress writes are throttled (default every 25 events). 5,000 upserts of
  the run row would dominate the runtime and hammer Postgres.
* Cancellation is cooperative and checked every batch, so a cancel lands
  within a fraction of a second instead of after the whole replay.
* Failures are recorded on the row and never raised into the worker, so a
  dead run is visible in the UI instead of vanishing.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

log = logging.getLogger("tdapp.simulation")

RUNS_TABLE = "simulation_runs"
EVENTS_TABLE = "simulation_events"

#: One worker on purpose — see "WHY A JOB" above.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="simulate")

#: run_id -> cancel flag, so /cancel can reach a run already in flight.
_CANCEL: dict[str, threading.Event] = {}

#: Guards the "is a run live" check without relying on DB freshness.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: Optional[str] = None

DEFAULT_TARGET = 5000
DEFAULT_COOLDOWN = 1
DEFAULT_ASSETS = [
    "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "EURCHF", "AUDCHF",
    "CADCHF", "GBPCHF", "GBPJPY", "CHFJPY", "CADJPY", "AUDJPY", "EURJPY",
    "NZDJPY", "AUDNZD", "EURAUD", "EURCAD", "EURGBP", "EURNZD", "GBPAUD",
    "GBPCAD", "NZDCAD", "AUDCAD", "USDCAD", "USDCHF", "GBPNZD", "XAUUSD",
]

SL_MULTIPLES = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
#: TP grid the sweep explores. 2.5R/3.0R were REMOVED (owner 2026-10-06): the
#: 5,000-sample run showed only ~2% of signals ever offered +2R, so those two
#: cells spent resolution on targets the market practically never prints.
#: Full grid is now 8 x 6 x 3 = 144 cells.
TP_RS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
MAX_BARS = (5, 10, 20)

#: CSV export columns — the same fields the tab streams live, in a stable
#: order, so a downloaded file and the on-screen trace always agree.
SIM_EXPORT_COLUMNS = (
    "seq", "asset", "direction", "entry", "atr_pct", "opportunity",
    "confidence", "sl_mult", "tp_r", "max_bars", "label", "r_multiple",
    "bars_held", "exit_price", "ambiguous", "gap_fill", "mfe_r", "mae_r",
    # bar_index LAST on purpose: it is the join key to the history CSV
    # (asset + bar_index), and appending keeps every existing column position
    # stable for readers of older files.
    "bar_index",
)


def _csv_cell(v: Any) -> str:
    """One CSV cell. None -> empty; quoting only when the value needs it, so
    numeric columns stay clean for spreadsheets."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    if any(c in s for c in (",", '"', "\n", "\r")):
        return '"' + s.replace('"', '""') + '"'
    return s


def iter_export_rows(db, run_id: str, page: int = 1000):
    """Yield a run's labelled events oldest-first, in seq pages.

    Sorted defensively: the live query orders by seq, but the generator must
    not depend on it — and it must terminate even against a client that
    ignores the cursor (the `top <= after` guard).
    """
    after = -1
    while True:
        res = db._client.table(EVENTS_TABLE).select(
            ",".join(SIM_EXPORT_COLUMNS)
        ).eq("run_id", run_id).gt("seq", after).order("seq") \
            .limit(max(1, page)).execute()
        rows = sorted(list(res.data or []),
                      key=lambda r: int(r.get("seq") or 0))
        if not rows:
            return
        top = int(rows[-1].get("seq") or 0)
        if top <= after:
            return
        after = top
        for r in rows:
            yield r


def export_csv(db, run_id: str):
    """Whole run as CSV text. Small wrapper over the iterator for tests and
    the route; the route streams instead of building the string."""
    lines = [",".join(SIM_EXPORT_COLUMNS)]
    for r in iter_export_rows(db, run_id):
        lines.append(",".join(_csv_cell(r.get(c)) for c in SIM_EXPORT_COLUMNS))
    return "\n".join(lines) + "\n"

#: Signal-gate thresholds the sweep explores. The simulation NEVER reads these
#: from production — a candidate config that inherited the live gate could
#: never be evaluated independently of it, which is the whole point of running
#: the simulation separately.
GATE_OPPS = (0, 50, 60, 70)
GATE_CONFS = (0, 60, 70, 80)


def live_baseline(db) -> dict[str, Any]:
    """What production is running RIGHT NOW, as a comparable config.

    Read-only and used purely as the benchmark every candidate is scored
    against. It is captured into the run row at START so the comparison stays
    reproducible even after someone edits the live settings mid-run — a
    baseline that moves under the experiment is not a baseline.

    The barrier cell is reconstructed the way ``effective_sl_tp`` builds it:
    the mode's ATR multiple, clamped to the configured band.
    """
    out: dict[str, Any] = {"source": "unavailable"}
    try:
        from app.api.routes.settings import try_load_settings
        s = try_load_settings(db)
        if s is None:
            return out
        from app.models.schemas import SL_TIER_MULT
        mode = str(getattr(s, "sl_distance_mode", "") or "medium")
        out.update({
            "source": "db",
            "sl_distance_mode": mode,
            "sl_atr_mult": float(SL_TIER_MULT.get(mode, 1.5)),
            "sl_min_pct": float(getattr(s, "sl_distance_min_pct", 0) or 0),
            "sl_max_pct": float(getattr(s, "sl_distance_max_pct", 0) or 0),
            "rr_target": float(getattr(s, "rr_target", 0) or 0),
            "min_opportunity": float(getattr(s, "min_opportunity", 0) or 0),
            "min_confidence": float(getattr(s, "min_confidence", 0) or 0),
        })
        return out
    except Exception as exc:
        out["error"] = str(exc)[:200]
        return out

#: How long a run and its events are kept. 14 days, not 7: a simulation is
#: something the owner comes back to compare ("the last run said the opposite
#: — which is right?"), so a week is barely enough to notice the difference.
SIMULATION_TTL_DAYS = 14
#: Keep this many finished runs even when they are older than the TTL, so the
#: history panel never goes blank and there is always something to re-open.
SIMULATION_KEEP_RUNS = 20
_purge_lock = threading.Lock()
_last_purge = 0.0
PURGE_INTERVAL_S = 3600.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def active_run_id() -> Optional[str]:
    with _ACTIVE_LOCK:
        return _ACTIVE


def _set_active(run_id: Optional[str]) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = run_id


def cancel_requested(run_id: str) -> bool:
    ev = _CANCEL.get(run_id)
    return bool(ev and ev.is_set())


def request_cancel(run_id: str) -> bool:
    """Ask a live run to stop. Returns False when it is not cancellable."""
    ev = _CANCEL.get(run_id)
    if ev is None or ev.is_set():
        return False
    ev.set()
    return True


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def start_run(db, *, target_events: int = DEFAULT_TARGET,
              cooldown: int = DEFAULT_COOLDOWN,
              assets: Optional[list[str]] = None,
              days: int = 1095,
              sl_multiples=SL_MULTIPLES, tp_rs=TP_RS,
              max_bars=MAX_BARS,
              gate_opps=GATE_OPPS, gate_confs=GATE_CONFS) -> dict[str, Any]:
    """Register a run and hand it to the worker. Returns immediately.

    ``gate_opps`` / ``gate_confs`` are the SIMULATION's own thresholds. They
    default to the exploration ladder, NOT to production — the owner asked
    (2026-10-06) for the simulation to be judgeable separately from live
    settings, so nothing here is inherited from the trading config. Production
    is captured once as ``live_baseline`` purely to be scored against.
    """
    if not db or not db.available:
        return {"ok": False, "error": "DB unavailable — cannot start a run"}
    live = active_run_id()
    if live:
        return {"ok": False, "error": "มีการรันอยู่แล้ว", "run_id": live}
    try:
        run_id = uuid.uuid4().hex
        cfg = {
            "target_events": int(target_events),
            "cooldown": int(cooldown),
            "assets": list(assets or DEFAULT_ASSETS),
            "days": int(days),
            "sl_multiples": [float(x) for x in sl_multiples],
            "tp_rs": [float(x) for x in tp_rs],
            "max_bars": [int(x) for x in max_bars],
            "gate_opps": [float(x) for x in (gate_opps or GATE_OPPS)],
            "gate_confs": [float(x) for x in (gate_confs or GATE_CONFS)],
            # captured once, at start, so the benchmark cannot drift mid-run
            "live_baseline": live_baseline(db),
        }
        db.insert(RUNS_TABLE, {
            "id": run_id,
            "status": "pending",
            "stage": "queued",
            "target_events": cfg["target_events"],
            "total_events": 0,
            "processed": 0,
            "config": cfg,
        })
    except Exception as exc:
        log.error("start_run: could not create the run row: %s", exc)
        return {"ok": False,
                "error": "สร้างงานไม่สำเร็จ — ตรวจว่ารัน migration 057 แล้วหรือยัง",
                "detail": str(exc)}

    _CANCEL[run_id] = threading.Event()
    _set_active(run_id)
    _POOL.submit(_run_job, db, run_id, cfg)
    return {"ok": True, "run_id": run_id, "config": cfg}


# ---------------------------------------------------------------------------
# The worker (blocking — runs on the pool thread)
# ---------------------------------------------------------------------------
def _run_job(db, run_id: str, cfg: dict[str, Any]) -> None:
    """Execute one run to completion. Never raises."""
    cancel = _CANCEL.setdefault(run_id, threading.Event())
    t0 = time.time()
    try:
        _patch(db, run_id, status="running", stage="fetching", started_at=_now())
        series, history_report = _load_history(db, cfg)
        if cancel.is_set():
            return _finish(db, run_id, "cancelled", cancel, t0)

        assets_ok = sorted(series)
        _patch(db, run_id, stage="replaying",
               result={"assets": assets_ok,
                       "bars": {a: len(c) for a, c in series.items()},
                       "history": history_report})

        from app.engine.strategy_replay import replay
        events = replay(series, cooldown_bars=cfg["cooldown"])
        if cancel.is_set():
            return _finish(db, run_id, "cancelled", cancel, t0)

        # The caller asked for N samples; history is finite, so a run can come
        # up short. Report the real number rather than padding or pretending.
        target = int(cfg.get("target_events") or 0)
        if target and len(events) > target:
            # Sort BEFORE thinning. replay() emits events grouped by asset
            # (each asset's own history in time order), so thinning the raw
            # list would sample by list position — which means taking a
            # different slice of TIME for every pair. The train/test split
            # downstream would then compare "the early half of pair A"
            # against "the late half of pair B". Sorting to one timeline
            # first is what makes the split chronological.
            events = _thin(sorted(events, key=lambda e: (e.bar_index, e.asset)),
                           target)

        total = len(events)
        _patch(db, run_id, stage="labelling", total_events=total)
        if not total:
            return _finish(db, run_id, "done", cancel, t0, result={
                "error": "ไม่มี event จากประวัติที่ดึงได้ — ลองเพิ่ม days หรือสินทรัพย์"})

        _persist_events(db, run_id, events, cfg, cancel, t0)
        if cancel.is_set():
            return _finish(db, run_id, "cancelled", cancel, t0)

        _patch(db, run_id, stage="analysing", processed=total)
        verdict = _analyse(events, cfg)
        _finish(db, run_id, "done", cancel, t0, result=verdict)
    except Exception as exc:                       # noqa: BLE001 - must not kill the pool
        log.exception("simulation run %s failed", run_id)
        try:
            _patch(db, run_id, status="failed", stage="failed",
                   error=f"{type(exc).__name__}: {exc}"[:400], finished_at=_now())
        except Exception:
            pass
    finally:
        _set_active(None)
        _CANCEL.pop(run_id, None)


def _thin(events: list, target: int) -> list:
    """Evenly sample the event list down to ``target``.

    Assumes the caller already put the list on one timeline (sorted by
    ``bar_index``) — see the call site. Even spacing, not a head/tail cut:
    taking the first N would silently restrict the study to the oldest slice
    of history, which is the one way a sample can be made to look better
    without changing a single number.
    """
    n = len(events)
    if target >= n:
        return events
    step = n / float(target)
    return [events[int(i * step)] for i in range(target)]


def _load_history(db, cfg: dict[str, Any]) -> tuple[dict, dict]:
    """History for a run: stored bars first, fetching only the missing ones.

    Falls back to a direct fetch when the history table is unavailable
    (migration 058 not applied, DB hiccup) — a missing cache must degrade the
    run, never kill it. The report says which path was taken so the verdict
    can be judged accordingly.
    """
    try:
        from app.services import price_history
        series, report = price_history.load_series(
            db, cfg.get("assets") or [], int(cfg.get("days") or 1095))
        if series:
            return series, report
        raise RuntimeError("history empty after sync")
    except Exception as exc:
        series = _fetch_series(cfg)
        return series, {"source": "direct fetch (history unavailable)",
                        "error": str(exc)[:160]}


def _fetch_series(cfg: dict[str, Any]) -> dict[str, list]:
    """Daily candles for every requested pair, oldest-first."""
    import httpx
    from app.integrations import quotes

    days = int(cfg.get("days") or 1095)
    out: dict[str, list] = {}

    async def _go():
        async with httpx.AsyncClient() as client:
            for asset in cfg.get("assets") or []:
                try:
                    candles = await quotes.fetch_candles(asset, client, days=days)
                except Exception as exc:
                    log.info("simulate: %s history unavailable: %s", asset, exc)
                    continue
                if candles:
                    out[asset] = candles

    try:
        asyncio.run(_go())
    except Exception as exc:
        log.error("simulate: history fetch failed: %s", exc)
    return out


def _persist_events(db, run_id: str, events: list, cfg: dict[str, Any],
                    cancel: threading.Event, t0: float) -> None:
    """Label every event against the grid and write it out in batches.

    Labelling happens HERE rather than up front so the run has something to
    show almost immediately: the UI can stream TP/SL/expired counts and a
    running equity curve while the remaining events are still being processed.
    """
    from app.engine.triple_barrier import grid_key, label_event, label_grid

    sl_mults = cfg.get("sl_multiples") or list(SL_MULTIPLES)
    tp_rs = cfg.get("tp_rs") or list(TP_RS)
    max_bars = cfg.get("max_bars") or list(MAX_BARS)

    # One cell is labelled per event (the "primary" cell) so the live trace
    # shows a single coherent equity curve instead of a hundred interleaved
    # ones. The full grid is still swept at the end.
    primary = (1.5, 1.5, 20)
    rows: list[dict] = []
    seq = 0
    batch = 200
    done = 0
    last_write = 0.0

    for ev in events:
        if cancel.is_set():
            break
        try:
            atr = ev.entry * ev.atr_pct / 100.0
            sp_w = primary[0] * atr
            res = label_event(ev.direction, ev.entry,
                              _spec(sp_w, sp_w * primary[1], primary[2]),
                              ev.future)
        except Exception:
            continue
        if res.label == "pending":
            # no future bars to label against — not a loss, just not a sample
            continue
        seq += 1
        done += 1
        rows.append({
            "id": uuid.uuid4().hex,
            "run_id": run_id,
            "seq": seq,
            "asset": ev.asset,
            "direction": ev.direction,
            "bar_index": ev.bar_index,
            "entry": round(ev.entry, 6),
            "atr_pct": round(ev.atr_pct, 4),
            "opportunity": round(ev.opportunity, 2),
            "confidence": round(ev.confidence, 2),
            "sl_mult": primary[0],
            "tp_r": primary[1],
            "max_bars": primary[2],
            "label": res.label,
            "r_multiple": round(res.r_multiple, 4),
            "bars_held": res.bars_held,
            "exit_price": round(res.exit_price, 6),
            "ambiguous": bool(res.ambiguous),
            "gap_fill": bool(res.gap_fill),
            "mfe_r": round(res.mfe_r, 4),
            "mae_r": round(res.mae_r, 4),
        })
        # label the full grid too — cheap relative to the replay, and it
        # means the final sweep never has to re-walk history
        try:
            label_grid(ev, sl_mults, tp_rs, max_bars)
        except Exception:
            pass

        if len(rows) >= batch or done == len(events):
            _flush(db, rows)
            rows = []
            now = time.time()
            if now - last_write >= 0.4 or done == len(events):
                _patch(db, run_id, processed=done)
                last_write = now
    if rows:
        _flush(db, rows)
    _patch(db, run_id, processed=done)


def _spec(sl_w: float, tp_w: float, mb: int):
    from app.engine.triple_barrier import BarrierSpec
    return BarrierSpec(sl_w, tp_w, mb)


def _flush(db, rows: list[dict]) -> None:
    if not rows:
        return
    try:
        db._client.table(EVENTS_TABLE).insert(rows).execute()
    except Exception as exc:
        log.error("simulate: event batch insert failed (%d rows): %s",
                  len(rows), exc)


def _patch(db, run_id: str, **changes) -> None:
    try:
        db.update(RUNS_TABLE, run_id, changes)
    except Exception as exc:
        log.debug("simulate: run patch failed: %s", exc)


def _finish(db, run_id: str, status: str, cancel: threading.Event,
            t0: float, result: Optional[dict] = None) -> None:
    _patch(db, run_id, status=status, stage=status, finished_at=_now(),
           result=result or {}, error=None if status == "done" else status)


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------
def purge_old_runs(db, force: bool = False) -> int:
    """Drop runs past the TTL (and their events). Never raises.

    POLICY: the newest ``SIMULATION_KEEP_RUNS`` runs are kept regardless of
    age; everything older than the TTL beyond that is removed. The two rules
    together BOUND the table at ~KEEP runs — a small table is never pruned
    below the floor, which is intentional: an owner comparing "the last run
    said the opposite" should still find it there. Unbounded growth is
    prevented by the floor, not by the TTL alone.

    A run that is still pending/running is never touched — losing the run
    whose progress bar is on screen is worse than any table size.

    Throttled unless forced, matching the other log purges. Events are swept
    explicitly as well as via the cascade, in case a row predates the
    constraint. Returns the number of RUNS removed.
    """
    global _last_purge
    with _purge_lock:
        now = time.time()
        if not force and now - _last_purge < PURGE_INTERVAL_S:
            return 0
        _last_purge = now
    try:
        if not db or not db.available:
            return 0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=SIMULATION_TTL_DAYS)).isoformat()
        # Newest first, then drop the protected head, then apply the TTL.
        newest_first = db.select(RUNS_TABLE, order="created_at", desc=True,
                                 limit=500)
        doomed = [r for r in newest_first[SIMULATION_KEEP_RUNS:]
                  if str(r.get("created_at") or "") < cutoff
                  and str(r.get("status")) not in ("pending", "running")]
        removed = 0
        for r in doomed:
            rid = str(r.get("id") or "")
            if not rid:
                continue
            try:
                db._client.table(EVENTS_TABLE).delete().eq("run_id", rid).execute()
            except Exception as exc:
                log.debug("simulate event purge %s failed: %s", rid, exc)
            try:
                db._client.table(RUNS_TABLE).delete().eq("id", rid).execute()
                removed += 1
            except Exception as exc:
                log.debug("simulate run purge %s failed: %s", rid, exc)
        return removed
    except Exception as exc:
        log.debug("simulation purge failed: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Analysis (runs after every event is labelled)
# ---------------------------------------------------------------------------
def _gate_pass(ev, min_opp: float, min_conf: float) -> bool:
    return (float(ev.opportunity or 0) >= min_opp
            and float(ev.confidence or 0) >= min_conf)


def _score(events, sl_mult: float, tp_r: float, max_bars: int,
           min_opp: float, min_conf: float) -> dict[str, Any]:
    """Mean R / win rate for one (barrier cell, gate) pair."""
    from app.engine.triple_barrier import grid_key, summarise_cell, BarrierSpec, label_event

    keep = [e for e in events if _gate_pass(e, min_opp, min_conf)]
    rs: list[float] = []
    for e in keep:
        key = grid_key(sl_mult, tp_r, max_bars)
        if key in e.outcomes:
            rs.append(e.outcomes[key].r_multiple)
            continue
        atr = e.entry * e.atr_pct / 100.0
        sp = BarrierSpec(sl_mult * atr, sl_mult * atr * tp_r, int(max_bars))
        rs.append(label_event(e.direction, e.entry, sp, e.future).r_multiple)
    n = len(rs)
    return {
        "n": n,
        "mean_r": round(sum(rs) / n, 4) if n else 0.0,
        "win_rate_pct": round(100.0 * sum(1 for x in rs if x > 0) / n, 1) if n else 0.0,
    }


def _analyse(events: list, cfg: dict[str, Any]) -> dict[str, Any]:
    """Sweep the grid, then hold every ranking to a chronological test split.

    The split is not optional decoration. Ranking the whole grid on one sample
    is a multi-hundred-way race, and the winner of such a race is positive
    even with no edge at all — that is exactly what the first run of this
    study showed (+0.101R in-sample collapsing to -0.061R out). Anything
    reported without the out-of-sample number is a number that has not been
    tested.

    SELECTION DISCIPLINE (owner 2026-10-06: "แยกค่า setting sim กับของจริง"):
    every choice below is made on the TRAIN period only, then scored once on
    TEST:
      1. the gate (min_opportunity x min_confidence),
      2. the barrier cell (SL x ATR, target R, clock).
    Two independent selections rather than one search over the product,
    because the product (grid cells x gate pairs, in the thousands) raced at
    once tells you nothing about its winner. Production is then scored the
    same way as a benchmark, not as a candidate.
    """
    from app.engine.triple_barrier import summarise_cell, sweep_grid

    sl_mults = cfg.get("sl_multiples") or list(SL_MULTIPLES)
    tp_rs = cfg.get("tp_rs") or list(TP_RS)
    max_bars = cfg.get("max_bars") or list(MAX_BARS)
    gate_opps = cfg.get("gate_opps") or list(GATE_OPPS)
    gate_confs = cfg.get("gate_confs") or list(GATE_CONFS)
    base = cfg.get("live_baseline") or {}

    min_n = max(30, len(events) // 20)
    ordered = sorted(events, key=lambda e: (e.bar_index, e.asset))
    cut = int(len(ordered) * 0.7)
    train, test = ordered[:cut], ordered[cut:]
    enough = len(train) >= 60 and len(test) >= 30

    # ---- ungated views (what the whole grid looks like) ------------------
    by_paid = sweep_grid(events, sl_mults, tp_rs, max_bars, min_n=min_n)
    by_worth = sweep_grid(events, sl_mults, tp_rs, max_bars, min_n=min_n,
                          rank_by_resolved=True)
    verdict: dict[str, Any] = {
        "n_events": len(events),
        "min_n": min_n,
        "assets": sorted({e.asset for e in events}),
        "bars": cfg.get("days"),
        "cooldown": cfg.get("cooldown"),
        "grid_cells": len(sl_mults) * len(tp_rs) * len(max_bars),
        "gate_candidates": len(gate_opps) * len(gate_confs),
        "top_paid": [r.as_row() for r in by_paid[:12]],
        "top_worth": [r.as_row() for r in by_worth[:12]],
    }

    if not enough:
        verdict["walk_forward"] = {"error": "เหตุการณ์ไม่พอสำหรับแบ่ง train/test"}
        verdict["mfe_mae"] = _mfe_mae(events)
        return verdict

    # ---- stage 1: pick the gate on TRAIN only -----------------------------
    # Averaged across a representative subset of the grid (3 SL x 3 TP at the
    # longest clock), so the gate is chosen for how the signal behaves overall
    # rather than for one lucky cell. Averaging across the FULL grid on every
    # run would multiply the cost by the cell count; the 9-cell subset keeps it
    # flat while covering tight/typical/wide stops against 1R/1.5R/2R targets.
    # The subset is fixed below so the number stays comparable run to run.
    ref_cells = [(m, r, max_bars[-1])
                 for m in (1.0, 1.5, 2.0) for r in (1.0, 1.5, 2.0)]

    def _gate_avg(evts, mo, mc):
        rs, ns = [], []
        for sm, tr_, mb in ref_cells:
            s = _score(evts, sm, tr_, mb, mo, mc)
            if s["n"] >= 40:
                rs.append(s["mean_r"])
                ns.append(s["n"])
        n = min(ns) if ns else 0
        return (sum(rs) / len(rs), n) if rs else (0.0, 0)

    gate_rows = []
    for mo in gate_opps:
        for mc in gate_confs:
            tr_mean, tr_n = _gate_avg(train, mo, mc)
            if tr_n < 40:
                continue
            te_mean, te_n = _gate_avg(test, mo, mc)
            te_wr = _score(test, 1.5, 1.5, max_bars[-1], mo, mc)["win_rate_pct"]
            gate_rows.append({"min_opp": mo, "min_conf": mc,
                              "train_n": tr_n, "train_mean_r": round(tr_mean, 4),
                              "test_n": te_n, "test_mean_r": round(te_mean, 4),
                              "test_win_rate_pct": te_wr})
    gate_rows.sort(key=lambda r: -r["train_mean_r"])
    verdict["gate_sweep"] = gate_rows
    best_gate = (gate_rows[0]["min_opp"], gate_rows[0]["min_conf"]) \
        if gate_rows else (0.0, 0.0)
    verdict["best_gate"] = {"min_opp": best_gate[0], "min_conf": best_gate[1],
                            "selected_on": "train",
                            "train_mean_r": gate_rows[0]["train_mean_r"]
                            if gate_rows else None,
                            "test_mean_r": gate_rows[0]["test_mean_r"]
                            if gate_rows else None}

    # ---- stage 2: pick the barrier cell on TRAIN only, gate fixed ---------
    tr_events = [e for e in train if _gate_pass(e, *best_gate)]
    te_events = [e for e in test if _gate_pass(e, *best_gate)]
    tr_rows = sweep_grid(tr_events, sl_mults, tp_rs, max_bars,
                         min_n=max(20, len(tr_events) // 20)) if tr_events else []
    checks = []
    for r in tr_rows[:8]:
        te = summarise_cell(te_events, r.sl_width, r.tp_r, r.max_bars)
        d = te.as_row()
        checks.append({"sl_mult": r.sl_width, "tp_r": r.tp_r,
                       "max_bars": r.max_bars,
                       "train_mean_r": round(r.mean_r, 4),
                       "test_mean_r": d["mean_r"],
                       "test_win_rate_pct": d["win_rate_pct"],
                       "test_n": d["n"]})
    verdict["walk_forward"] = {
        "train_n": len(train), "test_n": len(test),
        "gate": {"min_opp": best_gate[0], "min_conf": best_gate[1]},
        "gated_train_n": len(tr_events), "gated_test_n": len(te_events),
        "checks": checks,
    }
    if tr_rows:
        win = tr_rows[0]
        tst = summarise_cell(te_events, win.sl_width, win.tp_r, win.max_bars)
        td = tst.as_row()
        verdict["walk_forward"].update({
            "winner": {"sl_mult": win.sl_width, "tp_r": win.tp_r,
                       "max_bars": win.max_bars,
                       "train_mean_r": round(win.mean_r, 4)},
            "test_mean_r": td["mean_r"],
            "test_win_rate_pct": td["win_rate_pct"],
            "test_n": td["n"],
            "holds_out": bool(td["mean_r"] > 0),
            "survivors": sum(1 for c in checks if c["test_mean_r"] > 0),
            "candidates_checked": len(checks),
        })

    # ---- the benchmark: production, scored identically -------------------
    verdict["production_benchmark"] = _score_production(train, test, base)

    # ---- per asset at the train-selected cell ----------------------------
    best = tr_rows[0] if tr_rows else (by_paid[0] if by_paid else None)
    if best and te_events:
        per = []
        for asset in sorted({e.asset for e in events}):
            a = [e for e in tr_events if e.asset == asset]
            b = [e for e in te_events if e.asset == asset]
            ra = summarise_cell(a, best.sl_width, best.tp_r, best.max_bars)
            rb = summarise_cell(b, best.sl_width, best.tp_r, best.max_bars)
            if not ra.n:
                continue
            per.append({"asset": asset, "train_n": ra.n,
                        "train_mean_r": round(ra.mean_r, 4),
                        "train_win_rate_pct": round(100 * ra.win_rate, 1),
                        "test_n": rb.n,
                        "test_mean_r": round(rb.mean_r, 4),
                        "test_win_rate_pct": round(100 * rb.win_rate, 1),
                        "keep": bool(ra.mean_r > 0)})
        per.sort(key=lambda r: -r["train_mean_r"])
        verdict["per_asset"] = per
        verdict["per_asset_cell"] = {"sl_mult": best.sl_width,
                                     "tp_r": best.tp_r,
                                     "max_bars": best.max_bars,
                                     "gate": verdict["best_gate"]}

    verdict["mfe_mae"] = _mfe_mae(events)
    # The next-round proposal, built from this verdict only. Stored with the
    # run so the UI can offer "run next with these values" without recomputing.
    verdict["recommendation"] = _recommend_next(cfg, verdict)
    return verdict


def _score_production(train: list, test: list, base: dict[str, Any]) -> dict[str, Any]:
    """Score the LIVE config on the same data, same split.

    The production stop is a percentage of price clamped to a band, so it is
    not one ATR multiple — it is re-derived per event the way
    ``effective_sl_tp`` does. Skipping this would leave the report comparing
    candidates against nothing.
    """
    if not base or base.get("source") != "db":
        return {"available": False,
                "reason": base.get("error") or "live settings unavailable"}

    mult = float(base.get("sl_atr_mult") or 1.5)
    rr = float(base.get("rr_target") or 0)
    lo = float(base.get("sl_min_pct") or 0) / 100.0
    hi = float(base.get("sl_max_pct") or 0) / 100.0
    mo = float(base.get("min_opportunity") or 0)
    mc = float(base.get("min_confidence") or 0)
    if rr <= 0:
        return {"available": False, "reason": "rr_target not readable"}

    from app.engine.triple_barrier import BarrierSpec, label_event

    def run(events):
        rs, n = [], 0
        for e in events:
            if not _gate_pass(e, mo, mc):
                continue
            dist = e.entry * (e.atr_pct / 100.0) * mult
            if hi > 0 and dist > e.entry * hi:
                dist = e.entry * hi
            if lo > 0 and dist < e.entry * lo:
                dist = e.entry * lo
            sp = BarrierSpec(dist, dist * rr, 20)
            res = label_event(e.direction, e.entry, sp, e.future)
            if res.label == "pending":
                continue
            rs.append(res.r_multiple)
            n += 1
        return {"n": n,
                "mean_r": round(sum(rs) / n, 4) if n else 0.0,
                "win_rate_pct": round(100.0 * sum(1 for x in rs if x > 0) / n, 1)
                if n else 0.0}

    tr, te = run(train), run(test)
    return {
        "available": True,
        "config": {k: base.get(k) for k in (
            "sl_distance_mode", "sl_atr_mult", "sl_min_pct", "sl_max_pct",
            "rr_target", "min_opportunity", "min_confidence")},
        "train": tr, "test": te,
        "positive_test": bool(te["mean_r"] > 0),
    }


def _mfe_mae(events: list) -> dict[str, Any]:
    """Excursion distribution — answers "was the target ever reachable?".

    MFE/MAE are denominated in R, so they depend on which barrier cell
    produced them. The cell is NAMED in the result rather than assumed —
    an earlier version hardcoded a key that was not in the grid and returned
    {} silently, which cost the first 5,000-sample run its most important
    number (only 2.3% of signals ever offered +2R, i.e. the live target was
    unreachable) without any error to show for it.

    Falls back to whatever cell the events actually carry, so a custom grid
    still produces excursion stats instead of nothing.
    """
    cells = ("1.5000|1.5000|20", "1.5000|2.2500|20")
    key = next((k for k in cells
                if any(k in e.outcomes for e in events)), None)
    if key is None:
        counts: dict[str, int] = {}
        for e in events:
            for k in e.outcomes:
                counts[k] = counts.get(k, 0) + 1
        if not counts:
            return {"error": "no labelled cell available for the excursion stats"}
        key = max(counts.items(), key=lambda kv: kv[1])[0]
    mfes = sorted(e.outcomes[key].mfe_r for e in events if key in e.outcomes)
    maes = sorted(e.outcomes[key].mae_r for e in events if key in e.outcomes)
    if not mfes:
        return {"error": "no excursions recorded"}

    def q(a, p):
        return a[min(len(a) - 1, int(len(a) * p))]

    out = {
        "cell": key,
        "mfe_p25": round(q(mfes, .25), 3), "mfe_median": round(q(mfes, .5), 3),
        "mfe_p75": round(q(mfes, .75), 3), "mfe_p95": round(q(mfes, .95), 3),
        "mfe_max": round(mfes[-1], 3),
        "mae_p25": round(q(maes, .25), 3), "mae_median": round(q(maes, .5), 3),
        "mae_p75": round(q(maes, .75), 3), "mae_min": round(maes[0], 3),
    }
    # Keys use underscores, never dots: the frontend reads `reached_1_5r_pct`,
    # and the old `reached_1.5r_pct` silently rendered as "—".
    for r, key in ((1.0, "reached_1r_pct"), (1.5, "reached_1_5r_pct"),
                   (2.0, "reached_2r_pct")):
        n = sum(1 for m in mfes if m >= r)
        out[key] = round(100.0 * n / len(mfes), 1)
    return out


# ---------------------------------------------------------------------------
# Next-round recommendation (owner 2026-10-06)
# ---------------------------------------------------------------------------
# "หลังรันแล้วสรุปว่ารอบหน้าจะปรับค่าจากอะไรเป็นอะไร แล้วให้รันรอบหน้าใช้ค่า
# ใหม่ได้เลย" — with one hard rule: the proposal changes the SIMULATION config
# only. Live trading settings are never touched by this; at most the report
# carries a text-only suggestion the owner must confirm by hand in Settings.
#
# SELECTION DISCIPLINE, same as everything else here: a change is proposed
# only from TRAIN-selected evidence. Anything else is labelled exploration,
# never a finding.
def _neighbors(cur: list, winner: float) -> list:
    """Winner plus one grid step each side, clipped to the current grid."""
    xs = sorted({float(x) for x in cur})
    if not xs:
        return []
    i = min(range(len(xs)), key=lambda k: abs(xs[k] - float(winner)))
    lo = max(0, i - 1)
    return xs[lo:i + 2]


def _prune_tp_by_mfe(tp_rs: list, mfe: dict) -> tuple[list, str]:
    """Drop targets the signals demonstrably never reach.

    Mechanical pruning, NOT an edge claim: when fewer than 10% of signals ever
    offered +2R, spending cells on 2.5R/3.0R targets wastes the next run's
    resolution. The MFE distribution (not the ranking) is the evidence, so
    this cannot cherry-pick a winner — it only removes dead search space.
    Never narrows below 3 values; an over-pruned grid tests nothing.
    """
    cur = sorted({float(x) for x in tp_rs})
    if len(cur) <= 3 or not mfe or mfe.get("error"):
        return cur, ""
    r2 = float(mfe.get("reached_2r_pct") or 0)
    r15 = float(mfe.get("reached_1_5r_pct") or 0)
    r1 = float(mfe.get("reached_1r_pct") or 0)
    if r2 >= 10:
        return cur, ""
    cap = 2.0 if r15 >= 10 else (1.5 if r1 >= 10 else 1.0)
    kept = [t for t in cur if t <= cap + 1e-9]
    if len(kept) < 3:
        return cur, ""
    return kept, (f"สัญญาณไปถึง +2R แค่ {r2:.1f}% — "
                  f"ตัดเป้าเกิน {cap:g}R ออก เหลือที่ไปถึงจริง")


def _nearest_mode(sl_mult: float) -> str:
    if sl_mult <= 1.25:
        return "short"
    if sl_mult <= 1.75:
        return "medium"
    return "long"


def _recommend_next(cfg: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """Build the next-round proposal. Never raises; never touches live."""
    rec: dict[str, Any] = {"has_plan": False, "changes": [], "next_config": {},
                           "live_suggestion": None, "note": ""}
    try:
        sl_mults = sorted({float(x) for x in (cfg.get("sl_multiples") or [])})
        tp_rs = sorted({float(x) for x in (cfg.get("tp_rs") or [])})
        max_bars = sorted({int(x) for x in (cfg.get("max_bars") or [])})
        wf = verdict.get("walk_forward") or {}
        if wf.get("error") or not wf.get("winner"):
            rec["note"] = ("รอบนี้ข้อมูลไม่พอแบ่ง train/test — "
                           "ยังสรุปอะไรไม่ได้ รันใหม่ด้วยตัวอย่างที่มากขึ้น")
            return rec

        holds = bool(wf.get("holds_out"))
        win = wf["winner"]
        nxt: dict[str, Any] = {}

        if holds and (wf.get("survivors") or 0) >= 1:
            # Evidence-backed zoom: the winner survived out-of-sample, so the
            # next run spends its resolution around it instead of re-racing
            # the whole grid.
            new_sl = _neighbors(sl_mults, win["sl_mult"])
            new_tp = _neighbors(tp_rs, win["tp_r"])
            new_mb = [int(win["max_bars"])]
            if new_sl != sl_mults:
                rec["changes"].append({
                    "field": "sl_multiples", "field_th": "SL (×ATR)",
                    "from": sl_mults, "to": new_sl,
                    "reason": (f"ช่อง {win['sl_mult']:g}×ATR รอดนอกตัวอย่าง "
                               f"({wf.get('test_mean_r'):+.3f}R) — ซูมรอบมัน")})
                nxt["sl_multiples"] = new_sl
            if new_tp != tp_rs:
                rec["changes"].append({
                    "field": "tp_rs", "field_th": "เป้าหมาย (R)",
                    "from": tp_rs, "to": new_tp,
                    "reason": (f"เป้า {win['tp_r']:g}R รอดนอกตัวอย่าง — "
                               "ซูมรอบมัน")})
                nxt["tp_rs"] = new_tp
            if new_mb != max_bars:
                rec["changes"].append({
                    "field": "max_bars", "field_th": "นาฬิกา (แท่ง)",
                    "from": max_bars, "to": new_mb,
                    "reason": "คงนาฬิกาของช่องที่รอด"})
                nxt["max_bars"] = new_mb
        else:
            # No survivor: narrowing around a failed winner would be
            # cherry-picking. Prune only the demonstrably unreachable targets.
            kept, why = _prune_tp_by_mfe(tp_rs, verdict.get("mfe_mae") or {})
            if kept != tp_rs:
                rec["changes"].append({
                    "field": "tp_rs", "field_th": "เป้าหมาย (R)",
                    "from": tp_rs, "to": kept, "reason": why})
                nxt["tp_rs"] = kept

        # Gate: fix it only when the train-picked gate is ALSO positive out
        # of sample. Otherwise the ladder stays — the data said nothing.
        bg = verdict.get("best_gate") or {}
        if (bg.get("test_mean_r") or 0) > 0:
            nxt["gate_opps"] = [bg["min_opp"]]
            nxt["gate_confs"] = [bg["min_conf"]]
            rec["changes"].append({
                "field": "gate", "field_th": "เกณฑ์กรองสัญญาณ",
                "from": "หลายค่าทดสอบ",
                "to": f"opp≥{bg['min_opp']:g} conf≥{bg['min_conf']:g}",
                "reason": (f"เกณฑ์นี้บวกทั้งในตัว ({bg.get('train_mean_r'):+.3f}R) "
                           f"และนอกตัว ({bg.get('test_mean_r'):+.3f}R)")})

        # Assets: subset only behind a surviving cell, and only pairs positive
        # in BOTH periods with a real sample. Anything looser is the same
        # multiple-testing trap this whole module exists to avoid.
        per = verdict.get("per_asset") or []
        total_assets = len({a.get("asset") for a in per if a.get("asset")})
        strict = [a["asset"] for a in per
                  if (a.get("train_mean_r") or 0) > 0
                  and (a.get("test_mean_r") or 0) > 0
                  and (a.get("test_n") or 0) >= 30]
        if holds and strict and 0 < len(strict) < total_assets:
            nxt["assets"] = sorted(strict)
            rec["changes"].append({
                "field": "assets", "field_th": "สินทรัพย์",
                "from": f"{total_assets} คู่", "to": f"{len(strict)} คู่",
                "reason": ("บวกทั้งสองช่วงพร้อมตัวอย่างจริง — "
                           "ยังเป็นสมมติฐาน ต้องทดสอบต่อ")})

        if nxt:
            rec["has_plan"] = True
            rec["next_config"] = nxt
        else:
            rec["note"] = ("รอบนี้ไม่มีหลักฐานพอจะปรับอะไร — ไม่มีช่องไหนรอดนอก"
                           "ตัวอย่าง เกณฑ์ไหนก็ไม่บวกนอกตัว "
                           "การปรับตอนนี้คือเดา ไม่ใช่สรุป")

        # Live suggestion: text ONLY, and only when the candidate beats
        # production out-of-sample by a margin AND is positive itself.
        # Applying it stays a manual act in Settings.
        pb = verdict.get("production_benchmark") or {}
        if (holds and pb.get("available") and pb.get("test")
                and (wf.get("test_mean_r") or 0) > 0
                and wf["test_mean_r"] - (pb["test"].get("mean_r") or 0) > 0.05):
            base = cfg.get("live_baseline") or {}
            mode_to = _nearest_mode(win["sl_mult"])
            live_changes = []
            if str(base.get("sl_distance_mode") or "") != mode_to:
                live_changes.append({
                    "field": "sl_distance_mode", "field_th": "โหมด SL",
                    "from": base.get("sl_distance_mode"), "to": mode_to})
            if abs(float(base.get("rr_target") or 0) - float(win["tp_r"])) > 1e-9:
                live_changes.append({
                    "field": "rr_target", "field_th": "เป้า RR",
                    "from": base.get("rr_target"), "to": float(win["tp_r"])})
            if abs(float(base.get("min_opportunity") or 0) - float(bg.get("min_opp", 0))) > 1e-9:
                live_changes.append({
                    "field": "min_opportunity", "field_th": "เกณฑ์ opportunity",
                    "from": base.get("min_opportunity"), "to": bg.get("min_opp")})
            if abs(float(base.get("min_confidence") or 0) - float(bg.get("min_conf", 0))) > 1e-9:
                live_changes.append({
                    "field": "min_confidence", "field_th": "เกณฑ์ confidence",
                    "from": base.get("min_confidence"), "to": bg.get("min_conf")})
            if live_changes:
                rec["live_suggestion"] = {
                    "changes": live_changes,
                    "candidate_test_r": round(wf["test_mean_r"], 4),
                    "production_test_r": round(pb["test"].get("mean_r") or 0, 4),
                    "note": ("ข้อเสนอสำหรับ setting จริง — ยังไม่เปลี่ยน "
                             "ต้องกดยืนยันเองที่หน้า Settings"),
                }
        return rec
    except Exception as exc:
        log.debug("recommendation failed: %s", exc)
        return {"has_plan": False, "changes": [], "next_config": {},
                "live_suggestion": None,
                "note": "คำนวณข้อเสนอไม่สำเร็จ — ดูตารางเอง"}
