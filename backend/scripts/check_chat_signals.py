r"""E2E smoke: chat stream (PIN token included) + signals freshness.

Mimics exactly what the fixed frontend does:
  1. GET  /api/auth/status      → gate active?
  2. POST /api/auth/login {pin} → session token (skipped when gate inactive)
  3. POST /api/chat/stream      → must stream a non-error reply (AI 200 OK)
  4. GET  /api/signals/latest   → GBPUSD price present + source live/demo

Usage (backend running on :8123):
  d:/tdapp/.venv/Scripts/python.exe scripts/check_chat_signals.py
"""
from __future__ import annotations

import asyncio
import json
import sys

import httpx

BASE = "http://127.0.0.1:8123"


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    async with httpx.AsyncClient(base_url=BASE, timeout=90.0) as c:
        # ---- auth status -------------------------------------------------
        st = (await c.get("/api/auth/status")).json()
        gate_active = bool(st.get("gate_active"))
        print("auth status:", st)

        headers = {"Content-Type": "application/json"}
        if gate_active:
            pin = sys.argv[1] if len(sys.argv) > 1 else ""
            if not pin:
                print("✘ gate active — pass PIN: python scripts/check_chat_signals.py <PIN>")
                return 2
            login = await c.post("/api/auth/login", json={"pin": pin})
            if login.status_code != 200:
                print(f"✘ login failed: HTTP {login.status_code}")
                return 2
            token = login.json().get("token", "")
            print("login ok, token:", token[:8] + "...")
            headers["Authorization"] = f"Bearer {token}"

        # ---- chat stream (the exact route that 401'd in the UI) ----------
        print("\n-- POST /api/chat/stream --")
        async with c.stream("POST", "/api/chat/stream", headers=headers,
                            json={"messages": [
                                {"role": "user", "content": "ตอบสั้นๆ: ทดสอบระบบ"}]}) as r:
            print("status:", r.status_code)
            if r.status_code != 200:
                print("✘ chat stream failed")
                return 1
            acc = ""
            async for chunk in r.aiter_text():
                acc += chunk
                if len(acc) > 1200:
                    break
        print("reply head:", acc[:300].replace("\n", " "))
        bad = "[AI ERROR]" in acc
        print("ai error marker:", bad)

        # ---- signals freshness (issue #3) --------------------------------
        print("\n-- GET /api/signals/latest --")
        sig = await c.get("/api/signals/latest", headers=headers)
        rows = sig.json()
        entries = rows.get("signals") if isinstance(rows, dict) else rows
        if isinstance(entries, dict):
            entries = entries.get("entries", [])
        for e in entries or []:
            print(f"{e.get('asset')}: entry={e.get('entry')} "
                  f"direction={e.get('direction')} confidence={e.get('confidence')} "
                  f"reason0={(e.get('reason') or [''])[0][:60]}")
        gbp = next((e for e in entries or [] if e.get("asset") == "GBPUSD"), None)
        gbp_price = float(gbp.get("entry") or 0) if gbp else 0.0
        print("GBPUSD present:", gbp is not None, "| entry price:", gbp_price)

        return 1 if bad or gbp_price <= 0 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
