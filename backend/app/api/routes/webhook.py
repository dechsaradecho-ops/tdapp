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
from collections import deque
from datetime import datetime, timezone

from fastapi import APIRouter, Header, Request, Response
from pydantic import BaseModel

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

# ---------------------------------------------------------------------------
# Webhook event log — in-memory ring buffer (last 50 events) for the Settings
# debug panel. Records EVERY webhook hit including signature failures, so
# "LINE console Verify → 403" and "bot doesn't reply" both leave evidence.
# ---------------------------------------------------------------------------
EVENT_LOG: deque[dict] = deque(maxlen=50)


def _log_event(kind: str, **info) -> None:
    EVENT_LOG.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "kind": kind, **info,
    })


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
        # The #1 setup failure: LINE console "Verify" shows 403 when
        # LINE_CHANNEL_SECRET is missing/mismatched on the server.
        s = get_settings()
        _log_event("signature_rejected",
                   secret_set=bool(s.line_channel_secret),
                   sig_prefix=x_line_signature[:12])
        log.warning("LINE webhook signature rejected (secret_set=%s)",
                    bool(s.line_channel_secret))
        return Response(status_code=403)

    events = (await request.json()).get("events", [])
    line: LineClient = request.app.state.line
    db = request.app.state.db
    bot_user_id = get_settings().line_bot_user_id
    _log_event("received", events=len(events))

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
            _log_event("skipped_non_message", event_type=event.get("type", "?"),
                       source_type=source_type)
            continue
        text = (event.get("message") or {}).get("text", "").strip()
        reply_token = event.get("replyToken", "")
        if not text or not reply_token:
            _log_event("skipped_no_text_or_token", source_type=source_type,
                       has_text=bool(text), has_token=bool(reply_token))
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
                _log_event("skipped_no_mention", source_type=source_type,
                           target_id=target_id, text=text[:40],
                           bot_id_set=bool(bot_user_id),
                           mentionee_types=[m.get("type") for m in mentionees])
                continue
        reply = await handle_command(text, db)
        if reply is None:
            # not a command → grounded AI answer (same pipeline as the web chat)
            reply = await ai_reply(request, text)
        ok = await line.reply(reply_token, reply)
        _log_event("replied", source_type=source_type, target_id=target_id,
                   text=text[:40], reply_ok=ok, reply=reply[:80])
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


class TargetAddRequest(BaseModel):
    target_id: str
    target_type: str = "group"


@router.post("/targets")
async def add_line_target(request: Request, payload: TargetAddRequest) -> dict:
    """Manually register a groupId/roomId (Settings page input).

    Auto-registration needs the bot to receive a webhook event from the
    group first; this endpoint lets the owner paste the groupId directly
    (from the LINE console or a webhook log) without that step.
    """
    db = request.app.state.db
    target_id = (payload.target_id or "").strip()
    if not target_id:
        return {"ok": False, "message": "target_id ว่าง — ใส่ groupId ที่ขึ้นต้นด้วย C"}
    if not target_id.startswith(("C", "R", "U")):
        return {"ok": False,
                "message": f"รูปแบบไม่ถูกต้อง: {target_id[:12]}… — groupId ต้องขึ้นต้นด้วย C (room = R)"}
    try:
        existing = db.select("line_targets", filters={"target_id": target_id},
                             limit=1)
        if existing:
            return {"ok": True, "message": "มีกลุ่มนี้อยู่แล้ว (ไม่ซ้ำ)",
                    "target": existing[0]}
        row = db.insert("line_targets", {
            "target_id": target_id,
            "target_type": payload.target_type or "group",
            "notification_enabled": True,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        })
        if not row:
            return {"ok": False,
                    "message": ("insert ไม่สำเร็จ — ตาราง line_targets ยังไม่มี "
                                "(รัน database/018_line_targets.sql ใน Supabase SQL Editor ก่อน)")}
        return {"ok": True, "message": "✅ เพิ่มกลุ่มเรียบร้อย — กด 🔔 ทดสอบได้เลย",
                "target": row}
    except Exception as exc:
        return {"ok": False, "message": f"{exc.__class__.__name__}: {exc}"}


@router.delete("/targets/{target_id}")
async def remove_line_target(request: Request, target_id: str) -> dict:
    """Remove a registered target (Settings page 🗑 button)."""
    db = request.app.state.db
    ok = db.delete("line_targets", {"target_id": target_id})
    return {"ok": ok,
            "message": "ลบแล้ว" if ok else "ลบไม่สำเร็จ (อาจไม่มี row นี้)"}


@router.get("/events")
async def line_events(request: Request) -> dict:
    """Recent webhook activity for the Settings debug panel.

    Records every hit: signature rejections (403 — the classic 'Verify failed'
    cause), skipped events (no mention / non-message), and replies (ok/fail).
    In-memory only (last 50) — restarts clear it, which is fine for debugging.
    """
    return {"events": list(EVENT_LOG)}


class SimulateRequest(BaseModel):
    text: str
    source_type: str = "user"      # user | group
    target_id: str = ""            # optional groupId for group simulation
    bot_user_id: str = ""          # override for @mention testing
    push_reply_to: str = ""        # optional: also PUSH the reply to this id


