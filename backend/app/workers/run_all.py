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
from app.workers import auto_trader, market_scanner, news_analysis, notification_worker, portfolio_monitor, position_guard

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    db = Database()
    line = LineClient()
    broker = PaperBroker()
    await broker.connect()
    notifier = NotificationService(db, line)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(_safe(lambda: market_scanner.scan_once(db)),
                      "interval", minutes=5, id="market_scanner", max_instances=1)
    scheduler.add_job(_safe(lambda: news_analysis.analyze_once(db)),
                      "interval", minutes=15, id="news_analysis", max_instances=1)
    scheduler.add_job(_safe(lambda:
        asyncio.to_thread(portfolio_monitor.monitor_once, db, broker, notifier)),
        "interval", minutes=1, id="portfolio_monitor", max_instances=1)
    scheduler.add_job(_safe(lambda: notification_worker.dispatch_pending(db, notifier)),
                      "interval", minutes=1, id="notifications", max_instances=1)
    scheduler.add_job(_safe(lambda: auto_trader.trade_once(db, broker, notifier)),
                      "interval", minutes=1, id="auto_trader", max_instances=1)
    scheduler.add_job(_safe(lambda: position_guard.guard_once(db, broker, notifier)),
                      "interval", minutes=1, id="position_guard", max_instances=1)

    scheduler.start()
    log.info("Workers started: scanner(5m) news(15m) monitor(1m) notify(1m) "
             "auto_trader(1m) position_guard(1m)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


def _safe(factory):
    """Coroutine-function wrapper APScheduler can await on the event loop.

    A plain sync lambda calling asyncio.create_task() never runs under
    AsyncIOScheduler — sync callables execute in an executor thread with no
    running event loop, so every tick failed before the task was created.
    """
    async def _job() -> None:
        try:
            await factory()
        except Exception:
            log.exception("Worker job failed")
    return _job


if __name__ == "__main__":
    asyncio.run(main())
