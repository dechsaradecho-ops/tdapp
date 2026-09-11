"""Worker — Log/table maintenance (every 10 min).

Fixes the two gaps found by the prod audit (2026-09-11, read-only):

  1. NO PROACTIVE RETENTION. quote_api_logs (66,151 rows / 7 days) was only
     trimmed when fetch_spot_prices happened to run or the user opened the
     Quote-Logs page, and signal_logs (1,263 rows), scheduler_runs (1,782)
     and market_analysis (~8k rows/day, no TTL at all) were never trimmed on
     a schedule. This job calls every purge with force=True so retention no
     longer depends on which page the user visits.
  2. NO ERROR WATCHDOG. 380 quote failures/week (0.57%, every one of them on
     the Yahoo subset) were visible ONLY if the user opened the Quote-Logs
     page. When the error rate over the last hour crosses
     QUOTE_ERROR_ALERT_RATE we raise a `risk_warning` notification (the same
     category portfolio_monitor uses for limit breaches) and log it — once
     per hour at most.

Registered as job id "log_maintenance" in BOTH app/main.py and
app/workers/run_all.py (every worker must be in both files).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.services import quote_log, scheduler_log, signal_log
from app.workers import market_scanner

log = logging.getLogger(__name__)

JOB_ID = "log_maintenance"

# Minimum quote calls in the window before a rate is meaningful — the scanner
# wakes every 5 min, so a single quiet hour can legitimately hold <20 calls.
QUOTE_ERROR_MIN_SAMPLE = 20
# 30% (not 0.57%): a healthy week sits near 0.5%, so this fires only on real
# feed outages, not on the normal Yahoo hiccup rate.
QUOTE_ERROR_ALERT_RATE = 0.30
ALERT_COOLDOWN_S = 3600.0
_last_alert = 0.0


def quote_error_rate(db: Any, window_min: int = 60) -> tuple[int, int, float]:
    """(total, errors, rate) for quote_api_logs rows in the last window_min.

    Rate is 0.0 when the window is empty. Never raises.
    """
    try:
        if db is None or not getattr(db, "available", False):
            return 0, 0, 0.0
        count = getattr(db, "count", None)
        if count is None:
            return 0, 0, 0.0
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(minutes=window_min)).isoformat()
        total = int(count(quote_log.TABLE, created_after=cutoff) or 0)
        ok = int(count(quote_log.TABLE, filters={"status": "success"},
                       created_after=cutoff) or 0)
        if total <= 0:
            return 0, 0, 0.0
        return total, total - ok, (total - ok) / total
    except Exception as exc:
        log.debug("quote error rate probe failed: %s", exc)
        return 0, 0, 0.0


async def _alert_quote_errors(db: Any, notifier: Any) -> str:
    """Raise one risk_warning when the hourly quote error rate is high."""
    global _last_alert
    if notifier is None:
        return ""
    if time.monotonic() - _last_alert < ALERT_COOLDOWN_S:
        return ""
    total, errors, rate = quote_error_rate(db)
    if total < QUOTE_ERROR_MIN_SAMPLE or rate < QUOTE_ERROR_ALERT_RATE:
        return ""
    _last_alert = time.monotonic()
    # execution.DEFAULT_USER mirrors portfolio_monitor's limit-breach alert —
    # the pseudo-user is delivered straight to LINE by notification_worker.
    from app.services import execution

    pct = round(rate * 100, 1)
    message = (f"⚠️ ดึงราคาล้มเหลวผิดปกติ: {errors}/{total} ครั้ง ({pct}%) "
               f"ใน 1 ชั่วโมงที่ผ่านมา — ตรวจหน้า Quote Logs ก่อนตัดสินใจเทรด")
    try:
        await notifier.notify(execution.DEFAULT_USER, "risk_warning", message)
    except Exception as exc:
        log.debug("quote error alert failed: %s", exc)
        return ""
    log.warning("quote error rate alert: %d/%d (%.1f%%)", errors, total, pct)
    return f"alert {errors}/{total} ({pct}%)"


async def run_once(db: Any, notifier: Any = None) -> dict[str, Any]:
    """Purge every TTL table (forced) + watch the quote error rate.

    Returns a summary dict; each purge is individually guarded so one broken
    table can never stop the others or the tick itself.
    """
    deleted: dict[str, int] = {}
    for name, fn in (("quote_api_logs", lambda: quote_log.purge_old_logs(
                         db, force=True)),
                     ("signal_logs", lambda: signal_log.purge_old_logs(
                         db, force=True)),
                     ("scheduler_runs", lambda: scheduler_log.purge_old_logs(
                         db, force=True)),
                     ("market_analysis", lambda: market_scanner
                         .purge_old_market_analysis(db, force=True))):
        try:
            deleted[name] = int(fn() or 0)
        except Exception as exc:
            log.debug("%s purge failed: %s", name, exc)
            deleted[name] = 0
    total, errors, rate = quote_error_rate(db)
    alert = await _alert_quote_errors(db, notifier)
    return {
        "purged": deleted,
        "quote_calls_1h": total,
        "quote_errors_1h": errors,
        "quote_error_rate": round(rate, 4),
        **({"alert": alert} if alert else {}),
    }
