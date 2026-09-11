"""Monitor windows must be measured at `closed_at`, not `created_at`.

Regression (prod 2026-09-11): three positions closed for a combined
-6.26 USD, yet the monitor showed "PnL วันนี้ $0.00" and "เทรดวันนี้ 1"
because the window filtered on the day a trade was OPENED. A position
opened last week and closed this morning fell into no window at all.

`execution.realized_stats` is now the single source for these fields and
is shared by /monitor (monitor_snapshot) and the stats-reset response
(trading._fresh_stats), so the two can't drift apart again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.execution import realized_stats


def _row(*, opened_days_ago: float, closed_days_ago: float | None,
         pnl: float | None, status: str = "closed") -> dict:
    now = datetime.now(timezone.utc)
    opened = now - timedelta(days=opened_days_ago)
    row = {"status": status, "pnl": pnl,
           "created_at": opened.isoformat()}
    row["closed_at"] = (None if closed_days_ago is None
                        else (now - timedelta(days=closed_days_ago)).isoformat())
    return row


def test_trade_opened_last_week_but_closed_today_counts_today():
    """THE bug: window must be the close date, not the open date."""
    rows = [_row(opened_days_ago=9, closed_days_ago=0.0, pnl=-8.34),
            _row(opened_days_ago=8, closed_days_ago=0.0, pnl=-1.16),
            _row(opened_days_ago=7, closed_days_ago=0.0, pnl=3.24)]
    st = realized_stats(rows)
    assert st["trades_today"] == 3
    assert st["pnl_today"] == -6.26
    assert st["trades_week"] == 3
    assert st["pnl_week"] == -6.26
    assert st["pnl_total"] == -6.26


def test_today_week_and_total_windows_are_nested():
    rows = [_row(opened_days_ago=20, closed_days_ago=0.2, pnl=10.0),
            _row(opened_days_ago=20, closed_days_ago=3.0, pnl=5.0),
            _row(opened_days_ago=20, closed_days_ago=8.0, pnl=-2.0)]
    st = realized_stats(rows)
    assert (st["trades_today"], st["pnl_today"]) == (1, 10.0)
    assert (st["trades_week"], st["pnl_week"]) == (2, 15.0)   # 8d row excluded
    assert (st["pnl_total"], st["closed_count"]) == (13.0, 3)


def test_open_and_pnl_less_rows_never_reach_realized_fields():
    """Realized means realized: open rows and unmarked closes are ignored."""
    rows = [
        _row(opened_days_ago=1, closed_days_ago=None, pnl=None, status="open"),
        _row(opened_days_ago=30, closed_days_ago=0.1, pnl=None, status="closed"),
        _row(opened_days_ago=30, closed_days_ago=0.1, pnl=4.0),
    ]
    st = realized_stats(rows)
    assert st["trades_today"] == 1 and st["pnl_today"] == 4.0
    assert st["closed_count"] == 1          # pnl-less close is not counted
    assert st["win_rate"] == 100.0          # 1 win / 1 counted close
    assert st["pnl_total"] == 4.0


def test_win_rate_and_closed_count_share_one_denominator():
    rows = [_row(opened_days_ago=5, closed_days_ago=1.0, pnl=20.0),
            _row(opened_days_ago=5, closed_days_ago=1.0, pnl=-10.0),
            _row(opened_days_ago=5, closed_days_ago=1.0, pnl=0.0)]
    st = realized_stats(rows)
    assert st["closed_count"] == 3
    assert st["win_rate"] == 33.3           # a flat close is not a win
    assert st["pnl_week"] == 10.0


def test_missing_closed_at_still_reaches_total_but_not_windows():
    rows = [_row(opened_days_ago=40, closed_days_ago=None, pnl=7.5)]
    st = realized_stats(rows)
    assert st["trades_today"] == 0 and st["pnl_today"] == 0.0
    assert st["trades_week"] == 0 and st["pnl_week"] == 0.0
    assert st["pnl_total"] == 7.5 and st["closed_count"] == 1


def test_closed_rows_arg_is_respected_and_never_raises():
    """/monitor passes its already-filtered rows; junk input is survivable."""
    rows = [_row(opened_days_ago=5, closed_days_ago=0.0, pnl=1.0),
            _row(opened_days_ago=5, closed_days_ago=None, pnl=9.0,
                 status="open")]
    # explicit closed_rows wins over a mixed `rows` list
    st = realized_stats(rows, [rows[0]])
    assert st["pnl_total"] == 1.0 and st["closed_count"] == 1
    # even a hostile payload returns zeros instead of exploding a page render
    st = realized_stats([{"status": "closed", "pnl": "not-a-number",
                          "closed_at": "not-a-date"}])
    assert st["pnl_total"] == 0.0 and st["pnl_today"] == 0.0
