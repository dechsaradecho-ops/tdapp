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


class LineClient:
    def __init__(self, access_token: Optional[str] = None) -> None:
        s = get_settings()
        self.token = access_token or s.line_channel_access_token
        self.enabled = bool(self.token)

    # ------------------------------------------------------------------
    async def push(self, line_user_id: str, text: str) -> bool:
        if not self.enabled:
            log.info("LINE disabled — would push to %s: %s", line_user_id, text[:80])
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API}/push",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json={"to": line_user_id,
                      "messages": [{"type": "text", "text": text}]},
            )
            ok = resp.status_code == 200
            if not ok:
                log.warning("LINE push failed %s: %s", resp.status_code, resp.text[:200])
            return ok

    async def reply(self, reply_token: str, text: str) -> bool:
        if not self.enabled:
            return False
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{API}/reply",
                headers={"Authorization": f"Bearer {self.token}",
                         "Content-Type": "application/json"},
                json={"replyToken": reply_token,
                      "messages": [{"type": "text", "text": text}]},
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
