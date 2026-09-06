"""Fetch newest expired/order_opened events from prod signal log."""
import json
import urllib.request

BASE = "https://tdapp-api.onrender.com"
OUT = __file__.replace("check_expired_now.py", "_check_expired.txt")


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

code, text = req("/api/system/signal-logs?limit=40", token=token)
rows = json.loads(text).get("logs", [])
with open(OUT, "w", encoding="utf-8") as f:
    for x in rows:
        if x.get("event") in ("expired", "order_opened"):
            f.write(f"{x['created_at']} {x['event']} {x.get('asset')} "
                    f"ticket={x.get('ticket')} | {x.get('reason')}\n")
print("DONE")
