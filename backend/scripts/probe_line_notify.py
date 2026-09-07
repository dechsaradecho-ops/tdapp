"""Live probe: NotificationService.notify() with trade_opened (critical).

Wires the real Database + LineClient exactly like app.main does, then calls
notify() for a critical type. Expected behaviour after the fix:
  1. push_line iterates ALL enabled line_users + line_targets (no user_id filter)
  2. LINE message arrives immediately
  3. queue_notification stamps a row with status="sent" (survives uuid-cast
     failure on the bogus "demo" user_id via the retry-with-NULL path)
"""
import asyncio
import logging
import sys

sys.path.insert(0, r"d:\tdapp\backend")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.integrations.line_client import LineClient  # noqa: E402
from app.services.database import Database, queue_notification  # noqa: E402
from app.services.notification_service import NotificationService  # noqa: E402


async def main() -> None:
    db = Database()
    line = LineClient()
    svc = NotificationService(db, line)

    # 1) Show what push_line will iterate (no user_id filter anymore)
    lus = db.select("line_users", filters={"notification_enabled": True})
    tgts = db.select("line_targets", filters={"notification_enabled": True})
    print(f"line_users enabled: {len(lus)} -> {[u.get('line_user_id', '')[:12] + '...' for u in lus]}")
    print(f"line_targets enabled: {len(tgts)} -> {[t.get('target_id', '')[:12] + '...' for t in tgts]}")

    # 2) Critical notify: immediate push + queue row stamped 'sent'
    ok = await svc.notify("demo", "trade_opened",
                          "[TEST] trade_opened probe — ทดสอบแจ้งเตือนเปิดไม้ (fix 2026-09-07)")
    print(f"notify() returned: {ok!r}")

    # 3) Verify the queue row landed (status should be 'sent' if push ok)
    rows = db.select("notifications", filters={"type": "trade_opened"},
                     order="created_at", desc=True, limit=3)
    print(f"notifications rows (trade_opened, newest 3): {len(rows)}")
    for r in rows:
        print(f"  status={r.get('status')!r} user_id={r.get('user_id')!r} "
              f"msg={str(r.get('message'))[:40]!r}")


if __name__ == " __main__" or __name__ == "__main__":
    asyncio.run(main())
