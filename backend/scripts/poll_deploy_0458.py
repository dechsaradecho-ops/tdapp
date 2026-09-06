"""Poll prod signal-logs until the next 'expired' event appears, then check
its wording: NEW deploy says "สัญญาณนี้ pending เกิน 30 นาที — หมดอายุ
ไม่ได้ใช้เปิดออเดอร์" (commit 8ebb98d); OLD deploy says "pending เกิน 30
นาที — ไม่ได้เปิดออเดอร์". Signals pending since 22:32/22:37 UTC expire at
~23:02/23:07 UTC, so this resolves within minutes of a fresh deploy.
"""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"
OUT = __file__.replace("poll_deploy_0458.py", "_poll_0458.txt")
NEW_MARK = "หมดอายุ ไม่ได้ใช้เปิดออเดอร์"


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

# baseline: newest expired event before we start watching
code, text = req("/api/system/signal-logs?limit=30", token=token)
rows = json.loads(text).get("logs", [])
baseline = next((r.get("created_at") for r in rows if r.get("event") == "expired"),
                None)
print(f"baseline newest expired: {baseline}")

verdict = "TIMEOUT"
for attempt in range(20):  # up to ~10 min
    time.sleep(30)
    code, text = req("/api/system/signal-logs?limit=30", token=token)
    rows = json.loads(text).get("logs", [])
    fresh = [r for r in rows if r.get("event") == "expired"
             and r.get("created_at") and r.get("created_at") != baseline]
    if fresh:
        newest = fresh[0]
        reason = str(newest.get("reason") or "")
        verdict = "NEW_DEPLOY_LIVE" if NEW_MARK in reason else "STILL_OLD_DEPLOY"
        lines = [verdict,
                 f"expired @ {newest.get('created_at')} asset={newest.get('asset')}",
                 f"reason: {reason}"]
        with open(OUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(verdict)
        break
    print(f"attempt {attempt + 1}: no fresh expired yet, waiting 30s")
else:
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("TIMEOUT — no fresh expired event in ~10 min")
    print("TIMEOUT")
