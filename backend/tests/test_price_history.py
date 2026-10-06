"""Price history storage — validate before insert, fetch only the missing.

Owner 2026-10-06: history must live in the database, syncs fetch only new
values, and every fetched bar is checked before it is stored.

The properties that matter:
  * an invalid bar never reaches the table (bad print protection),
  * a stored bar older than the latest is never overwritten (reproducible
    re-runs), a disagreeing feed is reported as a conflict instead,
  * only the gap + the forming bar move per sync — never a full rewrite.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_price_history.py -v
"""
from __future__ import annotations

import pytest

from app.integrations.quotes import Candle
from app.services import price_history

TODAY = "2026-10-06"


class HistDB:
    """In-memory Database surface with REAL date ordering.

    The shared fakes cannot be used here: one ignores the `order` parameter
    (which this service depends on for oldest-first reads) and the other's
    `available` is a read-only property. Both gaps would make these tests pass
    for the wrong reason.
    """

    available = True

    def __init__(self):
        self.t: list[dict] = []

    def insert(self, table, row):
        self.t.append(dict(row))
        return dict(row)

    def select(self, table, filters=None, order="bar_date", desc=False,
               limit=50, offset=0, **k):
        rows = [r for r in self.t
                if all(r.get(c) == v for c, v in (filters or {}).items())]
        rows.sort(key=lambda r: str(r.get(order or "")), reverse=bool(desc))
        return [dict(r) for r in rows[offset:offset + limit]]

    def delete(self, table, filters=None):
        before = len(self.t)
        self.t = [r for r in self.t
                  if not all(r.get(c) == v for c, v in (filters or {}).items())]
        return len(self.t) != before

    def count(self, table, filters=None, **k):
        return len([r for r in self.t
                    if all(r.get(c) == v for c, v in (filters or {}).items())])


def bar(day: str, o=1.1, h=1.11, l=1.09, c=1.105):
    return {"asset": "EURUSD", "bar_date": day, "open": o, "high": h,
            "low": l, "close": c}


class TestValidateBar:
    def test_clean_bar_passes(self):
        assert price_history.validate_bar(
            "EURUSD", "2026-10-01", 1.1, 1.11, 1.09, 1.105,
            prev_close=1.10, today=TODAY) == []

    @pytest.mark.parametrize("bad", [0, -1.1, None, "x", float("nan"),
                                     float("inf")])
    def test_bad_price_is_quarantined(self, bad):
        errs = price_history.validate_bar(
            "EURUSD", "2026-10-01", bad, 1.11, 1.09, 1.105, today=TODAY)
        assert any(e.startswith("bad_") for e in errs)

    def test_inverted_and_inconsistent_ranges_fail(self):
        assert "inverted_range" in price_history.validate_bar(
            "EURUSD", "2026-10-01", 1.1, 1.08, 1.09, 1.105, today=TODAY)
        # high 1.10 sits BELOW the 1.105 close — the bar contradicts itself
        assert "range_inconsistent" in price_history.validate_bar(
            "EURUSD", "2026-10-01", 1.1, 1.10, 1.09, 1.105, today=TODAY)

    def test_tiny_float_dust_does_not_fail(self):
        assert price_history.validate_bar(
            "EURUSD", "2026-10-01", 1.1, 1.1 * (1 + 1e-12), 1.09, 1.1,
            today=TODAY) == []

    def test_future_and_missing_dates_fail(self):
        assert "future_bar" in price_history.validate_bar(
            "EURUSD", "2026-10-20", 1.1, 1.11, 1.09, 1.105, today=TODAY)
        assert price_history.validate_bar(
            "EURUSD", "", 1.1, 1.11, 1.09, 1.105, today=TODAY) == ["no_date"]
        assert price_history.validate_bar(
            "EURUSD", "not-a-date", 1.1, 1.11, 1.09, 1.105,
            today=TODAY) == ["bad_date"]

    def test_a_25pct_jump_is_quarantined_not_stored(self):
        errs = price_history.validate_bar(
            "EURUSD", "2026-10-01", 1.1, 1.4, 1.09, 1.375,
            prev_close=1.10, today=TODAY)
        assert "spike_suspect" in errs

    def test_validator_never_raises(self):
        assert isinstance(price_history.validate_bar(
            "EURUSD", object(), object(), object(), object(), object()), list)


