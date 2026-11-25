"""Perf guard for execution.monitor_snapshot (production: ~27.6s).

Contract under test:
  * monitor_snapshot calls fetch_display_prices EXACTLY once and
    fetch_spot_prices ZERO times (one price batch for the monitor).
  * independent blocking DB stages overlap (elapsed < sum of the
    simulated blocking delays) while results stay correct.
  * response shape unchanged (marks, sources, freshness, stats, rules).

Run from backend/: python -m pytest tests/test_monitor_perf.py -q
"""
from __future__ import annotations

import threading
import time

import pytest

from app.models.schemas import AppSettings
from app.services import execution as execution_mod
from tests.test_workers import FakeDatabase


class DelayDB(FakeDatabase):
    """FakeDatabase whose select() blocks like a real Supabase read.

    Records every call's (table, start, end) plus the max concurrent
    depth so the test can prove independent stages overlapped.
    """

    def __init__(self, delay: float = 0.05, **kw):
        super().__init__(**kw)
        self.delay = delay
        self.calls: list[tuple[str, float, float]] = []
        self._lock = threading.Lock()
        self._depth = 0
        self.max_depth = 0

    def select(self, *a, **k):
        with self._lock:
            self._depth += 1
            self.max_depth = max(self.max_depth, self._depth)
        start = time.monotonic()
        time.sleep(self.delay)  # simulated blocking Supabase round-trip
        try:
            return super().select(*a, **k)
        finally:
            end = time.monotonic()
            table = a[0] if a else k.get("table", "?")
            with self._lock:
                self._depth -= 1
                self.calls.append((str(table), start, end))


class _FakeBroker:
    async def all_positions(self):
        return []


def _rows() -> dict[str, list[dict]]:
    now = "2026-11-20T12:00:00+00:00"
    return {
        "paper_trades": [
            {"id": "o1", "ticket": "PAPER-EURUSD", "asset": "EURUSD",
             "direction": "BUY", "volume": 0.01, "entry_price": 1.0850,
             "stop_loss": 1.0800, "take_profit": None,
             "status": "open", "source": "auto", "created_at": now},
            {"id": "o2", "ticket": "PAPER-USDJPY", "asset": "USDJPY",
             "direction": "BUY", "volume": 0.01, "entry_price": 159.29,
             "stop_loss": 158.79, "take_profit": None,
             "status": "open", "source": "auto", "created_at": now},
            # closed WITH pnl — no extra read needed
            {"id": "c1", "ticket": "PAPER-C1", "asset": "EURUSD",
             "direction": "BUY", "volume": 0.01, "entry_price": 1.0800,
             "stop_loss": 1.0750, "take_profit": None, "pnl": 5.0,
             "status": "closed", "source": "auto",
             "created_at": "2026-11-18T12:00:00+00:00",
             "closed_at": "2026-11-20T10:00:00+00:00"},
            # closed WITHOUT pnl — needs a partial_realized_pnl lookup each
            {"id": "c2", "ticket": "P-T1", "asset": "EURUSD",
             "direction": "BUY", "volume": 0.01, "entry_price": 1.0800,
             "stop_loss": 1.0750, "take_profit": None, "pnl": None,
             "status": "closed", "source": "auto",
             "created_at": "2026-11-18T12:00:00+00:00",
             "closed_at": "2026-11-20T10:00:00+00:00"},
            {"id": "c3", "ticket": "P-T2", "asset": "USDJPY",
             "direction": "BUY", "volume": 0.01, "entry_price": 159.00,
             "stop_loss": 158.50, "take_profit": None, "pnl": None,
             "status": "closed", "source": "auto",
             "created_at": "2026-11-18T12:00:00+00:00",
             "closed_at": "2026-11-20T10:00:00+00:00"},
        ],
        "signal_logs": [
            {"ticket": "P-T1", "event": "closed", "pnl": 1.5,
             "created_at": "2026-11-20T10:00:00+00:00"},
            {"ticket": "P-T2", "event": "closed", "pnl": 2.5,
             "created_at": "2026-11-20T10:00:00+00:00"},
        ],
    }


