"""Auth routes: PIN status, login, set/change, logout.

The PIN gate only protects the dashboard — MT5/broker connectivity is not
part of this project (paper trading journal only), so there is no broker
auth to worry about here.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.models.schemas import PinLoginRequest, PinLoginResponse, PinSetRequest, PinStatus
from app.services import pin_auth

log = logging.getLogger(__name__)

router = APIRouter()


def _db(request: Request):
    return request.app.state.db


@router.get("/status", response_model=PinStatus)
async def status(request: Request) -> PinStatus:
    """Public: does a PIN exist, and is the account currently locked?"""
    return PinStatus(**{k: v for k, v in pin_auth.pin_status(_db(request)).items() if k in PinStatus.model_fields})


@router.post("/login", response_model=PinLoginResponse)
async def login(payload: PinLoginRequest, request: Request) -> PinLoginResponse:
    ok, msg, extra = pin_auth.verify_pin(_db(request), payload.pin)
    return PinLoginResponse(
        ok=ok, token=extra.get("token"), message=msg,
        remaining_attempts=extra.get("remaining_attempts"),
        locked_until=extra.get("locked_until"),
    )


@router.post("/set-pin", response_model=PinLoginResponse)
async def set_pin(payload: PinSetRequest, request: Request) -> PinLoginResponse:
    """First-time setup OR change of the dashboard PIN.

    - No PIN set yet  → open setup (bootstrap, allowed without a session).
    - PIN already set → requires a valid session token (Bearer).
    """
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else ""
    st = pin_auth.pin_status(_db(request))
    if st["pin_set"] and not pin_auth.session_valid(token):
        raise HTTPException(status_code=401, detail="unauthorized")
    ok, msg = pin_auth.set_pin(_db(request), payload.pin)
    if not ok:
        return PinLoginResponse(ok=False, message=msg)
    return PinLoginResponse(ok=True, token=pin_auth.create_session(), message="ตั้ง PIN เรียบร้อย")


@router.post("/logout")
async def logout(request: Request) -> dict:
    """Invalidate the caller's session token.

    Auth is enforced by the app-level PIN middleware (/api/auth/logout is
    not in the whitelist), so no extra dependency needed here.
    """
    header = request.headers.get("authorization") or ""
    token = header[7:] if header.startswith("Bearer ") else ""
    pin_auth.revoke_session(token)
    return {"ok": True}
