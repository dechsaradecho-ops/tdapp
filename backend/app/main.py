"""FastAPI application entrypoint."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (ai, auth, chat, goal, market, portfolio, risk, settings as settings_routes, signals,
                            system, trading, webhook)
from app.core.config import get_settings
from app.core.logging import setup_logging
from app.integrations.ai_provider import get_ai_provider
from app.services.database import Database
from app.services.notification_service import NotificationService
from app.services import quote_log
from app.services import scheduler_log
from app.integrations.line_client import LineClient
from app.integrations.brokers import PaperBroker
from app.workers import (auto_trader, calendar_sync, daily_digest,
                         market_scanner, news_analysis, notification_worker,
                         portfolio_monitor, position_guard)

log = logging.getLogger(__name__)

# --- scheduler observability ------------------------------------------------
# A tick can end WITHOUT a scheduler_runs row for reasons that are all SILENT
# in APScheduler 3.10 with its defaults:
#   1. max_instances reached → submit_job raises MaxInstancesReachedError
#      inside Scheduler._process_jobs; it only logs a warning, no row.
#   2. misfire_grace_time defaults to ONE second → the executor's job runner
#      logs "Run time of job ... was missed by ..." and skips the tick, again
#      without a row, while job.next_run_time still advances (so /health's
#      job_next looked perfectly healthy).
#   3. the coroutine is cancelled (loop shutdown / wait_for) → CancelledError
#      is a BaseException, escaped the old ``except Exception`` and skipped
#      the log call entirely.
# Prod 2026-09-11: position_guard produced ZERO rows all day while SL moves
# really happened and /guard-now finished in 9s — the Guard tab was "empty"
# for days of invisible skips. The fix: never depend on APScheduler's silent
# skipping. Jobs are registered with max_instances=2 + misfire_grace_time, and
# a plain in-process lock turns every overlap/cancel into a VISIBLE row.
_JOB_STATS: dict[str, dict[str, Any]] = {}
_JOB_IN_FLIGHT: set[str] = set()
_SKIP_DETAIL = ("checked=0, closed=0, moved_sl=0, "
                "partial_closed=0, smart_closed=0, smart_partials=0, "
                "smart_skipped=0, emergency_closed=0, skipped_prev_running=1")


def _stats(job_id: str) -> dict[str, Any]:
    return _JOB_STATS.setdefault(job_id, {
        "ticks": 0, "ok": 0, "error": 0, "skipped": 0,
        "last_started": None, "last_ms": None, "running_since": None,
        "last_status": "", "last_detail": "", "last_error": "",
    })


def job_stats() -> dict[str, dict[str, Any]]:
    """Per-job heartbeat for /health — proves a job is being INVOKED at all.

    scheduler_runs can only show finished ticks; this shows the scheduler's
    own view (ticks/ok/error/skipped + running_since) so a stuck or
    never-submitted job is distinguishable without Render log access.
    """
    import time as _t
    out: dict[str, dict[str, Any]] = {}
    for k, v in _JOB_STATS.items():
        d = dict(v)
        rs = d.get("running_since")
        d["running_s"] = round(_t.monotonic() - rs, 1) if rs else None
        out[k] = d
    return out


async def _safe_job(coro, db=None, job_id: str = "",
                    timeout_s: float = 0.0) -> None:
    """Run a scheduled coroutine, logging failures instead of crashing the app.

    Each tick writes EXACTLY ONE row to scheduler_runs (migration 030) so the
    Logs page can prove the scheduler is alive — status ok/error, wall time in
    ms, and a compact summary of the job's return value. Fail-soft: the run
    log must never break the job itself.

    `timeout_s` (>0) is a watchdog on the whole tick: a job that overruns its
    interval must still end and free its slot.

    A tick is never silently dropped: if the previous tick of the same job is
    still running we log a ``skipped_prev_running=1`` row instead of letting
    APScheduler skip it invisibly.
    """
    import time as _time
    import traceback as _tb
    st = _stats(job_id) if job_id else None
    if job_id and job_id in _JOB_IN_FLIGHT:
        age = 0
        if st is not None and st.get("running_since"):
            age = int(_time.monotonic() - st["running_since"])
        log.warning("Worker job %s skipped — previous tick still running (%ss)",
                    job_id, age)
        try:
            coro.close()                      # never await → avoid a warning
        except Exception:
            pass
        if st is not None:
            st["skipped"] += 1
            st["last_status"] = "ok"
            st["last_detail"] = _SKIP_DETAIL
            st["last_error"] = ""
        if db is not None and job_id:
            try:
                scheduler_log.log_run(db=db, job_id=job_id, status="ok",
                                      duration_ms=0, detail=_SKIP_DETAIL)
            except Exception:
                log.debug("scheduler skip log failed: %s", job_id)
        return

    started = _time.monotonic()
    if job_id:
        _JOB_IN_FLIGHT.add(job_id)
    if st is not None:
        st["ticks"] += 1
        st["last_started"] = _time.time()
        st["running_since"] = started
    err: str | None = None
    result = None
    done = False
    try:
        if timeout_s and timeout_s > 0:
            result = await asyncio.wait_for(coro, timeout=timeout_s)
        else:
            result = await coro
        done = True
    except asyncio.TimeoutError:
        # Distinguish OUR watchdog from an inner timeout the job raised: only
        # report a watchdog trip when we actually ran out the clock.
        if timeout_s and _time.monotonic() - started >= timeout_s - 0.05:
            log.error("Worker job %s exceeded %.0fs watchdog",
                      job_id or "?", timeout_s)
            err = f"watchdog timeout after {timeout_s:.0f}s"
        else:
            log.exception("Worker job failed")
            err = _tb.format_exc(limit=3)[-500:]
    except Exception:
        log.exception("Worker job failed")
        err = _tb.format_exc(limit=3)[-500:]
    finally:
        # Runs for BaseException too (CancelledError on shutdown): a missing
        # row is indistinguishable from "the scheduler never fired this job",
        # which is exactly what made the empty Guard tab so hard to explain.
        if job_id:
            _JOB_IN_FLIGHT.discard(job_id)
        if st is not None:
            st["running_since"] = None
        ms = int((_time.monotonic() - started) * 1000)
        if err is None and not done:
            err = "tick cancelled before completion"
        if st is not None:
            st["last_ms"] = ms
            if err is None:
                st["ok"] += 1
                st["last_status"] = "ok"
                st["last_detail"] = scheduler_log._summarize(result)
                st["last_error"] = ""
            else:
                st["error"] += 1
                st["last_status"] = "error"
                st["last_detail"] = ""
                st["last_error"] = err
        if db is not None and job_id:
            try:
                if err is None:
                    scheduler_log.log_run(
                        db=db, job_id=job_id, status="ok", duration_ms=ms,
                        detail=scheduler_log._summarize(result))
                else:
                    scheduler_log.log_run(
                        db=db, job_id=job_id, status="error", duration_ms=ms,
                        error=err)
            except Exception:
                log.debug("scheduler run log failed: %s", job_id)


def _safe(factory, db=None, job_id: str = "", timeout_s: float = 0.0):
    """Wrap a coroutine factory in a coroutine function APScheduler can await.

    GOTCHA (prod 2026-09-03): scheduling a plain sync lambda that calls
    asyncio.create_task() silently never runs — AsyncIOScheduler executes sync
    callables in an executor thread with NO running event loop, so every tick
    raised "RuntimeError: no running event loop" before the task was created
    (all six workers quietly did nothing; scanner wrote nothing, position
    guard never marked/closed). A coroutine function is awaited directly on
    the scheduler's event loop and works.
    """
    async def _job() -> None:
        await _safe_job(factory(), db, job_id, timeout_s)
    return _job


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    app.state.db = Database()
    app.state.line = LineClient()
    app.state.broker = PaperBroker()
    await app.state.broker.connect()
    # Quote API call log (7-day auto-expiry) writes through this module-level
    # reference — quotes.py logs every external price fetch via quote_log.
    quote_log.set_db(app.state.db)

    # The PaperBroker book is in-memory — restore any DB rows still marked
    # "open" so position_guard can enforce their SL/TP again after a restart
    # (otherwise positions survive restarts in the DB but lose enforcement).
    try:
        await position_guard.rehydrate_book(app.state.db, app.state.broker)
    except Exception:
        log.exception("broker book rehydrate failed (continuing)")

    # Background workers run inside this single web service when
    # ENABLE_WORKERS=1 (set on tdapp-api only — never on more than one
    # instance, or jobs will run duplicated).
    scheduler: AsyncIOScheduler | None = None
    if get_settings().enable_workers:
        scheduler = AsyncIOScheduler()
        db = app.state.db
        notifier = NotificationService(db, app.state.line)

        # max_instances=2 (not 1) + coalesce + a generous misfire window: the
        # in-process lock in _safe_job now records an explicit "skipped" row,
        # whereas APScheduler's own max_instances=1 / misfire_grace_time=1
        # dropped overdue ticks silently (job_next still advanced, so /health
        # looked healthy while nothing ran). coalesce avoids a burst of
        # catch-up ticks after the loop was busy.
        common = {"misfire_grace_time": 120, "coalesce": True,
                  "max_instances": 2}
        scheduler.add_job(_safe(lambda: market_scanner.scan_once(db),
                                   db, "market_scanner"),
                          "interval", minutes=5, id="market_scanner", **common)
        scheduler.add_job(_safe(lambda: news_analysis.analyze_once(db),
                                   db, "news_analysis"),
                          "interval", minutes=15, id="news_analysis", **common)
        scheduler.add_job(_safe(lambda: asyncio.to_thread(
            portfolio_monitor.monitor_once, db, app.state.broker, notifier),
            db, "portfolio_monitor"),
            "interval", minutes=1, id="portfolio_monitor", **common)
        scheduler.add_job(_safe(lambda:
            notification_worker.dispatch_pending(db, notifier),
            db, "notifications"),
            "interval", minutes=1, id="notifications", **common)
        scheduler.add_job(_safe(lambda:
            auto_trader.trade_once(db, app.state.broker, notifier),
            db, "auto_trader"),
            "interval", minutes=1, id="auto_trader", **common)
        scheduler.add_job(_safe(lambda:
            position_guard.guard_once(db, app.state.broker, notifier),
            db, "position_guard", timeout_s=50),
            "interval", minutes=1, id="position_guard", **common)
        scheduler.add_job(_safe(lambda: calendar_sync.sync_once(db),
                                   db, "calendar_sync"),
                          "interval", hours=6, id="calendar_sync", **common)
        scheduler.add_job(_safe(lambda:
            daily_digest.send_digest_once(db, notifier),
            db, "daily_digest"),
            "interval", minutes=60, id="daily_digest", **common)
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("In-app workers ENABLED: scanner(5m) news(15m) monitor(1m) "
                 "notify(1m) auto_trader(1m) position_guard(1m) "
                 "calendar_sync(6h) daily_digest(1h, idempotent)")
    else:
        app.state.scheduler = None
        log.info("In-app workers disabled (ENABLE_WORKERS not set)")

    yield

    if app.state.scheduler is not None:
        app.state.scheduler.shutdown(wait=False)


settings = get_settings()

app = FastAPI(
    title="AI Wealth & Trading Advisor API",
    version="0.1.0",
    description=(
        "Multi-asset trading advisory API. Provides goal feasibility assessment, "
        "market regime analysis, opportunity scoring, portfolio recommendation and "
        "risk management. Probabilistic only — never guarantees profit."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# PIN gate — every /api/* request needs a valid session token, except the
# whitelist below. The token comes from POST /api/auth/login (6-digit PIN).
# Fail-CLOSED: if the check errors out, the request is rejected.
#
# Registered BEFORE CORSMiddleware (last-added = outermost) so CORS wraps the
# gate's 401s with Access-Control-Allow-Origin. Without this the browser
# blocks the 401 entirely and the frontend can never react to it (the PIN pad
# and re-lock flow silently break).
# ---------------------------------------------------------------------------
_PIN_EXEMPT_PATHS = {
    "/ping", "/health",
    "/api/auth/status", "/api/auth/login", "/api/auth/set-pin",
    # LINE servers call the webhook — they have no PIN session. The webhook
    # verifies its own HMAC signature (X-Line-Signature) instead.
    "/api/line/webhook",
}


@app.middleware("http")
async def pin_gate(request, call_next):
    from app.services import pin_auth
    path = request.url.path
    if request.method == "OPTIONS":
        return await call_next(request)   # CORS preflight has no auth header
    if path in _PIN_EXEMPT_PATHS or not path.startswith("/api/"):
        return await call_next(request)
    # Gate is enforced only once a PIN exists (bootstrap stays open until
    # the owner sets their PIN from the dashboard).
    if not pin_auth.gate_active(request.app.state.db):
        return await call_next(request)
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else ""
    if not pin_auth.session_valid(token):
        return JSONResponse(status_code=401, content={"detail": "unauthorized"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.frontend_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(goal.router, prefix="/api/goal", tags=["goal"])
app.include_router(market.router, prefix="/api/market", tags=["market"])
app.include_router(portfolio.router, prefix="/api/portfolio", tags=["portfolio"])
app.include_router(risk.router, prefix="/api/risk", tags=["risk"])
app.include_router(signals.router, prefix="/api/signals", tags=["signals"])
app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
app.include_router(ai.router, prefix="/api/ai", tags=["ai"])
app.include_router(webhook.router, prefix="/api/line", tags=["line"])
app.include_router(system.router, prefix="/api/system", tags=["system"])
app.include_router(trading.router, prefix="/api/trading", tags=["trading"])
app.include_router(settings_routes.router, prefix="/api/settings", tags=["settings"])
app.include_router(auth.router, prefix="/api/auth", tags=["auth"])


@app.get("/ping", tags=["system"])
async def ping() -> dict:
    """Ultra-light keepalive for external cron/uptime pingers.

    Deliberately touches nothing (no DB client, no AI provider, no scheduler
    inspection) so it stays fast even on a cold or degraded instance.
    Point a cron-job at this every ~10 min to prevent Render spin-down.
    """
    return {"pong": True}


@app.get("/health", tags=["system"])
async def health() -> dict:
    """Liveness + integration diagnostics (used by scripts/check_live.py)."""
    db: Database = app.state.db
    provider = get_ai_provider()
    scheduler = getattr(app.state, "scheduler", None)
    # Scheduler forensics: a job whose tick never returns holds its
    # max_instances=1 slot forever and is then SKIPPED on every later tick,
    # so it never completes-and-logs (prod 2026-09-11: position_guard wrote
    # zero scheduler_runs rows while the table/RLS were perfectly fine).
    # `job_running` exposes the executor's live instance counts, `job_next`
    # the next fire time — together they name the stuck job from /health
    # without needing Render log access. Private attrs are read defensively.
    job_next: dict[str, Any] = {}
    job_running: dict[str, int] = {}
    if scheduler is not None:
        try:
            job_next = {j.id: (j.next_run_time.isoformat()
                               if j.next_run_time else None)
                        for j in scheduler.get_jobs()}
            executor = (scheduler._executors or {}).get("default")
            job_running = dict(getattr(executor, "_instances", {}) or {})
        except Exception:                    # diagnostics must never 500
            log.debug("health scheduler probe failed", exc_info=True)
    return {
        "status": "ok",
        "platform": "AI Wealth & Trading Advisor",
        "deployment": "render",
        # Deploy marker: Render injects RENDER_GIT_COMMIT into every build, so
        # `curl /health | jq .commit` proves WHICH commit is actually live —
        # never compare local vs prod hashes by hand (see tdapp-deploy memory).
        "commit": (os.environ.get("RENDER_GIT_COMMIT") or "local")[:7],
        "workers": ("running" if scheduler is not None
                    else ("enabled" if get_settings().enable_workers else "disabled")),
        "jobs": [j.id for j in scheduler.get_jobs()] if scheduler is not None else [],
        "job_next": job_next,
        "job_running": job_running,
        # Invocation heartbeat (ticks/ok/error/skipped + running_s): unlike
        # scheduler_runs this proves the scheduler actually CALLED the job,
        # so a silently skipped tick is visible even if no row was written.
        "job_stats": job_stats(),
        "db": "ok" if db.available else "unavailable",
        "db_detail": (db.init_error or "connected"),
        "ai": provider.name,
    }
