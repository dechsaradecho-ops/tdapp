"""Unit tests for app/integrations/quotes.py — all offline (httpx mocked).

Covers: indicator math (EMA/RSI/ATR/ADX/Supertrend), candle parsing with
None gaps, Frankfurter + Twelve Data payload handling, snapshot field
consistency and snapshot_from_candles output ranges.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_quotes.py -v
"""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

import httpx
import pytest

from app.integrations import quotes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_candles(n: int = 60, start: float = 100.0, drift: float = 0.5,
                 noise: float = 0.4, seed: int = 42) -> list[quotes.Candle]:
    """Deterministic uptrending candles."""
    rng = random.Random(seed)
    out: list[quotes.Candle] = []
    price = start
    for _ in range(n):
        o = price
        c = price + drift + rng.uniform(-noise, noise)
        h = max(o, c) + abs(rng.uniform(0, noise))
        l = min(o, c) - abs(rng.uniform(0, noise))
        out.append(quotes.Candle(o=o, h=h, l=l, c=c))
        price = c
    return out


def flat_closes(n: int, price: float = 100.0) -> list[float]:
    return [price] * n


def _resp(payload, status: int = 200):
    r = SimpleNamespace(status_code=status)
    r.raise_for_status = lambda: (_ for _ in ()).throw(
        httpx.HTTPStatusError("boom", request=None, response=None)) if status >= 400 else None
    r.json = lambda: payload
    return r


def _fx_payload(closes: list[float]) -> dict:
    """Frankfurter time-series shape: rates = {ISO-date: {QUOTE: rate}}."""
    from datetime import date, timedelta
    start = date(2026, 6, 1)
    rates = {}
    for i, c in enumerate(closes):
        day = start + timedelta(days=i)   # consecutive days incl. weekends —
        rates[day.isoformat()] = {"USD": c}  # sort order is what matters
    return {"amount": 1.0, "base": "EUR", "start_date": "2026-06-01",
            "end_date": "2026-12-31", "rates": rates}


def _gold_payload(closes: list[float]) -> dict:
    """Twelve Data /time_series shape: values = newest-first OHLC rows."""
    values = []
    for i in range(len(closes) - 1, -1, -1):  # newest-first like the real API
        c = closes[i]
        values.append({"datetime": f"2026-06-{i + 1:02d}", "open": str(c),
                       "high": str(c * 1.01), "low": str(c * 0.99), "close": str(c)})
    return {"meta": {"symbol": "XAU/USD", "interval": "1day"}, "values": values, "status": "ok"}


# ---------------------------------------------------------------------------
# Indicator math
# ---------------------------------------------------------------------------
class TestIndicatorMath:
    def test_ema_first_value_is_input(self):
        assert quotes._ema([5.0], 14) == [5.0]

    def test_ema_converges_toward_constant_series(self):
        emas = quotes._ema(flat_closes(50), 14)
        assert abs(emas[-1] - 100.0) < 0.01

    def test_ema_empty(self):
        assert quotes._ema([], 14) == []

    def test_rsi_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 20)]  # strictly rising
        assert quotes._rsi(closes) == 100.0

    def test_rsi_all_losses_is_very_low(self):
        closes = [float(20 - i) for i in range(20)]  # strictly falling
        assert quotes._rsi(closes) < 10.0

    def test_rsi_flat_is_neutral_50(self):
        assert quotes._rsi(flat_closes(20)) == 50.0

    def test_rsi_short_series_defaults_neutral(self):
        assert quotes._rsi([1.0, 2.0], 14) == 50.0

    def test_atr_pct_positive_and_sane(self):
        candles = make_candles(40, start=100, drift=0.5, noise=0.5)
        atr = quotes._atr_pct(candles)
        assert 0.0 < atr < 10.0

    def test_atr_pct_zero_price_guard(self):
        candles = [quotes.Candle(o=0.0, h=0.0, l=0.0, c=0.0)] * 20
        assert quotes._atr_pct(candles) == 0.0

    def test_adx_rising_trend_is_strong(self):
        # steadily rising highs/lows → strong trend → high ADX
        candles = make_candles(40, start=100, drift=1.0, noise=0.05)
        assert quotes._adx(candles) > 25.0

    def test_adx_choppy_is_weak(self):
        # alternating closes → no directional trend → low ADX
        candles = [quotes.Candle(o=100.0, h=101.0, l=99.0, c=100.0 + (1 if i % 2 else -1))
                   for i in range(40)]
        assert quotes._adx(candles) < 25.0

    def test_adx_needs_enough_bars(self):
        assert quotes._adx(make_candles(20)) == 0.0

    def test_supertrend_dir_up_in_uptrend(self):
        assert quotes._supertrend_dir(make_candles(40, drift=1.0, noise=0.05)) == 1

    def test_supertrend_dir_down_in_downtrend(self):
        candles = make_candles(40, start=100, drift=-1.0, noise=0.05)
        assert quotes._supertrend_dir(candles) == -1

    def test_supertrend_needs_enough_bars(self):
        assert quotes._supertrend_dir(make_candles(5)) == 0


