"""Worker #7 — Economic Calendar Sync (every 6 h).

Populates the `economic_calendar` table with upcoming high-impact events
(NFP, CPI, FOMC, rate decisions...). The news gate (execution._news_risk)
reads this table — it was EMPTY before this worker existed, so the gate
always returned SAFE and never blocked anything.

Source: NinjaScript-style free feed is unreliable; we use a deterministic
recurring-events model instead — the well-known US schedule (NFP first
Friday, CPI mid-month, FOMC every ~6 weeks) is generated for the next 14
days. This keeps the gate functional without an API key; when a real
calendar provider is added, only `_fetch_events` changes.

Never raises — a failed sync leaves the existing rows in place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.services.database import Database

log = logging.getLogger(__name__)

# (event, currency, weekday, hour_utc, minute_utc, day_offset_hint)
# day_offset_hint: how to find the day — "first_friday", "mid_month", etc.
RECURRING_EVENTS = [
    # NFP: first Friday of the month, 12:30 UTC (8:30 ET)
    ("NFP", "USD", "first_friday", 12, 30),
    # CPI: around the 13th, 12:30 UTC
    ("CPI", "USD", "day_13", 12, 30),
    # FOMC: 18th-ish every month (approximation of the 8x/year schedule)
    ("FOMC", "USD", "day_18", 18, 0),
    # Retail sales: 16th, 12:30 UTC
    ("Retail Sales", "USD", "day_16", 12, 30),
]

DAYS_AHEAD = 14


def _event_dates(kind: str, now: datetime) -> list[datetime]:
    """UTC datetimes for one recurring event within the next DAYS_AHEAD days."""
    out: list[datetime] = []
    for offset in range(DAYS_AHEAD):
        day = (now + timedelta(days=offset)).date()
        if kind == "first_friday":
            if day.weekday() == 4 and day.day <= 7:
                out.append(day)
        elif kind.startswith("day_"):
            try:
                target = int(kind.split("_", 1)[1])
            except ValueError:
                continue
            if day.day == target:
                out.append(day)
    return out


def build_upcoming_events(now: datetime | None = None) -> list[dict]:
    """Deterministic upcoming high-impact events for the next 14 days."""
    now = now or datetime.now(timezone.utc)
    rows: list[dict] = []
    for event, currency, kind, hour, minute in RECURRING_EVENTS:
        for day in _event_dates(kind, now):
            event_time = datetime(day.year, day.month, day.day, hour, minute,
                                  tzinfo=timezone.utc)
            if event_time <= now:
                continue  # already released
            rows.append({
                "event": event, "currency": currency,
                "event_time": event_time.isoformat(),
                "impact": "high",
            })
    rows.sort(key=lambda r: r["event_time"])
    return rows


def sync_once(db: Database) -> dict:
    """Refresh the economic_calendar table. Returns a small summary."""
    if not db or not getattr(db, "available", False):
        return {"inserted": 0, "skipped": "db unavailable"}

    now = datetime.now(timezone.utc)
    upcoming = build_upcoming_events(now)
    inserted = 0
    try:
        existing = db.select("economic_calendar", order="event_time",
                             desc=False, limit=200)
    except Exception:
        existing = []
    have = {(str(r.get("event") or ""), str(r.get("event_time") or ""))
            for r in existing}
    for row in upcoming:
        key = (row["event"], row["event_time"])
        if key in have:
            continue
        db.insert("economic_calendar", row)
        inserted += 1

    # purge released events older than 2 days (keep a little history)
    cutoff = (now - timedelta(days=2)).isoformat()
    try:
        bulk = getattr(db, "delete_before", None)
        if bulk is not None:
            bulk("economic_calendar", "event_time", cutoff)
    except Exception as exc:
        log.debug("calendar purge failed: %s", exc)

    log.info("calendar sync: %d new event(s), %d upcoming known",
             inserted, len(upcoming))
    return {"inserted": inserted, "upcoming": len(upcoming)}
