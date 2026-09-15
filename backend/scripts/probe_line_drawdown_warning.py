"""Live probe: ยิง LINE จริง — drawdown early warning (ใกล้ถึงเพดาน).

Sends the SAME text portfolio_monitor pushes when drawdown eats 80% of the
Max Drawdown budget (build_drawdown_approach_alert) to every enabled
line_users + line_targets target.

NOTHING is paused or widened by this probe: no kill_expand_requests row is
created, no trading_pause write, no notification queue row — it only pushes
the message body so the owner can see the wording on their phone.

Usage:
    .venv/Scripts/python.exe scripts/probe_line_drawdown_warning.py
    .venv/Scripts/python.exe scripts/probe_line_drawdown_warning.py --send
    (default = dry run: prints the message, sends nothing)
"""
from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, r"d:\tdapp\backend")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.api.routes.settings import get_app_settings           # noqa: E402
from app.integrations.line_client import (  # noqa: E402
    DRAWDOWN_APPROACH_COOLDOWN_MIN,
    build_drawdown_approach_alert,
)
from app.integrations.line_client import LineClient
from app.services.database import Database                      # noqa: E402
from app.workers.portfolio_monitor import (  # noqa: E402
    DRAWDOWN_APPROACH_RATIO,
)

SEND = "--send" in sys.argv


async def main() -> None:
    db = Database()
    line = LineClient()

    users = list(db.select("line_users", filters={"notification_enabled": True}) or [])
    targets = list(db.select("line_targets", filters={"notification_enabled": True}) or [])
    print(f"token set      : {bool(line.token)}")
    print(f"line_users     : {len(users)} -> {[str(u.get('line_user_id'))[:14] + '...' for u in users]}")
    print(f"line_targets   : {len(targets)} -> {[str(t.get('target_id'))[:14] + '...' for t in targets]}")

    s = get_app_settings(db)
    max_dd = float(getattr(s, "max_drawdown_pct", 10.0) or 10.0)
    # Demo at 90% of the LIVE limit so the numbers match the owner's Settings.
    dd = round(max_dd * 0.9, 2)
    remaining = round(max_dd - dd, 2)
    peak = 10000.0
    equity = round(peak * (1.0 - dd / 100.0), 2)

    head = "[ทดสอบระบบ] ข้อความเตือน drawdown ตัวอย่าง (ไม่มีการ pause/ขยายลิมิต)"
    body = build_drawdown_approach_alert(
        dd, max_dd, remaining, equity=equity, peak_equity=peak,
        open_positions=3, open_risk_pct=2.5,
        warn_ratio=DRAWDOWN_APPROACH_RATIO)
    text = head + "\n\n" + body

    print(f"max_dd (DB)    : {max_dd:.2f}% (warn at {DRAWDOWN_APPROACH_RATIO * 100:.0f}% "
          f"= {max_dd * DRAWDOWN_APPROACH_RATIO:.2f}%, cooldown {DRAWDOWN_APPROACH_COOLDOWN_MIN:.0f} min)")
    print("\n--- MESSAGE ---")
    print(text)
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
        ok, err = await line.push_ex(dest, text)
        ok_all = ok_all and ok
        print(f"push {kind:6s} {str(dest)[:14]}...: {'OK' if ok else 'FAIL ' + str(err)}")
    print(f"\nresult: {'ALL OK' if ok_all else 'มีบางปลายทางล้มเหลว'}")


if __name__ == "__main__":
    asyncio.run(main())
