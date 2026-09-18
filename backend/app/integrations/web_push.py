"""Web Push (VAPID) transport — browser/OS notifications for the alert pipeline.

WHY: LINE is currently the only channel, and it is a third-party app the user
must keep installed. Web Push delivers the SAME alerts straight into the OS
notification tray, so an alert arrives even when the dashboard tab is closed.
  - Android (Chrome/Edge/Samsung Internet/Firefox): works from a NORMAL tab,
    no install required.
  - iOS/iPadOS 16.4+: only after the PWA is added to the home screen.
  - in-app browsers (Facebook/TikTok/IG/LINE webviews): no Push API at all.

DESIGN
- **Optional by construction**: with no VAPID keys configured every entry point
  is a no-op returning 0/disabled, so the app behaves exactly as before (LINE
  only) and no caller needs a feature flag.
- **pywebpush is BLOCKING** (requests + crypto) → always executed in a worker
  thread via ``asyncio.to_thread``. A scheduler tick must never stall on a
  slow/dead push service.
- **Expired subscriptions are DISABLED, never deleted** (push services answer
  404/410 once an endpoint is gone). The Settings page can then show what
  happened instead of the device silently vanishing from the list.
- **Never raises.** Push is a best-effort side channel: a dead push service
  must never break a trade alert, a worker tick, or an API response.

GOTCHA (Android): Chrome enforces ``userVisibleOnly`` — every push that
arrives MUST call ``showNotification()`` or Chrome shows its own "This site has
been updated in the background" fallback. The handler lives in
``frontend/public/sw.js`` and always shows something, even with no payload.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.core.config import get_settings
from app.services.database import Database

log = logging.getLogger(__name__)

SUBSCRIPTIONS_TABLE = "push_subscriptions"

# Disable a device after this many CONSECUTIVE failures. A permanently broken
# endpoint would otherwise be retried on every single alert forever.
MAX_FAILS = 10

# Push-service read timeout (seconds) — passed to requests via pywebpush.
TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def vapid_configured() -> bool:
    """True when both VAPID keys exist (env vars on the API service)."""
    s = get_settings()
    return bool((s.vapid_public_key or "").strip()
                and (s.vapid_private_key or "").strip())


def public_key() -> str:
    """Application server key handed to the browser (public — safe to expose)."""
    return (get_settings().vapid_public_key or "").strip()


def _private_key() -> str:
    return (get_settings().vapid_private_key or "").strip()


def _subject() -> str:
    """VAPID `sub` claim — REQUIRED by the spec. Push services reject empty."""
    sub = (get_settings().vapid_subject or "").strip()
    return sub or "mailto:admin@example.com"


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _send_sync(info: dict[str, Any], payload: str) -> tuple[str, str]:
    """Blocking send to ONE device. Returns (status, error).

    status: "ok" | "gone" (endpoint dead → disable) | "error" (retry later).
    Runs in a worker thread — never call directly from async code.
    """
    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # pragma: no cover - dependency missing
        return "error", f"pywebpush import failed: {exc}"

    subscription = {
        "endpoint": str(info.get("endpoint") or ""),
        "keys": {
            "p256dh": str(info.get("p256dh") or ""),
            "auth": str(info.get("auth") or ""),
        },
    }
    try:
        webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=_private_key(),
            vapid_claims={"sub": _subject()},
            timeout=TIMEOUT_S,
        )
        return "ok", ""
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        # 404 (no such subscription) / 410 (gone) → the device is unreachable
        # for good; the browser must subscribe again from scratch.
        if code in (404, 410):
            return "gone", f"endpoint หมดอายุ (HTTP {code})"
        return "error", f"HTTP {code or '?'}: {str(exc)[:200]}"
    except Exception as exc:
        return "error", f"{exc.__class__.__name__}: {str(exc)[:200]}"


async def push_one(db: Database, row: dict[str, Any], payload: str) -> bool:
    """Send to one subscription row and record the outcome in the DB.

    Never raises. Returns True only when the push service accepted it.
    """
    status, error = await asyncio.to_thread(_send_sync, row, payload)
    row_id = row.get("id")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    if status == "ok":
        # Clear the failure streak so an intermittent outage heals itself.
        if row_id is not None and int(row.get("fail_count") or 0) > 0:
            db.update(SUBSCRIPTIONS_TABLE, row_id,
                      {"fail_count": 0, "last_error": None, "last_ok_at": now})
        elif row_id is not None:
            db.update(SUBSCRIPTIONS_TABLE, row_id, {"last_ok_at": now})
        return True

    if row_id is None:
        return False
    fails = int(row.get("fail_count") or 0) + 1
    changes: dict[str, Any] = {"fail_count": fails, "last_error": error[:500]}
    if status == "gone" or fails >= MAX_FAILS:
        changes["enabled"] = False
        log.info("push subscription %s disabled (%s)", row_id, error)
    db.update(SUBSCRIPTIONS_TABLE, row_id, changes)
    return False


async def push_all(db: Database, title: str, body: str, url: str = "/",
                   tag: str = "tdapp", ntype: str = "") -> int:
    """Broadcast one alert to every enabled device. Returns the number sent.

    Never raises: a missing table (migration not run), no keys, no devices or
    a dead push service all degrade to 0.
    """
    if not vapid_configured():
        return 0
    try:
        rows = db.select(SUBSCRIPTIONS_TABLE, filters={"enabled": True},
                         limit=50) or []
    except Exception as exc:
        log.warning("push: subscription read failed: %s", exc)
        return 0
    if not rows:
        return 0

    payload = json.dumps({
        "title": title or "AI Trading",
        "body": body or "",
        "url": url or "/",
        "tag": tag or "tdapp",
        "ntype": ntype or "",
    }, ensure_ascii=False)

    sent = 0
    for row in rows:
        try:
            if await push_one(db, dict(row), payload):
                sent += 1
        except Exception as exc:  # push_one already guards; belt and braces
            log.warning("push: send failed: %s", exc)
    return sent


async def push_test_rows(db: Database, title: str, body: str) -> list[dict]:
    """Send to EVERY enabled device and report per-device result.

    Used by POST /api/push/test so the Settings button can show which phone
    actually received the alert (the whole point of a test button on a channel
    the user cannot inspect from the dashboard).
    """
    if not vapid_configured():
        return []
    try:
        rows = db.select(SUBSCRIPTIONS_TABLE, filters={"enabled": True},
                         limit=50) or []
    except Exception:
        return []
    payload = json.dumps({"title": title, "body": body, "url": "/settings.html",
                          "tag": "push_test", "ntype": "push_test"},
                         ensure_ascii=False)
    out: list[dict] = []
    for row in rows:
        r = dict(row)
        ok = await push_one(db, r, payload)
        out.append({
            "device": _device_label(r),
            "ok": ok,
            "error": "" if ok else str(r.get("last_error") or ""),
        })
    return out


def _device_label(row: dict[str, Any]) -> str:
    """Short human label for a device row (a raw endpoint is unreadable)."""
    ua = str(row.get("user_agent") or "")
    if "Android" in ua:
        os_name = "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iPhone/iPad"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Macintosh" in ua or "Mac OS" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "ไม่ทราบอุปกรณ์"
    if "Edg" in ua:
        browser = "Edge"
    elif "Chrome" in ua:
        browser = "Chrome"
    elif "Firefox" in ua:
        browser = "Firefox"
    elif "Safari" in ua:
        browser = "Safari"
    else:
        browser = "?"
    return f"{os_name} · {browser}"


def device_label(row: dict[str, Any]) -> str:
    """Public alias — routes/push.py builds the Settings device list."""
    return _device_label(row)


def subscription_count(db: Database) -> tuple[int, int]:
    """(total, enabled) device counts for the Settings card. Never raises."""
    try:
        rows = db.select(SUBSCRIPTIONS_TABLE, limit=100) or []
    except Exception:
        return 0, 0
    enabled = sum(1 for r in rows if r.get("enabled"))
    return len(rows), enabled


def row_to_public(row: dict[str, Any]) -> dict[str, Any]:
    """Strip the secret columns (endpoint/keys) — NEVER return them to a client."""
    return {
        "id": row.get("id"),
        "device": _device_label(row),
        "enabled": bool(row.get("enabled")),
        "fail_count": int(row.get("fail_count") or 0),
        "last_error": (str(row.get("last_error") or ""))[:200],
        "last_ok_at": row.get("last_ok_at"),
        "created_at": row.get("created_at"),
    }


def find_by_endpoint(db: Database, endpoint: str) -> Optional[dict]:
    """Existing row for this device, or None. Never raises."""
    try:
        rows = db.select(SUBSCRIPTIONS_TABLE, filters={"endpoint": endpoint},
                         limit=1) or []
    except Exception:
        return None
    return dict(rows[0]) if rows else None
