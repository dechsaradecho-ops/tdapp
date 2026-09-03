"""PIN auth tests — hashing, lockout, sessions, and the HTTP gate.

Covers the user-facing contract:
  - 6-digit PIN only
  - 5 wrong attempts → 15-minute lockout (survives across calls)
  - correct PIN after lockout window resets → works again
  - every /api/* route 401s without a token once a PIN is set
  - bootstrap: with NO pin set, everything stays open
"""
from __future__ import annotations

import httpx
import pytest
from datetime import datetime, timedelta, timezone

from app.main import app
from app.services import pin_auth
from tests.test_workers import FakeDatabase


def auth_db(pin: str | None = "123456", failed: int = 0, locked_until: str | None = None) -> FakeDatabase:
    salt = pin_auth.new_salt()
    row = {
        "id": 1,
        "pin_hash": pin_auth.hash_pin(pin, salt) if pin else "",
        "salt": salt,
        "failed_attempts": failed,
        "locked_until": locked_until,
        "pin_set_at": None,
    }
    return FakeDatabase(rows={"app_auth": [row]})


@pytest.fixture(autouse=True)
def clean_sessions():
    pin_auth.reset_gate_cache()
    pin_auth._sessions.clear()
    yield
    pin_auth.reset_gate_cache()
    pin_auth._sessions.clear()


# ---------------------------------------------------------------------------
# Hashing + sessions
# ---------------------------------------------------------------------------
class TestHashing:
    def test_deterministic_with_same_salt(self):
        a = pin_auth.hash_pin("123456", "salt")
        b = pin_auth.hash_pin("123456", "salt")
        assert a == b and len(a) == 64

    def test_different_salt_different_hash(self):
        assert pin_auth.hash_pin("123456", "s1") != pin_auth.hash_pin("123456", "s2")

    def test_salts_are_random(self):
        assert pin_auth.new_salt() != pin_auth.new_salt()


class TestSessions:
    def test_create_and_validate(self):
        token = pin_auth.create_session()
        assert pin_auth.session_valid(token)

    def test_invalid_token(self):
        assert not pin_auth.session_valid("nope")
        assert not pin_auth.session_valid(None)

    def test_revoke(self):
        token = pin_auth.create_session()
        pin_auth.revoke_session(token)
        assert not pin_auth.session_valid(token)


