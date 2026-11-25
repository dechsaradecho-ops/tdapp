"""Unit tests for app/integrations/quotes.py — all offline (httpx mocked).

Covers: indicator math (EMA/RSI/ATR/ADX/Supertrend), candle parsing with
None gaps, Frankfurter + Twelve Data payload handling, snapshot field
consistency and snapshot_from_candles output ranges.

Run from backend/: C:/Python314/python.exe -m pytest tests/test_quotes.py -v
"""
from __future__ import annotations

import asyncio
import math
import random
import time
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


@pytest.fixture(autouse=True)
def _clear_feed_caches():
    """Reset module-level feed caches so live data can't bleed across tests.

    One pre-existing test hits the live network and populates the
    quotes-module caches (Biquote ticks, TD rates, OHLC, spot/quote/display
    dicts). Later mocked tests then take cache-short-circuit paths and never
    execute their fake `client.get`, failing only in full-suite order.
    Clearing before AND after each test keeps every test hermetic.
    """
    quotes._spot_cache.clear()
    quotes._spot_live.clear()
    quotes._spot_source.clear()
    quotes._trusted_quotes.clear()
    quotes._gold_price_source.clear()
    quotes._quote_cache.clear()
    quotes._display_cache.clear()
    quotes._GOLD_TICK_CACHE = None
    quotes._gold_ohlc_cache = None
    quotes._td_rate_cache = None
    yield
    quotes._spot_cache.clear()
    quotes._spot_live.clear()
    quotes._spot_source.clear()
    quotes._trusted_quotes.clear()
    quotes._gold_price_source.clear()
    quotes._quote_cache.clear()
    quotes._display_cache.clear()
    quotes._GOLD_TICK_CACHE = None
    quotes._gold_ohlc_cache = None
    quotes._td_rate_cache = None


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
        """Yahoo down + no TwelveData key → the fallback chain exhausts."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)
        client = httpx.AsyncClient()

        async def fake_get(*a, **kw):
            return _resp({}, status=503)  # Yahoo (and everything) fails
        client.get = fake_get
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
# Yahoo-first candle chain + supported universe (2026-09-07)
# ---------------------------------------------------------------------------
def _yahoo_chart_payload(closes: list[float]) -> dict:
    """Minimal Yahoo /chart payload — one bar per close, OHLC synthesized."""
    return {"chart": {"result": [{
        "meta": {"symbol": "TEST"},
        "indicators": {"quote": [{
            "open": [c * 0.999 for c in closes],
            "high": [c * 1.001 for c in closes],
            "low": [c * 0.998 for c in closes],
            "close": closes,
        }]},
    }], "error": None}}


class TestYahooFirstChain:
    @pytest.mark.asyncio
    async def test_yahoo_is_primary_for_fx(self):
        """fetch_candles hits Yahoo FIRST for FX pairs (not Frankfurter)."""
        closes = [1.10 + 0.001 * i for i in range(35)]
        client = httpx.AsyncClient()
        called = {"url": ""}

        async def fake_get(url, *a, **kw):
            called["url"] = url
            return _resp(_yahoo_chart_payload(closes))
        client.get = fake_get
        candles = await quotes.fetch_candles("EURUSD", client)
        assert "query1.finance.yahoo.com" in called["url"]
        assert "EURUSD=X" in called["url"]
        assert len(candles) == 35
        assert candles[0].c == closes[0]
        await client.aclose()

    @pytest.mark.asyncio
    async def test_yahoo_covers_settings_added_pair(self):
        """A pair added via Settings (e.g. GBPJPY) prices through Yahoo."""
        closes = [190.0 + i for i in range(35)]
        client = httpx.AsyncClient()
        called = {"url": ""}

        async def fake_get(url, *a, **kw):
            called["url"] = url
            return _resp(_yahoo_chart_payload(closes))
        client.get = fake_get
        candles = await quotes.fetch_candles("GBPJPY", client)
        assert "GBPJPY=X" in called["url"]
        assert len(candles) == 35
        await client.aclose()

    @pytest.mark.asyncio
    async def test_fx_falls_back_to_frankfurter_when_yahoo_fails(self, monkeypatch):
        """Yahoo down → FX falls through to the Frankfurter fallback feed."""
        closes = [100.0 + i for i in range(35)]
        client = httpx.AsyncClient()
        urls: list[str] = []

        async def fake_get(url, *a, **kw):
            urls.append(url)
            if "yahoo" in url:
                return _resp({}, status=503)
            return _resp(_fx_payload(closes))
        client.get = fake_get
        candles = await quotes.fetch_candles("EURUSD", client)
        assert any("yahoo" in u for u in urls)
        assert any("frankfurter" in u for u in urls)
        assert len(candles) == 35
        await client.aclose()

    @pytest.mark.asyncio
    async def test_gold_falls_back_to_twelvedata_when_yahoo_fails(self, monkeypatch):
        """Yahoo down → XAUUSD falls through to TwelveData (key present)."""
        from app.core.config import get_settings as _gs
        closes = [2400.0 + i for i in range(35)]
        client = httpx.AsyncClient()
        urls: list[str] = []

        async def fake_get(url, *a, **kw):
            urls.append(url)
            if "yahoo" in url:
                return _resp({}, status=503)
            return _resp(_gold_payload(closes))
        client.get = fake_get
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "td-demo-key",
                            raising=False)
        candles = await quotes.fetch_candles("XAUUSD", client)
        assert any("twelvedata" in u for u in urls)
        assert len(candles) == 35
        await client.aclose()

    @pytest.mark.asyncio
    async def test_unsupported_asset_still_raises(self):
        client = httpx.AsyncClient()
        with pytest.raises(quotes.QuotesUnavailable):
            await quotes.fetch_candles("NOPE", client)
        await client.aclose()

    def test_supported_assets_registry(self):
        """Registry covers the original 5 + Settings-addable pairs; every
        entry is Yahoo-priceable (generic XXXYYY=X or GC=F)."""
        for a in ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "XAUUSD"):
            assert a in quotes.SUPPORTED_ASSETS
        for a in ("GBPJPY", "NZDUSD", "USDCAD", "EURGBP", "CADCHF"):
            assert a in quotes.SUPPORTED_ASSETS
        assert quotes._yahoo_symbol("GBPJPY") == "GBPJPY=X"
        assert quotes._yahoo_symbol("XAUUSD") == "GC=F"
        assert quotes._yahoo_symbol("NOPE") is None

    def test_generic_fx_parts(self):
        assert quotes.fx_parts("EURUSD") == ("EUR", "USD")
        assert quotes.fx_parts("GBPJPY") == ("GBP", "JPY")
        assert quotes.fx_parts("XAUUSD") is None  # not pure-alpha FX
        assert quotes.fx_parts("BTCUSD") == ("BTC", "USD")

    def test_exchange_pair_generic(self):
        assert quotes._exchange_pair("GBPJPY") == ("GBP", "JPY")
        assert quotes._exchange_pair("XAUUSD") is None  # no rate-API mapping
        assert quotes._exchange_pair("ZZZQQQ") is None  # non-ECB currencies


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
        # Gold needs 200+ bars for true EMA200 (short history → None,
        # fail closed), so the uptrend check uses a full-length series.
        snap = quotes.snapshot_from_candles(
            "XAUUSD", make_candles(250, start=2400.0, drift=2.0, noise=1.0))
        assert snap["ema_fast"] > snap["ema_slow"]
        assert snap["supertrend_dir"] == 1
        assert snap["price_change_pct_20"] > 0

    def test_snapshot_feeds_strategy_engine(self):
        from app.engine.strategy_engine import IndicatorSnapshot, StrategyEngine
        snap = quotes.snapshot_from_candles(
            "XAUUSD", make_candles(250, start=2400.0, drift=2.0, noise=1.0))
        model_fields = {k: v for k, v in snap.items()
                        if k in IndicatorSnapshot.__dataclass_fields__}
        ind = IndicatorSnapshot(**model_fields, source="live")
        opp = StrategyEngine().opportunity_score(ind)
        assert 0 <= opp.score <= 100

    def test_snapshot_requires_no_extra_keys_for_model(self):
        """IndicatorSnapshot(**snap) must not raise (field mismatch guard)."""
        from app.engine.strategy_engine import IndicatorSnapshot
        snap = quotes.snapshot_from_candles("EURUSD", make_candles(60))
        model_fields = {k: v for k, v in snap.items()
                        if k in IndicatorSnapshot.__dataclass_fields__}
        ind = IndicatorSnapshot(**model_fields)  # must not raise
        assert ind.source == "demo"  # default

    def test_supertrend_down_in_downtrend_snapshot(self):
        down = make_candles(60, start=2400.0, drift=-2.0, noise=1.0)
        snap = quotes.snapshot_from_candles("XAUUSD", down)
        assert snap["supertrend_dir"] == -1


# ---------------------------------------------------------------------------
# Breakout-retest state (strategy D — gold)
# ---------------------------------------------------------------------------
class TestBreakoutState:
    def _candles(self, closes: list[float], wick: float = 0.5) -> list[quotes.Candle]:
        out: list[quotes.Candle] = []
        for i, c in enumerate(closes):
            o = closes[i - 1] if i else c
            out.append(quotes.Candle(o=o, h=max(o, c) + wick,
                                     l=min(o, c) - wick, c=c))
        return out

    def test_breakout_detected(self):
        """Close crosses above the prior 20-bar high → state 2 + level."""
        closes = [100.0] * 25 + [101.0]
        state, level = quotes._breakout_state(self._candles(closes))
        assert state == 2.0
        assert level == pytest.approx(100.5)  # prior high incl. wick

    def test_retest_detected(self):
        """Dip back into the level zone then close above it → state 1."""
        closes = [100.0] * 25 + [101.0, 100.6]
        state, level = quotes._breakout_state(self._candles(closes))
        assert state == 1.0
        assert level == pytest.approx(100.5)

    def test_no_setup_when_below_level(self):
        closes = [100.0] * 26
        state, level = quotes._breakout_state(self._candles(closes))
        assert state == 0.0 and level == 0.0

    def test_too_few_candles(self):
        state, level = quotes._breakout_state(self._candles([100.0] * 10))
        assert state == 0.0 and level == 0.0

    def test_snapshot_fields_gold_only(self):
        """XAUUSD carries breakout fields; other assets stay 0.0."""
        up = make_candles(60, start=2400.0, drift=2.0, noise=1.0)
        gold = quotes.snapshot_from_candles("XAUUSD", up)
        assert "breakout_state" in gold and "breakout_level" in gold
        fx = quotes.snapshot_from_candles("EURUSD", make_candles(60))
        assert fx["breakout_state"] == 0.0 and fx["breakout_level"] == 0.0


# ---------------------------------------------------------------------------
# Spot feed — Yahoo chart API primary + exchangerate-api.com fallback
# ---------------------------------------------------------------------------
def _ex_resp(quote: str, rate: float, status: int = 200):
    """exchangerate-api.com /latest/{base} payload shape."""
    return _resp({"result": "success", "base_code": "USD",
                  "conversion_rates": {quote: rate}}, status=status)


def _yahoo_payload(price: float):
    return _resp({"chart": {"result": [{"meta": {"regularMarketPrice": price}}]}})


def _fake_client_factory(calls: list[str], responses: list):
    """AsyncClient stand-in: pops one response (or exception) per GET."""
    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            calls.append(url)
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
    return _FakeClient


class TestSpotFeed:
    @pytest.fixture(autouse=True)
    def _reset_spot_state(self):
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0
        yield
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0

    @pytest.fixture(autouse=True)
    def _biquote_down(self, monkeypatch):
        """Pin the Biquote-first leg down: these tests cover the unchanged
        Yahoo → last-intraday → exchangerate chain underneath it."""
        from app.integrations.biquote import BiquoteError

        async def _down(symbol: str = "XAUUSD", *, allow_stale: bool = False,
                       client=None):
            raise BiquoteError("biquote: down (test)")

        monkeypatch.setattr(quotes.biquote, "fetch_tick", _down,
                            raising=False)

    def test_pair_mapping_covers_fx_only(self):
        assert quotes._exchange_pair("GBPUSD") == ("GBP", "USD")
        assert quotes._exchange_pair("XAUUSD") is None  # no gold on free tier

    @pytest.mark.asyncio
    async def test_returns_rate_for_fx_pair(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [_ex_resp("USD", 1.3485)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        price, err = await quotes._fetch_spot_exchangerate("GBPUSD")
        assert price == 1.3485
        assert err == ""
        assert calls == [f"{quotes.EXCHANGERATE_URL}/keyA/latest/GBP"]

    @pytest.mark.asyncio
    async def test_unmapped_asset_needs_no_http(self, monkeypatch):
        calls: list[str] = []
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, []))
        price, err = await quotes._fetch_spot_exchangerate("XAUUSD")
        assert price == 0.0
        assert "no exchangerate mapping" in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_rotates_key_on_429(self, monkeypatch):
        """Exhausted key → advance to the next one transparently."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA,keyB", raising=False)
        calls: list[str] = []
        responses = [_ex_resp("USD", 1.0, status=429), _ex_resp("USD", 1.35)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        price, err = await quotes._fetch_spot_exchangerate("GBPUSD")
        assert price == 1.35
        assert err == ""
        assert "/keyA/latest/GBP" in calls[0]
        assert "/keyB/latest/GBP" in calls[1]

    @pytest.mark.asyncio
    async def test_rotates_key_on_transport_error(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA,keyB", raising=False)
        calls: list[str] = []
        responses = [httpx.ConnectError("dns fail"), _ex_resp("JPY", 159.08)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        price, err = await quotes._fetch_spot_exchangerate("USDJPY")
        assert price == 159.08
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_all_keys_exhausted_reports_error(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA,keyB", raising=False)
        calls: list[str] = []
        responses = [_ex_resp("USD", 1.0, status=429),
                     _ex_resp("USD", 1.0, status=429)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        price, err = await quotes._fetch_spot_exchangerate("EURUSD")
        assert price == 0.0
        assert "429" in err
        assert len(calls) == 2  # tried both keys, stopped

    @pytest.mark.asyncio
    async def test_no_keys_configured_short_circuits(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "", raising=False)
        calls: list[str] = []
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, []))
        price, err = await quotes._fetch_spot_exchangerate("GBPUSD")
        assert price == 0.0
        assert "no EXCHANGERATE_API_KEYS" in err
        assert calls == []

    @pytest.mark.asyncio
    async def test_inverse_leg_priced_from_yahoo_without_exchangerate(self, monkeypatch):
        """Conversion legs (CHFUSD) price as 1/USDCHF from Yahoo intraday —
        exchangerate is never touched (prod 2026-09-24: quota 429 on all keys)."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [_yahoo_payload(1.10)]  # USDCHF=X
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices, failures = await quotes.fetch_spot_prices(["CHFUSD"])
        assert prices["CHFUSD"] == pytest.approx(1 / 1.10)
        assert failures == {}
        assert quotes.spot_source("CHFUSD") == "spot"
        assert any("USDCHF=X" in c for c in calls)
        assert not any("exchangerate" in c for c in calls)

    @pytest.mark.asyncio
    async def test_inverse_leg_failure_still_falls_through(self, monkeypatch):
        """Inverse quote missing too → existing fallback chain runs."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _resp({}, status=404),            # USDCHF=X missing
            _ex_resp("USD", 0.909),           # exchangerate /latest/CHF → USD leg
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices, failures = await quotes.fetch_spot_prices(["CHFUSD"])
        assert prices["CHFUSD"] == pytest.approx(0.909)
        assert failures == {}

    @pytest.mark.asyncio
    async def test_fetch_spot_prices_chain_yahoo_then_exchangerate(self, monkeypatch):
        """Yahoo serves both FX and gold; exchangerate stays untouched."""
        from app.core.config import get_settings as _gs
        from app.integrations.biquote import BiquoteError
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        # Gold is Biquote-first now: force the trusted legs down so XAUUSD
        # reaches the degraded-Yahoo display leg (same single Yahoo GET).
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)

        async def _no_biquote(*a, **kw):
            raise BiquoteError("biquote: down (test)")
        monkeypatch.setattr(quotes.biquote, "fetch_tick", _no_biquote,
                            raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(1.15),            # EURUSD → yahoo primary ok
            _yahoo_payload(4540.2),          # XAUUSD → yahoo (GC=F) ok
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices, failures = await quotes.fetch_spot_prices(["EURUSD", "XAUUSD"])
        assert prices == {"EURUSD": 1.15, "XAUUSD": 4540.2}
        assert failures == {}
        assert quotes.spot_source("EURUSD") == "spot"
        assert any(f"{quotes.YAHOO_CHART_URL}/EURUSD=X" in c for c in calls)
        assert any(f"{quotes.YAHOO_CHART_URL}/GC=F" in c for c in calls)
        assert not any("exchangerate" in c for c in calls)  # fallback not needed

    @pytest.mark.asyncio
    async def test_fetch_spot_prices_exchangerate_fallback_when_yahoo_dead(self, monkeypatch):
        """Yahoo 429s → GBPUSD still priced via exchangerate fallback."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _resp({}, status=429),               # yahoo dead
            _ex_resp("USD", 1.3485),             # exchangerate saves the day
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices, failures = await quotes.fetch_spot_prices(["GBPUSD"])
        assert prices == {"GBPUSD": 1.3485}
        assert failures == {}
        assert quotes.spot_source("GBPUSD") == "daily"  # cold start: no live ref
        assert len([c for c in calls if "yahoo" in c]) == 1
        assert len([c for c in calls if "exchangerate" in c]) == 1

    @pytest.mark.asyncio
    async def test_fetch_spot_prices_second_call_is_cached(self, monkeypatch):
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [_yahoo_payload(1.3485)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices1, _ = await quotes.fetch_spot_prices(["GBPUSD"])
        prices2, _ = await quotes.fetch_spot_prices(["GBPUSD"])
        assert prices1 == prices2 == {"GBPUSD": 1.3485}
        assert len(calls) == 1  # second call served from the 30s cache

    # ---- daily-rate trust gate (prod 2026-09-14 phantom NZDUSD TP) --------
    @pytest.mark.asyncio
    async def test_stale_daily_rate_never_overrides_last_intraday_print(
            self, monkeypatch):
        """Yahoo down → reuse the last tick we SAW, not the daily rate.

        Live 0.57667, daily fallback 0.5812 (= TP 0.58081 crossed). The old
        chain returned 0.5812 and the guard booked a TP the market never hit.
        """
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(0.57667),   # cycle 1: intraday print
            _resp({}, status=429),     # cycle 2: Yahoo timed out/dead
            _ex_resp("USD", 0.5812),   # ... daily fallback would say 0.5812
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        first, _ = await quotes.fetch_spot_prices(["NZDUSD"])
        assert first == {"NZDUSD": 0.57667}
        quotes._spot_cache.clear()          # force a refetch (TTL not reached)
        prices, failures = await quotes.fetch_spot_prices(["NZDUSD"])
        assert prices == {"NZDUSD": 0.57667}   # last REAL tick wins
        assert quotes.spot_source("NZDUSD") == "spot"
        assert "live feed down" in failures["NZDUSD"]  # ...but never silent
        assert not any("exchangerate" in c for c in calls)  # fallback skipped

    @pytest.mark.asyncio
    async def test_daily_rate_rejected_when_it_disagrees_with_live(
            self, monkeypatch):
        """Beyond SPOT_LIVE_TTL the fallback is tried — but sanity-checked."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(0.57667),   # intraday print (reference)
            _resp({}, status=429),     # > 15 min later: Yahoo still dead
            _ex_resp("USD", 0.5812),   # daily rate 0.79% away → refuse
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        await quotes.fetch_spot_prices(["NZDUSD"])
        # age the live print past SPOT_LIVE_TTL (but inside the 24h ref window)
        ts, price = quotes._spot_live["NZDUSD"]
        quotes._spot_live["NZDUSD"] = (ts - quotes.SPOT_LIVE_TTL - 1.0, price)
        quotes._spot_cache.clear()
        prices, failures = await quotes.fetch_spot_prices(["NZDUSD"])
        assert prices == {}                     # nothing tradable this cycle
        assert "ปฏิเสธ" in failures["NZDUSD"]
        assert len([c for c in calls if "exchangerate" in c]) == 1

    @pytest.mark.asyncio
    async def test_daily_rate_accepted_when_it_agrees_with_live(
            self, monkeypatch):
        """A fallback within SPOT_DAILY_MAX_DEVIATION of the live tick is OK."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(1.34850),
            _resp({}, status=429),
            _ex_resp("USD", 1.34980),   # +0.096% → plausible
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        await quotes.fetch_spot_prices(["GBPUSD"])
        ts, price = quotes._spot_live["GBPUSD"]
        quotes._spot_live["GBPUSD"] = (ts - quotes.SPOT_LIVE_TTL - 1.0, price)
        quotes._spot_cache.clear()
        prices, failures = await quotes.fetch_spot_prices(["GBPUSD"])
        assert prices == {"GBPUSD": 1.3498}
        assert failures == {}
        # provenance is tagged so the guard can refuse it for hard stops
        assert quotes.spot_source("GBPUSD") == "daily"

    def test_spot_source_defaults_to_empty(self):
        assert quotes.spot_source("EURUSD") == ""
        assert quotes.spot_source("eurusd") == ""


# ---------------------------------------------------------------------------
# fetch_trusted_spot — the WRITE-path half of the daily-rate gate
# ---------------------------------------------------------------------------
class TestTrustedSpot:
    """fetch_spot_prices may RETURN a daily rate (display needs it);
    fetch_trusted_spot must never hand one to a caller that STORES it
    (scanner emit re-anchor, entry re-anchor, plan legs). Prod 2026-09-14:
    the NZDUSD daily rate became the entry/SL/TP of a real paper trade.
    """

    @pytest.fixture(autouse=True)
    def _reset_spot_state(self):
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0
        yield
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0

    @pytest.fixture(autouse=True)
    def _biquote_down(self, monkeypatch):
        """Pin the Biquote-first leg down: writer-path tests cover the
        unchanged Yahoo → fallback chain and its trusted-drop gate."""
        from app.integrations.biquote import BiquoteError

        async def _down(symbol: str = "XAUUSD", *, allow_stale: bool = False,
                       client=None):
            raise BiquoteError("biquote: down (test)")

        monkeypatch.setattr(quotes.biquote, "fetch_tick", _down,
                            raising=False)

    @pytest.mark.asyncio
    async def test_accepted_daily_rate_is_still_dropped_for_writers(
            self, monkeypatch):
        """The case that matters: the chain ACCEPTS the fallback (close
        enough to display) — a writer must still refuse it."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(1.34850),   # intraday print (reference)
            _resp({}, status=429),     # > 15 min later: Yahoo dead
            _ex_resp("USD", 1.34980),  # +0.096% → chain accepts it
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        await quotes.fetch_spot_prices(["GBPUSD"])
        ts, price = quotes._spot_live["GBPUSD"]
        quotes._spot_live["GBPUSD"] = (ts - quotes.SPOT_LIVE_TTL - 1.0, price)
        quotes._spot_cache.clear()
        shown, _ = await quotes.fetch_spot_prices(["GBPUSD"])
        assert shown == {"GBPUSD": 1.3498}            # display keeps it
        assert quotes.spot_source("GBPUSD") == "daily"
        trusted, failures = await quotes.fetch_trusted_spot(["GBPUSD"])
        assert trusted == {}                          # writer must not
        assert "ปฏิเสธราคา daily fallback" in failures["GBPUSD"]

    @pytest.mark.asyncio
    async def test_intraday_print_passes_through_untouched(self, monkeypatch):
        calls: list[str] = []
        responses = [_yahoo_payload(1.3485)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        trusted, failures = await quotes.fetch_trusted_spot(["GBPUSD"])
        assert trusted == {"GBPUSD": 1.3485}
        assert failures == {}
        assert quotes.spot_source("GBPUSD") == "spot"

    def test_slow_feed_still_resolves_inside_cycle_budget(self):
        # A hung-everywhere feed must resolve (with partials) well inside
        # the 50 s worker watchdog: snapshot total + spot total <= 32 s.
        assert (quotes._SNAP_TOTAL_TIMEOUT
                + quotes._SPOT_TOTAL_TIMEOUT) <= 32.0

    @pytest.mark.asyncio
    async def test_chain_level_refusal_also_yields_nothing_to_writers(
            self, monkeypatch):
        """A refused daily rate never reaches the writer either way."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        calls: list[str] = []
        responses = [
            _yahoo_payload(0.57667),
            _resp({}, status=429),
            _ex_resp("USD", 0.5812),   # 0.79% away → refused by the chain
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        await quotes.fetch_spot_prices(["NZDUSD"])
        ts, price = quotes._spot_live["NZDUSD"]
        quotes._spot_live["NZDUSD"] = (ts - quotes.SPOT_LIVE_TTL - 1.0, price)
        quotes._spot_cache.clear()
        trusted, failures = await quotes.fetch_trusted_spot(["NZDUSD"])
        assert trusted == {}
        assert "NZDUSD" in failures


# ---------------------------------------------------------------------------
# fetch_all_snapshots — fallback semantics
# ---------------------------------------------------------------------------
class TestFetchAllSnapshots:
    @pytest.mark.asyncio
    async def test_failed_assets_are_absent_not_none(self):
        """Assets whose fetch fails are omitted from the result dict."""
        quotes._quote_cache.clear()
        try:
            result = await quotes.fetch_all_snapshots(["EURUSD", "GBPUSD", "AUDUSD"])
        finally:
            quotes._quote_cache.clear()
        assert isinstance(result, dict)
        assert all(v is not None for v in result.values())


# ---------------------------------------------------------------------------
# Batch hardening — shared client + per-asset/total timeouts (2026-09-11)
# A hung feed must resolve to a miss/failure row, never stall the 1-min
# position-guard cycle ("เคยขึ้นไป 1R ทำไมไม่ขยับ sl").
# ---------------------------------------------------------------------------
class TestBatchTimeouts:
    @pytest.fixture(autouse=True)
    def _reset_batch_state(self):
        quotes._spot_cache.clear()
        quotes._quote_cache.clear()
        yield
        quotes._spot_cache.clear()
        quotes._quote_cache.clear()

    @pytest.mark.asyncio
    async def test_spot_batch_uses_single_shared_client(self, monkeypatch):
        """One AsyncClient for the whole spot batch (was: one per asset)."""
        import asyncio  # noqa: F401 (keeps the batch event-loop contract explicit)
        from app.core.config import get_settings as _gs
        from app.integrations.biquote import BiquoteError
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA", raising=False)
        # Gold is Biquote-first now: force the trusted legs down so XAUUSD
        # uses its single degraded-Yahoo GET (client count stays one).
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)

        async def _no_biquote(*a, **kw):
            raise BiquoteError("biquote: down (test)")
        monkeypatch.setattr(quotes.biquote, "fetch_tick", _no_biquote,
                            raising=False)
        calls: list[str] = []
        responses = [_yahoo_payload(1.15), _yahoo_payload(4540.2)]
        FakeCls = _fake_client_factory(calls, responses)
        constructed = {"n": 0}
        _orig_init = FakeCls.__init__

        def _counting_init(self, *a, **kw):
            constructed["n"] += 1
            _orig_init(self, *a, **kw)
        FakeCls.__init__ = _counting_init
        monkeypatch.setattr(quotes.httpx, "AsyncClient", FakeCls)
        prices, failures = await quotes.fetch_spot_prices(["EURUSD", "XAUUSD"])
        assert prices == {"EURUSD": 1.15, "XAUUSD": 4540.2}
        assert failures == {}
        assert constructed["n"] == 1  # shared client, not per-asset

    @pytest.mark.asyncio
    async def test_snapshot_hung_asset_isolated(self, monkeypatch):
        """One hung snapshot resolves to a miss; fast assets still return."""
        import asyncio
        import time
        monkeypatch.setattr(quotes, "_SNAP_PER_ASSET_TIMEOUT", 0.1, raising=False)
        monkeypatch.setattr(quotes, "_SNAP_TOTAL_TIMEOUT", 2.0, raising=False)

        async def fake_fetch(asset, client):
            if asset == "HANGUSD":
                await asyncio.sleep(10.0)
                return {"asset": asset}
            return {"asset": asset, "price": 1.0}
        monkeypatch.setattr(quotes, "fetch_snapshot", fake_fetch, raising=False)
        t0 = time.monotonic()
        result = await quotes.fetch_all_snapshots(
            ["EURUSD", "HANGUSD", "GBPUSD"], ttl=0)
        elapsed = time.monotonic() - t0
        assert "EURUSD" in result and "GBPUSD" in result
        assert "HANGUSD" not in result
        assert elapsed < 2.0  # bounded by the total budget, not the hang

    @pytest.mark.asyncio
    async def test_spot_hung_asset_times_out(self, monkeypatch):
        """One hung spot resolves to a failure row; others still price."""
        import asyncio
        import time
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "", raising=False)
        monkeypatch.setattr(quotes, "_SPOT_PER_ASSET_TIMEOUT", 0.1, raising=False)
        monkeypatch.setattr(quotes, "_SPOT_TOTAL_TIMEOUT", 2.0, raising=False)

        class _HangingClient:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, **kw):
                if "EURUSD" in url:
                    await asyncio.sleep(10.0)  # hung feed
                    raise AssertionError("cancelled hang should time out first")
                return _yahoo_payload(1.35)
        monkeypatch.setattr(quotes.httpx, "AsyncClient", _HangingClient)
        t0 = time.monotonic()
        prices, failures = await quotes.fetch_spot_prices(["EURUSD", "GBPUSD"])
        elapsed = time.monotonic() - t0
        assert prices.get("GBPUSD") == 1.35
        assert "EURUSD" in failures and "timeout" in failures["EURUSD"]
        assert elapsed < 2.0


# ---------------------------------------------------------------------------
# Trusted gold feed — Biquote-first OHLC + spot (Task 4: XAUUSD gold gate)
# ---------------------------------------------------------------------------
def _biquote_ohlc_payload(closes: list[float], open_last: bool = True) -> dict:
    """Biquote /api/XAUUSD/ohlc shape: oldest-first bars with is_open flag."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = []
    for i, c in enumerate(closes):
        bars.append({
            "openTime": (base + timedelta(days=i)).isoformat(),
            "open": c * 0.999, "high": c * 1.001,
            "low": c * 0.998, "close": c,
            "tickVolume": 100 + i,
            "isOpen": bool(open_last and i == len(closes) - 1),
        })
    return {"symbol": "XAUUSD", "bars": bars}


def _biquote_tick_payload(bid: float, ask: float) -> dict:
    mid = (bid + ask) / 2.0
    return {"symbol": "XAUUSD", "bid": bid, "ask": ask, "mid": mid,
            "spread": ask - bid, "timestamp": "2026-11-25T12:00:00Z",
            "source": "biquote", "exchange": "BQT", "marketState": "open",
            "stale": False, "quoteAgeSeconds": 3}


def _td_exchange_rate_payload(rate: float) -> dict:
    import time as _time
    return {"symbol": "XAUUSD", "rate": rate, "timestamp": int(_time.time())}


class TestGoldTrustedFeed:
    @pytest.fixture(autouse=True)
    def _reset_gold_state(self):
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._quote_cache.clear()
        quotes._trusted_quotes.clear()
        quotes._gold_price_source.clear()
        quotes._GOLD_TICK_CACHE = None
        quotes._gold_ohlc_cache = None
        quotes._td_rate_cache = None
        yield
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._quote_cache.clear()
        quotes._trusted_quotes.clear()
        quotes._gold_price_source.clear()
        quotes._GOLD_TICK_CACHE = None
        quotes._gold_ohlc_cache = None
        quotes._td_rate_cache = None

    @pytest.mark.asyncio
    async def test_gold_candles_prefer_biquote_and_use_wilder_ema200(self):
        """Biquote-first: 500 daily bars requested, trusted, EMA200/Wilder."""
        from app.core.gold_policy import true_ema, wilder_atr
        closes = [2400.0 + 0.5 * i for i in range(500)]
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                seen["limit"] = request.url.params.get("limit")
                return httpx.Response(200, json=_biquote_ohlc_payload(closes))
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            result = await quotes.fetch_candles_with_source("XAUUSD", client)
            assert result.source == "biquote" and result.trusted is True
            assert seen.get("limit") == "500"
            # the forming (is_open) bar is dropped — only closed bars map
            assert len(result.candles) == 499
            snap = await quotes.fetch_snapshot("XAUUSD", client)
            assert snap["ema_slow_period"] == 200
            assert snap["atr_method"] == "wilder"
            assert snap["atr_period"] == 14
            assert snap["bar_count"] >= 200
            assert snap["ohlc_source"] == "biquote"
            assert snap["feed_source"] == "biquote"
            assert snap["trusted"] is True
            assert snap["ema_slow"] == pytest.approx(
                true_ema([c.c for c in result.candles], 200))
            atr = wilder_atr(result.candles, 14)
            assert snap["atr_pct"] == pytest.approx(
                round(atr / result.candles[-1].c * 100.0, 2))

    @pytest.mark.asyncio
    async def test_gold_candles_fall_back_to_twelvedata_then_degraded_yahoo(
            self, monkeypatch):
        """Biquote down → Twelve Data XAU/USD; all down → yahoo_degraded."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "td-demo-key",
                            raising=False)
        closes = [2400.0 + i for i in range(250)]

        def handler_td(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(503, json={})
            if "twelvedata.com/time_series" in url:
                return httpx.Response(200, json=_gold_payload(closes))
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_td)) as client:
            first = await quotes.fetch_candles_with_source("XAUUSD", client)
            assert first.source == "twelvedata" and first.trusted is True
            assert len(first.candles) == 250

        def handler_yahoo(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url or "twelvedata.com" in url:
                return httpx.Response(200, json={"status": "error",
                                                 "code": 503,
                                                 "message": "down"})
            return httpx.Response(200, json=_yahoo_chart_payload(closes))

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_yahoo)) as client:
            second = await quotes.fetch_candles_with_source("XAUUSD", client)
            assert second.source == "yahoo_degraded"
            assert second.trusted is False
            snap = await quotes.fetch_snapshot("XAUUSD", client)
            assert snap["trusted"] is False
            assert snap["ohlc_source"] == "yahoo_degraded"

    @pytest.mark.asyncio
    async def test_gold_spot_uses_biquote_and_rejects_degraded_yahoo(
            self, monkeypatch):
        """Biquote tick feeds trusted_quote/live_spread; degraded Yahoo is
        display-only — fetch_trusted_spot must drop XAUUSD then."""
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)
        calls: list[str] = []
        _real_client = httpx.AsyncClient

        def handler_tick(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if "biquote.io" in url:
                return httpx.Response(
                    200, json=_biquote_tick_payload(2650.0, 2650.155))
            return httpx.Response(200, json=_yahoo_payload(2651.0).json())

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler_tick)))
        prices, failures = await quotes.fetch_spot_prices(["XAUUSD"])
        q = quotes.trusted_quote("XAUUSD")
        assert q is not None and q.source == "biquote"
        assert prices["XAUUSD"] == q.price == pytest.approx(2650.0775)
        assert q.bid == pytest.approx(2650.0)
        assert q.ask == pytest.approx(2650.155)
        assert quotes.live_spread("XAUUSD") == pytest.approx(0.155)
        assert failures == {}
        assert len(calls) == 1 and "biquote.io" in calls[0]

        # Only Yahoo succeeds now → display price served, but untrusted.
        quotes._spot_cache.clear()
        quotes._GOLD_TICK_CACHE = None

        def handler_degraded(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url or "twelvedata.com" in url:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_yahoo_payload(2651.0).json())

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler_degraded)))
        prices2, _ = await quotes.fetch_spot_prices(["XAUUSD"])
        assert prices2["XAUUSD"] == pytest.approx(2651.0)  # display OK
        assert quotes.trusted_quote("XAUUSD") is None
        assert quotes.live_spread("XAUUSD") is None
        trusted, failures2 = await quotes.fetch_trusted_spot(["XAUUSD"])
        assert "XAUUSD" not in trusted
        assert "XAUUSD" in failures2

    @pytest.mark.asyncio
    async def test_short_history_gold_candles_are_untrusted(self, monkeypatch):
        """40 bars (< 200 for EMA200) → trusted False on every trusted leg.

        Fail closed: downstream guards key off `trusted`, and short history
        yields None indicators — a `trusted=True` flag must never carry such
        a snapshot past the guards.
        """
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "td-demo-key",
                            raising=False)
        closes = [2400.0 + i for i in range(40)]

        # Biquote leg with short history.
        def handler_short_biquote(request: httpx.Request) -> httpx.Response:
            if "biquote.io" in str(request.url):
                return httpx.Response(200, json=_biquote_ohlc_payload(closes))
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_short_biquote)) as client:
            result = await quotes.fetch_candles_with_source("XAUUSD", client)
            assert result.source == "biquote"
            assert result.trusted is False
            snap = await quotes.fetch_snapshot("XAUUSD", client)
            assert snap["trusted"] is False
            assert snap["ema_slow"] is None  # < 200 bars: EMA200 impossible
            # (39 bars still satisfy Wilder ATR14 — the single `trusted`
            # flag is what carries the fail-closed signal downstream).
        quotes._gold_ohlc_cache = None

        # Twelve Data leg with short history (Biquote down).
        def handler_short_td(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(503, json={})
            if "twelvedata.com/time_series" in url:
                return httpx.Response(200, json=_gold_payload(closes))
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_short_td)) as client:
            result = await quotes.fetch_candles_with_source("XAUUSD", client)
            assert result.source == "twelvedata"
            assert result.trusted is False

    @pytest.mark.asyncio
    async def test_td_rate_cache_hit_revalidates_max_age(self, monkeypatch):
        """A cached TD rate older than 5 min (remote age) is refetched/evicted,
        never served — the cache-hit path must enforce TD_MAX_RATE_AGE too."""
        import time as _time
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "td-demo-key",
                            raising=False)
        # Fresh poll window but stale quote: accepted at ~4 min, now ~400 s old.
        quotes._td_rate_cache = (_time.monotonic(), 2650.0,
                                 _time.time() - 400.0)
        calls: list[str] = []

        def handler_fresh(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, json=_td_exchange_rate_payload(2660.0))

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_fresh)) as client:
            price, _ = await quotes._fetch_td_gold_rate(client)
            assert price == pytest.approx(2660.0)  # refetched, not 2650.0
            assert any("exchange_rate" in u for u in calls)
        # Fresh quote within max age is still served from cache (no HTTP).
        quotes._td_rate_cache = (_time.monotonic(), 2660.0, _time.time() - 60.0)

        def handler_no_http(request: httpx.Request) -> httpx.Response:
            raise AssertionError("cache hit must not hit the network")

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_no_http)) as client:
            price, _ = await quotes._fetch_td_gold_rate(client)
            assert price == pytest.approx(2660.0)


# ---------------------------------------------------------------------------
# Biquote-first FX candle chain (Task 3)
# ---------------------------------------------------------------------------
class TestFxBiquoteFirst:
    @pytest.mark.asyncio
    async def test_fx_candles_prefer_biquote_and_drop_open_bar(self):
        """Biquote FX OHLC wins: 500 daily bars requested, isOpen bar dropped."""
        closes = [1.10 + 0.001 * i for i in range(500)]

        def handler(request: httpx.Request) -> httpx.Response:
            if "biquote.io" in str(request.url):
                return httpx.Response(200, json=_biquote_ohlc_payload(closes))
            return httpx.Response(503, json={})

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as client:
            result = await quotes.fetch_candles_with_source("AUDNZD", client)
            assert result.source == "biquote" and result.trusted is True
            assert len(result.candles) >= 400
            # the forming (is_open) bar is dropped — only closed bars map
            assert len(result.candles) == 499
            assert result.candles[-1].c == pytest.approx(closes[-2])

    @pytest.mark.asyncio
    async def test_fx_candles_fall_back_to_yahoo_then_frankfurter(self):
        """Biquote down → Yahoo daily; both down → Frankfurter synth."""
        closes = [1.10 + 0.001 * i for i in range(35)]

        def handler_yahoo(request: httpx.Request) -> httpx.Response:
            if "biquote.io" in str(request.url):
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_yahoo_chart_payload(closes))

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_yahoo)) as client:
            yahoo_result = await quotes.fetch_candles_with_source(
                "EURUSD", client)
            assert yahoo_result.source == "yahoo"
            assert yahoo_result.trusted is True

        fx_closes = [100.0 + i for i in range(35)]

        def handler_frankfurter(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url or "yahoo" in url:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_fx_payload(fx_closes))

        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler_frankfurter)) as client:
            frank_result = await quotes.fetch_candles_with_source(
                "EURUSD", client)
            assert frank_result.source == "frankfurter"
            assert frank_result.trusted is True


# ---------------------------------------------------------------------------
# Display spot feed — Biquote-first batched fetch (Task 2)
# ---------------------------------------------------------------------------
def _display_tick(sym: str, mid: float, age: int = 3,
                  stale: bool = False, state: str = "open") -> dict:
    """One Biquote /api/latest entry (keyed-object response shape)."""
    bid = mid - 0.00003
    ask = mid + 0.00003
    return {"symbol": sym, "bid": bid, "ask": ask, "mid": mid,
            "spread": ask - bid, "timestamp": "2026-11-25T12:00:00Z",
            "source": "biquote", "exchange": "BQT",
            "marketState": state, "stale": stale,
            "quoteAgeSeconds": age}


def _display_yahoo_price(price: float, rmt: float | None = None) -> dict:
    meta: dict = {"regularMarketPrice": price}
    if rmt is not None:
        meta["regularMarketTime"] = rmt
    return {"chart": {"result": [{"meta": meta}]}}


class TestDisplayPrices:
    @pytest.fixture(autouse=True)
    def _reset_display_state(self):
        if hasattr(quotes, "_display_cache"):
            quotes._display_cache.clear()
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._trusted_quotes.clear()
        quotes._gold_price_source.clear()
        quotes._gold_yahoo_age.clear()
        quotes._GOLD_TICK_CACHE = None
        quotes._gold_ohlc_cache = None
        quotes._td_rate_cache = None
        yield
        if hasattr(quotes, "_display_cache"):
            quotes._display_cache.clear()
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._trusted_quotes.clear()
        quotes._gold_price_source.clear()
        quotes._gold_yahoo_age.clear()
        quotes._GOLD_TICK_CACHE = None
        quotes._gold_ohlc_cache = None
        quotes._td_rate_cache = None

    @pytest.mark.asyncio
    async def test_display_prefers_biquote_batch(self, monkeypatch):
        """Fresh Biquote batch wins; exactly one Biquote HTTP call."""
        calls: list[str] = []
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if "biquote.io/api/latest" in url:
                return httpx.Response(200, json={
                    "EURUSD": _display_tick("EURUSD", 1.0946),
                    "CHFJPY": _display_tick("CHFJPY", 170.13),
                })
            return httpx.Response(200, json=_display_yahoo_price(1.0))

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, failures = await quotes.fetch_display_prices(
            ["EURUSD", "CHFJPY"])
        assert meta["EURUSD"].source == "biquote"
        assert meta["EURUSD"].age_seconds <= 10
        assert sum(1 for c in calls if "biquote.io" in c) == 1
        assert "EURUSD" in prices
        assert failures == {}

    @pytest.mark.asyncio
    async def test_display_batch_outage_falls_back_per_asset(self, monkeypatch):
        """Whole /api/latest 500 → per-asset Yahoo fallback, nothing blank."""
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(500, json={})
            if "EURUSD=X" in url:
                return httpx.Response(200, json=_display_yahoo_price(1.15))
            if "GBPJPY=X" in url:
                return httpx.Response(200, json=_display_yahoo_price(190.5))
            return httpx.Response(404, json={})

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, failures = await quotes.fetch_display_prices(
            ["EURUSD", "GBPJPY"])
        assert set(prices) == {"EURUSD", "GBPJPY"}
        assert all(m.source.startswith("yahoo") for m in meta.values())

    @pytest.mark.asyncio
    async def test_display_prefers_fresh_yahoo_over_stale_biquote(
            self, monkeypatch):
        """Stale Biquote + fresh Yahoo → Yahoo wins as yahoo_fallback."""
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(200, json={
                    "EURUSD": _display_tick("EURUSD", 1.09,
                                            age=120, stale=True),
                })
            return httpx.Response(200, json=_display_yahoo_price(1.10))

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, _ = await quotes.fetch_display_prices(["EURUSD"])
        assert meta["EURUSD"].source == "yahoo_fallback"
        assert prices["EURUSD"] == pytest.approx(1.10)

    @pytest.mark.asyncio
    async def test_display_gold_prefers_biquote_tick(self, monkeypatch):
        """Biquote gold tick → source "biquote" with age/state, one HTTP call."""
        calls: list[str] = []
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            calls.append(url)
            if "biquote.io" in url:
                return httpx.Response(
                    200, json=_biquote_tick_payload(2650.0, 2650.155))
            return httpx.Response(503, json={})

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, failures = await quotes.fetch_display_prices(["XAUUSD"])
        assert prices["XAUUSD"] == pytest.approx(2650.0775)
        assert meta["XAUUSD"].source == "biquote"
        assert meta["XAUUSD"].age_seconds == pytest.approx(3.0)
        assert meta["XAUUSD"].market_state == "open"
        assert meta["XAUUSD"].stale is False
        assert failures == {}
        # One Biquote request per gold display resolution (no second tick).
        assert sum(1 for c in calls if "biquote.io" in c) == 1

    @pytest.mark.asyncio
    async def test_display_gold_degraded_yahoo_keeps_legacy_source(
            self, monkeypatch):
        """All trusted gold legs down → degraded Yahoo keeps "yahoo_degraded".

        The degraded print must never look trusted-grade downstream.
        """
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url or "twelvedata.com" in url:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_display_yahoo_price(2651.0))

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, failures = await quotes.fetch_display_prices(["XAUUSD"])
        assert prices["XAUUSD"] == pytest.approx(2651.0)
        assert meta["XAUUSD"].source == "yahoo_degraded"
        assert meta["XAUUSD"].stale is False
        assert failures == {}
        assert quotes.trusted_quote("XAUUSD") is None

    @pytest.mark.asyncio
    async def test_display_gold_degraded_unknown_yahoo_age_not_fresh(
            self, monkeypatch):
        """Degraded Yahoo without regularMarketTime → age None (delayed).

        A Yahoo Friday-close with no usable timestamp must never render สด.
        """
        from app.core.config import get_settings as _gs
        monkeypatch.setattr(_gs(), "twelvedata_api_key", "", raising=False)
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url or "twelvedata.com" in url:
                return httpx.Response(503, json={})
            return httpx.Response(200, json=_display_yahoo_price(2651.0))

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, _ = await quotes.fetch_display_prices(["XAUUSD"])
        assert prices["XAUUSD"] == pytest.approx(2651.0)
        assert meta["XAUUSD"].source == "yahoo_degraded"
        assert meta["XAUUSD"].age_seconds is None

    @pytest.mark.asyncio
    async def test_display_yahoo_real_age_and_unknown_age(
            self, monkeypatch):
        """regularMarketTime → real age; missing → None (never fresh-glow)."""
        _real_client = httpx.AsyncClient
        now = time.time()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(500, json={})
            if "EURUSD=X" in url:
                return httpx.Response(
                    200, json=_display_yahoo_price(1.15, rmt=now - 4))
            if "GBPJPY=X" in url:
                return httpx.Response(200, json=_display_yahoo_price(190.5))
            return httpx.Response(404, json={})

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, _ = await quotes.fetch_display_prices(
            ["EURUSD", "GBPJPY"])
        assert meta["EURUSD"].age_seconds == pytest.approx(4.0, abs=5.0)
        assert meta["GBPJPY"].age_seconds is None
        assert set(prices) == {"EURUSD", "GBPJPY"}

    @pytest.mark.asyncio
    async def test_display_closed_tick_surfaced_when_yahoo_down(
            self, monkeypatch):
        """Biquote closed + Yahoo down → closed tick (ปิดตลาด, no glow).

        The closed tick must never fall through to the daily rate as open.
        """
        _real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(200, json={
                    "EURUSD": _display_tick("EURUSD", 1.0946, state="closed"),
                })
            return httpx.Response(500, json={})

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, _ = await quotes.fetch_display_prices(["EURUSD"])
        assert prices["EURUSD"] == pytest.approx(1.0946)
        assert meta["EURUSD"].source == "biquote"
        assert meta["EURUSD"].market_state == "closed"

    @pytest.mark.asyncio
    async def test_display_closed_yields_to_fresh_yahoo(
            self, monkeypatch):
        """Biquote closed + genuinely fresh Yahoo → Yahoo wins as fallback."""
        _real_client = httpx.AsyncClient
        now = time.time()

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "biquote.io" in url:
                return httpx.Response(200, json={
                    "EURUSD": _display_tick("EURUSD", 1.09, state="closed"),
                })
            if "EURUSD=X" in url:
                return httpx.Response(
                    200, json=_display_yahoo_price(1.10, rmt=now - 2))
            return httpx.Response(404, json={})

        monkeypatch.setattr(
            quotes.httpx, "AsyncClient",
            lambda *a, **kw: _real_client(
                transport=httpx.MockTransport(handler)))
        prices, meta, _ = await quotes.fetch_display_prices(["EURUSD"])
        assert prices["EURUSD"] == pytest.approx(1.10)
        assert meta["EURUSD"].source == "yahoo_fallback"
        assert meta["EURUSD"].market_state == "open"
        assert meta["EURUSD"].age_seconds == pytest.approx(2.0, abs=5.0)

    @pytest.mark.asyncio
    async def test_display_total_timeout_bounds_hanging_feeds(
            self, monkeypatch):
        """Biquote batch hang + Yahoo hang → returns within budget.

        Failure rows for every asset, no live network, suite stays fast.
        """
        monkeypatch.setattr(quotes, "_DISPLAY_TOTAL_TIMEOUT", 0.3)
        monkeypatch.setattr(quotes, "_DISPLAY_PER_ASSET_TIMEOUT", 0.2)

        async def _hang(*args, **kwargs):
            await asyncio.sleep(30.0)
            raise AssertionError("hung leg should have been cancelled")

        monkeypatch.setattr(quotes.biquote, "fetch_latest", _hang)
        monkeypatch.setattr(quotes, "_display_yahoo_one", _hang)
        monkeypatch.setattr(quotes, "_fetch_spot_exchangerate", _hang)
        t0 = time.monotonic()
        prices, meta, failures = await quotes.fetch_display_prices(
            ["EURUSD", "GBPJPY"])
        elapsed = time.monotonic() - t0
        assert prices == {}
        assert meta == {}
        assert set(failures) == {"EURUSD", "GBPJPY"}
        assert elapsed < 5.0

    @pytest.mark.asyncio
    async def test_display_batch_hang_returns_partials_within_budget(
            self, monkeypatch):
        """Hung Biquote batch + one fast Yahoo leg → partials, never wipeout.

        Single shared deadline: the batch hang must degrade to per-asset
        Yahoo fallback, keeping the fast asset in `out` and the hung one in
        `failures` — never a blank `{}` + blanket "display batch timeout"
        wipeout. No live network; hangs are deferred (cancelled on timeout),
        so wall time stays within budget + slack.
        """
        monkeypatch.setattr(quotes, "_DISPLAY_TOTAL_TIMEOUT", 1.5)
        monkeypatch.setattr(quotes, "_DISPLAY_PER_ASSET_TIMEOUT", 1.0)

        async def _hang(*args, **kwargs):
            await asyncio.Event().wait()
            raise AssertionError("hung leg should have been cancelled")

        async def _fake_yahoo(asset, client):
            if asset == "EURUSD":
                return 1.15, 2.0, ""
            await asyncio.Event().wait()
            raise AssertionError("hung leg should have been cancelled")

        async def _no_legacy(asset, client=None):
            return 0.0, f"{asset}: no legacy"

        monkeypatch.setattr(quotes.biquote, "fetch_latest", _hang)
        monkeypatch.setattr(quotes, "_display_yahoo_one", _fake_yahoo)
        monkeypatch.setattr(
            quotes, "_fetch_spot_exchangerate", _no_legacy)
        t0 = time.monotonic()
        prices, meta, failures = await quotes.fetch_display_prices(
            ["EURUSD", "GBPJPY"])
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0
        assert "EURUSD" in prices  # fast leg survives — no blank wipeout
        assert meta["EURUSD"].source.startswith("yahoo")
        assert "GBPJPY" in failures
        assert "timeout" in failures["GBPJPY"]

    @pytest.mark.asyncio
    async def test_display_partial_progress_survives_slow_batch(
            self, monkeypatch):
        """Slow-but-under-budget Biquote batch → both legs resolve.

        The batch serves EURUSD while GBPJPY falls back to fast Yahoo; the
        batch delay must not eat the fan-out budget (single shared deadline)
        and must not wipe out either leg.
        """
        from datetime import datetime, timezone

        monkeypatch.setattr(quotes, "_DISPLAY_TOTAL_TIMEOUT", 2.0)
        monkeypatch.setattr(quotes, "_DISPLAY_PER_ASSET_TIMEOUT", 1.0)

        async def _slow_batch(symbols, client=None):
            await asyncio.sleep(0.3)
            return {"EURUSD": quotes.biquote.BiquoteTick(
                symbol="EURUSD", bid=1.0945, ask=1.0947, mid=1.0946,
                spread=0.0002,
                timestamp=datetime(2026, 11, 25, 12, 0, 0,
                                   tzinfo=timezone.utc),
                source="biquote", exchange="BQT", market_state="open",
                stale=False, quote_age_seconds=3)}

        async def _fake_yahoo(asset, client):
            return 190.5, None, ""

        async def _no_legacy(asset, client=None):
            return 0.0, f"{asset}: no legacy"

        monkeypatch.setattr(quotes.biquote, "fetch_latest", _slow_batch)
        monkeypatch.setattr(quotes, "_display_yahoo_one", _fake_yahoo)
        monkeypatch.setattr(
            quotes, "_fetch_spot_exchangerate", _no_legacy)
        t0 = time.monotonic()
        prices, meta, failures = await quotes.fetch_display_prices(
            ["EURUSD", "GBPJPY"])
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0
        assert prices["EURUSD"] == pytest.approx(1.0946)
        assert meta["EURUSD"].source == "biquote"
        assert prices["GBPJPY"] == pytest.approx(190.5)
        assert failures == {}


# ---------------------------------------------------------------------------
# Execution spot parity — Biquote-first non-gold branch (Task 6)
# ---------------------------------------------------------------------------
def _exec_tick(symbol: str, mid: float):
    """Fresh-open Biquote tick stand-in (fetch_tick mock return value)."""
    from datetime import datetime, timezone
    return quotes.biquote.BiquoteTick(
        symbol=symbol, bid=mid - 0.00003, ask=mid + 0.00003, mid=mid,
        spread=0.00006, timestamp=datetime(2026, 11, 25, 12, 0, 0,
                                           tzinfo=timezone.utc),
        source="biquote", exchange="BQT", market_state="open",
        stale=False, quote_age_seconds=3)


def _capture_providers(monkeypatch) -> list:
    """Record quote_log provider tags (DB sink is a no-op in tests)."""
    seen: list = []
    orig = quotes.quote_log.log_call

    def _cap(*a, **kw):
        if "provider" in kw:
            seen.append(kw["provider"])
        return orig(*a, **kw)

    monkeypatch.setattr(quotes.quote_log, "log_call", _cap)
    return seen


class TestExecutionSpotParity:
    @pytest.fixture(autouse=True)
    def _reset_spot_state(self):
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0
        yield
        quotes._spot_cache.clear()
        quotes._spot_live.clear()
        quotes._spot_source.clear()
        quotes._exchange_key_idx = 0

    @pytest.mark.asyncio
    async def test_execution_spot_prefers_biquote_tick(self, monkeypatch):
        """Biquote tick first: fresh-open print wins, Yahoo never touched."""
        from app.integrations.biquote import BiquoteError  # noqa: F401
        mids = {"EURUSD": 1.0946, "CHFJPY": 170.13}
        biquote_calls: list[str] = []

        async def _tick(symbol: str = "XAUUSD", *, allow_stale: bool = False,
                       client=None):
            assert allow_stale is False
            biquote_calls.append(str(symbol).upper())
            return _exec_tick(str(symbol).upper(), mids[str(symbol).upper()])

        monkeypatch.setattr(quotes.biquote, "fetch_tick", _tick,
                            raising=False)
        providers = _capture_providers(monkeypatch)
        calls: list[str] = []
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, []))
        prices, failures = await quotes.fetch_spot_prices(
            ["EURUSD", "CHFJPY"])
        assert failures == {} and set(prices) == {"EURUSD", "CHFJPY"}
        assert prices["EURUSD"] == pytest.approx(1.0946)
        assert prices["CHFJPY"] == pytest.approx(170.13)
        assert quotes.spot_source("EURUSD") == "spot"
        assert len(biquote_calls) >= 1
        assert "biquote" in providers
        assert not any("yahoo" in c for c in calls)

    @pytest.mark.asyncio
    async def test_execution_spot_biquote_outage_keeps_yahoo_chain(
            self, monkeypatch):
        """Biquote down → silent fallthrough to the Yahoo leg."""
        from app.integrations.biquote import BiquoteError

        async def _down(symbol: str = "XAUUSD", *, allow_stale: bool = False,
                       client=None):
            raise BiquoteError("biquote: down (test)")

        monkeypatch.setattr(quotes.biquote, "fetch_tick", _down,
                            raising=False)
        providers = _capture_providers(monkeypatch)
        calls: list[str] = []
        responses = [_yahoo_payload(1.15)]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        prices, failures = await quotes.fetch_spot_prices(["EURUSD"])
        assert "EURUSD" in prices and failures == {}
        assert "yahoo" in providers and "biquote" in providers

    @pytest.mark.asyncio
    async def test_trusted_spot_still_drops_daily_rates(self, monkeypatch):
        """Writer path still refuses exchangerate daily rates (USDCAD)."""
        from app.core.config import get_settings as _gs
        from app.integrations.biquote import BiquoteError
        monkeypatch.setattr(_gs(), "exchangerate_api_keys", "keyA",
                            raising=False)

        async def _down(symbol: str = "XAUUSD", *, allow_stale: bool = False,
                       client=None):
            raise BiquoteError("biquote: down (test)")

        monkeypatch.setattr(quotes.biquote, "fetch_tick", _down,
                            raising=False)
        calls: list[str] = []
        responses = [
            _resp({}, status=429),          # yahoo dead, cold start: no live ref
            _ex_resp("CAD", 1.35),          # exchangerate /latest/USD → CAD leg
        ]
        monkeypatch.setattr(quotes.httpx, "AsyncClient",
                            _fake_client_factory(calls, responses))
        trusted, failures = await quotes.fetch_trusted_spot(["USDCAD"])
        assert "USDCAD" not in trusted and "USDCAD" in failures
