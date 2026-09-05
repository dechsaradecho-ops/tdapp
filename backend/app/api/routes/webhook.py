"""LINE webhook — receives messages, handles commands and SEMI-AUTO approvals.

Free-form chat (DM or group) is answered by the same grounded AI used by the
web chat widget: slash commands stay instant, everything else goes to the AI
provider with the live market/risk/portfolio context block attached.

Group chats are also auto-registered as notification targets: the first time
the bot is added to a group (or mentioned there), the group ID is stored in
line_targets so all alerts (signals, trades, risk warnings, daily digest)
can be pushed to the group too.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, Response

from app.api.routes.settings import get_app_settings
from app.core.config import get_settings
from app.integrations.line_client import LineClient
from app.services import execution

log = logging.getLogger(__name__)
router = APIRouter()

COMMAND_HELP = (
    "Commands: /portfolio /market /positions /risk /summary /pause /resume\n"
    "หรือพิมพ์คำถามอิสระ เช่น “วันนี้ควรเทรดทองไหม” — AI จะตอบพร้อมบริบทตลาดจริง"
)


def verify_signature(body: bytes, signature: str) -> bool:
    secret = get_settings().line_channel_secret.encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhook")
async def line_webhook(
    request: Request,
    x_line_signature: str = Header(default=""),
) -> Response:
    body = await request.body()
    if not verify_signature(body, x_line_signature):
        return Response(status_code=403)

    events = (await request.json()).get("events", [])
    line: LineClient = request.app.state.line
    db = request.app.state.db
    bot_user_id = get_settings().line_bot_user_id

    for event in events:
        source = event.get("source", {}) or {}
        source_type = source.get("type", "user")
        target_id = (source.get("groupId") or source.get("roomId")
                     or source.get("userId") or "")

        # Auto-register group/room chats as notification targets so alerts
        # (signals, trades, risk warnings, digest) reach the group too.
        if target_id and source_type in ("group", "room"):
            _register_target(db, target_id, source_type)

        if event.get("type") != "message":
            continue
        text = (event.get("message") or {}).get("text", "").strip()
        reply_token = event.get("replyToken", "")
        if not text or not reply_token:
            continue
        # Group chats: only answer when mentioned or replying to the bot —
        # otherwise the bot would spam every conversation in the group.
        if source_type == "group":
            mentionees = [
                m for m in (event.get("message") or {}).get("mention", {})
                .get("mentionees", []) if isinstance(m, dict)
            ]
            mentioned = any(
                m.get("type") == "user" and m.get("userId") == bot_user_id
                for m in mentionees
            ) or any(m.get("type") == "all" for m in mentionees)
            if not mentioned:
                continue
        reply = await handle_command(text, db)
        if reply is None:
            # not a command → grounded AI answer (same pipeline as the web chat)
            reply = await ai_reply(request, text)
        await line.reply(reply_token, reply)
    return Response(status_code=200)


def _register_target(db, target_id: str, target_type: str) -> None:
    """Insert the group/room into line_targets (idempotent, never raises).

    line_targets rows are keyed by target_id — the notification service and
    worker read them on every dispatch, so a group added today receives
    tomorrow's alerts without any manual setup. Every event also stamps
    last_seen_at so the Settings page can prove the webhook is receiving.
    """
    try:
        existing = db.select("line_targets", filters={"target_id": target_id},
                             limit=1)
        now = datetime.now(timezone.utc).isoformat()
        if existing:
            db.update("line_targets", existing[0].get("id", ""),
                      {"last_seen_at": now})
            return
        db.insert("line_targets", {
            "target_id": target_id, "target_type": target_type,
            "notification_enabled": True, "last_seen_at": now,
        })
        log.info("LINE target registered: %s (%s)", target_id, target_type)
    except Exception as exc:
        log.debug("line_targets register failed: %s", exc)


async def ai_reply(request: Request, text: str) -> str:
    """Grounded AI answer for free-form LINE chat.

    Reuses the exact context block the web chat widget uses (market regime,
    opportunity scores, risk, portfolio, open orders) so LINE answers are
    grounded in the same real data. Never raises — failures degrade to a
    short error note, because a dead webhook must still 200 to LINE.
    """
    try:
        from app.api.routes.chat import _build_context
        from app.integrations.ai_provider import get_ai_provider

        db = request.app.state.db
        context = await _build_context(db, request.app.state.broker)
        provider = get_ai_provider()
        reply = await provider.chat([{"role": "user", "content": text + "\n\n" + context}])
        return reply or COMMAND_HELP
    except Exception as exc:
        log.warning("LINE AI reply failed: %s", exc)
        return f"[AI ERROR] ตอบไม่สำเร็จชั่วคราว ({exc.__class__.__name__}) — ลองใหม่อีกครั้งครับ"


async def handle_command(text: str, db) -> str | None:
    """Slash commands → instant canned reply. Returns None when the text is
    not a command, signalling the caller to route it to the AI instead."""
    cmd = text.lower().split()[0] if text else ""
    if not cmd.startswith("/"):
        return None
    if cmd == "/portfolio":
        s = get_app_settings(db)
        cap = s.capital or 10_000.0
        pnl = cap * 0.012
        achievement = pnl / (cap * 0.03) * 100
        return (f"📊 Portfolio\nCapital: {cap:,.2f}\nEquity: {cap + pnl:,.2f}\n"
                f"PnL: +{pnl:,.2f}\nGoal: 3% ({achievement:.0f}% achievement)")
    if cmd == "/market":
        return "🌎 Market\nRegime: Bull Trend\nSentiment: Bullish\nOpportunity: XAUUSD 85 / EURUSD 76"
    if cmd == "/positions":
        return "Open Positions: XAUUSD BUY 0.01 @ 2398.00 (floating +12.50)"
    if cmd == "/risk":
        return "🛡 Risk\nExposure: 2.1%\nDrawdown: 0.8% / 10.0%\nStatus: OK"
    if cmd == "/summary":
        s = get_app_settings(db)
        cap = s.capital or 10_000.0
        pnl = cap * 0.012
        achievement = pnl / (cap * 0.03) * 100
        return (f"📊 Daily Summary\nPnL today: +{pnl:,.2f}\n"
                f"Goal 3%: achieved {achievement:.0f}%\nTop: XAUUSD (85)")
    if cmd == "/pause":
        execution.set_pause(db, True, "paused from LINE /pause")
        return "🛑 Auto trading PAUSED — ทุก order (auto + approve) ถูกบล็อก.\nUse /resume to continue."
    if cmd == "/resume":
        execution.set_pause(db, False, "")
        return "▶️ Auto trading RESUMED (risk checks active)."
    if text in ("[Approve]", "[Reject]"):
        return f"Received {text}. Processing SEMI-AUTO decision..."
    return COMMAND_HELP


# ---------------------------------------------------------------------------
# Test button + registered-target list (Settings page)
# ---------------------------------------------------------------------------
@router.get("/targets")
async def line_targets(request: Request) -> dict:
    """List every registered notification target (groups/rooms + personal).

    The Settings page shows the groupId here so the owner can verify which
    chats will receive alerts. `last_seen_at` is the most recent webhook
    event received from that chat (proves the webhook is actually receiving).
    """
    db = request.app.state.db
    targets = db.select("line_targets", order="created_at", desc=True, limit=100)
    users = db.select("line_users", limit=100)
    return {
        "targets": [
            {
                "target_id": t.get("target_id", ""),
                "target_type": t.get("target_type", "group"),
                "notification_enabled": bool(t.get("notification_enabled")),
                "created_at": t.get("created_at"),
                "last_seen_at": t.get("last_seen_at"),
            }
            for t in targets
        ],
        "users": [
            {
                "line_user_id": u.get("line_user_id", ""),
                "notification_enabled": bool(u.get("notification_enabled")),
            }
            for u in users
        ],
    }


@router.post("/test")
async def line_test(request: Request) -> dict:
    """Push a test message to every enabled target — the Settings page's
    🔔 ทดสอบ button. Reports per-target ok/fail so a wrong groupId or a
    bot-not-in-group error is visible immediately."""
    line: LineClient = request.app.state.line
    db = request.app.state.db
    message = ("🔔 ทดสอบการแจ้งเตือน — ระบบเชื่อมต่อ LINE สำเร็จ\n"
               "(ส่งจากหน้า Settings ของ tdapp)")

    results: list[dict] = []
    for t in db.select("line_targets", filters={"notification_enabled": True}):
        ok = await line.push(t["target_id"], message)
        results.append({"target_id": t["target_id"],
                        "target_type": t.get("target_type", "group"),
                        "ok": ok})
    for u in db.select("line_users", filters={"notification_enabled": True}):
        ok = await line.push(u["line_user_id"], message)
        results.append({"target_id": u["line_user_id"],
                        "target_type": "user", "ok": ok})

    sent = sum(1 for r in results if r["ok"])
    return {
        "ok": sent > 0,
        "sent": sent,
        "failed": len(results) - sent,
        "results": results,
        "hint": ("ตรวจกลุ่ม LINE — ควรได้รับข้อความทดสอบแล้ว"
                 if sent else
                 "ยังไม่มีปลายทางที่ส่งได้ — เพิ่มบอทเข้ากลุ่มแล้ว @mention บอท 1 ครั้ง "
                 "หรือตั้ง LINE_CHANNEL_ACCESS_TOKEN บน Render"),
    }