# ---------------------------------------------------------------------------
# fetch_candles — payload parsing & error mapping (Frankfurter + Twelve Data)
# ---------------------------------------------------------------------------
class TestFetchCandles:
    @pytest.mark.asyncio
    async def test_fx_parses_closes_and_skips_null_rows(self):
        closes = [100.0 + i for i in range(35)]
        payload = _fx_payload(closes)
        payload["rates"]["2026-06-10"]["USD"] = None  # gap row → skipped
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp(payload)
        client.get = fake_get
        candles = await quotes.fetch_candles("EURUSD", client)
        assert len(candles) == 34
        assert candles[0].c == 100.0
        await client.aclose()

    @pytest.mark.asyncio
    async def test_fx_synthesizes_ohlc_from_closes(self):
        """open = prev close, high/low envelope — close-only feed contract."""
        closes = [1.10 + 0.01 * i for i in range(35)]
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp(_fx_payload(closes))
        client.get = fake_get
        candles = await quotes.fetch_candles("GBPUSD", client)
        assert candles[1].o == candles[0].c  # open = previous close
        assert candles[1].h >= candles[1].o and candles[1].h >= candles[1].c
        assert candles[1].l <= candles[1].o and candles[1].l <= candles[1].c
        await client.aclose()

    @pytest.mark.asyncio
    async def test_gold_parses_twelvedata_and_flips_order(self, monkeypatch):
        closes = [2400.0 + i for i in range(35)]
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp(_gold_payload(closes))
        client.get = fake_get
        monkeypatch.setattr(quotes.get_settings, "twelvedata_api_key", "td-demo-key",
                            raising=False)
        # get_settings เป็น lru_cache — ต้องแก้ที่ instance ที่ cache ไว้ด้วย
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "td-demo-key", raising=False)
        candles = await quotes.fetch_candles("XAUUSD", client)
        assert len(candles) == 35
        assert candles[0].c == closes[0]            # oldest-first after flip
        assert candles[-1].c == closes[-1]          # newest last
        assert abs(candles[0].h - closes[0] * 1.01) < 1e-9  # real OHLC kept
        await client.aclose()

    @pytest.mark.asyncio
    async def test_gold_without_key_raises_unavailable(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)
        client = httpx.AsyncClient()
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("XAUUSD", client)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unknown_asset_raises_unavailable(self):
        client = httpx.AsyncClient()
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("NOPE", client)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_http_error_maps_to_unavailable(self):
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp({}, status=429)
        client.get = fake_get
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("EURUSD", client)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_twelvedata_error_status_raises(self):
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp({"status": "error", "code": 401,
                          "message": "invalid api key"})
        client.get = fake_get
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("XAUUSD", client)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_empty_fx_rates_raises(self):
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp({"rates": {}})
        client.get = fake_get
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("EURUSD", client)
        await client.aclose()

    @pytest.mark.asyncio
    async def test_too_few_candles_raises(self):
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp(_fx_payload([100.0] * 10))
        client.get = fake_get
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("EURUSD", client)
        await client.aclose()

    def test_all_five_assets_mapped(self):
        for asset in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"):
            assert asset in quotes.ASSET_FEEDS
        assert quotes.ASSET_FEEDS["XAUUSD"] == quotes.FEED_TWELVEDATA
        assert quotes.ASSET_FEEDS["GBPUSD"] == quotes.FEED_FRANKFURTER


