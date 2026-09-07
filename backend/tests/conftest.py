"""Pytest config: make `app` importable when running from backend/."""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Tests must not depend on the developer's local .env — several suites use
# Origin http://localhost:3000 (the config default) for CORS assertions.
# Force the default BEFORE any test module imports app.main (which builds
# the CORS middleware from settings at import time).
os.environ["FRONTEND_ORIGINS"] = "http://localhost:3000"

# Tests must never touch the developer's real Supabase/LINE accounts —
# Database()/LineClient() are designed to no-op when creds are missing, but
# backend/.env may now carry real credentials. Empty-string env vars override
# dotenv values (pydantic-settings: env > .env file, env_ignore_empty=False),
# restoring the no-op assumption the whole suite was written against.
for _k in ("SUPABASE_URL", "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_KEY",
           "SUPABASE_ANON_KEY", "SUPABASE_PUBLISHABLE_KEY",
           "LINE_CHANNEL_ACCESS_TOKEN", "LINE_CHANNEL_SECRET"):
    os.environ[_k] = ""
