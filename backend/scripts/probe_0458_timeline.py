"""Probe 2: full signal-log timeline 2026-09-06 20:30–23:59 UTC + git gate history."""
import json
import urllib.request

BASE = "https://tdapp-api.onrender.com"
OUT = "_probe_0458b.txt"


def req(path, token=None, method="GET", body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data, method=method)
    if token:
        r.add_header("Authorization", f"Bearer {token}")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    tok = req("/api/auth/login", method="POST", body={"pin": "777777"})
    tok = tok.get("access_token") or tok.get("token")
    lines = []

    raw = req("/api/system/signal-logs?limit=200", token=tok)
    logs = raw.get("logs") or raw.get("items") or []
    lo, hi = "2026-09-06T20:30", "2026-09-06T23:59"
    sel = [l for l in logs if lo <= str(l.get("created_at", ""))[:16] <= hi]
    lines.append(f"events {lo}..{hi}: {len(sel)}")
    for l in sorted(sel, key=lambda x: (str(x.get("created_at")),
                                        str(x.get("asset") or ""))):
        lines.append(
            f"{l.get('created_at', '')[:19]} | {l.get('asset')} "
            f"{l.get('direction')} | {l.get('event'):13s} | "
            f"sid={str(l.get('signal_id'))[:8]} | src={l.get('source')} | "
            f"tk={str(l.get('ticket'))[:14]:14s} | "
            f"{str(l.get('reason'))[:95].replace(chr(10), ' ')}")

    # all order_opened events in the fetched window (any time)
    opened = [l for l in logs if l.get("event") == "order_opened"]
    lines.append("")
    lines.append(f"order_opened events in fetched 200 rows: {len(opened)}")
    for l in sorted(opened, key=lambda x: str(x.get("created_at")))[-15:]:
        lines.append(
            f"{l.get('created_at', '')[:19]} | {l.get('asset')} | "
            f"sid={str(l.get('signal_id'))[:8]} | tk={l.get('ticket')} | "
            f"vol={l.get('volume')} | {str(l.get('reason'))[:70]}")

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"wrote {OUT}: {len(lines)} lines")


if __name__ == "__main__":
    main()
