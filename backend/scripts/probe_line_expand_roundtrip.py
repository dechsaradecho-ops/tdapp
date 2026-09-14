"""Live probe: ยิงคำขอยืนยัน (limit expand) เข้า LINE จริง แล้วรอผลตอบกลับ.

Round trip (3 ขั้น แยกคำสั่ง):

    ask      สร้างแถว ``kill_expand_requests`` สถานะ pending จริง + push
             ข้อความคำขอยืนยัน (ปุ่ม ✅ อนุมัติ / ❌ ไม่อนุมัติ) เข้า LINE จริง
    wait     เฝ้าฐานข้อมูลจนกว่าแถวนั้นจะเปลี่ยนสถานะ (เจ้าของกดปุ่มที่ LINE)
    cleanup  ลบแถวทดสอบ + คืนค่า ``trading_pause`` ให้เหมือนก่อนทดสอบ

Safety (ทำไมยิงจริงได้):
  * ข้อเสนอในแถวทดสอบ = ลิมิตปัจจุบันทุกตัว → ``_approve`` ไม่เขียนอะไรเลย
    (outcome "skipped") และเส้นทางหมดอายุก็ไม่ขยายลิมิตเช่นกัน
  * กด ❌ ยังตั้ง pause จริง (นโยบาย "ไม่อนุมัติ = ลิมิตเดิม + เทรดหยุด") —
    ``cleanup`` จึง snapshot ค่า pause ไว้ก่อน แล้วคืนค่ากลับให้เป๊ะ
  * ไม่มีการแตะลิมิต, orders, SL/TP หรือ broker แม้แต่ที่เดียว

Usage:
    python scripts/probe_line_expand_roundtrip.py ask
    python scripts/probe_line_expand_roundtrip.py ask --send
    python scripts/probe_line_expand_roundtrip.py wait 15
    python scripts/probe_line_expand_roundtrip.py cleanup
    (default ของ ask = dry run: พิมพ์ข้อความ ไม่สร้างแถว ไม่ยิง LINE)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, r"d:\tdapp\backend")

from app.api.routes.settings import get_app_settings       # noqa: E402
from app.integrations.line_client import (build_limit_expand_prompt,  # noqa: E402
                                          LineClient)
from app.services import limit_expand                      # noqa: E402
from app.services.database import Database                 # noqa: E402
from app.services.execution import DEFAULT_USER            # noqa: E402

TABLE = limit_expand.TABLE
SOURCE = "test:roundtrip"
SNAP = os.path.join(os.environ.get("TEMP", "."), "tdapp_line_expand_test.json")

# Which limit the test request quotes. The proposal equals the CURRENT limit so
# nothing can ever be widened by this probe.
FIELDS = (("drawdown", "max_drawdown_pct", "Drawdown"),)

FOOTER = (
    "———\n"
    "🧪 โหมดทดสอบ (ไม่มีการขยายลิมิตจริง ไม่ว่ากดอะไร)\n"
    "• ข้อเสนอในระบบ = ลิมิตปัจจุบัน จึงไม่มีอะไรถูกเขียนทับ\n"
    "• กด ✅ = ระบบจะตอบว่าไม่มีการเขียนทับ + ปิดคำขอ\n"
    "• กด ❌ = ลิมิตเดิมมีผล และระบบจะ pause เทรด (จะคืนค่าเดิมให้หลังทดสอบ)\n"
    "• อายุคำขอเท่าของจริง (Settings → kill_expand_ttl_min)"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot() -> dict:
    try:
        with open(SNAP, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_snapshot(data: dict) -> None:
    with open(SNAP, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def _test_rows(db) -> list[dict]:
    rows = db.select(TABLE, order="requested_at", desc=True, limit=10) or []
    return [r for r in rows
            if str((r.get("detail") or {}).get("source") or "") == SOURCE]


async def ask(db: Database, send: bool) -> None:
    line = LineClient()
    s = get_app_settings(db)
    ttl = limit_expand.ttl_minutes(s)
    pause = (db.select("trading_pause", filters={"id": 1}, order="id", limit=1)
             or [{}])[0]

    users = list(db.select("line_users",
                           filters={"notification_enabled": True}) or [])
    targets = list(db.select("line_targets",
                             filters={"notification_enabled": True}) or [])

    triggers = []
    for trigger, field, label in FIELDS:
        cur = float(getattr(s, field, 0) or 0)
        triggers.append({"trigger": trigger, "field": field, "label": label,
                         "value": round(cur + 0.06, 2), "limit": cur,
                         "new_limit": cur})          # ← no-op proposal

    text = "\n".join([
        "🧪 [ทดสอบระบบ] คำขอยืนยันตัวอย่าง — กดปุ่มด้านล่างเพื่อทดสอบ",
        "ว่าเส้นทางตอบกลับ (postback → webhook → บันทึกผล → ตอบกลับ LINE) ทำงาน",
        "",
        build_limit_expand_prompt(triggers, paused=bool(pause.get("paused")),
                                  ttl_min=ttl),
        "",
        FOOTER,
    ])
    qr = limit_expand.quick_reply_items()

    print(f"token set      : {bool(line.token)}")
    print(f"users/targets  : {len(users)} / {len(targets)}")
    print(f"ttl            : {ttl:.0f} นาที")
    print(f"pause before   : {pause.get('paused')} ({pause.get('reason')!r})")
    print(f"proposal       : {[(t['field'], t['limit'], t['new_limit']) for t in triggers]}")
    print("\n--- MESSAGE ---")
    print(text)
    print("--- END ---\n")

    if not send:
        print("dry run — ยังไม่สร้างแถวและไม่ยิง LINE (ใส่ --send)")
        return

    _save_snapshot({"pause": pause, "started_at": _now_iso(), "row_id": ""})

    row = {
        "user_id": DEFAULT_USER,
        "status": "pending",
        "trigger_type": ",".join(t["trigger"] for t in triggers),
        "metric_value": triggers[0]["value"],
        "limit_before": triggers[0]["limit"],
        "limit_after": triggers[0]["new_limit"],
        "detail": {"triggers": triggers, "source": SOURCE, "multi": False},
        "requested_at": _now_iso(),
    }
    db.insert(TABLE, row)
    fresh = limit_expand.pending_request(db, settings=s)
    if not fresh:
        print("!! สร้างแถวคำขอไม่สำเร็จ — ไม่ยิง LINE")
        return
    rid = str(fresh.get("id"))
    snap = _snapshot()
    snap["row_id"] = rid
    _save_snapshot(snap)
    print(f"pending row    : id={rid} status={fresh.get('status')}")

    dests = [("user", u["line_user_id"]) for u in users] + \
            [(t.get("target_type", "group"), t["target_id"]) for t in targets]
    if not dests:
        print("!! ไม่มีปลายทางที่เปิดแจ้งเตือน — ยกเลิกการทดสอบ")
        return
    for kind, dest in dests:
        ok, err = await line.push_ex(dest, text, quick_reply=qr)
        print(f"push {kind:6s} {str(dest)[:14]}...: "
              f"{'OK' if ok else 'FAIL ' + str(err)}")
    print(f"\nรอผลตอบกลับ: python scripts/probe_line_expand_roundtrip.py wait 15")


def wait(db: Database, minutes: float) -> None:
    snap = _snapshot()
    rid = str(snap.get("row_id") or "")
    if not rid:
        rows = [r for r in _test_rows(db) if r.get("status") == "pending"]
        rid = str(rows[0].get("id")) if rows else ""
    if not rid:
        print("ไม่พบแถวทดสอบที่ค้างอยู่ — ต้องรัน `ask --send` ก่อน")
        return

    deadline = time.monotonic() + minutes * 60.0
    print(f"เฝ้าแถว {rid} สูงสุด {minutes:.0f} นาที "
          f"(กดปุ่ม ✅/❌ ที่ LINE ได้เลย)…")
    while True:
        row = (db.select(TABLE, filters={"id": rid}, limit=1) or [{}])[0]
        status = str(row.get("status") or "?")
        age = limit_expand.pending_age_min(row) if row.get("id") else 0.0
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] status={status} age={age:.1f}m "
              f"decided_by={row.get('decided_by')!r}")
        if status != "pending":
            print("\n✅ ได้ผลตอบกลับแล้ว")
            print(f"   id          : {rid}")
            print(f"   status      : {status}")
            print(f"   decided_by  : {row.get('decided_by')}")
            print(f"   requested_at: {row.get('requested_at')}")
            print(f"   limit       : {row.get('limit_before')} → {row.get('limit_after')}")
            pause = (db.select("trading_pause", filters={"id": 1},
                               order="id", limit=1) or [{}])[0]
            print(f"   pause now   : {pause.get('paused')} ({pause.get('reason')!r})")
            print("\nต่อด้วย: python scripts/probe_line_expand_roundtrip.py cleanup")
            return
        if time.monotonic() >= deadline:
            print(f"\n⏰ หมดเวลาเฝ้า {minutes:.0f} นาที — แถวยัง pending "
                  "(ยังกดตอบได้อยู่ หรือรัน wait ต่อ)")
            return
        time.sleep(10)


def cleanup(db: Database) -> None:
    snap = _snapshot()
    rows = [r for r in _test_rows(db) if str(r.get("status")) == "pending"]
    if snap.get("row_id") and not any(
            str(r.get("id")) == str(snap["row_id"]) for r in rows):
        one = db.select(TABLE, filters={"id": snap["row_id"]}, limit=1)
        rows = list(one or []) + rows
    for r in rows:
        ok = db.delete(TABLE, {"id": r["id"]})
        print(f"delete row {r['id']} (status={r.get('status')}): {ok}")

    before = (snap.get("pause") or {}).get("paused")
    if before is not None:
        cur = (db.select("trading_pause", filters={"id": 1}, order="id",
                         limit=1) or [{}])[0]
        if bool(cur.get("paused")) != bool(before):
            db.update("trading_pause", 1, {
                "paused": bool(before),
                "reason": (snap.get("pause") or {}).get("reason") or "",
                "paused_at": (snap.get("pause") or {}).get("paused_at"),
            })
            print(f"trading_pause restored → {before}")
        else:
            print(f"trading_pause unchanged ({cur.get('paused')})")
    left = [r for r in (db.select(TABLE, order="requested_at", desc=True,
                                 limit=10) or [])
            if str((r.get("detail") or {}).get("source") or "") == SOURCE]
    print(f"test rows left : {len(left)}")
    pend = limit_expand.pending_request(db)
    print(f"pending now    : {pend.get('id') if pend else None}")

    # audit trail the decision wrote (risk_events) — proof the press reached the
    # decision path with the row id we created
    events = db.select("risk_events", order="created_at", desc=True, limit=5) or []
    print("risk_events (newest 5):")
    for e in events:
        det = e.get("detail") or {}
        print(f"   {e.get('created_at')} {e.get('event_type')} "
              f"req={det.get('request_id')} approved={det.get('approved')}")


def main() -> None:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "ask").lower()
    db = Database()
    if mode == "ask":
        asyncio.run(ask(db, send="--send" in sys.argv))
    elif mode == "wait":
        arg = [a for a in sys.argv[2:] if a.replace(".", "", 1).isdigit()]
        wait(db, float(arg[0]) if arg else 15.0)
    elif mode == "cleanup":
        cleanup(db)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