# ---------------------------------------------------------------------------
# verify_pin / set_pin
# ---------------------------------------------------------------------------
class TestVerifyPin:
    def test_format_rejected(self):
        ok, msg, _ = pin_auth.verify_pin(auth_db(), "12345")
        assert not ok and "6" in msg
        ok, msg, _ = pin_auth.verify_pin(auth_db(), "12a456")
        assert not ok

    def test_correct_pin_creates_session(self):
        db = auth_db()
        ok, msg, extra = pin_auth.verify_pin(db, "123456")
        assert ok and pin_auth.session_valid(extra["token"])
        assert db.rows["app_auth"][0]["failed_attempts"] == 0

    def test_wrong_pin_counts_attempts(self):
        db = auth_db()
        ok, msg, extra = pin_auth.verify_pin(db, "999999")
        assert not ok and extra["remaining_attempts"] == 4
        assert db.rows["app_auth"][0]["failed_attempts"] == 1

    def test_lockout_after_five_failures(self):
        db = auth_db()
        last = {}
        for _ in range(5):
            last = pin_auth.verify_pin(db, "999999")[2]
        assert last.get("locked") is True
        row = db.rows["app_auth"][0]
        assert row["failed_attempts"] == 0          # counter reset for next window
        assert row["locked_until"] is not None

    def test_correct_pin_rejected_while_locked(self):
        until = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        db = auth_db(locked_until=until)
        ok, msg, extra = pin_auth.verify_pin(db, "123456")
        assert not ok and "ล็อก" in msg and extra.get("locked") is True

    def test_expired_lock_allows_login(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        db = auth_db(locked_until=past)
        ok, _, extra = pin_auth.verify_pin(db, "123456")
        assert ok and pin_auth.session_valid(extra["token"])
        assert db.rows["app_auth"][0]["locked_until"] is None

    def test_fail_closed_when_db_unreadable(self):
        class Dead:
            def select(self, *a, **k):
                raise RuntimeError("db down")
        ok, msg, _ = pin_auth.verify_pin(Dead(), "123456")
        assert not ok

    def test_no_pin_set_needs_setup(self):
        ok, msg, extra = pin_auth.verify_pin(auth_db(pin=None), "123456")
        assert not ok and extra.get("need_setup") is True


class TestSetPin:
    def test_set_and_login(self):
        db = auth_db(pin=None)
        ok, _ = pin_auth.set_pin(db, "654321")
        assert ok
        ok, msg, extra = pin_auth.verify_pin(db, "654321")
        assert ok and pin_auth.session_valid(extra["token"])
        # old pin no longer works
        ok, _, _ = pin_auth.verify_pin(db, "123456")
        assert not ok

    def test_invalid_pin_rejected(self):
        db = auth_db(pin=None)
        ok, msg = pin_auth.set_pin(db, "abc")
        assert not ok
        ok, msg = pin_auth.set_pin(db, "12345")
        assert not ok

    def test_set_pin_clears_lockout(self):
        db = auth_db()
        pin_auth.set_pin(db, "111222")
        row = db.rows["app_auth"][0]
        assert row["locked_until"] is None and row["failed_attempts"] == 0


# ---------------------------------------------------------------------------
# HTTP layer — the middleware gate
# ---------------------------------------------------------------------------
async def call(method: str, path: str, json_body: dict | None = None,
               token: str | None = None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request(method, path, json=json_body, headers=headers)


class TestHttpGate:
    @pytest.mark.asyncio
    async def test_status_endpoint_public(self):
        app.state.db = auth_db()
        r = await call("GET", "/api/auth/status")
        assert r.status_code == 200
        assert r.json()["pin_set"] is True

    @pytest.mark.asyncio
    async def test_open_before_pin_is_set(self):
        app.state.db = auth_db(pin=None)
        r = await call("GET", "/api/market/summary")
        assert r.status_code == 200          # bootstrap mode — nothing locked

    @pytest.mark.asyncio
    async def test_locked_without_token_after_pin_set(self):
        app.state.db = auth_db()
        r = await call("GET", "/api/market/summary")
        assert r.status_code == 401

    @pytest.mark.asyncio
    async def test_gate_401_carries_cors_headers(self):
        # Regression: the gate used to answer 401 OUTSIDE the CORS wrapper, so
        # the browser blocked the response entirely and the frontend could not
        # react (the PIN pad / re-lock flow silently broke).
        app.state.db = auth_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.get("/api/market/summary",
                            headers={"Origin": "http://localhost:3000"})
        assert r.status_code == 401
        assert r.headers.get("access-control-allow-origin") == "http://localhost:3000"

    @pytest.mark.asyncio
    async def test_login_then_access(self):
        app.state.db = auth_db()
        r = await call("POST", "/api/auth/login", {"pin": "123456"})
        assert r.status_code == 200 and r.json()["ok"] is True
        token = r.json()["token"]
        r = await call("GET", "/api/market/summary", token=token)
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_login_reports_remaining_attempts(self):
        app.state.db = auth_db()
        r = await call("POST", "/api/auth/login", {"pin": "000000"})
        assert r.json()["ok"] is False
        assert r.json()["remaining_attempts"] == 4

    @pytest.mark.asyncio
    async def test_change_pin_requires_session(self):
        app.state.db = auth_db()
        r = await call("POST", "/api/auth/set-pin", {"pin": "111222"})
        assert r.status_code == 401          # no token → cannot change
        token = (await call("POST", "/api/auth/login", {"pin": "123456"})).json()["token"]
        r = await call("POST", "/api/auth/set-pin", {"pin": "111222"}, token=token)
        assert r.json()["ok"] is True
        # old pin dead, new pin works, new token returned
        assert (await call("POST", "/api/auth/login", {"pin": "123456"})).json()["ok"] is False
        assert (await call("POST", "/api/auth/login", {"pin": "111222"})).json()["ok"] is True

    @pytest.mark.asyncio
    async def test_logout_invalidates(self):
        app.state.db = auth_db()
        token = (await call("POST", "/api/auth/login", {"pin": "123456"})).json()["token"]
        assert (await call("GET", "/api/market/summary", token=token)).status_code == 200
        await call("POST", "/api/auth/logout", token=token)
        assert (await call("GET", "/api/market/summary", token=token)).status_code == 401

    @pytest.mark.asyncio
    async def test_ping_and_health_stay_open(self):
        app.state.db = auth_db()
        assert (await call("GET", "/ping")).status_code == 200

    @pytest.mark.asyncio
    async def test_options_preflight_passes(self):
        app.state.db = auth_db()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            r = await c.options("/api/market/summary",
                                headers={"Origin": "http://localhost:3000",
                                         "Access-Control-Request-Method": "GET"})
        assert r.status_code in (200, 204)
