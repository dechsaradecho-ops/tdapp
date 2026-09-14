"""Live probe: ยิง LINE จริง — owner-confirmation prompt (limit expand).

Verifies the 2026-09-14 owner-confirmed flow on the REAL LINE channel:
  1. which enabled targets the prompt reaches (line_users + line_targets)
  2. the confirmation window that is actually quoted
     (Settings → ``kill_expand_ttl_min`` via ``limit_expand.ttl_minutes``)
  3. the exact message body + Approve/Reject quick replies (dd_ok / dd_no)
  4. per-target LINE API result (HTTP error text when a push fails)

NOTHING is widened by this probe: no ``kill_expand_requests`` row is created,
no decision is applied, no settings/limit column is written. It only sends the
message the portfolio monitor would push on a real breach.

Usage:
    d:\\tdapp\\.venv\\Scripts\\python.exe scripts\\probe_line_limit_expand.py
    d:\\tdapp\\.venv\\Scripts\\python.exe scripts\\probe_line_limit_expand.py --send
    (default = dry run: prints the message, sends nothing)
"""
from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, r"d:\tdapp\backend")
BASE = r"d:\tdapp\backend"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.api.routes.settings import get_app_settings           # noqa: E402
from app.integrations.line_client import (build_limit_expand_prompt,  # noqa: E402
                                          LineClient)
from app.services import execution, limit_expand                # noqa: E402
from app.services.database import Database                      # noqa: E402
from app.services.execution import DEFAULT_USER                 # noqa: E402

SEND = "--send" in sys.argv

# Fallback rows so the message body can be checked even when nothing is
# currently breached (the numbers are marked DEMO in the header line).
DEMO_TRIGGERS = [
    {"trigger": "drawdown", "field": "max_drawdown_pct", "label": "Drawdown",
     "value": 10.06, "limit": 10.0, "new_limit": 15.0},
    {"trigger": "daily", "field": "kill_daily_loss_pct", "label": "Daily loss",
     "value": 2.41, "limit": 2.0, "new_limit": 7.0},
]


async def main() -> None:
    db = Database()
    line = LineClient()

    users = list(db.select("line_users", filters={"notification_enabled": True}) or [])
    targets = list(db.select("line_targets", filters={"notification_enabled": True}) or [])
    print(f"token set      : {bool(line.token)}")
    print(f"line_users     : {len(users)} -> {[str(u.get('line_user_id'))[:14] + '...' for u in users]}")
    print(f"line_targets   : {len(targets)} -> {[str(t.get('target_id'))[:14] + '...' for t in targets]}")

    s = get_app_settings(db)
    ttl = limit_expand.ttl_minutes(s)
    print(f"ttl from DB    : {getattr(s, 'kill_expand_ttl_min', None)!r} -> ttl_minutes = {ttl:.0f} นาที")
    print(f"036 applied    : {limit_expand.table_ready(db)}")
    print(f"limit_expand ∈ CRITICAL_TYPES (immediate push): "
          f"{'limit_expand' in __import__('app.services.notification_service', fromlist=['x']).CRITICAL_TYPES}")

    real = limit_expand.breached_triggers(db, s)
    triggers = real or DEMO_TRIGGERS
    head = "[ทดสอบระบบ] คำขอตัวอย่าง (ไม่มีการขยายลิมิตจริง)" if not real \
        else "[ทดสอบระบบ] ตัวเลข breach จริง ณ ปัจจุบัน — ยังไม่มีการขยายลิมิต"

    text = "\n".join([head, "", build_limit_expand_prompt(triggers, ttl_min=ttl)])
    qr = limit_expand.quick_reply_items()

    print("\n--- MESSAGE ---")
    print(text)
    print("--- QUICK REPLY ---")
    for item in qr:
        print(" ", item["action"]["label"], "->", item["action"]["data"])
    print("--- END ---\n")

    if not SEND:
        print("dry run — ยังไม่ยิงจริง (ใส่ --send เพื่อยิง LINE จริง)")
        return

    dests = [("user", u["line_user_id"]) for u in users] + \
            [(t.get("target_type", "group"), t["target_id"]) for t in targets]
    if not dests:
        print("ไม่มีปลายทางที่เปิดแจ้งเตือน — เพิ่ม groupId/line_user ก่อน")
        return

    ok_all = True
    for kind, dest in dests:
        ok, err = await line.push_ex(dest, text, quick_reply=qr)
        ok_all = ok_all and ok
        print(f"push {kind:6s} {str(dest)[:14]}...: {'OK' if ok else 'FAIL ' + str(err)}")
    print(f"\nresolved user_id: {DEFAULT_USER} · result: {'ALL OK' if ok_all else 'มีบางปลายทางล้มเหลว'}")


if __name__ == "__main__":
    asyncio.run(main())
