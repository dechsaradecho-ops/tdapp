"""One-shot prod verification after migrations 023/024/025.

Round-trip: set min_confidence_gold=65 via PUT (full payload like the frontend
sends), GET to confirm persistence, then clear back to null and confirm the
clearing path works too. Net effect: settings unchanged.
"""
import json
import os

import httpx

BASE = "https://tdapp-api.onrender.com"
PIN = "777777"


def login(c: httpx.Client) -> dict:
    r = c.post("/api/auth/login", json={"pin": PIN})
    r.raise_for_status()
    tok = r.json().get("token")
    assert tok, f"no token: {r.text[:200]}"
    return {"Authorization": f"Bearer {tok}"}


def get_settings(c: httpx.Client, h: dict) -> dict:
    r = c.get("/api/settings", headers=h)
    r.raise_for_status()
    return r.json()


def put_settings(c: httpx.Client, h: dict, payload: dict) -> dict:
    r = c.put("/api/settings", headers=h, json=payload)
    print("PUT status:", r.status_code, "| msg:", r.json().get("message", "")[:120])
    r.raise_for_status()
    return r.json()


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=60) as c:
        h = login(c)
        s0 = get_settings(c, h)
        print("before  min_confidence_gold:", s0.get("min_confidence_gold"),
              "| sl clamp:", s0.get("sl_distance_min_pct"), "/", s0.get("sl_distance_max_pct"),
              "| max_hold_days:", s0.get("max_hold_days"),
              "| gold_breakout_only:", s0.get("gold_breakout_only"))

        # 1) save with value (full payload, exactly like frontend save())
        p1 = dict(s0)
        p1["min_confidence_gold"] = 65
        put_settings(c, h, p1)
        s1 = get_settings(c, h)
        ok1 = s1.get("min_confidence_gold") == 65
        print("after set  min_confidence_gold:", s1.get("min_confidence_gold"), "->", "OK" if ok1 else "FAIL")

        # 2) clear back to null (explicit null clearing path)
        p2 = dict(s1)
        p2["min_confidence_gold"] = None
        put_settings(c, h, p2)
        s2 = get_settings(c, h)
        ok2 = s2.get("min_confidence_gold") is None
        print("after clear min_confidence_gold:", s2.get("min_confidence_gold"), "->", "OK" if ok2 else "FAIL")

        ok = ok1 and ok2
        print("ROUND-TRIP:", "PASS" if ok else "FAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
