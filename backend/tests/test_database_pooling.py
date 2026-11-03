"""Supabase connection pooling — one shared keep-alive httpx client.

Prod 2026-11-03: simple log endpoints took 8-9s for ~12 tiny DB roundtrips
while the DB itself answers in ~0.15s — every query paid a fresh TLS
handshake because supabase-py was created with no shared httpx client.

Run from backend/: .\\.venv\\Scripts\\python.exe -m pytest tests/test_database_pooling.py -q
"""
from __future__ import annotations

import types

import pytest

import app.services.database as dbmod
from app.services.database import Database


def _fake_settings():
    return types.SimpleNamespace(
        supabase_url="https://xyz.supabase.co",
        effective_service_key="dummy-key")


def test_create_client_receives_shared_pooled_options(monkeypatch):
    """Database must hand supabase-py ONE shared keep-alive httpx client.

    Shared identity across instances is the whole mechanism: connection
    reuse only happens when requests flow through the same client object.
    """
    import httpx
    import supabase

    seen: list = []

    def fake_create(url, key, options=None):
        seen.append(options)
        return object()

    monkeypatch.setattr(dbmod, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(supabase, "create_client", fake_create)

    Database()
    Database()

    assert len(seen) == 2
    assert seen[0] is not None, \
        "create_client got no options — every query pays a fresh handshake"
    assert isinstance(seen[0].httpx_client, httpx.Client)
    assert seen[0].httpx_client is seen[1].httpx_client, \
        "each Database() built its own http client — no pool reuse"


def test_falls_back_to_plain_client_when_options_unsupported(monkeypatch):
    """Ancient supabase-py without httpx_client support must still connect.

    Fail-soft: pooling is an optimization, never a reason to lose the DB.
    """
    import supabase

    def boom_options(**kwargs):
        raise TypeError("no httpx_client support")

    built = {}

    def fake_create(url, key, options=None):
        built["options"] = options
        return object()

    monkeypatch.setattr(dbmod, "get_settings", lambda: _fake_settings())
    monkeypatch.setattr(supabase, "ClientOptions", boom_options)
    monkeypatch.setattr(supabase, "create_client", fake_create)

    db = Database()
    assert db.available, "DB must stay available without pooling support"
    assert built.get("options") is None
