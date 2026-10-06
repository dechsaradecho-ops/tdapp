"""Regression: a partial settings READ must not reset the fields it missed.

The bug this pins (prod 2026-10-06): ``save_settings`` merged the incoming
patch onto ``_load_settings``, which fills any field the read did not return
with the ``AppSettings()`` schema DEFAULT, then upserted the WHOLE row. A
stale PostgREST schema cache can drop a single column from ``select("*")``
while the rest of the row reads fine — and that one column was then written
back as its default.

It happened for real: ``allowed_assets`` came back empty and a 14-pair
whitelist was overwritten with the 5-pair schema default, unbinding every
pair the owner had added, in a single unrelated save.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_settings_partial_read.py -v
"""
from __future__ import annotations

import httpx
import pytest

from app.main import app
from app.models.schemas import AppSettings
from tests.test_settings import SimpleResult, set_state
from tests.test_workers import FakeDatabase


class OmittingSelectClient:
    """A supabase client whose READ hides some columns but whose WRITE keeps them.

    This is what a stale PostgREST schema cache looks like from the app's
    side: ``select("*")`` answers without the column, while the stored row
    still holds the real value.
    """

    def __init__(self, row: dict, omit_on_read: tuple[str, ...] = ()):
        self.row = dict(row)
        self.omit_on_read = set(omit_on_read)
        self.written: list[dict] = []

    def table(self, _name):
        return self

    def select(self, _cols):
        return self

    def eq(self, _col, _val):
        return self

    def limit(self, _n):
        return self

    def upsert(self, row: dict):
        self.written.append(dict(row))
        return self

    def delete(self):
        return self

    def execute(self):
        # Supabase returns the merged row after an upsert; the read path is
        # where the column goes missing.
        if getattr(self, "_in_write", False):
            return SimpleResult([dict(self.row)])
        return SimpleResult([{k: v for k, v in self.row.items()
                              if k not in self.omit_on_read}])


class OmitWriteClient(OmittingSelectClient):
    """Upsert that only replaces the columns it was given (true partial write)."""

    def upsert(self, row: dict):
        self.row.update(dict(row))
        self.written.append(dict(row))
        self._in_write = True
        return self


class Db(FakeDatabase):
    def __init__(self, row: dict, omit: tuple[str, ...]):
        super().__init__()
        self._client = OmitWriteClient(row, omit)


def _fully_migrated_row() -> dict:
    """A row shaped like production: every AppSettings column present.

    Built from the model so the fixture cannot drift, and so omitting ONE
    column yields unread=1 — which is what a stale PostgREST schema cache
    actually looks like, and what the warning's naming logic is written for.
    """
    row = AppSettings().model_dump(mode="json")
    row.update({
        "id": 1,
        "capital": 500.0,
        "max_drawdown_pct": 35.0,
        "rr_target": 1.5,
        "min_confidence": 70.0,
        "allowed_assets": ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD",
                           "GBPCHF", "EURCHF", "AUDNZD", "NZDUSD", "AUDCHF",
                           "CADCHF", "GBPJPY", "CHFJPY", "CADJPY"],
    })
    return row


STORED = _fully_migrated_row()


async def put(db, payload: dict) -> httpx.Response:
    set_state(db)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        return await c.request("PUT", "/api/settings", json=payload)


class TestPartialReadDoesNotReset:
    @pytest.mark.asyncio
    async def test_unread_allowed_assets_is_not_overwritten_with_the_default(self):
        """The prod incident, reproduced: the read omits allowed_assets, the
        caller patches something unrelated, and the whitelist must survive."""
        db = Db(STORED, omit=("allowed_assets",))
        r = await put(db, {"max_open_positions": 4})
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True
        assert db._client.row["max_open_positions"] == 4
        assert db._client.row["allowed_assets"] == STORED["allowed_assets"], \
            "whitelist was reset by the default value"
        assert db._client.row["capital"] == 500.0

    @pytest.mark.asyncio
    async def test_unread_safety_limit_keeps_its_stored_value(self):
        """max_drawdown_pct defaulting to 10.0 is the scenario the loader's
        own docstring warns about — Emergency Exit closed 6 positions over it
        on 2026-09-22. A read that cannot see the limit must not lower it."""
        db = Db(STORED, omit=("max_drawdown_pct",))
        r = await put(db, {"min_lot": 0.01})
        assert r.json()["ok"] is True
        assert db._client.row["max_drawdown_pct"] == 35.0, \
            "kill-switch limit was reset to the schema default"

    @pytest.mark.asyncio
    async def test_response_names_the_columns_it_could_not_read(self):
        """Silent partial saves are how this goes unnoticed — the UI has to be
        able to say what did not come from the database."""
        db = Db(STORED, omit=("allowed_assets", "max_drawdown_pct"))
        r = await put(db, {"min_lot": 0.01})
        msg = r.json().get("message") or ""
        assert "allowed_assets" in msg
        assert "max_drawdown_pct" in msg

    @pytest.mark.asyncio
    async def test_an_explicit_patch_of_the_omitted_field_still_lands(self):
        """Partial-write protection must not block a DELIBERATE change to a
        field the read could not see — that is the one case where writing the
        caller's value is correct."""
        db = Db(STORED, omit=("allowed_assets",))
        r = await put(db, {"allowed_assets": ["EURUSD", "USDJPY"]})
        assert r.json()["ok"] is True
        assert db._client.row["allowed_assets"] == ["EURUSD", "USDJPY"]

    @pytest.mark.asyncio
    async def test_full_read_writes_everything_as_before(self):
        """No behaviour change when the read is complete."""
        db = Db(STORED, omit=())
        r = await put(db, {"rr_target": 2.0})
        assert r.json()["ok"] is True
        assert db._client.row["rr_target"] == 2.0
        assert db._client.row["allowed_assets"] == STORED["allowed_assets"]
        assert "อ่านค่าไม่ได้" not in (r.json().get("message") or "")

    @pytest.mark.asyncio
    async def test_failed_read_refuses_the_save_entirely(self):
        """If the base row cannot be read there is nothing safe to merge
        onto — better to reject than to persist defaults."""
        class BrokenClient(OmitWriteClient):
            def execute(self):
                raise RuntimeError("connection reset by peer")

        db = Db(STORED, omit=())
        db._client = BrokenClient(STORED)
        r = await put(db, {"rr_target": 3.0})
        body = r.json()
        assert body["ok"] is False
        assert db._client.written == [], "a write happened despite the failed read"


class TestStoredRowIntegrity:
    def test_stored_row_matches_app_settings_shape(self):
        """Guard the fixture itself — a STORED dict that drifts from the model
        would make every test above pass for the wrong reason."""
        s = AppSettings()
        for key in ("capital", "max_drawdown_pct", "rr_target",
                    "min_confidence"):
            assert key in s.model_fields, key
        assert set(STORED) - {"id"} <= set(s.model_fields)
