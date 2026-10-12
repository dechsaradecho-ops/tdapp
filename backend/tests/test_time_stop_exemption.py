"""Task 4 (ข้อ 5): time-stop defers when the trailed SL already locks profit.

Sim evidence: expired trades averaged +0.34R with MFE +0.84R — the rigid
time-stop cuts winners-pending. A position whose trailed SL already locks
≥0.25R profit is left to the stop instead of being closed on age.

Run from backend/: py -3 -m pytest tests/test_time_stop_exemption.py -q
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.workers import position_guard


class _AsyncList:
    def __init__(self, items):
        self._items = items

    def __await__(self):
        async def _coro():
            return self._items
        return _coro().__await__()


class _AsyncClosed:
    ok = True
    message = "closed"

    def __await__(self):
        async def _coro():
            return self
        return _coro().__await__()


class _AsyncFloat:
    def __init__(self, value):
        self._value = value

    def __await__(self):
        async def _coro():
            return self._value
        return _coro().__await__()


class _AsyncClosedWith:
    def __init__(self, sink: list, ticket: str):
        self._sink, self._ticket = sink, ticket

    def __await__(self):
        async def _coro():
            self._sink.append(self._ticket)
            return _AsyncClosed()
        return _coro().__await__()


class _AsyncPartial:
    def __init__(self, sink: list, value: float):
        self._sink, self._value = sink, value

    def __await__(self):
        async def _coro():
            self._sink.append(self._value)
            from app.integrations.brokers import OrderResult
            return OrderResult(ok=True, message="partial")
        return _coro().__await__()


class _SilentNotifier:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def notify(self, user_id, ntype, message, **_kw):
        self.sent.append((user_id, ntype, message))
        return None


class FakeDatabase:
    def __init__(self, rows=None):
        self.rows = rows or {}
        self.inserted: list[tuple[str, dict]] = []
        from types import SimpleNamespace as _SN

        class _Q:
            def __init__(self, rs):
                self._rs = rs

            def select(self, *_a):
                return self

            def eq(self, col, val):
                self._rs = [r for r in self._rs if r.get(col) == val]
                return self

            def limit(self, _n):
                return self

            def execute(self):
                return _SN(data=list(self._rs))

        class _KV:
            def __init__(self, db):
                self._db = db

            def table(self, name):
                return _Q(list(self._db.rows.get(name, [])))

        self._client = _KV(self)

    def select(self, table, filters=None, order="created_at", desc=True,
               limit=50, offset=0, columns="*"):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return rows[offset:offset + limit]

    def select_ex(self, *a, **k):
        return self.select(*a, **k)

    def select_paged(self, table, filters=None, **_k):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return rows

    def count(self, table, filters=None, **_k):
        rows = list(self.rows.get(table, []))
        for col, val in (filters or {}).items():
            rows = [r for r in rows if r.get(col) == val]
        return len(rows)

    def insert(self, table, row):
        self.inserted.append((table, dict(row)))
        self.rows.setdefault(table, []).insert(
            0, {**row, "id": f"fake-{len(self.inserted)}"})
        return row

    def insert_raw(self, table, row):
        self.insert(table, row)
        return dict(self.rows[table][0]), None

    def update(self, table, row_id, changes):
        for r in self.rows.get(table, []):
            if r.get("id") == row_id:
                r.update(changes)
                return True
        return False

    def delete(self, table, filters):
        rows = self.rows.get(table, [])
        kept = [r for r in rows
                if not all(r.get(c) == v for c, v in filters.items())]
        self.rows[table] = kept
        return len(kept) < len(rows)

    @property
    def available(self):
        return True

    init_error = None


@pytest.fixture(autouse=True)
def _no_network_spot(monkeypatch):
    async def fake_spot(assets, **_kw):
        return {a: 1.2500 for a in assets}, {}

    async def fake_snaps(assets, **_kw):
        return {}

    monkeypatch.setattr(position_guard.quotes, "fetch_spot_prices", fake_spot)
    monkeypatch.setattr(position_guard.quotes, "fetch_all_snapshots",
                        fake_snaps)


def _settings(**overrides):
    from app.models.schemas import AppSettings
    s = AppSettings()
    base = dict(max_hold_days=5, time_stop_min_r=0.0,
                breakeven_trigger_r=0, trail_atr_mult=0,
                partial_close_pct=0, partial_trigger_r=0,
                smart_exit_enabled=False)
    base.update(overrides)
    for k, v in base.items():
        setattr(s, k, v)
    return s


def _broker(entry=1.1000, sl=1.1050, tp=None, direction="BUY"):
    from app.integrations.brokers import Position
    broker = SimpleNamespace()
    broker._positions = {"T1": Position(
        ticket="T1", user_id="u1", asset="EURUSD", direction=direction,
        volume=0.02, entry_price=entry, stop_loss=sl, take_profit=tp,
        current_price=entry)}
    broker._positions["T1"].opened_at = (
        datetime.now(timezone.utc) - timedelta(days=10))
    broker.all_positions = lambda: _AsyncList(list(broker._positions.values()))
    broker.mark_price = lambda ticket: _AsyncFloat(0.0)
    broker.quote = lambda asset: _AsyncFloat(0.0)
    broker.close_position = lambda ticket: _AsyncClosed()
    broker.partial_close = lambda ticket, vol: _AsyncPartial([], vol)
    broker.set_rates = lambda rates: None
    return broker


def _aged_db(stop_loss, initial_stop_loss, days=10, **overrides):
    row = {"id": "p1", "ticket": "T1", "asset": "EURUSD", "status": "open",
           "direction": "buy", "volume": 0.02, "entry_price": 1.1000,
           "stop_loss": stop_loss, "initial_stop_loss": initial_stop_loss,
           "partial_done": False,
           "created_at": (datetime.now(timezone.utc)
                          - timedelta(days=days)).isoformat()}
    row.update(overrides)
    return FakeDatabase(rows={"paper_trades": [row]})


@pytest.mark.asyncio
async def test_time_stop_defers_when_sl_locks_profit(monkeypatch):
    """BUY, age > max_hold, SL trailed above entry → time-stop defers.

    entry 1.1000 / initial SL 1.0900 → R = 0.0100; trailed SL 1.1050 locks
    (1.1050 − 1.1000) / 0.0100 = +0.50R ≥ 0.25 → no close this cycle.
    """
    from app.services import execution
    monkeypatch.setattr(execution,
                        "market_closed_discretionary_close_block",
                        lambda: "")
    closed: list[str] = []
    broker = _broker(sl=1.1050)  # trailed above entry: locks +0.50R
    broker.close_position = lambda ticket: _AsyncClosedWith(closed, ticket)
    db = _aged_db(stop_loss=1.1050, initial_stop_loss=1.0900)
    summary = await position_guard.guard_once(
        db, broker, _SilentNotifier(), settings=_settings())
    assert summary["closed"] == 0
    assert closed == []
    assert db.rows["paper_trades"][0]["status"] == "open"
    assert "deferred" in (summary.get("skip_assets") or "").lower()


@pytest.mark.asyncio
async def test_time_stop_still_closes_untrailed_control(monkeypatch):
    """Same age, SL still below entry → time-stop closes as before."""
    from app.services import execution
    monkeypatch.setattr(execution,
                        "market_closed_discretionary_close_block",
                        lambda: "")
    closed: list[str] = []
    broker = _broker(sl=1.0900)  # never trailed: locks −1.0R
    broker.close_position = lambda ticket: _AsyncClosedWith(closed, ticket)
    db = _aged_db(stop_loss=1.0900, initial_stop_loss=1.0900)
    summary = await position_guard.guard_once(
        db, broker, _SilentNotifier(), settings=_settings())
    assert closed == ["T1"]
    assert summary["closed"] == 1
    row = db.rows["paper_trades"][0]
    assert row["status"] == "closed"
    assert row["close_reason"] == "time"
