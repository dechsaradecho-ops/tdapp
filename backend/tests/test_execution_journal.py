"""paper_trades journaling must never fail silently.

Prod 2026-09-11: POST /api/trading/extended-open opened AUDCHF (ticket
PAPER-000041) — it reached the broker book, signal_logs and LINE — but the
paper_trades insert was rejected by

    paper_trades_source_check  (23514: source in ('auto','approved'))

because the Extended path writes source='extended'. `Database.insert` swallows
errors into a log line, so the position simply never appeared on /monitor and
never counted in monitor stats / PnL.

These tests lock in the two fixes:
  1. record_trade RETURNS the raw error (so the caller can warn the user).
  2. record_trade retries with a schema-safe source when the CHECK rejects
     'extended' — the trade must still be journaled (audit keeps 'extended'
     in signal_logs) even before migration 031 is deployed.
"""
from __future__ import annotations

from app.services import execution


class _JournalDB:
    """Minimal fake with the real insert_raw contract + a source CHECK."""

    def __init__(self, allowed_sources=("auto", "approved"), fail_all=False):
        self.allowed_sources = set(allowed_sources)
        self.fail_all = fail_all
        self.rows: list[dict] = []

    def insert(self, table, row):  # pragma: no cover - swallow-path only
        self.rows.append(dict(row))
        return row

    def insert_raw(self, table, row):
        if self.fail_all:
            return None, ('{"message": "new row violates row-level security '
                          'policy", "code": "42501"}')
        src = str(row.get("source") or "")
        if src not in self.allowed_sources:
            return None, ('{"message": "new row for relation \\"paper_trades\\" '
                          'violates check constraint '
                          '\\"paper_trades_source_check\\"", "code": "23514"}')
        self.rows.append(dict(row))
        return row, None


def _trade(source: str) -> dict:
    return {"asset": "AUDCHF", "direction": "BUY", "volume": 0.01,
            "entry_price": 0.5831, "stop_loss": 0.5761,
            "take_profit": 0.5936, "status": "open", "source": source,
            "ticket": "PAPER-000041", "user_id": "u1"}


def test_extended_source_falls_back_instead_of_losing_the_trade():
    db = _JournalDB(allowed_sources=("auto", "approved"))  # migration 031 absent
    err = execution.record_trade(db, _trade("extended"))
    assert err is None                     # the position IS journaled
    assert len(db.rows) == 1
    assert db.rows[0]["source"] == "approved"   # schema-safe label
    assert db.rows[0]["ticket"] == "PAPER-000041"   # same trade
    # The levels are snapshotted for the monitor's moved-SL badge (mig 021).
    assert db.rows[0]["initial_stop_loss"] == 0.5761


def test_extended_source_kept_once_migration_031_is_applied():
    db = _JournalDB(allowed_sources=("auto", "approved", "extended"))
    assert execution.record_trade(db, _trade("extended")) is None
    assert [r["source"] for r in db.rows] == ["extended"]


def test_unrelated_failure_is_returned_not_hidden():
    db = _JournalDB(fail_all=True)
    err = execution.record_trade(db, _trade("extended"))
    assert err and "row-level security" in err   # caller can warn the user
    assert db.rows == []                          # nothing pretend-written


def test_old_fake_without_insert_raw_still_works():
    class _Plain:
        def __init__(self):
            self.rows = []

        def insert(self, table, row):
            self.rows.append(dict(row))

    db = _Plain()
    assert execution.record_trade(db, _trade("auto")) is None
    assert len(db.rows) == 1
