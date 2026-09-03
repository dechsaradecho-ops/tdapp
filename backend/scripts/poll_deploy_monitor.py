"""Poll prod /monitor until the new deploy (live marks) is live."""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return resp.status, resp.read().decode()


code, text = req("/api/auth/login", method="POST", body={"pin": "777777"})
token = json.loads(text).get("access_token") or json.loads(text).get("token")

for attempt in range(16):  # up to ~8 min
    code, text = req("/api/trading/monitor", token=token)
    snap = json.loads(text)
    ops = snap.get("open_positions", [])
    gbp = next((p for p in ops if p.get("asset") == "GBPUSD"), None)
    # contract-size PnL fix live when GBPUSD (entry 1.26797, mark ~1.35,
    # 0.01 lots) shows uPnL != 0 → old deploys pinned it at 0.0
    if gbp and abs(gbp.get("unrealized_pnl") or 0) > 0.005:
        lines = ["DEPLOY_LIVE", f"HTTP {code}"]
        for p in ops:
            diff = (p.get("current_price") or 0) - (p.get("entry_price") or 0)
            tag = "LIVE-MARK" if abs(diff) > 1e-9 else "equals-entry (daily rate unchanged)"
            lines.append(f"{p['asset']} entry={p['entry_price']} "
                         f"current={p['current_price']} uPnL={p['unrealized_pnl']} [{tag}]")
        with open(__file__.replace("poll_deploy_monitor.py", "monitor_verify_report.txt"),
                  "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("DEPLOY_LIVE")
        break
    print(f"attempt {attempt + 1}: still old deploy (GBPUSD pinned), waiting 30s")
    time.sleep(30)
else:
    print("TIMEOUT: deploy not detected in ~8 min")
