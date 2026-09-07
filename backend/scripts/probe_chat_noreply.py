"""Probe prod AI chat: /health provider status + live /api/chat + /api/chat/stream."""
import json
import time
import urllib.request

BASE = "https://tdapp-api.onrender.com"
OUT = __file__.replace("probe_chat_noreply.py", "_probe_chat.txt")


def req(path, token=None, method="GET", body=None, timeout=150):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()
    except Exception as e:
        return 0, str(e)


lines = []
code, text = req("/health")
lines.append(f"health {code}: {text[:500]}")

code, text = req("/api/auth/login", method="POST", body={"pin": "777777"})
assert code == 200, f"login failed: {code} {text}"
token = json.loads(text).get("access_token") or json.loads(text).get("token")

t0 = time.time()
code, text = req("/api/chat", token=token, method="POST",
                 body={"messages": [{"role": "user", "content": "สวัสดี สรุปสถานะตลาดสั้น ๆ"}]})
lines.append(f"--- /api/chat {code} in {time.time()-t0:.1f}s ---")
lines.append(text[:1500])

t0 = time.time()
code, text = req("/api/chat/stream", token=token, method="POST",
                 body={"messages": [{"role": "user", "content": "ทองตอนนี้เป็นอย่างไร ตอบสั้น ๆ"}]})
lines.append(f"--- /api/chat/stream {code} in {time.time()-t0:.1f}s ---")
lines.append(text[:1500])

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("DONE")
