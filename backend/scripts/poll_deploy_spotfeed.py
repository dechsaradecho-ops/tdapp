"""Poll prod until the spot-feed deploy is live, then dump monitor + feed_status."""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"
OUT = __file__.replace("poll_deploy_spotfeed.py", "monitor_verify_report.txt")


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

for attempt in range(16):
    code, text = req("/api/trading/monitor", token=token)
    snap = json.loads(text)
    ops = snap.get("open_positions", [])
    # new deploy = EURUSD no longer pinned to its ECB daily-close entry 1.1615
    eur = next((p for p in ops if p.get("asset") == "EURUSD"), None)
    if eur and abs((eur.get("current_price") or 0) - (eur.get("entry_price") or 0)) > 1e-9:
        lines = ["DEPLOY_LIVE", f"HTTP {code}"]
        for p in ops:
            lines.append(f"{p['asset']} entry={p['entry_price']} "
                         f"current={p['current_price']} uPnL={p['unrealized_pnl']}")
        fs = snap.get("feed_status")
        lines.append(f"feed_status: state={fs.get('state') if fs else None} "
                     f"failed={fs.get('failed_assets') if fs else None} "
                     f"msg={fs.get('message') if fs else None}")
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print("DEPLOY_LIVE")
        break
    print(f"attempt {attempt + 1}: spot feed not live yet, waiting 30s")
    time.sleep(30)
else:
    print("TIMEOUT: spot feed not detected in ~8 min")
