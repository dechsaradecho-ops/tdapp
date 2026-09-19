"""Pytest config: make `app` importable when running from backend/."""
import os
import sys
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _force_market_open(monkeypatch):
    """Force the FX/gold market OPEN for every test.

    The hard Gate 0b (`execution.market_closed_block`) refuses every new
    order while the market is closed, and the real clock is a weekend for
    ~2 of every 7 days — so without this the whole order suite would pass
    Mon–Fri and fail Sat/Sun. Tests that need the closed behaviour
    monkeypatch `execution.is_market_closed` (or `market_closed_block`)
    themselves, which overrides this fixture.

    The ORIGINAL function is stashed on the module as
    `_real_is_market_closed` so boundary tests can still exercise the real
    weekend-window logic.
    """
    from app.models import schemas
    from app.services import execution

    if not hasattr(schemas, "_real_is_market_closed"):
        schemas._real_is_market_closed = schemas.is_market_closed
    monkeypatch.setattr(schemas, "is_market_closed", lambda now=None: False)
    monkeypatch.setattr(execution, "is_market_closed", lambda now=None: False)


@pytest.fixture(autouse=True)
def _force_liquid_session(monkeypatch):
    """Force the session filter to see a LIQUID (London/NY) session.

    `execution.session_filter_block` (Gate 3b #3) blocks every new order
    while the real clock sits in a low-liquidity window (e.g. Tokyo-only,
    ~00:00–07:00 UTC). That is a WALL-CLOCK dependency exactly like the
    weekend gate above: the order suite would pass during London/NY hours
    and fail the rest of the day. Tests that want the low-liquidity block
    monkeypatch `SessionEngine.active` themselves, which overrides this.

    The ORIGINAL is stashed as `SessionEngine._real_active` so session
    tests can still exercise the real window logic.
    """
    from app.models.schemas import MarketSessionStatus, SessionEngine

    if not hasattr(SessionEngine, "_real_active"):
        SessionEngine._real_active = SessionEngine.active

    def _liquid(cls=None, now=None):
        return MarketSessionStatus(
            active_sessions=["London", "New York"],
            overlapping=True,
            volatility_hint="high",
            current_utc_time="13:00 UTC",
        )

    monkeypatch.setattr(SessionEngine, "active", classmethod(_liquid))
