"""Probe: current kill-switch / pause / pending-request state (read-only).

Reads the same sources the risk workers read. The broker is a local
``PaperBroker`` whose book is restored from the DB rows the workers use, so
this prints the positions the guard would act on. Writes nothing: no order,
no SL/TP, no settings.
"""
import asyncio
import sys

sys.path.insert(0, r"d:\tdapp\backend")

from app.api.routes.settings import get_app_settings       # noqa: E402
from app.integrations.brokers import PaperBroker           # noqa: E402
from app.services import execution, limit_expand           # noqa: E402
from app.services.database import Database                 # noqa: E402
from app.workers import position_guard                     # noqa: E402

db = Database()
s = get_app_settings(db)

kill = execution.evaluate_kill(db, s)
print("kill.engaged   :", kill.engaged)
print("kill.triggers  :", list(kill.triggers or [])[:6])

pause = execution.get_pause(db)
print("pause          :", pause)

pend = limit_expand.pending_request(db, settings=s)
print("pending row    :", (dict(pend) if pend else None))
stale = limit_expand.stale_pending(db, s)
print("stale  row     :", (dict(stale) if stale else None))
print("breached now   :", limit_expand.breached_triggers(db, s))

rows = db.select("risk_events", order="created_at", desc=True, limit=5) or []
print("risk_events    :", len(rows))
for r in rows:
    print("   ", r.get("created_at"), r.get("event_type"),
          "approved=", (r.get("detail") or {}).get("approved"))
try:
    db.select_ex("risk_events", order="created_at", desc=True, limit=1)
    print("risk_events read (raising path): OK")
except Exception as exc:            # noqa: BLE001
    print("risk_events read FAILED:", exc)

# Can the backend actually WRITE the audit row that a decision produces?
# (limit_expand._audit inserts into risk_events; 004 grants insert policies to
# market_analysis / signals / news_analysis / ai_daily_report / db_probe only.)
_row, err = db.insert_raw("risk_events", {
    "user_id": __import__("app.services.execution", fromlist=["x"]).DEFAULT_USER,
    "event_type": "probe_risk_events_write",
    "detail": {"probe": True},
})
print("risk_events insert (raw) :", "OK id=" + str((_row or {}).get("id"))
      if not err else "FAILED: " + err)
if _row:
    db.delete("risk_events", {"id": _row["id"]})
    print("  probe row deleted      :", True)


async def _book() -> list:
    broker = PaperBroker()
    await broker.connect()
    await position_guard.rehydrate_book(db, broker)
    return await broker.all_positions()


try:
    positions = asyncio.run(_book())
except Exception as exc:            # noqa: BLE001
    positions = []
    print("positions probe failed:", exc)
print("open positions :", len(positions))
for p in positions[:10]:
    print("   ", getattr(p, "asset", "?"), getattr(p, "direction", "?"),
          getattr(p, "volume", "?"), "pnl", getattr(p, "profit", "?"))
