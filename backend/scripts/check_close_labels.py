"""Show the newest `closed` signal_log reasons on prod — proves the new
truthful labels (TP / breakeven / trailing / SL) are written in live traffic.
"""
import json
import sys
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

r = urllib.request.Request(
    "https://tdapp-api.onrender.com/api/auth/login",
    data=json.dumps({"pin": "777777"}).encode(), method="POST")
r.add_header("Content-Type", "application/json")
token = json.loads(urllib.request.urlopen(r, timeout=60).read())["token"]

for event in ("closed", "order_opened"):
    q = urllib.request.Request(
        f"https://tdapp-api.onrender.com/api/system/signal-logs?days=2&event={event}&limit=15")
    q.add_header("Authorization", f"Bearer {token}")
    data = json.loads(urllib.request.urlopen(q, timeout=90).read())
    rows = data.get("logs") or []
    print(f"\n=== {event} — {len(rows)} shown of {data.get('total')} ===")
    for row in rows[:10]:
        print(f"{row.get('created_at', '')[:19]} {row.get('asset'):<7} "
              f"pnl={row.get('pnl')} | {row.get('reason')}")
