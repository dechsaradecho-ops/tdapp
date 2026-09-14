"""Ticket numbers must never be recycled — and a close must hit the right row.

WHY this file exists — prod 2026-09-14: the monitor's "ประวัติ SL/TP" popup of
the **open AUDCHF** position showed the **closed AUDNZD** trade (emergency exit
at 1.2349). Both were `PAPER-000001`:

  * `PaperBroker._seq` lives in memory and restarts at 0 on every deploy;
  * `rehydrate_book` only walks the sequence past rows that are still OPEN, and
    `POST /api/trading/stats/reset` had just DELETED the closed rows
    ("ลบไม้ที่ปิดแล้ว 17 ไม้", 11:06:50Z) — so the next trade took 000001 again;
  * `signal_logs` (7-day TTL, permanent relative to the broker's memory) still
    held the old `closed` event under that ticket, and the monitor grouped the
    timeline **by ticket only**.

Fix, locked here:
  1. `Database.max_ticket` — the highest `<prefix>NNNNNN` the DB still holds;
  2. `position_guard.seed_order_sequence` — at boot, push `_seq` past it
     (paper_trades + signal_logs), so a number is reused only once BOTH tables
     have forgotten it (then there is no history left to mix up);
  3. `execution.close_trade_rows(asset=, direction=)` — refuse to close a row
     whose symbol does not match the book (fail-safe > closing the wrong trade).

Run from backend/: d:\\tdapp\\.venv\\Scripts\\python.exe -m pytest tests/test_ticket_recycling.py -v
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from app.services import execution
from app.services.database import Database
from app.workers import position_guard
from tests.test_workers import FakeDatabase


# ---------------------------------------------------------------------------
# 1. Database.max_ticket — read the highest ticket the table still remembers
# ---------------------------------------------------------------------------
class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    """Minimal PostgREST chain: select → like → order → limit → execute."""

    def __init__(self, rows, boom: bool = False):
        self._rows = rows
        self._boom = boom
        self.calls: dict = {}
        self.desc = None

    def select(self, cols):
        self.calls["select"] = cols
        return self

    def like(self, col, pattern):
        self.calls["like"] = (col, pattern)
        return self

    def order(self, col, desc=False):
        self.calls["order"] = col
        self.desc = desc
        return self

    def limit(self, n):
        self.calls["limit"] = n
        return self

    def execute(self):
        if self._boom:
            raise RuntimeError("relation does not exist")
        return _Resp(self._rows)


class _Client:
    def __init__(self, rows=None, boom: bool = False):
        self._rows = rows or []
        self._boom = boom
        self.query: _Query | None = None

    def table(self, name):
        self.table_name = name
        self.query = _Query(self._rows, self._boom)
        return self.query


def _db_with(rows, boom: bool = False) -> tuple[Database, _Client]:
    db = Database()
    client = _Client(rows, boom)
    db._client = client                       # bypass settings/env for the test
    return db, client


class TestMaxTicket:
    def test_returns_highest_suffix(self):
        db, client = _db_with([{"ticket": "PAPER-000002"},
                               {"ticket": "PAPER-000048"},
                               {"ticket": "PAPER-000001"}])
        assert db.max_ticket("paper_trades") == 48
        assert client.table_name == "paper_trades"
        assert client.query.calls["like"] == ("ticket", "PAPER-*")
        assert client.query.desc is True           # highest first
        assert client.query.calls["select"] == "ticket"

    def test_empty_table_is_zero(self):
        db, _ = _db_with([])
        assert db.max_ticket("signal_logs") == 0

    @pytest.mark.parametrize("ticket", ["PAPER-abc", "OTHER-000009", "", None,
                                        "PAPER-"])
    def test_ignores_unparsable_tickets(self, ticket):
        db, _ = _db_with([{"ticket": ticket}, {"ticket": "PAPER-000007"}])
        assert db.max_ticket("paper_trades") == 7

    def test_missing_client_is_zero_not_a_crash(self):
        """local dev ไม่มี Supabase → ต้องคืน 0 (แค่ไม่ seed) ไม่ใช่ระเบิด."""
        db = Database()
        assert db._client is None
        assert db.max_ticket("paper_trades") == 0

    def test_query_error_is_swallowed(self):
        db, _ = _db_with([], boom=True)
        assert db.max_ticket("paper_trades") == 0


# ---------------------------------------------------------------------------
# 2. seed_order_sequence — boot path
# ---------------------------------------------------------------------------
class _TicketDb:
    def __init__(self, per_table: dict[str, int]):
        self.per_table = per_table
        self.asked: list[str] = []

    def max_ticket(self, table: str, prefix: str = "PAPER-", scan: int = 200) -> int:
        self.asked.append(table)
        return self.per_table.get(table, 0)


class TestSeedOrderSequence:
    def test_jumps_past_the_highest_ticket_in_the_db(self):
        """เคส prod: ไม้เปิดใหม่จะได้ 000006 ถ้าไม่ seed (ticket 000005 ยังเปิดอยู่)
        ทั้งที่ signal_logs ยังจำ 000048 — ต้องเดินไป 48."""
        db = _TicketDb({"paper_trades": 5, "signal_logs": 48})
        broker = SimpleNamespace(_seq=0)
        assert position_guard.seed_order_sequence(db, broker) == 48
        assert broker._seq == 48
        assert set(db.asked) == {"paper_trades", "signal_logs"}

    def test_never_moves_the_sequence_backwards(self):
        """ถ้า in-memory seq ไปไกลกว่า DB แล้ว (เพิ่งยิงไป) ห้ามลด."""
        db = _TicketDb({"paper_trades": 3, "signal_logs": 0})
        broker = SimpleNamespace(_seq=9)
        assert position_guard.seed_order_sequence(db, broker) == 9
        assert broker._seq == 9

    def test_broker_without_sequence_is_untouched(self):
        """broker จริง (MetaApi) ไม่มี _seq — ต้องไม่ไปงอก attribute ให้มัน."""
        broker = SimpleNamespace()
        assert position_guard.seed_order_sequence(_TicketDb({}), broker) == 0
        assert not hasattr(broker, "_seq")

    def test_old_fake_without_max_ticket_is_tolerated(self):
        class _Old:                      # ไม่มี max_ticket (fake รุ่นเก่า)
            def select(self, *a, **k):
                return []

        broker = SimpleNamespace(_seq=4)
        assert position_guard.seed_order_sequence(_Old(), broker) == 4
        assert broker._seq == 4

    def test_lookup_failure_keeps_current_sequence(self):
        class _Boom:
            def max_ticket(self, table, prefix="PAPER-", scan=200):
                raise RuntimeError("db down")

        broker = SimpleNamespace(_seq=7)
        assert position_guard.seed_order_sequence(_Boom(), broker) == 7
        assert broker._seq == 7


# ---------------------------------------------------------------------------
# 3. close_trade_rows — ต้องปิดแถวของ symbol ที่ตรงกับ book เท่านั้น
# ---------------------------------------------------------------------------
def _open(asset: str, direction: str = "BUY", ticket: str = "PAPER-000001",
          row_id: str = "r1") -> dict:
    return {"id": row_id, "ticket": ticket, "asset": asset,
            "direction": direction, "status": "open", "entry_price": 1.0}


class TestCloseTradeRowsMatching:
    def test_matching_asset_closes_the_row(self):
        row = _open("AUDCHF")
        db = FakeDatabase(rows={"paper_trades": [row]})
        execution.close_trade_rows(db, "PAPER-000001", 0.55, -12.5, "tp",
                                   asset="AUDCHF", direction="BUY")
        assert row["status"] == "closed"
        assert row["close_reason"] == "tp" and row["pnl"] == -12.5
        assert row["closed_at"]

    def test_recycled_ticket_of_another_symbol_is_refused(self, caplog):
        """เคส prod: book เป็น AUDCHF แต่แถวที่ open ใต้ ticket เดียวกันคือ AUDNZD
        (ticket ถูกใช้ซ้ำหลัง restart) — ต้อง *ไม่* ปิด และเตือนให้เห็น."""
        row = _open("AUDNZD")
        db = FakeDatabase(rows={"paper_trades": [row]})
        with caplog.at_level(logging.WARNING, logger=execution.__name__):
            execution.close_trade_rows(db, "PAPER-000001", 1.2349, -40.0,
                                       "emergency", asset="AUDCHF",
                                       direction="BUY")
        assert row["status"] == "open"           # ไม่ถูกปิดผิดไม้
        assert "close_reason" not in row
        assert any("refusing to close the wrong trade" in r.message
                   for r in caplog.records)

    def test_direction_mismatch_is_also_refused(self, caplog):
        row = _open("AUDCHF", direction="BUY")
        db = FakeDatabase(rows={"paper_trades": [row]})
        with caplog.at_level(logging.WARNING, logger=execution.__name__):
            execution.close_trade_rows(db, "PAPER-000001", 0.55, 1.0, "sl",
                                       asset="AUDCHF", direction="SELL")
        assert row["status"] == "open"

    def test_matching_row_is_found_among_duplicates(self):
        """ประวัติเก่ามี ticket ซ้ำ: ต้องเลือกแถวที่ asset ตรงกับ book."""
        wrong = _open("AUDNZD", row_id="r-old", ticket="PAPER-000001")
        right = _open("AUDCHF", row_id="r-new", ticket="PAPER-000001")
        db = FakeDatabase(rows={"paper_trades": [wrong, right]})
        execution.close_trade_rows(db, "PAPER-000001", 0.55, 2.0, "tp",
                                   asset="AUDCHF", direction="BUY")
        assert right["status"] == "closed"
        assert wrong["status"] == "open"

    def test_case_insensitive_match(self):
        row = _open("audchf", direction="buy")
        db = FakeDatabase(rows={"paper_trades": [row]})
        execution.close_trade_rows(db, "PAPER-000001", 0.55, 2.0, "tp",
                                   asset="AUDCHF", direction="BUY")
        assert row["status"] == "closed"

    def test_without_constraints_behaviour_is_unchanged(self):
        """ผู้เรียกที่ยังไม่ส่ง asset/ฝั่ง (โค้ดเก่า) ต้องปิดได้เหมือนเดิม."""
        row = _open("AUDCHF")
        db = FakeDatabase(rows={"paper_trades": [row]})
        execution.close_trade_rows(db, "PAPER-000001", 0.55, 2.0, "manual")
        assert row["status"] == "closed"

    def test_already_closed_row_is_not_touched(self):
        row = _open("AUDCHF")
        row["status"] = "closed"
        db = FakeDatabase(rows={"paper_trades": [row]})
        execution.close_trade_rows(db, "PAPER-000001", 0.55, 9.9, "tp",
                                   asset="AUDCHF", direction="BUY")
        assert row.get("pnl") != 9.9

    def test_missing_open_rows_is_a_no_op(self):
        db = FakeDatabase(rows={"paper_trades": []})
        execution.close_trade_rows(db, "PAPER-999999", 0.55, 1.0, "tp",
                                   asset="AUDCHF", direction="BUY")
        assert db.rows["paper_trades"] == []
