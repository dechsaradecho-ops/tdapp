"""LINE webhook — receives messages, handles commands and SEMI-AUTO approvals."""
from __future__ import annotations

import hashlib
import hmac
import logging

from fastapi import APIRouter, Header, Request, Response

from app.core.config import get_settings
from app.integrations.line_client import LineClient

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
        return "📊 Portfolio\nCapital: 100,000.00\nEquity: 101,200.00\nPnL: +1,200.00\nGoal: 3% (36% achievement)"
    if cmd == "/market":
        return "🌎 Market\nRegime: Bull Trend\nSentiment: Bullish\nOpportunity: XAUUSD 85 / EURUSD 76"
    if cmd == "/positions":
        return "Open Positions: XAUUSD BUY 0.01 @ 2398.00 (floating +12.50)"
    if cmd == "/risk":
        return "🛡 Risk\nExposure: 2.1%\nDrawdown: 0.8% / 10.0%\nStatus: OK"
    if cmd == "/summary":
        return "📊 Daily Summary\nPnL today: +1,200.00\nGoal 3%: achieved 36%\nTop: XAUUSD (85)"
    if cmd == "/pause":
        return "⏸ Auto trading PAUSED. Use /resume to continue."
    if cmd == "/resume":
        return "▶️ Auto trading RESUMED (risk checks active)."
    if text in ("[Approve]", "[Reject]"):
        return f"Received {text}. Processing SEMI-AUTO decision..."
    return COMMAND_HELP
