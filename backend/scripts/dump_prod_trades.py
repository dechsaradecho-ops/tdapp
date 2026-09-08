"""Dump production paper_trades + signal_logs for strategy analysis."""
import json
import os
import sys

import httpx

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"
OUT = os.path.join(os.path.dirname(__file__), "prod_trades_dump.json")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/api/auth/login", json={"pin": PIN})
        r.raise_for_status()
        tok = r.json().get("token")
        h = {"Authorization": f"Bearer {tok}"}

        out = {}
        for name, url in [
            ("monitor", "/api/trading/monitor"),
            ("signal_logs", "/api/system/signal-logs?limit=500"),
            ("stats", "/api/trading/paper-trading"),
            ("quality", "/api/trading/quality-report"),
        ]:
            try:
                rr = c.get(url, headers=h)
                print(name, rr.status_code)
                out[name] = rr.json() if rr.status_code == 200 else {"_status": rr.status_code, "_text": rr.text[:300]}
            except Exception as e:  # noqa: BLE001
                out[name] = {"_error": str(e)}

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print("saved ->", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
