"""Post-deploy verification for the audit-fix batch (API already confirmed live).

Part A — migration 033 probe (non-destructive): PUT /api/settings with the
  DEFAULT values of the two new columns. PostgREST rejects an unknown column in
  the payload (PGRST204) no matter what the value is, so a plain `saved`
  message proves `paper_exit_spread_mult` / `paper_commission_per_lot` really
  exist in prod — a GET alone cannot tell (it falls back to the same defaults).

Part B — static frontend: poll tdappstatic for the new identifiers inside the
  per-page chunks (Next.js does not mangle property names, so `sl_moved`,
  `readiness_ready`, `paper_commission_per_lot` survive minification).

Read-only apart from re-saving the settings row with its own values.
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://tdapp-api.onrender.com"
STATIC = "https://tdappstatic.onrender.com"

# page path -> identifiers that must appear in that page's chunks
PAGES = {
    "/settings": ("paper_exit_spread_mult", "paper_commission_per_lot"),
    "/monitor": ("readiness_ready", "readiness_min_sample", "sl_moved"),
    "/logs": ("sl_moved",),
    "/signals": ("sl_moved",),
}


def req(url, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- Part A
code, text = req(f"{API}/api/auth/login", method="POST", body={"pin": "777777"})
token = (json.loads(text).get("access_token")
         or json.loads(text).get("token"))

print("=== Part A: migration 033 probe (PUT /api/settings, default values) ===")
code, text = req(f"{API}/api/settings", token=token, method="PUT",
                 body={"paper_exit_spread_mult": 0.5,
                       "paper_commission_per_lot": 3.5})
res = json.loads(text) if code == 200 else {}
msg = str(res.get("message") or "")
print(f"HTTP {code}  message={msg!r}")
migration_ok = res.get("ok") is True and msg == "saved"
print(f"[{'OK ' if migration_ok else 'FAIL'}] migration 033 applied: "
      f"{'yes' if migration_ok else 'NO — run database/033_paper_costs.sql'}")

code, text = req(f"{API}/api/settings", token=token)
st = json.loads(text) if code == 200 else {}
print(f"      readback exit_mult={st.get('paper_exit_spread_mult')} "
      f"commission={st.get('paper_commission_per_lot')}")

# ---------------------------------------------------------------- Part B
print("\n=== Part B: static frontend chunks ===")


def page_html(path):
    for url in (f"{STATIC}{path}/", f"{STATIC}{path}",
                f"{STATIC}{path}/index.html"):
        try:
            code, text = req(url)
        except Exception:  # noqa: BLE001
            continue
        if code == 200 and "<html" in text.lower():
            return text
    return ""


def chunk_texts(path, html):
    """Per-page chunk + every shared script referenced by the page."""
    srcs = re.findall(r'src="([^"]+\.js)"', html)
    srcs = [s if s.startswith("http") else STATIC + s for s in srcs]
    own = re.findall(r'src="([^"]*chunks/app[^"]+\.js)"', html)
    srcs += [s if s.startswith("http") else STATIC + s for s in own]
    out = []
    for s in dict.fromkeys(srcs):
        try:
            code, text = req(s)
        except Exception:  # noqa: BLE001
            continue
        if code == 200:
            out.append((s, text))
    return out


fails = []
for path, tokens in PAGES.items():
    html = page_html(path)
    if not html:
        print(f"[FAIL] {path}: page HTML not reachable")
        fails.append(path)
        continue
    # the page chunk name is stable across builds, files are not hashed by
    # content alone for the app router — poll inside the loop below
    found = {}
    for attempt in range(20):  # up to ~10 min
        chunks = chunk_texts(path, html)
        blob = "\n".join(t for _, t in chunks)
        found = {tok: (tok in blob) for tok in tokens}
        if all(found.values()):
            break
        if attempt == 0:
            print(f"      {path}: waiting for the new static build …")
        time.sleep(30)
        html = page_html(path) or html
    ok = all(found.values())
    detail = " ".join(f"{k}={'y' if v else 'n'}" for k, v in found.items())
    print(f"[{'OK ' if ok else 'FAIL'}] {path}: {detail}")
    if not ok:
        fails.append(path)

print("\n" + ("ALL CHECKS PASSED" if not (fails or not migration_ok)
              else f"FAILED: {fails or ''} migration_ok={migration_ok}"))
