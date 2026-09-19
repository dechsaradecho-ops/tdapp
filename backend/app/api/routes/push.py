"""Web Push (VAPID) API — Settings page's "การแจ้งเตือนมือถือ" card.

Endpoints (mounted at /api/push, automatically PIN-protected like every other
/api/* route — see _PIN_EXEMPT_PATHS in app/main.py):

  GET  /api/push/key            → {enabled, public_key, subscriptions}
  POST /api/push/subscribe      → upsert this device (idempotent by endpoint)
  POST /api/push/unsubscribe    → disable this device (row kept for the record)
  GET  /api/push/subscriptions  → device list — NEVER includes endpoint/keys
  POST /api/push/test           → "ทดสอบ" button: push to every device

SECURITY: `endpoint` + p256dh/auth are credentials for pushing to a device.
They travel browser → server exactly once (subscribe) and are never returned
by any endpoint. The table has no anon policy either.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from app.integrations import web_push
from app.models.schemas import (
    PushKeyInfo,
    PushSubscribeRequest,
    PushSubscribeResult,
    PushSubscriptionsResponse,
    PushTestResult,
    PushUnsubscribeRequest,
    PushVerifyRequest,
    PushVerifyResult,
)

log = logging.getLogger(__name__)

router = APIRouter()

TABLE = web_push.SUBSCRIPTIONS_TABLE


def _hint_for_insert_error(err: str | None) -> str:
    """Turn a raw PostgREST error into something actionable in Thai.

    The two failures a user actually hits are "ไม่ได้รัน migration" and
    "RLS ปิดกั้น" — a raw English exception tells them nothing.
    """
    text = (err or "").lower()
    if "does not exist" in text or "schema cache" in text or "relation" in text:
        return ("ยังไม่พบตาราง push_subscriptions — "
                "รัน database/042_push_subscriptions.sql ใน Supabase SQL Editor ก่อน")
    if "row-level security" in text or "policy" in text or "permission denied" in text:
        return ("RLS ปิดกั้นการบันทึก — ต้องมี policy push_subscriptions_service_all "
                "(รัน migration 042 ทั้งไฟล์อีกครั้ง)")
    return f"บันทึกไม่สำเร็จ: {(err or 'unknown error')[:180]}"


@router.get("/key", response_model=PushKeyInfo)
async def push_key(request: Request) -> PushKeyInfo:
    """Public VAPID key + whether the channel is usable at all.

    `enabled=false` means the API service has no VAPID env vars — the frontend
    then shows guidance instead of a button that cannot work.
    """
    db = request.app.state.db
    configured = web_push.vapid_configured()
    total, enabled = web_push.subscription_count(db)
    message = ""
    if not configured:
        message = ("เซิร์ฟเวอร์ยังไม่ได้ตั้งค่า VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY — "
                   "เพิ่ม env ทั้งสองที่บริการ API แล้ว Redeploy "
                   "(วิธีสร้างกุญแจ: backend/scripts/gen_vapid_keys.py)")
    return PushKeyInfo(enabled=configured,
                       public_key=web_push.public_key() if configured else "",
                       subscriptions=enabled, total=total, message=message)


@router.post("/subscribe", response_model=PushSubscribeResult)
async def push_subscribe(payload: PushSubscribeRequest,
                         request: Request) -> PushSubscribeResult:
    """Register (or refresh) one device. Idempotent — same endpoint = update.

    Re-subscribing on the same endpoint RESETS fail_count and re-enables the
    row: the browser only issues a new subscription after the old one died, so
    a re-subscribe is exactly the signal that the device is alive again.
    """
    db = request.app.state.db
    endpoint = (payload.endpoint or "").strip()
    keys = payload.keys
    p256dh = (keys.p256dh or "").strip() if keys else ""
    auth = (keys.auth or "").strip() if keys else ""
    if not endpoint or not p256dh or not auth:
        return PushSubscribeResult(ok=False,
                                   message="ข้อมูลอุปกรณ์ไม่ครบ (endpoint/keys)")
    if not db.available:
        return PushSubscribeResult(ok=False,
                                   message="ฐานข้อมูลไม่พร้อม — ลงทะเบียนไม่สำเร็จ")

    row = {
        "endpoint": endpoint,
        "p256dh": p256dh,
        "auth": auth,
        "user_agent": (payload.user_agent or "")[:300],
        "enabled": True,
        "fail_count": 0,
        "last_error": None,
    }

    existing = web_push.find_by_endpoint(db, endpoint)
    if existing and existing.get("id") is not None:
        ok = db.update(TABLE, existing["id"], row)
        return PushSubscribeResult(
            ok=bool(ok), subscribed=bool(ok),
            message=("อัปเดตอุปกรณ์นี้แล้ว" if ok
                     else "อัปเดตไม่สำเร็จ — ลองกดใหม่อีกครั้ง"))

    saved, err = db.insert_raw(TABLE, row)
    if saved is None:
        return PushSubscribeResult(ok=False, message=_hint_for_insert_error(err))
    return PushSubscribeResult(ok=True, subscribed=True,
                               message="ผูกอุปกรณ์นี้เรียบร้อย — จะได้รับการแจ้งเตือนแล้ว")


@router.post("/unsubscribe", response_model=PushSubscribeResult)
async def push_unsubscribe(payload: PushUnsubscribeRequest,
                           request: Request) -> PushSubscribeResult:
    """Disable one device. The row is KEPT (enabled=false) so the Settings card
    can still explain why a phone stopped receiving alerts."""
    db = request.app.state.db
    endpoint = (payload.endpoint or "").strip()
    if not endpoint:
        return PushSubscribeResult(ok=False, message="ไม่พบรหัสอุปกรณ์")
    existing = web_push.find_by_endpoint(db, endpoint)
    if not existing or existing.get("id") is None:
        # The browser already dropped the subscription — treat as success so
        # the UI can settle instead of showing a spurious error.
        return PushSubscribeResult(ok=True, subscribed=False,
                                   message="อุปกรณ์นี้ไม่ได้ผูกอยู่แล้ว")
    ok = db.update(TABLE, existing["id"],
                   {"enabled": False, "last_error": "ปิดโดยผู้ใช้"})
    return PushSubscribeResult(ok=bool(ok), subscribed=False,
                               message=("ปิดการแจ้งเตือนบนอุปกรณ์นี้แล้ว" if ok
                                        else "ปิดไม่สำเร็จ — ลองใหม่อีกครั้ง"))


@router.get("/subscriptions", response_model=PushSubscriptionsResponse)
async def push_subscriptions(request: Request) -> PushSubscriptionsResponse:
    """Device list for the Settings card. endpoint/keys are stripped — they are
    push credentials, not display data."""
    db = request.app.state.db
    try:
        rows = db.select(TABLE, limit=50) or []
    except Exception as exc:
        log.warning("push subscriptions read failed: %s", exc)
        rows = []
    devices = [web_push.row_to_public(r) for r in rows]
    return PushSubscriptionsResponse(
        total=len(devices),
        enabled_count=sum(1 for d in devices if d.get("enabled")),
        devices=devices,
    )


@router.post("/verify", response_model=PushVerifyResult)
async def push_verify(payload: PushVerifyRequest,
                      request: Request) -> PushVerifyResult:
    """Probe THIS device's endpoint and say whether it is still alive.

    WHY: a browser cannot detect that its own subscription died (FCM answers
    HTTP 410 only to the server), so the Settings card asks the server to probe
    the exact endpoint the device holds. ``gone=true`` tells the card to force
    a fresh subscription instead of re-POSTing the dead one forever.
    """
    db = request.app.state.db
    if not db.available:
        return PushVerifyResult(ok=False, message="ฐานข้อมูลไม่พร้อม")
    out = await web_push.verify_endpoint(db, payload.endpoint)
    return PushVerifyResult(**out)


@router.post("/test", response_model=PushTestResult)
async def push_test(request: Request) -> PushTestResult:
    """The Settings page's ทดสอบ button — mirrors the LINE test endpoint.

    Reports per-device ok/fail so "โทรศัพท์ไม่ได้รับ" is diagnosable from the
    dashboard (permission denied vs. dead endpoint vs. no device registered).
    """
    db = request.app.state.db
    if not web_push.vapid_configured():
        return PushTestResult(
            ok=False, enabled=False,
            message=("ยังส่งไม่ได้ — เซิร์ฟเวอร์ไม่มี VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY "
                     "(เพิ่ม env ที่บริการ API แล้ว Redeploy)"))

    title = "ทดสอบการแจ้งเตือน"
    body = "ถ้าเห็นข้อความนี้บนมือถือ = เชื่อมต่อ Web Push สำเร็จ"
    results = await web_push.push_test_rows(db, title, body)
    sent = sum(1 for r in results if r["ok"])
    failed = len(results) - sent

    if not results:
        message = ("ยังไม่มีอุปกรณ์ที่ผูกไว้ — กด \"เปิดการแจ้งเตือนบนอุปกรณ์นี้\" "
                   "แล้วกดอนุญาตในการแจ้งเตือนของเบราว์เซอร์")
    elif sent:
        message = f"ส่งสำเร็จ {sent} อุปกรณ์ — ตรวจ notification tray ของมือถือ"
    else:
        message = ("ส่งไม่สำเร็จทุกอุปกรณ์ — ดูเหตุผลรายอุปกรณ์ด้านล่าง "
                   "(endpoint หมดอายุ = ต้องเปิดการแจ้งเตือนใหม่บนเครื่องนั้น)")

    return PushTestResult(ok=sent > 0, enabled=True, sent=sent, failed=failed,
                          total=len(results), message=message, results=results)