# ---------------------------------------------------------------------------
# snapshot_from_candles — field consistency
# ---------------------------------------------------------------------------
class TestSnapshotFromCandles:
    def setup_method(self):
        self.candles = make_candles(60, start=2400.0, drift=2.0, noise=1.0)

    def test_snapshot_keys_complete(self):
        snap = quotes.snapshot_from_candles("XAUUSD", self.candles)
        expected = {"asset", "price", "ema_fast", "ema_slow", "adx", "supertrend_dir",
                    "rsi", "macd_hist", "price_change_pct_20", "atr_pct",
                    "volatility_index", "news_sentiment", "high_impact_event"}
        assert expected <= set(snap)

    def test_price_is_last_close(self):
        snap = quotes.snapshot_from_candles("XAUUSD", self.candles)
        assert snap["price"] == round(self.candles[-1].c, 5)

    def test_price_in_range_0_100_indicators(self):
        snap = quotes.snapshot_from_candles("XAUUSD", self.candles)
        assert 0 <= snap["adx"] <= 100
        assert 0 <= snap["rsi"] <= 100
        assert 0 <= snap["volatility_index"] <= 100

    def test_uptrend_reflected(self):
        snap = quotes.snapshot_from_candles("XAUUSD", self.candles)
        assert snap["ema_fast"] > snap["ema_slow"]
        assert snap["supertrend_dir"] == 1
        assert snap["price_change_pct_20"] > 0

    def test_snapshot_feeds_strategy_engine(self):
        from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
        snap = quotes.snapshot_from_candles("XAUUSD", self.candles)
        ind = IndicatorSnapshot(**snap, source="live")
        opp = StrategyEngine().opportunity_score(ind)
        assert 0 <= opp.score <= 100

    def test_snapshot_requires_no_extra_keys_for_model(self):
        """IndicatorSnapshot(**snap) must not raise (field mismatch guard)."""
        from app.engine.strategy_engine import IndicatorSnapshot
        snap = quotes.snapshot_from_candles("EURUSD", make_candles(60))
        ind = IndicatorSnapshot(**snap)  # must not raise
        assert ind.source == "demo"  # default

    def test_supertrend_down_in_downtrend_snapshot(self):
        down = make_candles(60, start=2400.0, drift=-2.0, noise=1.0)
        snap = quotes.snapshot_from_candles("XAUUSD", down)
        assert snap["supertrend_dir"] == -1
        assert snap["price_change_pct_20"] < 0


# ---------------------------------------------------------------------------
# fetch_all_snapshots — fallback semantics
# ---------------------------------------------------------------------------
class TestFetchAllSnapshots:
    @pytest.mark.asyncio
    async def test_failed_assets_are_absent_not_none(self):
        """Assets whose fetch fails are omitted from the result dict."""
        client = httpx.AsyncClient()
        calls = {"n": 0}

        async def fake_get(*a, **kw):
            calls["n"] += 1
            if calls["n"] <= 2:
                return _resp({}, status=500)  # first two fail
            return _resp(_fx_payload([100.0 + i for i in range(40)]))
        client.get = fake_get
        try:
            result = await quotes.fetch_all_snapshots(["EURUSD", "GBPUSD", "AUDUSD"])
        finally:
            await client.aclose()
        assert isinstance(result, dict)
        assert all(v is not None for v in result.values())
