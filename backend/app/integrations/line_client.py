"""LINE Messaging API client — alerts, daily summaries and the SEMI-AUTO approval flow.

Notification types supported:
    new_signal, trade_opened, trade_closed, stop_loss, risk_warning,
    daily_portfolio_summary, daily_market_summary, economic_news, semi_auto_approval
Critical alerts are sent immediately; scheduled ones are queued by worker #4.
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from app.core.config import get_settings

log = logging.getLogger(__name__)

API = "https://api.line.me/v2/bot/message"


def _text_message(text: str, quick_reply: Optional[list[dict]] = None) -> dict:
    """One text message, optionally carrying quick-reply buttons.

    LINE accepts ``quickReply`` on any message, and the postback actions in
    limit_expand.quick_reply_items() let the owner answer a risk decision with
    one tap instead of typing /dd_ok (see app/services/limit_expand.py).
    """
    msg: dict = {"type": "text", "text": text}
    if quick_reply:
        msg["quickReply"] = {"items": list(quick_reply)}
    return msg


class LineClient:
    def __init__(self, access_token: Optional[str] = None) -> None:
        s = get_settings()
        self.token = access_token or s.line_channel_access_token
        self.enabled = bool(self.token)

    # ------------------------------------------------------------------
    async def push(self, line_user_id: str, text: str,
                   quick_reply: Optional[list[dict]] = None) -> bool:
        ok, _ = await self.push_ex(line_user_id, text, quick_reply=quick_reply)
        return ok

    async def push_ex(self, line_user_id: str, text: str,
                      quick_reply: Optional[list[dict]] = None) -> tuple[bool, str]:
        """Push returning (ok, raw_error) — the Settings test button surfaces
        the LINE API error so a wrong token / bot-not-in-group is visible in
        the UI instead of a silent False."""
        if not self.enabled:
            log.info("LINE disabled — would push to %s: %s", line_user_id, text[:80])
            return False, "LINE_CHANNEL_ACCESS_TOKEN ยังไม่ได้ตั้ง (env บน Render)"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{API}/push",
                    headers={"Authorization": f"Bearer {self.token}",
                             "Content-Type": "application/json"},
                    json={"to": line_user_id,
                          "messages": [_text_message(text, quick_reply)]},
                )
                if resp.status_code == 200:
                    return True, ""
                detail = resp.text[:200]
                log.warning("LINE push failed %s: %s", resp.status_code, detail)
                return False, f"HTTP {resp.status_code}: {detail}"
        except Exception as exc:
            return False, f"{exc.__class__.__name__}: {exc}"

    async def reply(self, reply_token: str, text: str,
                    quick_reply: Optional[list[dict]] = None) -> bool:
        if not self.enabled:
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API}/reply",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json={"replyToken": reply_token,
                      "messages": [_text_message(text, quick_reply)]},
            )
            return resp.status_code == 200

    # ------------------------------------------------------------------
    # Quick-reply approval (SEMI-AUTO flow)
    # ------------------------------------------------------------------
    def approval_message(self, signal: dict) -> str:
        return (
            "📈 New Trading Signal (SEMI-AUTO — approval required)\n"
            f"Asset: {signal['asset']}\n"
            f"Direction: {signal['direction']}\n"
            f"Confidence: {signal['confidence']}%\n"
            f"Entry: {signal['entry']}\n"
            f"SL: {signal['stop_loss']}\n"
            f"TP: {signal['take_profit']}\n"
            f"RR: {signal['expected_rr']}\n"
            f"Opportunity Score: {signal['opportunity_score']}\n\n"
            "[Approve] / [Reject] / [View Analysis]"
        )


def build_risk_alert(drawdown_pct: float, max_dd: float, recommendation: str) -> str:
    return (
        "🚨 Risk Warning\n"
        f"Current Drawdown: {drawdown_pct:.2f}%\n"
        f"Maximum Allowed Drawdown: {max_dd:.2f}%\n"
        f"Recommendation: {recommendation}"
    )


def build_trade_closed_alert(asset: str, result: str, pnl: float, growth_pct: float) -> str:
    emoji = "💰" if pnl >= 0 else "⚠️"
    return (
        f"{emoji} Trade Closed\n"
        f"Asset: {asset}\n"
        f"Result: {result}\n"
        f"PnL: {pnl:+,.2f}\n"
        f"Account Growth: {growth_pct:+.2f}%"
    )


def build_daily_portfolio_summary(capital: float, equity: float, pnl: float,
                                  goal_pct: float, achievement_pct: float,
                                  probability: str) -> str:
    return (
        "📊 Portfolio Progress\n"
        f"Capital: {capital:,.2f}\n"
        f"Current Equity: {equity:,.2f}\n"
        f"Current PnL: {pnl:+,.2f}\n"
        f"Monthly Goal: {goal_pct:.1f}%\n"
        f"Achievement: {achievement_pct:.1f}%\n"
        f"Probability: {probability}"
    )


def build_daily_market_summary(regime: str, sentiment: str, top_opportunity: str,
                               top_assets: str, risk_status: str) -> str:
    return (
        "🌎 Market Summary\n"
        f"Market Regime: {regime}\n"
        f"Market Sentiment: {sentiment}\n"
        f"Top Opportunity: {top_opportunity}\n"
        f"Top Assets: {top_assets}\n"
        f"Risk Status: {risk_status}"
    )


# ---------------------------------------------------------------------------
# Risk-limit expansion — owner confirmation (app/services/limit_expand.py)
# ---------------------------------------------------------------------------
def build_limit_expand_prompt(triggers: list[dict], paused: bool = True,
                              setup_required: bool = False,
                              ttl_min: float = 180.0) -> str:
    """ONE actionable prompt: every breached limit + the proposed +5pp value.

    The owner must confirm before ANY limit grows — the platform never widens
    a risk limit on its own (prod 2026-09-14 incident). Buttons: Approve /
    Reject (postback dd_ok / dd_no) with the typed commands as fallback.

    ``ttl_min`` = the configured confirmation window (Settings →
    ``kill_expand_ttl_min``, default 180). After it the request is retired as
    expired and the monitor issues a fresh one — the message must say the same
    number the web popup shows.
    """
    lines = ["🛑 เกินลิมิตความเสี่ยง — ต้องยืนยันจากเจ้าของบัญชีก่อนขยายลิมิต", ""]
    for t in triggers or []:
        lines.append(
            f"• {t.get('label', t.get('trigger', '?'))}: "
            f"{float(t.get('value', 0)):.2f}% เกิน {float(t.get('limit', 0)):.2f}% "
            f"→ เสนอ {float(t.get('new_limit', 0)):.2f}% (+5%)"
        )
    lines.append("")
    if setup_required:
        lines.append("⚠️ ยังบันทึกคำขอไม่ได้ — รัน database/036_kill_expand_confirm.sql "
                     "ใน Supabase SQL Editor ก่อน")
        lines.append("ลิมิตยังไม่ถูกแตะต้อง (ระบบไม่ขยายลิมิตเองโดยอัตโนมัติ)")
        return "\n".join(lines)
    lines.append(f"สถานะ: {'🛑 หยุดเปิดออเดอร์ใหม่ (pause)' if paused else '🟢 เทรดอยู่'}"
                 " — ลิมิตยังไม่ถูกแตะต้อง")
    lines.append("ระบบจะไม่ขยายลิมิตเองโดยอัตโนมัติ")
    lines.append("")
    lines.append("ยืนยันได้ 2 วิธี")
    lines.append("1) กดปุ่ม Approve / Reject ด้านล่าง")
    lines.append("2) พิมพ์ /dd_ok (อนุมัติ) หรือ /dd_no (ไม่อนุมัติ)")
    lines.append("")
    lines.append(f"⏱ คำขอมีอายุ {float(ttl_min):.0f} นาที · อนุมัติแล้วระบบจะขยายลิมิต "
                 "+ เปิดเทรดต่อ + ประเมิน kill switch ให้ใหม่ทันที")
    return "\n".join(lines)


def build_limit_expand_result(approved: bool, applied: list[dict],
                              remaining: list[dict], kill_engaged: bool,
                              note: str = "") -> str:
    """Post-decision report pushed back to LINE (step 3 of the flow)."""
    def _fmt(items: list[dict]) -> list[str]:
        return [f"• {t.get('label', t.get('trigger', '?'))}: "
                f"{float(t.get('limit', 0)):.2f}% → {float(t.get('new_limit', 0)):.2f}%"
                for t in (items or [])]

    def _fmt_cur(items: list[dict]) -> list[str]:
        return [f"• {t.get('label', t.get('trigger', '?'))}: ลิมิต "
                f"{float(t.get('limit', 0)):.2f}% (ปัจจุบัน {float(t.get('value', 0)):.2f}%)"
                for t in (items or [])]

    if not approved:
        lines = ["❌ ยกเลิกการขยายลิมิต — ลิมิตเดิมไม่ถูกแตะต้อง"]
        lines += _fmt_cur(remaining)
        lines.append("")
        lines.append("สถานะ: 🛑 เทรดยังหยุดอยู่ (pause)")
        if note:
            lines.append(note)
        return "\n".join(lines)

    lines = ["✅ ยืนยันแล้ว — ขยายลิมิตเรียบร้อย"]
    lines += _fmt(applied)
    lines.append("")
    if kill_engaged:
        lines.append("สถานะ: 🛑 ยังไม่เปิดเทรด — kill switch ยังทำงานอยู่")
        if remaining:
            # remaining = breached_triggers() on the MERGED settings, so
            # `limit` is already the new limit and `new_limit` is a further
            # +5pp proposal that must not be quoted here.
            lines.append("ลิมิตที่ยังเกิน: " + ", ".join(
                f"{t.get('label', t.get('trigger'))} "
                f"{float(t.get('value', 0)):.2f}% > {float(t.get('limit', 0)):.2f}%"
                for t in remaining))
        else:
            lines.append("ลิมิตที่ยังเกิน: ตรวจไม่พบ — เกิดจาก infra fail-safe")
        lines.append("✅ อัปเดต + resume แล้ว แต่ระบบ pause กลับเพราะยังประเมินไม่ผ่าน")
    else:
        lines.append("สถานะ: ▶️ เปิดเทรดต่อทันที (pause ยกเลิกแล้ว)")
        lines.append("ประเมินใหม่ (kill switch): 🟢 ปลอดภัย — ไม่มี trigger ค้าง")
    if note:
        lines.append(f"หมายเหตุ: {note}")
    return "\n".join(lines)
