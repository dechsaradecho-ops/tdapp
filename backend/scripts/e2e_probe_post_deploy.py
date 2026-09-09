r"""Post-deploy E2E probe: quotes -> scanner -> signal -> LINE dry-run.

Runs against the LIVE backend (default: local, pass a base URL + PIN for
prod). Verifies the whole pipeline still flows after a deploy:

  1. quotes      GET  /api/system/quote-logs   (recent feed fetches OK?)
  2. market      GET  /api/market/summary      (worker rows or live quotes?)
  3. scanner     POST /api/system/scan-now     (cycle inserts market_analysis?)
  4. signal      GET  /api/signals/latest      (proposals served? entry live?)
  5. risk        POST /api/risk/check          (limits = DB row, not ENV?)
  6. LINE dry-run POST /api/line/simulate      (bot pipeline answers, no push)
  7. trade path  POST /api/system/autotrader-dry-run (gates evaluated?)

Read-only EXCEPT scan-now + autotrader-dry-run (both may insert rows into
market_analysis / journal — same as the scheduler would; no real orders,
no LINE pushes — simulate never pushes unless push_reply_to is set, and
the probe never sets it).

Usage:
  d:/tdapp/.venv/Scripts/python.exe scripts/e2e_probe_post_deploy.py [base] [pin]
  d:/tdapp/.venv/Scripts/python.exe scripts/e2e_probe_post_deploy.py https://tdapp-api.onrender.com 777777

Exit 0 = all stages pass, 1 = any stage fails. Writes a report next to
this script (e2e_probe_report.txt).
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

import httpx

DEFAULT_BASE = "http://127.0.0.1:8123"

lines: list[str] = []
failures = 0


def log(ok: bool, stage: str, detail: str = "") -> None:
    global failures
    mark = "PASS" if ok else "FAIL"
    if not ok:
        failures += 1
    lines.append(f"[{mark}] {stage}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    pin = sys.argv[2] if len(sys.argv) > 2 else "123456"

    async with httpx.AsyncClient(base_url=base, timeout=120.0) as c:
        # ---- 0. health + login ---------------------------------------
        try:
            h = await c.get("/health")
            log(h.status_code == 200, "health",
                f"HTTP {h.status_code} workers={h.json().get('workers')}")
        except Exception as exc:
            log(False, "health", f"{exc.__class__.__name__}: {exc}")
            return _finish()

        st = (await c.get("/api/auth/status")).json()
        token = None
        if st.get("pin_set"):
            login = await c.post("/api/auth/login", json={"pin": pin})
            token = (login.json() or {}).get("token")
            log(login.status_code == 200 and bool(token), "auth/login",
                "token issued" if token else f"HTTP {login.status_code}")
        else:
            sp = await c.post("/api/auth/set-pin", json={"pin": pin})
            token = (sp.json() or {}).get("token")
            log(True, "auth/set-pin (bootstrap, no PIN was set)", "")
        if not token:
            log(False, "auth", "no token — cannot probe authenticated stages")
            return _finish()
        hdr = {"Authorization": f"Bearer {token}"}

        # ---- 1. quotes: recent feed fetches --------------------------
        try:
            q = (await c.get("/api/system/quote-logs?limit=20", headers=hdr)).json()
            rows = q.get("logs", q if isinstance(q, list) else [])
            ok_rows = [r for r in rows
                       if str(r.get("status", "ok")).lower() == "ok"]
            log(bool(rows), "quotes/quote-logs",
                f"{len(ok_rows)}/{len(rows)} ok in last 20 fetches")
        except Exception as exc:
            log(False, "quotes/quote-logs", f"{exc.__class__.__name__}: {exc}")

        # ---- 2. market summary ---------------------------------------
        try:
            m = (await c.get("/api/market/summary", headers=hdr)).json()
            opps = m.get("opportunities", [])
            srcs = {o.get("asset"): o.get("source", "?") for o in opps}
            live = sum(1 for v in srcs.values() if v == "live")
            log(bool(opps), "market/summary",
                f"{len(opps)} assets, {live} live "
                f"regime={m.get('regime')}")
        except Exception as exc:
            log(False, "market/summary", f"{exc.__class__.__name__}: {exc}")

        # ---- 3. scanner cycle ----------------------------------------
        try:
            s = await c.post("/api/system/scan-now", headers=hdr)
            body = s.json()
            log(s.status_code == 200 and body.get("verdict") == "ok",
                "system/scan-now",
                f"scanned={body.get('scanned')} "
                f"market_analysis_rows={body.get('market_analysis_rows')}")
        except Exception as exc:
            log(False, "system/scan-now", f"{exc.__class__.__name__}: {exc}")

        # ---- 4. signals ----------------------------------------------
        try:
            sigs = (await c.get("/api/signals/latest", headers=hdr)).json()
            first = sigs[0] if isinstance(sigs, list) and sigs else {}
            live_price = first.get("live_price")
            log(isinstance(sigs, list), "signals/latest",
                f"{len(sigs) if isinstance(sigs, list) else '?'} proposals; "
                f"first={first.get('asset')} {first.get('direction')} "
                f"entry={first.get('entry')} live={live_price}")
        except Exception as exc:
            log(False, "signals/latest", f"{exc.__class__.__name__}: {exc}")

        # ---- 5. risk check uses DB row --------------------------------
        try:
            cfg = (await c.get("/api/settings", headers=hdr)).json()
            db_daily = cfg.get("kill_daily_loss_pct")
            r = await c.post("/api/risk/check", headers=hdr, json={
                "starting_capital": 10000, "peak_equity": 10000,
                "current_equity": 10000})
            body = r.json()
            log(r.status_code == 200
                and body.get("daily_loss_limit") == db_daily,
                "risk/check reads DB row",
                f"limit={body.get('daily_loss_limit')} "
                f"(settings row={db_daily})")
        except Exception as exc:
            log(False, "risk/check reads DB row",
                f"{exc.__class__.__name__}: {exc}")

        # ---- 6. LINE dry-run (no push) --------------------------------
        try:
            sim = await c.post("/api/line/simulate", headers=hdr, json={
                "text": "/risk", "source_type": "user"})
            body = sim.json()
            log(sim.status_code == 200 and body.get("ok") is True
                and not body.get("pushed"),
                "line/simulate dry-run",
                f"via={body.get('via')} reply={str(body.get('reply'))[:60]}")
        except Exception as exc:
            log(False, "line/simulate dry-run",
                f"{exc.__class__.__name__}: {exc}")

        # ---- 7. autotrader dry-run (gates evaluated, maybe opens paper) -
        try:
            d = await c.post("/api/system/autotrader-dry-run",
                             headers=hdr, json={})
            body = d.json()
            log(d.status_code == 200 and body.get("verdict") == "ok",
                "system/autotrader-dry-run",
                f"pending={len(body.get('pending_signals', []))} "
                f"open={body.get('open_paper_trades')} "
                f"mode={body.get('order_mode')}")
        except Exception as exc:
            log(False, "system/autotrader-dry-run",
                f"{exc.__class__.__name__}: {exc}")

    return _finish()


def _finish() -> int:
    report = "\n".join(lines)
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "e2e_probe_report.txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(report + "\n")
    print(report)
    print(f"\n{'ALL STAGES PASS' if failures == 0 else f'{failures} STAGE(S) FAILED'}"
          f" — report: e2e_probe_report.txt")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
