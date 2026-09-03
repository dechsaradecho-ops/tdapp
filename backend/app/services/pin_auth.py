"""6-digit PIN auth — hashes, lockout, and in-memory session tokens.

Design notes:
- PIN is stored as PBKDF2-HMAC-SHA256 (100k iters) with a random salt.
  The PIN is only 6 digits (~1e6 combos) so the salt is mandatory to stop
  precomputed-table lookups; iteration count raises per-guess cost.
- Lockout state lives in the DB row (failed_attempts / locked_until), so it
  survives server restarts and is shared across instances.
- Sessions are in-memory tokens: fast, self-expiring, and nothing sensitive
  to revoke in the DB. A restart logs everyone out (acceptable for a single-
  user dashboard) — the frontend just re-prompts for the PIN.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.database import Database

log = logging.getLogger(__name__)

PIN_LENGTH = 6
MAX_FAILED_ATTEMPTS = 5
LOCK_MINUTES = 15
PBKDF2_ITERATIONS = 100_000
TOKEN_TTL_SECONDS = 12 * 3600          # 12h session
SETTINGS_TABLE = "app_settings"

# In-memory session store: token -> expiry epoch seconds.
_sessions: dict[str, float] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Hashing helpers (module-level so tests can compare against them)
# ---------------------------------------------------------------------------
def hash_pin(pin: str, salt: str) -> str:
    """PBKDF2-SHA256 over pin+salt — NOT reversible without the salt+pin."""
    return hashlib.pbkdf2_hmac(
        "sha256", pin.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ITERATIONS
    ).hex()


def _constant_time_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def new_salt() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Session store
# ---------------------------------------------------------------------------
def _purge_expired() -> None:
    now = time.time()
    expired = [t for t, exp in _sessions.items() if exp <= now]
    for t in expired:
        _sessions.pop(t, None)


def create_session() -> str:
    token = new_token()
    with _lock:
        _purge_expired()
        _sessions[token] = time.time() + TOKEN_TTL_SECONDS
    return token


def session_valid(token: Optional[str]) -> bool:
    if not token:
        return False
    with _lock:
        _purge_expired()
        return token in _sessions


def revoke_session(token: Optional[str]) -> None:
    if token:
        with _lock:
            _sessions.pop(token, None)


# ---------------------------------------------------------------------------
# Auth row access — fail-safe reads, explicit writes
# ---------------------------------------------------------------------------
def _auth_row(db: Database) -> Optional[dict]:
    """Read the single app_auth row; returns None if table/DB unavailable.

    None means "state unknown" — callers treat it as fail-CLOSED (block) for
    verification, but the status endpoint reports it so the UI can warn.
    """
    try:
        rows = db.select("app_auth", limit=1)
        return rows[0] if rows else None
    except Exception as exc:
        log.error("auth: reading app_auth failed: %s", exc)
        return None


def is_locked(row: dict, now: Optional[datetime] = None) -> tuple[bool, Optional[datetime]]:
    until_raw = row.get("locked_until")
    if not until_raw:
        return False, None
    until = until_raw if isinstance(until_raw, datetime) else None
    if until is None:
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00"))
        except ValueError:
            return False, None
    now = now or datetime.now(timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return until > now, until


def pin_status(db: Database) -> dict:
    row = _auth_row(db)
    if row is None:
        return {
            "pin_set": False, "locked": False, "locked_until": None,
            "failed_attempts": 0, "max_failed": MAX_FAILED_ATTEMPTS,
            "lock_minutes": LOCK_MINUTES, "db_ok": False,
        }
    locked, until = is_locked(row)
    return {
        "pin_set": bool(row.get("pin_hash")),
        "locked": locked,
        "locked_until": until.isoformat() if until else None,
        "failed_attempts": int(row.get("failed_attempts") or 0),
        "max_failed": MAX_FAILED_ATTEMPTS,
        "lock_minutes": LOCK_MINUTES,
        "db_ok": True,
    }


def verify_pin(db: Database, pin: str) -> tuple[bool, str, dict]:
    """Check a PIN against the stored hash.

    Returns (ok, message, extra) where extra carries token / remaining
    attempts / lock info for the response body. Fail-CLOSED: if the auth row
    can't be read, every attempt is rejected.
    """
    pin = (pin or "").strip()
    if len(pin) != PIN_LENGTH or not pin.isdigit():
        return False, f"PIN ต้องเป็นตัวเลข {PIN_LENGTH} หลัก", {}

    row = _auth_row(db)
    if row is None:
        return False, "ระบบยืนยันตัวตนยังไม่พร้อม (app_auth อ่านไม่ได้) — ลองใหม่", {}

    if not row.get("pin_hash"):
        return False, "ยังไม่ได้ตั้ง PIN — ไปที่ /settings เพื่อตั้งค่าก่อน", {"need_setup": True}

    locked, until = is_locked(row)
    if locked:
        mins = max(1, int(((until or datetime.now(timezone.utc)) - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        return False, f"บัญชีถูกล็อก ลองอีกครั้งใน ~{mins} นาที", {"locked": True, "locked_until": until.isoformat() if until else None}

    if not _constant_time_eq(hash_pin(pin, row.get("salt") or ""), row.get("pin_hash") or ""):
        failed = int(row.get("failed_attempts") or 0) + 1
        changes: dict = {"failed_attempts": failed, "updated_at": datetime.now(timezone.utc).isoformat()}
        if failed >= MAX_FAILED_ATTEMPTS:
            locked_until = datetime.now(timezone.utc) + timedelta(minutes=LOCK_MINUTES)
            changes["locked_until"] = locked_until.isoformat()
            changes["failed_attempts"] = 0   # reset counter for the next window
            db.update("app_auth", row["id"], changes)
            log.warning("auth: %d wrong PINs — locked for %d min", MAX_FAILED_ATTEMPTS, LOCK_MINUTES)
            return False, f"PIN ผิดครบ {MAX_FAILED_ATTEMPTS} ครั้ง — ล็อก {LOCK_MINUTES} นาที", {
                "locked": True, "locked_until": locked_until.isoformat(), "remaining_attempts": 0,
            }
        db.update("app_auth", row["id"], changes)
        remaining = MAX_FAILED_ATTEMPTS - failed
        return False, f"PIN ไม่ถูกต้อง เหลือโอกาสอีก {remaining} ครั้ง", {"remaining_attempts": remaining}

    # success — clear the counters
    db.update("app_auth", row["id"], {
        "failed_attempts": 0, "locked_until": None,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    return True, "ok", {"token": create_session()}


def set_pin(db: Database, pin: str) -> tuple[bool, str]:
    """First-time setup OR change (requires a valid session upstream)."""
    pin = (pin or "").strip()
    if len(pin) != PIN_LENGTH or not pin.isdigit():
        return False, f"PIN ต้องเป็นตัวเลข {PIN_LENGTH} หลัก"
    row = _auth_row(db)
    if row is None:
        return False, "ตาราง app_auth ยังไม่พร้อม — รัน database/008_pin_auth.sql ก่อน"
    salt = new_salt()
    ok = db.update("app_auth", row["id"], {
        "pin_hash": hash_pin(pin, salt), "salt": salt,
        "failed_attempts": 0, "locked_until": None, "pin_set_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    if not ok:
        return False, "บันทึก PIN ไม่สำเร็จ — ลองใหม่"
    reset_gate_cache()
    log.info("auth: PIN set/changed")
    return True, "ok"


# ---------------------------------------------------------------------------
# Gate state ("is a PIN configured?") — read per-request but cached briefly
# so the middleware costs ~0 DB calls in steady state.
# ---------------------------------------------------------------------------
_gate_cache: Optional[tuple[float, bool]] = None
_GATE_TTL_SECONDS = 10.0


def reset_gate_cache() -> None:
    global _gate_cache
    _gate_cache = None


def gate_active(db) -> bool:
    """True once a PIN hash exists in app_auth (→ all /api/* need a token)."""
    global _gate_cache
    now = time.time()
    if _gate_cache is not None and now - _gate_cache[0] < _GATE_TTL_SECONDS:
        return _gate_cache[1]
    row = _auth_row(db)
    active = bool(row and row.get("pin_hash")) if row is not None else False
    _gate_cache = (now, active)
    return active


# ---------------------------------------------------------------------------
# FastAPI dependency — guards every API route
# ---------------------------------------------------------------------------
def auth_guard(request: "Request") -> None:
    """Dependency for protected routers: raises 401 unless a valid session.

    Exempt paths (whitelist) are matched on `request.url.path`:
      /ping, /health, /api/auth/* — everything else needs the token.
    """
    path = request.url.path
    if path in ("/ping", "/health") or path.startswith("/api/auth/"):
        return
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else ""
    if not session_valid(token):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="unauthorized")
