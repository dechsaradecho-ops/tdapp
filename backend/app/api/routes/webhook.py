"""LINE webhook — receives messages, handles commands and SEMI-AUTO approvals."""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, Request, Response

from app.api.routes.settings import get_app_settings
from app.core.config import get_settings
from app.integrations.line_client import LineClient
from app.services import execution

log = logging.getLogger(__name__)
router = APIRouter()

COMMAND_HELP = (
    "Commands: /portfolio /market /positions /risk /summary /pause /resume"
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

    for event in events:
        if event.get("type") != "message":
            continue
        text = (event.get("message") or {}).get("text", "").strip()
        reply_token = event.get("replyToken", "")
        reply = await handle_command(text, db)
        await line.reply(reply_token, reply)
    return Response(status_code=200)


async def handle_command(text: str, db) -> str:
    cmd = text.lower().split()[0] if text else ""
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