def _candle(day: str, c=1.105, o=1.1, h=1.11, l=1.09):
    import calendar
    from datetime import datetime, timezone
    t = calendar.timegm(datetime.fromisoformat(day).timetuple())
    assert datetime.fromtimestamp(t, timezone.utc).date().isoformat() == day
    return Candle(o=o, h=h, l=l, c=c, t=t)


class TestSyncIncremental:
    def _db(self, dates):
        db = HistDB()
        for d in dates:
            db.insert(price_history.TABLE, bar(d))
        return db

    def test_only_the_gap_is_inserted(self):
        db = self._db(["2026-09-28", "2026-09-29", "2026-09-30"])
        calls = []

        def fetch(asset, span):
            calls.append((asset, span))
            return [_candle("2026-09-29"), _candle("2026-09-30"),
                    _candle("2026-10-01"), _candle("2026-10-02")]

        rep = price_history.sync_asset(db, "EURUSD", days=1095, fetch=fetch,
                                       _today=TODAY)
        assert rep["inserted"] == 2
        assert rep["skipped"] == 2
        assert rep["invalid"] == [] and rep["conflicts"] == []
        assert calls and calls[0][0] == "EURUSD"
        # the fetch span covers the gap, not the whole history
        assert calls[0][1] < 1095

    def test_invalid_bars_never_reach_the_table(self):
        db = self._db(["2026-09-30"])
        n_before = len(db.t)

        def fetch(asset, span):
            good = _candle("2026-10-01")
            bad_price = _candle("2026-10-02", c=0.0)
            inverted = _candle("2026-10-03", h=1.0, l=1.2)
            spike = _candle("2026-10-05", o=1.1, h=1.5, l=1.09, c=1.45)
            dateless = Candle(o=1.1, h=1.11, l=1.09, c=1.105, t=0)
            return [good, bad_price, inverted, spike, dateless]

        rep = price_history.sync_asset(db, "eurusd", fetch=fetch, _today=TODAY)
        assert rep["inserted"] == 1
        assert len(rep["invalid"]) == 4
        assert len(db.t) == n_before + 1
        reasons = {r["bar_date"]: r["reasons"] for r in rep["invalid"]}
        assert reasons["2026-10-05"] == ["spike_suspect"]

    def test_rounding_dust_is_not_a_conflict(self):
        """Stored bars are rounded to 6dp; a reprint differing only in the
        7th decimal is the same bar, not a revision. Without this tolerance
        every daily sync reported phantom conflicts."""
        db = HistDB()
        db.insert(price_history.TABLE, bar("2026-09-30"))

        def fetch(asset, span):
            dusty = _candle("2026-09-30", c=1.1050004, o=1.1000002,
                            h=1.1100001, l=1.0899998)
            return [dusty, _candle("2026-10-01")]

        rep = price_history.sync_asset(db, "EURUSD", fetch=fetch, _today=TODAY)
        assert rep["conflicts"] == []
        assert rep["skipped"] == 1 and rep["inserted"] == 1

    def test_a_real_revision_still_reports(self):
        db = HistDB()
        db.insert(price_history.TABLE, bar("2026-09-30"))

        def fetch(asset, span):
            return [_candle("2026-09-30", c=1.19, h=1.20),
                    _candle("2026-10-01")]

        rep = price_history.sync_asset(db, "EURUSD", fetch=fetch, _today=TODAY)
        assert len(rep["conflicts"]) == 1

    def test_a_frozen_bar_is_never_overwritten(self):
        """The feed reprints 09-30 with a different close: report the
        conflict, keep the stored bar. Reproducibility beats freshness."""
        db = self._db(["2026-09-30"])

        def fetch(asset, span):
            # the conflicting reprint must itself be a VALID bar, or the
            # test would pass via quarantine instead of via conflict
            return [_candle("2026-09-30", c=1.19, h=1.20)]

        rep = price_history.sync_asset(db, "EURUSD", fetch=fetch, _today=TODAY)
        assert len(rep["conflicts"]) == 1
        assert rep["conflicts"][0]["bar_date"] == "2026-09-30"
        stored = db.select(price_history.TABLE,
                           filters={"asset": "EURUSD", "bar_date": "2026-09-30"})
        assert stored and float(stored[0]["close"]) == 1.105

    def test_the_forming_bar_may_refresh(self):
        """Today's bar is still printing — it is the ONE date allowed to move."""
        db = self._db(["2026-10-05", "2026-10-06"])

        def fetch(asset, span):
            assert span == 7, "a forming refresh needs a tiny span, not history"
            return [_candle("2026-10-06", c=1.12, h=1.13)]

        rep = price_history.sync_asset(db, "EURUSD", fetch=fetch, _today=TODAY)
        assert rep["refreshed"] == 1
        assert rep["inserted"] == 0
        stored = db.select(price_history.TABLE,
                           filters={"asset": "EURUSD", "bar_date": "2026-10-06"})
        assert stored and float(stored[0]["close"]) == 1.12

    def test_small_spans_bypass_the_30_bar_live_minimum(self, monkeypatch):
        """The forming-bar refresh fetches a 7-day span (~5 bars). Going
        through fetch_candles would raise 'only N candles' every time, which
        is exactly how the refresh path silently died in production."""
        import app.services.price_history as ph

        seen = {}

        async def fake_yahoo(asset, client, days=40):
            seen["days"] = days
            return [_candle("2026-10-06")]

        async def boom(asset, client, days=40):
            raise AssertionError("fallback chain must not run on success")

        monkeypatch.setattr("app.integrations.quotes._fetch_yahoo_candles",
                            fake_yahoo)
        monkeypatch.setattr("app.integrations.quotes.fetch_candles", boom)
        out = ph._fetch("EURUSD", 7)
        assert len(out) == 1 and seen["days"] == 7

    def test_yahoo_failure_falls_back_to_the_full_chain(self, monkeypatch):
        import app.integrations.quotes as q
        import app.services.price_history as ph

        async def fail(asset, client, days=40):
            raise q.QuotesUnavailable("yahoo down")

        async def chain(asset, client, days=40):
            return [_candle("2026-10-06")]

        monkeypatch.setattr(q, "_fetch_yahoo_candles", fail)
        monkeypatch.setattr(q, "fetch_candles", chain)
        assert len(ph._fetch("EURUSD", 7)) == 1

    def test_db_down_and_fetch_down_are_reports_not_crashes(self):
        db = HistDB()
        db.available = False
        assert "error" in price_history.sync_asset(db, "EURUSD", _today=TODAY)

        def boom(asset, span):
            raise RuntimeError("yahoo down")

        rep = price_history.sync_asset(HistDB(), "EURUSD", fetch=boom,
                                       _today=TODAY)
        assert "error" in rep and rep["inserted"] == 0


