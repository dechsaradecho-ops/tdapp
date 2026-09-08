"""Fetch prod settings + counts to check kill-switch limits."""
import json
import os

import httpx

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"
OUT = os.path.join(os.path.dirname(__file__), "prod_settings_dump.json")


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        r = c.post("/api/auth/login", json={"pin": PIN})
        tok = r.json().get("token")
        h = {"Authorization": f"Bearer {tok}"}
        out = {}
        for name, url in [("settings", "/api/settings"), ("counts", "/api/system/counts")]:
            rr = c.get(url, headers=h)
            print(name, rr.status_code)
            out[name] = rr.json() if rr.status_code == 200 else {"_status": rr.status_code}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    s = out["settings"]
    if isinstance(s, dict):
        for k in ("capital", "risk_per_trade_pct", "kill_daily_loss_pct", "kill_weekly_loss_pct",
                  "kill_monthly_loss_pct", "max_drawdown_pct", "drawdown_throttle_pct",
                  "order_mode", "max_open_positions", "max_trades_daily", "min_confidence",
                  "min_confidence_gold", "min_opportunity", "sl_distance_mode"):
            print(f"{k}: {s.get(k)}")
    print("saved ->", OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
