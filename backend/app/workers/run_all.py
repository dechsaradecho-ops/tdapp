"""Run all background workers on schedule.

    python -m app.workers.run_all

Intervals: Market Scanner 5 min | News Analysis 15 min |
           Portfolio Monitor 1 min | Notification dispatch 1 min.
"""
from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.core.logging import setup_logging
from app.integrations.brokers import PaperBroker
from app.integrations.line_client import LineClient
from app.services.database import Database
from app.services.notification_service import NotificationService
from app.services import scheduler_log
from app.workers import (auto_trader, calendar_sync, daily_digest,
                         log_maintenance, market_scanner, news_analysis,
                         notification_worker, portfolio_monitor,
                         position_guard)

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    db = Database()
    line = LineClient()
    broker = PaperBroker()
    await broker.connect()
    notifier = NotificationService(db, line)

    scheduler = AsyncIOScheduler()

    # misfire_grace_time: APScheduler 3.10 defaults to ONE second, so a tick
    # whose fire time slipped (busy event loop) was discarded with only a log
    # line and NO scheduler_runs row — the tick looked like it never existed.
    # coalesce avoids a burst of catch-up runs. NOTE: this standalone runner
    # keeps max_instances=1 (it has no in-flight lock like app.main._safe_job,
    # which records an explicit "skipped" row instead of dropping the tick).
    common = {"misfire_grace_time": 120, "coalesce": True,
              "max_instances": 1}
    scheduler.add_job(_safe(lambda: market_scanner.scan_once(db),
                            db, "market_scanner"),
                      "interval", minutes=5, id="market_scanner", **common)
    scheduler.add_job(_safe(lambda: news_analysis.analyze_once(db),
                            db, "news_analysis"),
                      "interval", minutes=15, id="news_analysis", **common)
    scheduler.add_job(_safe(lambda:
        asyncio.to_thread(portfolio_monitor.monitor_once, db, broker, notifier),
        db, "portfolio_monitor"),
        "interval", minutes=1, id="portfolio_monitor", **common)
    scheduler.add_job(_safe(lambda: notification_worker.dispatch_pending(db, notifier),
                            db, "notifications"),
                      "interval", minutes=1, id="notifications", **common)
    scheduler.add_job(_safe(lambda: auto_trader.trade_once(db, broker, notifier),
                            db, "auto_trader"),
                      "interval", minutes=1, id="auto_trader", **common)
    scheduler.add_job(_safe(lambda: position_guard.guard_once(db, broker, notifier),
                            db, "position_guard", timeout_s=50),
                      "interval", minutes=1, id="position_guard", **common)
    scheduler.add_job(_safe(lambda: calendar_sync.sync_once(db),
                            db, "calendar_sync"),
                      "interval", hours=6, id="calendar_sync", **common)
    scheduler.add_job(_safe(lambda:
        daily_digest.send_digest_once(db, notifier),
        db, "daily_digest"),
        "interval", minutes=60, id="daily_digest", **common)
    # Retention + quote-error watchdog — must stay in sync with app/main.py.
    scheduler.add_job(_safe(lambda: log_maintenance.run_once(db, notifier),
                            db, "log_maintenance"),
                      "interval", minutes=10, id="log_maintenance", **common)

    scheduler.start()
    log.info("Workers started: scanner(5m) news(15m) monitor(1m) notify(1m) "
             "auto_trader(1m) position_guard(1m) calendar_sync(6h) "
             "daily_digest(1h, idempotent) log_maintenance(10m)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


def _safe(factory, db=None, job_id: str = "", timeout_s: float = 0.0):
    """Coroutine-function wrapper APScheduler can await on the event loop.

    A plain sync lambda calling asyncio.create_task() never runs under
    AsyncIOScheduler — sync callables execute in an executor thread with no
    running event loop, so every tick failed before the task was created.

    Each tick also writes ONE scheduler_runs row (migration 030) — the
    standalone runner mirrors main.py so both paths stay observable.

    `timeout_s` (>0) is a watchdog on the whole tick: APScheduler runs every
    1-min job with max_instances=1, so a job that overruns its interval is
    SKIPPED forever after and never logs a row (prod 2026-09-11:
    position_guard ~55s/cycle → no rows). Bounding wall time guarantees the
    tick ends, logs, and frees the slot.
    """
    import time as _time
    import traceback as _tb

    async def _job() -> None:
        started = _time.monotonic()
        err: str | None = None
        result = None
        try:
            if timeout_s and timeout_s > 0:
                result = await asyncio.wait_for(factory(), timeout=timeout_s)
            else:
                result = await factory()
        except asyncio.TimeoutError:
            # Only OUR watchdog, not an inner timeout raised by the job.
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

        if db is None or not job_id:
            return
        try:
            if err is None:
                scheduler_log.log_run(
                    db=db, job_id=job_id, status="ok",
                    duration_ms=int((_time.monotonic() - started) * 1000),
                    detail=scheduler_log._summarize(result))
            else:
                scheduler_log.log_run(
                    db=db, job_id=job_id, status="error",
                    duration_ms=int((_time.monotonic() - started) * 1000),
                    error=err)
        except Exception:
            log.debug("scheduler run log failed: %s", job_id)

    return _job


if __name__ == "__main__":
    asyncio.run(main())
