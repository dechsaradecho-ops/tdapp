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
from datetime import datetime, timezone
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
TP_RS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 2.5, 3.0)
MAX_BARS = (5, 10, 20)


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
              max_bars=MAX_BARS) -> dict[str, Any]:
    """Register a run and hand it to the worker. Returns immediately."""
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
        series = _fetch_series(cfg)
        if cancel.is_set():
            return _finish(db, run_id, "cancelled", cancel, t0)

        assets_ok = sorted(series)
        _patch(db, run_id, stage="replaying",
               result={"assets": assets_ok,
                       "bars": {a: len(c) for a, c in series.items()}})

        from app.engine.strategy_replay import replay
        events = replay(series, cooldown_bars=cfg["cooldown"])
        if cancel.is_set():
            return _finish(db, run_id, "cancelled", cancel, t0)

        # The caller asked for N samples; history is finite, so a run can come
        # up short. Report the real number rather than padding or pretending.
        target = int(cfg.get("target_events") or 0)
        if target and len(events) > target:
            events = _thin(events, target)

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

    Even spacing, not a head/tail cut: taking the first N would silently
    restrict the study to the oldest slice of history, which is the one way
    a sample can be made to look better without changing a single number.
    """
    n = len(events)
    if target >= n:
        return events
    step = n / float(target)
    return [events[int(i * step)] for i in range(target)]


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
    # shows a single coherent equity curve instead of 192 interleaved ones.
    # The full grid is still swept at the end.
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
# Analysis (runs after every event is labelled)
# ---------------------------------------------------------------------------
def _analyse(events: list, cfg: dict[str, Any]) -> dict[str, Any]:
    """Sweep the grid, then hold the ranking to a chronological test split.

    The split is not optional decoration. 192 cells ranked on one sample is a
    192-way race, and the winner of such a race is positive even when there is
    no edge at all — that is exactly what the first run of this study showed
    (+0.101R in-sample collapsing to -0.061R out). Anything reported without
    the out-of-sample number is a number that has not been tested.
    """
    from app.engine.triple_barrier import summarise_cell, sweep_grid

    sl_mults = cfg.get("sl_multiples") or list(SL_MULTIPLES)
    tp_rs = cfg.get("tp_rs") or list(TP_RS)
    max_bars = cfg.get("max_bars") or list(MAX_BARS)

    min_n = max(30, len(events) // 20)
    by_paid = sweep_grid(events, sl_mults, tp_rs, max_bars, min_n=min_n)
    by_worth = sweep_grid(events, sl_mults, tp_rs, max_bars, min_n=min_n,
                          rank_by_resolved=True)

    verdict: dict[str, Any] = {
        "n_events": len(events),
        "min_n": min_n,
        "assets": sorted({e.asset for e in events}),
        "bars": cfg.get("days"),
        "cooldown": cfg.get("cooldown"),
        # How many cells this ranking raced through. The UI needs the real
        # number to explain WHY the winner is suspect — a hardcoded "192"
        # would go stale the moment a preset changes and quietly keep making
        # the same argument about a different search.
        "grid_cells": len(sl_mults) * len(tp_rs) * len(max_bars),
        "top_paid": [r.as_row() for r in by_paid[:12]],
        "top_worth": [r.as_row() for r in by_worth[:12]],
    }

    ordered = sorted(events, key=lambda e: (e.bar_index, e.asset))
    cut = int(len(ordered) * 0.7)
    train, test = ordered[:cut], ordered[cut:]
    if len(train) >= 60 and len(test) >= 30:
        tr_rows = sweep_grid(train, sl_mults, tp_rs, max_bars,
                             min_n=max(20, len(train) // 20))
        if tr_rows:
            checks = []
            for r in tr_rows[:8]:
                t = summarise_cell(test, r.sl_width, r.tp_r, r.max_bars)
                d = t.as_row()
                checks.append({"sl_mult": r.sl_width, "tp_r": r.tp_r,
                               "max_bars": r.max_bars,
                               "train_mean_r": round(r.mean_r, 4),
                               "test_mean_r": d["mean_r"],
                               "test_win_rate_pct": d["win_rate_pct"],
                               "test_n": d["n"]})
            win = tr_rows[0]
            tst = summarise_cell(test, win.sl_width, win.tp_r, win.max_bars)
            td = tst.as_row()
            verdict["walk_forward"] = {
                "train_n": len(train), "test_n": len(test),
                "checks": checks,
                "winner": {"sl_mult": win.sl_width, "tp_r": win.tp_r,
                           "max_bars": win.max_bars,
                           "train_mean_r": round(win.mean_r, 4)},
                "test_mean_r": td["mean_r"],
                "test_win_rate_pct": td["win_rate_pct"],
                "holds_out": bool(td["mean_r"] > 0),
            }
    else:
        verdict["walk_forward"] = {"error": "เหตุการณ์ไม่พอสำหรับแบ่ง train/test"}

    # --- per asset, ranked on the TRAIN period only -----------------------
    best = by_paid[0] if by_paid else None
    if best and train:
        per = []
        for asset in sorted({e.asset for e in events}):
            a = [e for e in train if e.asset == asset]
            b = [e for e in test if e.asset == asset]
            ra = summarise_cell(a, best.sl_width, best.tp_r, best.max_bars)
            rb = summarise_cell(b, best.sl_width, best.tp_r, best.max_bars)
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
                                     "max_bars": best.max_bars}

    verdict["mfe_mae"] = _mfe_mae(events)
    verdict["elapsed_s"] = None       # filled in by the caller's log line
    return verdict


def _mfe_mae(events: list) -> dict[str, Any]:
    """Excursion distribution — answers "was the target ever reachable?"."""
    mfes = sorted(e.outcomes.get(k).mfe_r
                  for e in events
                  for k in [f"1.5000|2.2500|20"] if k in e.outcomes)
    maes = sorted(e.outcomes.get(k).mae_r
                  for e in events
                  for k in [f"1.5000|2.2500|20"] if k in e.outcomes)
    if not mfes:
        return {}

    def q(a, p):
        return a[min(len(a) - 1, int(len(a) * p))]

    out = {
        "mfe_p25": round(q(mfes, .25), 3), "mfe_median": round(q(mfes, .5), 3),
        "mfe_p75": round(q(mfes, .75), 3), "mfe_p95": round(q(mfes, .95), 3),
        "mfe_max": round(mfes[-1], 3),
        "mae_p25": round(q(maes, .25), 3), "mae_median": round(q(maes, .5), 3),
        "mae_p75": round(q(maes, .75), 3), "mae_min": round(maes[0], 3),
    }
    for r in (1.0, 1.5, 2.0):
        n = sum(1 for m in mfes if m >= r)
        out[f"reached_{r:g}r_pct"] = round(100.0 * n / len(mfes), 1)
    return out
