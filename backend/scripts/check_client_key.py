r"""Offline smoke-test: does the installed supabase-py accept the NEW sb_secret_ key format?

Root cause this guards against:
  supabase-py 2.9.1 validates the key with a JWT-only regex inside create_client
      re.match(r"^[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*$", key)
  and raises SupabaseException("Invalid API key") BEFORE any network call.
  So a perfectly valid sb_secret_ key (verified via check_supabase.py) still
  fails on Render while the old library version is pinned.

This script needs NO network and NO real key — it uses a fake key and only
exercises the constructor + query-builder chain used by app/services/database.py.

Usage:
  C:/Python314/python.exe d:/tdapp/backend/scripts/check_client_key.py

Exit codes: 0 = OK, 2 = sb_secret_ rejected (old lib), 3 = JWT rejected,
            4 = order(desc=) incompatible, 1 = unexpected.
"""
from __future__ import annotations

import sys


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        import supabase
        from supabase import create_client
    except ImportError as exc:
        print(f"✘ supabase package not installed: {exc}")
        return 1

    ver = getattr(supabase, "__version__", "?")
    print(f"supabase-py version: {ver}")

    # 1) new API key format must be accepted (no network involved)
    try:
        client = create_client(
            "https://example.supabase.co", "sb_secret_unit_test_token_0123456789abcdef"
        )
    except Exception as exc:
        print(f"✘ create_client REJECTED sb_secret_ key: {exc}")
        print("  → supabase-py too old for the new key format; bump supabase in requirements.txt")
        return 2
    print("✔ create_client accepts sb_secret_ key")

    # 2) legacy JWT key must still be accepted
    try:
        create_client(
            "https://example.supabase.co", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123"
        )
        print("✔ create_client accepts legacy JWT key")
    except Exception as exc:
        print(f"✘ legacy JWT key rejected: {exc}")
        return 3

    # 3) query-builder chain used by app/services/database.py must not have changed shape
    try:
        (client.table("signals").select("*")
         .eq("asset", "EURUSD")
         .order("created_at", desc=True)
         .limit(50))
        print("✔ query chain ok: select / eq / order(desc=True) / limit")
    except TypeError as exc:
        print(f"⚠ order(desc=...) incompatible with this postgrest version: {exc}")
        print("  → adapt app/services/database.py select() ordering")
        return 4

    print("\n✔ All good — this supabase-py version works with database.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