@pytest.mark.asyncio
async def test_monitor_snapshot_single_price_batch_and_overlapping_db(
        monkeypatch) -> None:
    """ONE display batch, ZERO spot calls, overlapping DB stages, same shape."""
    from app.integrations.quotes import DisplayQuote

    execution_mod._conv_rates_cache.clear()
    display_calls: list[list[str]] = []
    spot_calls: list[list[str]] = []
    spots = {"EURUSD": 1.0850, "USDJPY": 159.29, "JPYUSD": 1 / 159.29}

    async def fake_display(assets, **_kw):
        display_calls.append([str(a).upper() for a in (assets or [])])
        prices, meta, failures = {}, {}, {}
        for a in (assets or []):
            key = str(a).upper()
            if key not in spots:
                # Unknown leg, not a synthetic trusted price: the monitor
                # must fail closed on conversion exactly like prod.
                failures[key] = f"{key}: no feed"
                continue
            px = float(spots[key])
            prices[key] = px
            meta[key] = DisplayQuote(price=px, source="yahoo",
                                     age_seconds=1.0, market_state="open",
                                     stale=False)
        return prices, meta, failures

    async def fake_spot(assets, **_kw):
        spot_calls.append([str(a).upper() for a in (assets or [])])
        return {}, {str(a).upper(): "no feed" for a in (assets or [])}

    async def fake_snaps(assets, **_kw):
        return {}

    monkeypatch.setattr(execution_mod.quotes, "fetch_display_prices",
                        fake_display)
    monkeypatch.setattr(execution_mod.quotes, "fetch_spot_prices", fake_spot)
    monkeypatch.setattr(execution_mod.quotes, "fetch_all_snapshots",
                        fake_snaps)

    db = DelayDB(delay=0.05, rows=_rows())
    t0 = time.monotonic()
    snap = await execution_mod.monitor_snapshot(
        db, _FakeBroker(), AppSettings())
    elapsed = time.monotonic() - t0

    # ---- A. one price batch ------------------------------------------------
    assert len(display_calls) == 1, display_calls
    assert spot_calls == [], f"spot feed must not be hit: {spot_calls}"
    # every conversion leg the helper needs was already in the one batch
    assert "JPYUSD" in display_calls[0]

    # ---- B. DB stages overlapped -------------------------------------------
    total_blocking = sum(e - s for _, s, e in db.calls)
    assert db.max_depth >= 2, "independent DB reads never overlapped"
    assert elapsed < total_blocking - 0.02, (
        f"no overlap: elapsed={elapsed:.3f}s vs "
        f"blocking-sum={total_blocking:.3f}s over {len(db.calls)} reads")

    # ---- C. results still correct ------------------------------------------
    assert len(snap.open_positions) == 2
    by_asset = {p.asset: p for p in snap.open_positions}
    assert by_asset["EURUSD"].current_price == pytest.approx(1.0850)
    assert by_asset["USDJPY"].current_price == pytest.approx(159.29)
    assert {p.price_source for p in snap.open_positions} == {"spot"}
    assert all(p.price_age_s == pytest.approx(1.0)
               for p in snap.open_positions)
    assert all(p.market_state == "open" for p in snap.open_positions)
    # partial slices summed into the stats, closed row counted
    assert snap.stats.closed_count == 3
    assert snap.stats.pnl_total == pytest.approx(9.0)
    assert snap.stats.open_positions == 2
    # no-shape-change spot checks
    assert snap.feed_status is not None and snap.feed_status.state == "ok"
    assert snap.exit_rules is not None
    assert snap.kill is not None


def _stub_quiet_feed(monkeypatch, spots: dict[str, float],
                     snaps: dict[str, float] | None = None,
                     spot_prices: dict[str, float] | None = None) -> dict:
    """Display serves only listed spots (unknown → failure row, never a
    synthetic price); snapshots/spot fakes record calls for assertions."""
    from app.integrations.quotes import DisplayQuote

    calls: dict[str, list] = {"display": [], "spot": [], "snaps": []}

    async def fake_display(assets, **_kw):
        calls["display"].append([str(a).upper() for a in (assets or [])])
        prices, meta, failures = {}, {}, {}
        for a in (assets or []):
            key = str(a).upper()
            if key not in spots:
                failures[key] = f"{key}: no feed"
                continue
            px = float(spots[key])
            prices[key] = px
            meta[key] = DisplayQuote(price=px, source="yahoo",
                                     age_seconds=1.0, market_state="open",
                                     stale=False)
        return prices, meta, failures

    async def fake_spot(assets, **_kw):
        calls["spot"].append([str(a).upper() for a in (assets or [])])
        board = spot_prices if spot_prices is not None else {}
        return ({a: board[a] for a in (assets or [])
                 if str(a).upper() in board},
                {str(a).upper(): "no feed" for a in (assets or [])
                 if str(a).upper() not in board})

    async def fake_snaps(assets, **_kw):
        calls["snaps"].append([str(a).upper() for a in (assets or [])])
        return {str(a).upper(): {"price": (snaps or {})[str(a).upper()]}
                for a in (assets or []) if str(a).upper() in (snaps or {})}

    monkeypatch.setattr(execution_mod.quotes, "fetch_display_prices",
                        fake_display)
    monkeypatch.setattr(execution_mod.quotes, "fetch_spot_prices", fake_spot)
    monkeypatch.setattr(execution_mod.quotes, "fetch_all_snapshots",
                        fake_snaps)
    return calls