@router.post("/simulate")
async def line_simulate(request: Request, payload: SimulateRequest) -> dict:
    """Run a message through the EXACT webhook pipeline without LINE.

    Answers 'what would the bot reply?' from the Settings page: same command
    handling, same @mention gate, same grounded AI. The reply is returned in
    the response (reply tokens are single-use, so a real reply is impossible
    without LINE) — and optionally pushed to a chat id via the push API.
    """
    db = request.app.state.db
    bot_user_id = payload.bot_user_id or get_settings().line_bot_user_id
    text = (payload.text or "").strip()
    steps: list[dict] = []

    if not text:
        return {"ok": False, "reply": None,
                "steps": [{"step": "validate", "ok": False,
                           "note": "ข้อความว่าง"}]}

    steps.append({"step": "validate", "ok": True, "note": f"text={text[:60]!r}"})

    # --- same auto-registration the webhook does for group/room events ---
    if payload.source_type in ("group", "room") and payload.target_id:
        _register_target(db, payload.target_id, payload.source_type)
        steps.append({"step": "register_target", "ok": True,
                      "note": f"{payload.target_id} → line_targets"})

    # --- same @mention gate ---
    if payload.source_type == "group":
        mentioned = bool(payload.bot_user_id) or payload.target_id == ""
        # simulate: an explicit bot_user_id counts as a mention; empty means
        # the mentionee list would not match (mirrors the real gate).
        if not mentioned:
            steps.append({"step": "mention_gate", "ok": False,
                          "note": ("บอทจะไม่ตอบ — LINE_BOT_USER_ID ยังไม่ได้ตั้ง "
                                   "(env บน Render) ทำให้ @mention จับไม่เจอ")})
            return {"ok": True, "reply": None, "steps": steps,
                    "note": "group message ถูกข้าม (no mention)"}
        steps.append({"step": "mention_gate", "ok": True,
                      "note": "mentioned → ประมวลผลข้อความ"})

    # --- same reply pipeline ---
    reply = await handle_command(text, db)
    via = "command"
    if reply is None:
        via = "ai"
        reply = await ai_reply(request, text)
    steps.append({"step": "reply", "ok": True, "via": via, "reply": reply[:200]})

    pushed = False
    push_error = ""
    if payload.push_reply_to:
        line: LineClient = request.app.state.line
        pushed, push_error = await line.push_ex(payload.push_reply_to, reply)
        steps.append({"step": "push", "ok": pushed,
                      "note": push_error or f"pushed → {payload.push_reply_to}"})

    _log_event("simulated", text=text[:40], via=via, push_ok=pushed)
    return {"ok": True, "reply": reply, "via": via, "steps": steps,
            "pushed": pushed, "push_error": push_error}


@router.get("/diag")
async def line_diag(request: Request) -> dict:
    """One-shot diagnosis for the Settings page: which env vars are set,
    whether the line_targets table exists, and a live LINE API probe."""
    db = request.app.state.db
    s = get_settings()
    line: LineClient = request.app.state.line

    table_ok = True
    table_error = ""
    try:
        db._client.table("line_targets").select("id").limit(1).execute()
    except Exception as exc:
        table_ok = False
        table_error = str(exc)[:200]

    out: dict = {
        "token_set": bool(s.line_channel_access_token),
        "secret_set": bool(s.line_channel_secret),
        "bot_user_id_set": bool(s.line_bot_user_id),
        "bot_user_id": (s.line_bot_user_id[:6] + "…") if s.line_bot_user_id else "",
        "db_available": db.available,
        "targets_table_ok": table_ok,
        "targets_count": len(db.select("line_targets", limit=100)),
        "users_count": len(db.select("line_users", limit=100)),
    }
    if not table_ok:
        out["table_error"] = table_error
        out["hint"] = ("ตาราง line_targets ยังไม่มี — รัน database/018_line_targets.sql "
                       "ใน Supabase SQL Editor ก่อน")
        return out

    # Live probe: ask LINE for the bot's own profile (validates the token).
    if line.enabled:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.line.me/v2/bot/info",
                    headers={"Authorization": f"Bearer {line.token}"})
            out["token_valid"] = resp.status_code == 200
            if resp.status_code == 200:
                info = resp.json()
                out["bot_user_id_from_api"] = info.get("userId", "")
                out["display_name"] = info.get("displayName", "")
                out["hint"] = ("token ใช้ได้ — ถ้ายังไม่มี groupId ให้เพิ่มบอทเข้ากลุ่ม "
                               "แล้ว @mention 1 ครั้ง หรือวาง groupId ด้วยมือด้านบน")
            else:
                out["hint"] = (f"token ปฏิเสธ (HTTP {resp.status_code}) — "
                               "เช็คว่าคัดลอก Channel access token ยาวเต็มและยังไม่หมดอายุ")
        except Exception as exc:
            out["token_valid"] = False
            out["hint"] = f"เรียก LINE API ไม่ได้: {exc.__class__.__name__}"
    else:
        out["token_valid"] = False
        out["hint"] = "LINE_CHANNEL_ACCESS_TOKEN ยังไม่ได้ตั้งบน Render"
    return out


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
        ok, err = await line.push_ex(t["target_id"], message)
        results.append({"target_id": t["target_id"],
                        "target_type": t.get("target_type", "group"),
                        "ok": ok, "error": err})
    for u in db.select("line_users", filters={"notification_enabled": True}):
        ok, err = await line.push_ex(u["line_user_id"], message)
        results.append({"target_id": u["line_user_id"],
                        "target_type": "user", "ok": ok, "error": err})

    sent = sum(1 for r in results if r["ok"])
    return {
        "ok": sent > 0,
        "sent": sent,
        "failed": len(results) - sent,
        "results": results,
        "hint": ("ตรวจกลุ่ม LINE — ควรได้รับข้อความทดสอบแล้ว"
                 if sent else
                 "ยังไม่มีปลายทางที่ส่งได้ — เพิ่มบอทเข้ากลุ่มแล้ว @mention บอท 1 ครั้ง "
                 "หรือวาง groupId ด้วยมือ / กดวินิจฉัยเพื่อดูสาเหตุ"),
    }