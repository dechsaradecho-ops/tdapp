r"""Hit EVERY API endpoint against a running backend and report pass/fail.

Covers all 37 routes (health/ping, auth, goal, market, portfolio, risk,
signals, chat, ai, line webhook, system, trading, settings).

Usage (backend running):
  d:/tdapp/.venv/Scripts/python.exe scripts/check_all_apis.py [base] [pin]

Defaults: base=http://127.0.0.1:8123
Auth: when the PIN gate is active the script logs in with <pin> (or creates
one via set-pin when no PIN exists) and carries the token on every request.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

DEFAULT_BASE = "http://127.0.0.1:8123"

results: list[tuple[str, str, int, str]] = []  # (name, method, status, verdict)


async def call(c: httpx.AsyncClient, name: str, method: str, path: str,
               token: str | None = None, **kw) -> dict | list | None:
    headers = kw.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = await c.request(method, path, headers=headers, **kw)
    except Exception as exc:
        results.append((name, method, 0, f"EXC {exc.__class__.__name__}"))
        return None
    ok = 200 <= r.status_code < 300
    results.append((name, method, r.status_code, "OK" if ok else "FAIL"))
    try:
        return r.json()
    except Exception:
        return None


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    pin_arg = sys.argv[2] if len(sys.argv) > 2 else "123456"

    async with httpx.AsyncClient(base_url=base, timeout=90.0) as c:
        # ---- no-auth tier -------------------------------------------------
        await call(c, "ping", "GET", "/ping")
        health = await call(c, "health", "GET", "/health")
        print("health:", json.dumps(health, ensure_ascii=False)[:200])

        # ---- auth ----------------------------------------------------------
        st = await call(c, "auth/status", "GET", "/api/auth/status")
        token = None
        if st and st.get("pin_set"):
            login = await call(c, "auth/login", "POST", "/api/auth/login",
                               json={"pin": pin_arg})
            token = (login or {}).get("token")
        elif st:
            sp = await call(c, "auth/set-pin", "POST", "/api/auth/set-pin",
                            json={"pin": pin_arg})
            token = (sp or {}).get("token")
        print("token:", (token or "(none)")[:8])

        # ---- goal ----------------------------------------------------------
        await call(c, "goal/assess", "POST", "/api/goal/assess", token, json={
            "capital": 100000, "target_return_pct": 3.0,
            "risk_profile": "moderate", "max_drawdown_pct": 10.0,
            "trading_mode": "manual"})

        # ---- market / signals / chat --------------------------------------
        await call(c, "market/summary", "GET", "/api/market/summary", token)
        sigs = await call(c, "signals/latest", "GET", "/api/signals/latest", token)
        await call(c, "chat", "POST", "/api/chat", token, json={
            "messages": [{"role": "user", "content": "ตอบสั้นๆ ว่า ทดสอบ"}]})
        await call(c, "chat/stream", "POST", "/api/chat/stream", token, json={
            "messages": [{"role": "user", "content": "ตอบสั้นๆ ว่า ทดสอบ"}]})

        # ---- ai/explain ------------------------------------------------------
        entry, sl, tp = 1.10, 1.095, 1.11
        if isinstance(sigs, list) and sigs:
            e0 = sigs[0]
            entry, sl, tp = e0.get("entry", entry), e0.get("stop_loss", sl), e0.get("take_profit", tp)
        await call(c, "ai/explain", "POST", "/api/ai/explain", token, json={
            "asset": "EURUSD", "direction": "BUY", "entry": entry,
            "stop_loss": sl, "take_profit": tp, "opportunity_score": 70.0})

        # ---- portfolio / risk ------------------------------------------------
        await call(c, "portfolio/recommend", "POST", "/api/portfolio/recommend",
                   token, json={"capital": 10000, "target_return_pct": 3.0,
                                "max_drawdown_pct": 10.0, "risk_profile": "moderate"})
        await call(c, "risk/check", "POST", "/api/risk/check", token, json={
            "starting_capital": 10000, "peak_equity": 10500,
            "current_equity": 10100})

        # ---- trading (read-mostly) ------------------------------------------
        await call(c, "trading/frequency", "GET", "/api/trading/frequency", token)
        await call(c, "trading/order-plan", "POST", "/api/trading/order-plan",
                   token, json={"asset": "EURUSD", "direction": "BUY",
                                "entry": entry, "stop_loss": sl, "take_profit": tp})
        await call(c, "trading/correlation", "GET", "/api/trading/correlation", token)
        await call(c, "trading/calendar", "GET", "/api/trading/calendar", token)
        await call(c, "trading/session", "GET", "/api/trading/session", token)
        await call(c, "trading/kill-switch", "GET", "/api/trading/kill-switch", token)
        await call(c, "trading/risk-officer", "POST", "/api/trading/risk-officer",
                   token, json={"confidence": 70.0, "opportunity_score": 70.0})
        pause_before = await call(c, "trading/pause(GET)", "GET", "/api/trading/pause", token)
        await call(c, "trading/pause(POST)", "POST", "/api/trading/pause", token,
                   json={"paused": bool((pause_before or {}).get("paused")), "reason": "api-test"})
        await call(c, "trading/monitor", "GET", "/api/trading/monitor", token)
        await call(c, "trading/journal(GET)", "GET", "/api/trading/journal", token)
        await call(c, "trading/journal(POST)", "POST", "/api/trading/journal", token, json={
            "asset": "EURUSD", "direction": "BUY", "entry_price": entry,
            "exit_price": entry * 1.002, "holding_time_min": 45, "pnl": 12.5,
            "rr_ratio": 1.4, "market_regime": "bull_trend",
            "opportunity_score": 70.0, "ai_explanation": "api smoke"})
        await call(c, "trading/backtest", "POST", "/api/trading/backtest", token, json={
            "asset": "EURUSD", "indicator": "EMA", "days": 120,
            "initial_capital": 10000, "risk_per_trade_pct": 1.0})
        await call(c, "trading/walk-forward", "POST", "/api/trading/walk-forward",
                   token, json={"asset": "EURUSD", "indicator": "EMA", "days": 120})
        await call(c, "trading/paper-trading", "GET", "/api/trading/paper-trading", token)
        await call(c, "trading/extended-analysis", "GET", "/api/trading/extended-analysis", token)

        # ---- signals approve (rejected → no side effects) --------------------
        if isinstance(sigs, list) and sigs:
            sid = (sigs[0].get("id") or "")
            if sid:
                await call(c, "signals/approve(reject)", "POST", "/api/signals/approve",
                           token, json={"signal_id": sid, "approve": False})

        # ---- settings ---------------------------------------------------------
        cur = await call(c, "settings(GET)", "GET", "/api/settings", token)
        await call(c, "settings(PUT)", "PUT", "/api/settings", token,
                   json=cur if isinstance(cur, dict) else {})
        await call(c, "settings/reset", "POST", "/api/settings/reset", token)

        # ---- system -----------------------------------------------------------
        await call(c, "system/db-check", "GET", "/api/system/db-check", token)
        await call(c, "system/counts", "GET", "/api/system/counts", token)
        await call(c, "system/scan-now", "POST", "/api/system/scan-now", token)
        await call(c, "system/autotrader-dry-run", "POST", "/api/system/autotrader-dry-run", token)

        # ---- line webhook (LINE signature missing → 400/401 expected-fail) ----
        await call(c, "line/webhook", "POST", "/api/line/webhook", json={"events": []})

        # ---- logout last --------------------------------------------------------
        if token:
            await call(c, "auth/logout", "POST", "/api/auth/logout", token)

    # ---- report -----------------------------------------------------------
    lines = ["================ RESULTS ================"]
    fails = 0
    for name, method, status, verdict in results:
        mark = "PASS" if verdict == "OK" else "FAIL"
        if verdict != "OK":
            fails += 1
        lines.append(f"{mark} {name:<26} {method:<6} {status}")
    total = len(results)
    lines.append(f"\n{total - fails}/{total} passed")
    report = "\n".join(lines)
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "api_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(report)
    print(f"report: {report_path}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
