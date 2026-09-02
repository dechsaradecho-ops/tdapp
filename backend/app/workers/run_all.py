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
from app.workers import market_scanner, news_analysis, notification_worker, portfolio_monitor

log = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    db = Database()
    line = LineClient()
    broker = PaperBroker()
    await broker.connect()
    notifier = NotificationService(db, line)

    scheduler = AsyncIOScheduler()

    scheduler.add_job(lambda: asyncio.create_task(_safe(market_scanner.scan_once(db))),
                      "interval", minutes=5, id="market_scanner", max_instances=1)
    scheduler.add_job(lambda: asyncio.create_task(_safe(news_analysis.analyze_once(db))),
                      "interval", minutes=15, id="news_analysis", max_instances=1)
    scheduler.add_job(lambda: asyncio.create_task(_safe(
        asyncio.to_thread(portfolio_monitor.monitor_once, db, broker, notifier))),
        "interval", minutes=1, id="portfolio_monitor", max_instances=1)
    scheduler.add_job(lambda: asyncio.create_task(_safe(notification_worker.dispatch_pending(db, notifier))),
                      "interval", minutes=1, id="notifications", max_instances=1)

    scheduler.start()
    log.info("Workers started: scanner(5m) news(15m) monitor(1m) notify(1m)")

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("Worker job failed")


if __name__ == "__main__":
    asyncio.run(main())
