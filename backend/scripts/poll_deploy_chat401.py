"""Poll prod static site until the layout bundle changes (new deploy live)."""
import re
import time
import urllib.request

URL = "https://tdappstatic.onrender.com/"
OUT = __file__.replace("poll_deploy_chat401.py", "_poll_chat401.txt")


def chunks():
    with urllib.request.urlopen(urllib.request.Request(URL, headers={"Cache-Control": "no-cache"}), timeout=30) as r:
        html = r.read().decode()
    return set(re.findall(r"static/chunks/[\w.-]+\.js", html))


base = chunks()
print(f"baseline: {len(base)} chunks")
verdict = "TIMEOUT"
for attempt in range(30):  # up to ~15 min
    time.sleep(30)
    try:
        now = chunks()
    except Exception as e:
        print(f"attempt {attempt + 1}: fetch error {e}")
        continue
    added = now - base
    if added:
        verdict = f"DEPLOY_LIVE — new chunks: {sorted(added)[:5]}"
        break
    print(f"attempt {attempt + 1}: unchanged, waiting 30s")

with open(OUT, "w", encoding="utf-8") as f:
    f.write(verdict)
print(verdict)