class TestLoadSeries:
    def test_reads_back_oldest_first_with_time(self, monkeypatch):
        import app.services.price_history as ph

        db = HistDB()

        def fake_sync(db_, asset_, days_=1095, **k):
            for d in ("2026-10-03", "2026-10-01", "2026-10-02"):
                b = bar(d)
                b["asset"] = asset_
                db_.insert(ph.TABLE, b)
            return {"asset": asset_, "fetched": 3, "inserted": 3,
                    "refreshed": 0, "skipped": 0, "invalid": [],
                    "conflicts": [], "fresh": False}

        monkeypatch.setattr(ph, "sync_asset", fake_sync)
        series, report = ph.load_series(db, ["EURUSD"], days=1095)
        # inserted out of order — the readback must still come out ascending
        assert [c.t for c in series["EURUSD"]] == sorted(
            c.t for c in series["EURUSD"])
        assert all(c.t > 0 for c in series["EURUSD"])
        assert report["totals"]["inserted"] == 3
        assert report["source"].startswith("price_history_daily")


class TestSimulationFallback:
    def test_history_failure_degrades_to_direct_fetch(self, monkeypatch):
        from app.services import simulation
        from tests.test_simulation import FakeDB

        def boom(db_, assets_, days_=1095):
            raise RuntimeError("PGRST205 no table")

        async def _noop():
            return None

        monkeypatch.setattr("app.services.price_history.load_series", boom)
        monkeypatch.setattr(simulation, "_fetch_series",
                            lambda cfg: {"EURUSD": []})
        series, report = simulation._load_history(
            FakeDB(), {"assets": ["EURUSD"], "days": 1095})
        assert series == {"EURUSD": []}
        assert report["source"].startswith("direct fetch")
        assert "error" in report