@pytest.mark.asyncio
async def test_monitor_daily_and_broker_marks_never_seed_pnl(
        monkeypatch) -> None:
    """Daily-fallback and broker-book marks display but never price uPnL.

    Display misses USDJPY; only a daily snapshot mark exists and no JPYUSD
    leg anywhere → uPnL must be None (fail-closed, not 1/daily dollars) while
    the card still shows the daily mark. A broker-book-only asset likewise
    shows source "broker" with uPnL None (broker marks are not seeds).
    """
    execution_mod._conv_rates_cache.clear()
    _stub_quiet_feed(monkeypatch, spots={}, snaps={"USDJPY": 160.0},
                     spot_prices={})
    now = "2026-11-20T12:00:00+00:00"
    db = FakeDatabase(rows={"paper_trades": [
        {"id": "o1", "ticket": "PAPER-USDJPY", "asset": "USDJPY",
         "direction": "BUY", "volume": 0.01, "entry_price": 159.29,
         "stop_loss": 158.79, "take_profit": None,
         "status": "open", "source": "auto", "created_at": now},
        {"id": "o2", "ticket": "PAPER-EURGBP", "asset": "EURGBP",
         "direction": "BUY", "volume": 0.01, "entry_price": 0.8500,
         "stop_loss": 0.8450, "take_profit": None,
         "status": "open", "source": "auto", "created_at": now},
    ]})

    from types import SimpleNamespace

    class _BookBroker:
        async def all_positions(self):
            return [SimpleNamespace(ticket="PAPER-EURGBP", asset="EURGBP",
                                    current_price=0.8543)]

    snap = await execution_mod.monitor_snapshot(
        db, _BookBroker(), AppSettings())
    by_asset = {p.asset: p for p in snap.open_positions}
    assert set(by_asset) == {"USDJPY", "EURGBP"}

    # marks still displayed with honest provenance …
    assert by_asset["USDJPY"].current_price == pytest.approx(160.0)
    assert by_asset["USDJPY"].price_source == "daily"
    assert by_asset["EURGBP"].current_price == pytest.approx(0.8543)
    assert by_asset["EURGBP"].price_source == "broker"

    # … but neither seeded PnL conversion: no daily-derived dollars
    assert by_asset["USDJPY"].unrealized_pnl is None
    assert by_asset["EURGBP"].unrealized_pnl is None
    # risk keeps the helper's PRE-EXISTING entry-inverse fallback
    # (500/159.29 ≈ 3.14) — provably not the daily mark (500/160 = 3.125):
    # the daily print displays, it never prices.
    assert by_asset["USDJPY"].risk_amount == pytest.approx(
        round(500 / 159.29, 2), abs=1e-9)
    assert by_asset["USDJPY"].risk_amount != pytest.approx(
        round(500 / 160.0, 2))
    assert any("daily close" in n
               for n in by_asset["USDJPY"].calc_notes)


@pytest.mark.asyncio
async def test_monitor_partial_lookup_failure_cannot_500(
        monkeypatch) -> None:
    """One exploding partial_realized_pnl degrades its row, not the page."""
    execution_mod._conv_rates_cache.clear()
    _stub_quiet_feed(monkeypatch, spots={"EURUSD": 1.0850},
                     spot_prices={"EURUSD": 1.0850})
    now = "2026-11-20T12:00:00+00:00"
    db = FakeDatabase(rows={"paper_trades": [
        {"id": "o1", "ticket": "PAPER-EURUSD", "asset": "EURUSD",
         "direction": "BUY", "volume": 0.01, "entry_price": 1.0850,
         "stop_loss": 1.0800, "take_profit": None,
         "status": "open", "source": "auto", "created_at": now},
        {"id": "c1", "ticket": "P-GOOD", "asset": "EURUSD",
         "direction": "BUY", "volume": 0.01, "entry_price": 1.0800,
         "stop_loss": 1.0750, "take_profit": None, "pnl": 5.0,
         "status": "closed", "source": "auto",
         "created_at": "2026-11-18T12:00:00+00:00",
         "closed_at": "2026-11-20T10:00:00+00:00"},
        {"id": "c2", "ticket": "P-BAD", "asset": "EURUSD",
         "direction": "BUY", "volume": 0.01, "entry_price": 1.0800,
         "stop_loss": 1.0750, "take_profit": None, "pnl": None,
         "status": "closed", "source": "auto",
         "created_at": "2026-11-18T12:00:00+00:00",
         "closed_at": "2026-11-20T10:00:00+00:00"},
    ]})

    seen: list[str] = []

    def _flaky(db, ticket: str):
        seen.append(ticket)
        if ticket == "P-BAD":
            raise RuntimeError("supabase boom")
        return 1.0

    monkeypatch.setattr(execution_mod, "partial_realized_pnl", _flaky)
    snap = await execution_mod.monitor_snapshot(
        db, _FakeBroker(), AppSettings())
    # page survived the exploding lookup; bad row excluded, good row counted
    assert seen == ["P-BAD"]
    assert len(snap.open_positions) == 1
    assert snap.stats.closed_count == 1
    assert snap.stats.pnl_total == pytest.approx(5.0)
