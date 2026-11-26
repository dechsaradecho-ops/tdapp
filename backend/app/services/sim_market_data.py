"""Sim price-data provider adapters (V2, no persistence).

Biquote is authoritative for the most recent 500 daily bars per asset;
Twelve Data fills older history (default five years). Every bar carries
``source``; invalid bars are quarantined by the caller, never averaged
or mixed for the same date.

Twelve Data free-tier limits (8 req/min, 800/day) are honoured by the
later sync service — these adapters do one request per call and expose
no batching, so the throttle point stays in one place.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import get_settings
from app.integrations.biquote import fetch_daily_bars
from app.services.price_history import SPIKE_PCT, today_utc

log = logging.getLogger(__name__)

TWELVEDATA_URL = "https://api.twelvedata.com/time_series"
TWELVEDATA_OUTPUTSIZE = "5000"


class SimMarketDataError(Exception):
    """Raised for missing key, HTTP, schema, or provider-error failures."""


@dataclass(frozen=True)
class SimBar:
    asset: str
    bar_date: str  # ISO YYYY-MM-DD
    open: float
    high: float
    low: float
    close: float
    source: str  # "biquote" | "twelvedata"
    source_timestamp: str | None = None


def twelvedata_symbol(asset: str) -> str:
    """Map a compact asset code to a Twelve Data symbol.

    ``EURUSD`` -> ``EUR/USD``, ``XAUUSD`` -> ``XAU/USD``. Values already
    containing ``/`` pass through upper-cased.
    """
    text = str(asset or "").strip().upper().replace(" ", "")
    if "/" in text:
        return text
    if len(text) == 6 and text.isalpha():
        return f"{text[:3]}/{text[3:]}"
    return text


async def fetch_biquote_recent(
    asset: str, client: httpx.AsyncClient
) -> list[SimBar]:
    """Fetch the most recent 500 daily bars from Biquote, dropping the open bar.

    The newest Biquote bar is still forming intraday (``is_open``) and must
    not seed frozen history. Returns oldest-first SimBars with
    ``source="biquote"``.
    """
    symbol = str(asset or "").strip().upper()
    raw = await fetch_daily_bars(symbol, limit=500, client=client)
    today = today_utc()
    bars: list[SimBar] = []
    for row in raw:
        if row.is_open:
            continue
        bar_date = row.open_time.astimezone(timezone.utc).date().isoformat()
        if bar_date >= today:
            # Forming/today bar must never seed frozen history.
            continue
        bars.append(SimBar(
            asset=symbol,
            bar_date=bar_date,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
            source="biquote",
            source_timestamp=row.open_time.astimezone(timezone.utc).isoformat(),
        ))
    bars.sort(key=lambda b: b.bar_date)
    return bars


async def fetch_twelvedata_history(
    asset: str, *, years: int, client: httpx.AsyncClient
) -> list[SimBar]:
    """Fetch older daily history from Twelve Data ``/time_series``.

    One request with ``interval=1day`` and ``outputsize=5000`` (covers the
    default five-year span for FX trading days). Twelve Data returns
    newest-first; bars are reversed to oldest-first with UTC date
    normalization. Rows with non-finite/non-positive OHLC are skipped.
    """
    _ = years  # span is covered by outputsize=5000; kept for the sync API.
    symbol = str(asset or "").strip().upper()
    api_key = get_settings().twelvedata_api_key
    if not api_key:
        raise SimMarketDataError(
            f"{symbol}: TWELVEDATA_API_KEY not set (free key: twelvedata.com)")
    params = {"symbol": twelvedata_symbol(symbol), "interval": "1day",
              "outputsize": TWELVEDATA_OUTPUTSIZE, "apikey": api_key}
    try:
        resp = await client.get(TWELVEDATA_URL, params=params, timeout=15.0)
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise SimMarketDataError(
            f"{symbol}: twelvedata request failed ({exc})") from exc
    if not isinstance(payload, dict):
        raise SimMarketDataError(f"{symbol}: twelvedata invalid payload")
    if str(payload.get("status", "")) == "error":
        raise SimMarketDataError(
            f"{symbol}: twelvedata error {payload.get('code')}: "
            f"{payload.get('message')}")
    values = payload.get("values") or []
    if not isinstance(values, list):
        raise SimMarketDataError(f"{symbol}: twelvedata invalid payload")

    bars: list[SimBar] = []
    today = today_utc()
    for row in reversed(values):  # TD returns newest-first -> flip
        if not isinstance(row, dict):
            continue
        try:
            o = float(row["open"])
            h = float(row["high"])
            l = float(row["low"])
            c = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if any(not math.isfinite(v) or v <= 0 for v in (o, h, l, c)):
            continue
        try:
            bar_date = datetime.fromisoformat(
                str(row.get("datetime") or "")[:10]).date().isoformat()
        except ValueError:
            continue
        if bar_date >= today:
            # Today's forming bar is never immutable history.
            continue
        bars.append(SimBar(
            asset=symbol,
            bar_date=bar_date,
            open=o,
            high=h,
            low=l,
            close=c,
            source="twelvedata",
            source_timestamp=str(row.get("datetime") or "") or None,
        ))
    bars.sort(key=lambda b: b.bar_date)
    return bars


@dataclass(frozen=True)
class DatasetBuildReport:
    version: int
    assets: list[str] = field(default_factory=list)
    bars: int = 0
    per_asset: dict[str, int] = field(default_factory=dict)
    seam_discrepancies: list[dict] = field(default_factory=list)
    invalid: list[dict] = field(default_factory=list)
    complete: bool = False
    data_hash: str = ""


def _ohlc(bar: SimBar) -> dict:
    return {"open": bar.open, "high": bar.high, "low": bar.low,
            "close": bar.close}


def merge_history(
    asset: str, biquote: list[SimBar], twelvedata: list[SimBar]
) -> tuple[list[SimBar], list[dict]]:
    """Merge Twelve Data history with Biquote recents, oldest-first.

    Biquote wins every overlapping date; each overlap is recorded with
    both OHLC sets and never averaged. Output is deterministic.
    """
    by_date: dict[str, SimBar] = {}
    td_by_date: dict[str, SimBar] = {}
    for bar in twelvedata or []:
        by_date.setdefault(bar.bar_date, bar)
        td_by_date.setdefault(bar.bar_date, bar)
    discrepancies: list[dict] = []
    for bar in biquote or []:
        old = by_date.get(bar.bar_date)
        if old is not None:
            td_bar = td_by_date.get(bar.bar_date, old)
            discrepancies.append({
                "asset": asset,
                "bar_date": bar.bar_date,
                "biquote": _ohlc(bar),
                "twelvedata": _ohlc(td_bar),
                "selected_source": "biquote",
            })
        by_date[bar.bar_date] = bar
    merged = [by_date[k] for k in sorted(by_date)]
    discrepancies.sort(key=lambda d: d["bar_date"])
    return merged, discrepancies


def hash_bars(bars: list[SimBar]) -> str:
    """Canonical SHA-256 over sorted (asset, bar_date, OHLC, source) rows."""
    rows = sorted(bars or [], key=lambda b: (b.asset, b.bar_date))
    digest = hashlib.sha256()
    for b in rows:
        digest.update(
            f"{b.asset}|{b.bar_date}|{b.open!r}|{b.high!r}|{b.low!r}"
            f"|{b.close!r}|{b.source}\n".encode("utf-8"))
    return digest.hexdigest()


def validate_sim_bar(
    bar: SimBar, previous_close: float | None = None
) -> list[str]:
    """Check ONE SimBar. Returns reason codes; empty = usable.

    Mirrors ``price_history.validate_bar`` rules with SimBar-specific
    reason codes (single ``range_inconsistent`` for any OHLC geometry
    violation). Never raises.
    """
    try:
        return _validate(bar, previous_close)
    except Exception as exc:  # noqa: BLE001 - quarantine, don't crash
        log.debug("validate sim bar %s failed closed: %s", bar, exc)
        return ["validator_error"]


def _validate(bar: SimBar, previous_close: float | None) -> list[str]:
    errs: list[str] = []
    bar_date = bar.bar_date
    if not bar_date or not isinstance(bar_date, str):
        return ["no_date"]
    try:
        datetime.fromisoformat(bar_date).date()
    except ValueError:
        return ["bad_date"]
    if bar_date > today_utc():
        errs.append("future_bar")
    vals: dict[str, float] = {}
    for name in ("open", "high", "low", "close"):
        try:
            f = float(getattr(bar, name))
        except (TypeError, ValueError):
            errs.append(f"bad_{name}")
            continue
        if not math.isfinite(f) or f <= 0:
            errs.append(f"bad_{name}")
            continue
        vals[name] = f
    if errs:
        return errs
    o_, h_, l_, c_ = vals["open"], vals["high"], vals["low"], vals["close"]
    eps = max(o_, h_, l_, c_) * 1e-9
    if h_ < l_ - eps or h_ < max(o_, c_) - eps or l_ > min(o_, c_) + eps:
        errs.append("range_inconsistent")
    if previous_close:
        try:
            pc = float(previous_close)
            if pc > 0 and abs(c_ - pc) / pc * 100.0 > SPIKE_PCT:
                errs.append("spike_suspect")
        except (TypeError, ValueError):
            pass
    return errs


# ---------------------------------------------------------------------------
# Background dataset build + persistence (Task 4).
#
# WHY A JOB AND NOT A REQUEST
# ---------------------------
# A five-year two-provider fetch plus validation/persistence is slow (Twelve
# Data alone is throttled to one request per 7.5 s), so it cannot live inside
# a request/response cycle. It runs on a single-worker thread pool mirroring
# app/services/simulation.py: one build at a time, progress stages on the
# meta row, and a terminal complete/failed verdict. Datasets are immutable —
# every build mints a new version and old versions are never updated.
# ---------------------------------------------------------------------------

BARS_TABLE = "sim_price_bars_v2"
META_TABLE = "sim_dataset_meta_v2"

CODE_VERSION = "sim-data-v2-1"

#: A dataset asset is usable only with this many closed valid bars.
MIN_BARS_PER_ASSET = 250

#: Persist flush size: rows are accumulated per build and written in chunks
#: of this many. 500 rows per POST is the sane starting point (36k bars ≈
#: 72 requests instead of 36k — at ~0.2 s per PostgREST round trip that is
#: minutes, not hours). ``_persist_batch`` POSTs each chunk as one JSON
#: array; a failed chunk raises loudly and fails the version.
_PERSIST_BATCH_SIZE = 500

#: Bulk-persist switch: True = one array-POST per batch; False = legacy
#: per-row loop (identical rows, for test comparison only).
_BULK_PERSIST = True

#: Read page size for the offset/limit bars fallback (no select_paged).
#: Larger pages = fewer SELECT round trips on big versions; rows are
#: concatenated in page order so the result is identical at any size.
_BARS_PAGE_SIZE = 5000

#: Twelve Data free tier: 8 req/min and 800/day. Stay below both — minimum
#: 7.5 s between request starts, hard daily cap at 750 for safety.
_TD_MIN_GAP_S = 7.5
_TD_DAILY_CAP = 750

#: One worker on purpose — a second concurrent build would double Twelve
#: Data pressure and interleave progress on the meta rows.
_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sim-data")

#: The live build version, so a second start is refused while one runs.
_ACTIVE_LOCK = threading.Lock()
_ACTIVE: str | None = None

_TD_LOCK = threading.Lock()
_TD_LAST_START = 0.0
_TD_DAY = ""
_TD_COUNT = 0


def _set_active(version: str | None) -> None:
    global _ACTIVE
    with _ACTIVE_LOCK:
        _ACTIVE = version


def active_build() -> str | None:
    with _ACTIVE_LOCK:
        return _ACTIVE


def _try_start_active(version: str) -> bool:
    """Claim the single-worker slot iff it is free — one locked CAS.

    The check and the set happen under the same lock acquisition so two
    concurrent starts cannot both pass the guard.
    """
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE is not None:
            return False
        _ACTIVE = version
        return True


def _release_active(version: str) -> None:
    """Clear the slot, but only if this version still holds it."""
    global _ACTIVE
    with _ACTIVE_LOCK:
        if _ACTIVE == version:
            _ACTIVE = None


def _new_version() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"v{stamp}-{uuid.uuid4().hex[:6]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _td_throttle() -> None:
    """Wait until a Twelve Data request may start. Never raises."""
    global _TD_LAST_START, _TD_DAY, _TD_COUNT
    day = datetime.now(timezone.utc).date().isoformat()
    with _TD_LOCK:
        if _TD_DAY != day:
            _TD_DAY, _TD_COUNT = day, 0
        gap = _TD_MIN_GAP_S
        wait = gap - (time.monotonic() - _TD_LAST_START) if _TD_LAST_START else 0.0
    if wait and wait > 0:
        await asyncio.sleep(wait)
    with _TD_LOCK:
        _TD_LAST_START = time.monotonic()
        _TD_COUNT += 1


def _td_over_daily_cap() -> bool:
    day = datetime.now(timezone.utc).date().isoformat()
    with _TD_LOCK:
        return _TD_DAY == day and _TD_COUNT >= _TD_DAILY_CAP


def _date_range(years: int) -> tuple[str, str]:
    today = today_utc()
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=max(1, int(years)) * 365)).isoformat()
    return cutoff, today


def _cutoff_plus_30d(cutoff: str) -> str:
    """Requested cutoff + 30 calendar days (coverage tolerance window)."""
    try:
        day = datetime.fromisoformat(str(cutoff)[:10]).date()
    except ValueError:
        return str(cutoff)
    return (day + timedelta(days=30)).isoformat()


def _select_all_bars_fallback(db, version: str, page_size: int = _BARS_PAGE_SIZE,
                              max_rows: int = 200000) -> list[dict]:
    """Page through bars with offset/limit until a short page arrives.

    Replaces the old single ``limit=20000`` read which silently truncated
    multi-asset datasets. Raises the underlying DB error so callers can
    fail closed instead of replaying a truncated dataset.
    """
    out: list[dict] = []
    offset = 0
    while offset < max_rows:
        page = db.select(BARS_TABLE,
                         filters={"dataset_version": str(version)},
                         order="bar_date", desc=False,
                         limit=page_size, offset=offset)
        if not page:
            break
        out.extend(page)
        if len(page) < page_size:
            break
        offset += page_size
    return out


def _clean_provider_bars(
    bars: list[SimBar], cutoff: str, today: str
) -> tuple[list[SimBar], list[str]]:
    """Filter to [cutoff, today) and reject duplicate dates, first-seen wins.

    Duplicates are rejected BEFORE merge so one provider can never silently
    overwrite itself — the merge only resolves biquote-vs-twelvedata seams.
    ``today`` itself is excluded: only closed daily bars enter the immutable
    dataset, from either provider.
    """
    seen: set[str] = set()
    clean: list[SimBar] = []
    dups: list[str] = []
    for b in bars or []:
        if b.bar_date < cutoff or b.bar_date >= today:
            continue
        if b.bar_date in seen:
            dups.append(b.bar_date)
            continue
        seen.add(b.bar_date)
        clean.append(b)
    clean.sort(key=lambda b: b.bar_date)
    return clean, sorted(set(dups))


def _await_sync(coro):
    """Run a coroutine to completion from sync worker code.

    The worker normally has no running loop, but tests may drive the build
    inline from inside one — so the coroutine always runs on a helper thread
    with its own fresh loop instead of assuming either case.
    """
    out: dict = {}

    def _target() -> None:
        try:
            out["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised below
            out["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "error" in out:
        raise out["error"]
    return out.get("value")


def start_dataset_build(db, *, assets: list[str],
                        years: int = 5,
                        base_version: str | None = None) -> dict:
    """Mint a new dataset version and hand it to the worker. Returns at once.

    One build at a time: a second start while one is live is refused with
    ``ok: False`` instead of queueing, so Twelve Data pressure stays flat.

    ``base_version`` reuses a terminal-complete version's bars (validated up
    front; unknown/incomplete bases refuse with zero writes). ``None``
    preserves the original full-build behavior exactly.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    clean = sorted({str(a).strip().upper() for a in (assets or [])
                    if str(a).strip()})
    if not clean:
        return {"ok": False, "error": "assets required"}
    if len(clean) > 40:
        return {"ok": False, "error": "assets required (1-40)"}
    try:
        yrs = int(years or 5)
    except (TypeError, ValueError):
        yrs = 5
    yrs = max(1, min(yrs, 10))
    cutoff, today = _date_range(yrs)
    try:
        bv, base_map, _ = _resolve_base(db, base_version, cutoff, today)
    except _BaseRefused as exc:
        return {"ok": False, "error": str(exc)}
    return _start_build_inner(db, assets=clean, years=yrs,
                              base_version=bv, base_bars=base_map)


def update_dataset(db, *, assets: list[str] | None = None,
                   years: int = 5,
                   base_version: str | None = None) -> dict:
    """Start a dataset update: same worker, same verdicts, shared CAS slot.

    ``assets`` omitted defaults to the newest complete version's asset set
    (explicit base's set when ``base_version`` is given); with no complete
    version at all this is a full build (same as no base). ``years`` is
    validated 1-10 like the build path. Refusals write nothing.
    """
    if not db or not getattr(db, "available", False):
        return {"ok": False, "error": "DB unavailable"}
    try:
        yrs = int(years or 5)
    except (TypeError, ValueError):
        yrs = 5
    yrs = max(1, min(yrs, 10))
    given = sorted({str(a).strip().upper() for a in (assets or [])
                    if str(a).strip()})
    if len(given) > 40:
        return {"ok": False, "error": "assets required (1-40)"}
    cutoff, today = _date_range(yrs)
    bv_raw = (base_version or "").strip() or None
    if bv_raw is not None:
        try:
            bv, base_map, meta_assets = _resolve_base(
                db, bv_raw, cutoff, today)
        except _BaseRefused as exc:
            return {"ok": False, "error": str(exc)}
        clean = given or meta_assets
    else:
        latest = _latest_complete_row(db)
        if latest is None:
            bv, base_map, clean = None, {}, given
        else:
            try:
                bv, base_map, meta_assets = _resolve_base(
                    db, str(latest.get("dataset_version") or ""),
                    cutoff, today)
            except _BaseRefused as exc:
                return {"ok": False, "error": str(exc)}
            clean = given or meta_assets
    if not clean:
        return {"ok": False, "error": "assets required"}
    return _start_build_inner(db, assets=clean, years=yrs,
                              base_version=bv, base_bars=base_map)


def _start_build_inner(db, *, assets: list[str], years: int,
                       base_version: str | None,
                       base_bars: dict) -> dict:
    """Claim the single-worker slot, write the meta row, submit the worker."""
    version = _new_version()
    if not _try_start_active(version):
        live = active_build()
        return {"ok": False, "error": "มีงาน build รันอยู่แล้ว",
                "version": live}
    try:
        row = {
            "dataset_version": version,
            "status": "building",
            "years": years,
            "assets": assets,
            "started_at": _now_iso(),
            "finished_at": None,
            "report": {"stage": "queued", "version": version,
                       "assets": assets, "years": years},
            "data_hash": None,
            "code_version": CODE_VERSION,
        }
        if db.insert(META_TABLE, row) is None:
            _release_active(version)
            return {"ok": False, "error": "could not create dataset row"}
    except Exception as exc:
        log.error("sim-data: could not create dataset row: %s", exc)
        _release_active(version)
        return {"ok": False, "error": "could not create dataset row"}
    _POOL.submit(_run_build, db, version, assets, years,
                 base_version, base_bars)
    return {"ok": True, "version": version, "status": "building"}


class _BaseRefused(Exception):
    """A requested base version cannot be reused (unknown/incomplete/unreadable)."""


def _is_terminal_complete(meta: dict) -> bool:
    """Terminal-complete gate shared by load_dataset, update, and latest."""
    report = meta.get("report") or {}
    return (str(meta.get("status") or "") in ("complete", "done")
            and report.get("complete") is True)


def _latest_complete_row(db) -> dict | None:
    """Newest COMPLETE meta row, or None. Read-only; never raises.

    Newest = lexicographic max of ``dataset_version`` (versions embed a UTC
    ``YYYYMMDD-HHMMSS`` stamp, so max == newest). Compared in Python so the
    answer is stable regardless of DB ordering support. A read failure
    yields None — callers treat that as "no base", which can only trigger a
    fresh full build (same gates), never a stale/incomplete serve.
    """
    try:
        rows = db.select(META_TABLE, order="started_at", desc=True,
                         limit=1000)
    except Exception as exc:
        log.warning("sim-data latest-complete lookup failed: %s", exc)
        return None
    best: dict | None = None
    for r in rows or []:
        if not isinstance(r, dict) or not _is_terminal_complete(r):
            continue
        if best is None or (str(r.get("dataset_version") or "")
                            > str(best.get("dataset_version") or "")):
            best = r
    return best


def latest_complete_version(db) -> dict | None:
    """Public wrapper for the /sim-data/latest route. Read-only."""
    return _latest_complete_row(db)


def _resolve_base(db, base_version: str | None, cutoff: str,
                  today: str) -> tuple[str | None, dict, list[str]]:
    """Validate a base version and snapshot its in-range bars.

    Returns ``(base_version, {asset: [SimBar]}, meta_assets)`` with bars
    already filtered to ``[cutoff, today)``, deduplicated, oldest-first, and
    keeping their original ``source``/``source_timestamp``. Raises
    :class:`_BaseRefused` for unknown/incomplete/unreadable bases — callers
    refuse with zero writes, never fall back silently. ``None`` base passes
    through as ``(None, {}, [])``.
    """
    bv = (base_version or "").strip() or None
    if bv is None:
        return None, {}, []
    try:
        rows = db.select(META_TABLE, filters={"dataset_version": bv},
                         order="started_at", desc=True, limit=1)
    except Exception as exc:
        raise _BaseRefused(f"base dataset {bv} unreadable") from exc
    if not rows:
        raise _BaseRefused(f"unknown base version {bv}")
    meta = rows[0] or {}
    if not _is_terminal_complete(meta):
        raise _BaseRefused(f"base version {bv} is not complete")
    meta_assets = sorted({str(a).strip().upper()
                          for a in (meta.get("assets") or [])
                          if str(a).strip()})
    try:
        if hasattr(db, "select_paged"):
            bar_rows = db.select_paged(BARS_TABLE,
                                       filters={"dataset_version": bv},
                                       order="bar_date", desc=False)
        else:
            bar_rows = _select_all_bars_fallback(db, bv)
    except Exception as exc:
        raise _BaseRefused(f"base dataset {bv} bars unreadable") from exc
    grouped: dict[str, list[SimBar]] = {}
    for r in bar_rows or []:
        if not isinstance(r, dict):
            continue
        try:
            bar = SimBar(
                asset=str(r.get("asset") or "").upper(),
                bar_date=str(r.get("bar_date") or ""),
                open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]),
                source=str(r.get("source") or ""),
                source_timestamp=r.get("source_timestamp"),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if not bar.asset or bar.source not in ("biquote", "twelvedata"):
            continue
        grouped.setdefault(bar.asset, []).append(bar)
    # One paged read, filtered in memory per asset: no per-asset re-reads.
    cleaned: dict[str, list[SimBar]] = {}
    for asset, bars in grouped.items():
        keep, _dups = _clean_provider_bars(bars, cutoff, today)
        if keep:
            cleaned[asset] = keep
    return bv, cleaned, meta_assets


def _td_needed(base_asset_bars: list[SimBar] | None, cutoff: str,
               today: str, bq_start: str | None) -> bool:
    """True unless base Twelve Data bars cover every trading day in range.

    Skip requires FULL coverage: each weekday (Mon-Fri) in
    ``[cutoff, bq_start)`` — or ``[cutoff, today)`` with no fresh Biquote —
    must be present in the base TD-sourced date set. An interior hole (or a
    holiday gap, which is indistinguishable from one) forces a fetch: the
    safe conservative direction. The endpoint comparison stays only as a
    fast path to *require* a fetch; it can never grant a skip on its own.
    """
    if not base_asset_bars:
        return True
    td_set = {b.bar_date for b in base_asset_bars
              if b.source == "twelvedata"}
    if not td_set:
        return True
    try:
        start = datetime.fromisoformat(cutoff[:10]).date()
        end_excl = datetime.fromisoformat((bq_start or today)[:10]).date()
    except ValueError:
        return True
    # Fast path: endpoints outside the base TD span cannot be covered.
    if (min(td_set) > cutoff
            or max(td_set) < (end_excl - timedelta(days=1)).isoformat()):
        return True
    day = start
    while day < end_excl:
        if day.weekday() < 5 and day.isoformat() not in td_set:
            return True
        day += timedelta(days=1)
    return False


def dataset_build_status(db, version: str) -> dict:
    """Status + stage + report for one dataset version. Never raises."""
    try:
        rows = db.select(META_TABLE, filters={"dataset_version": str(version)},
                         order="started_at", desc=True, limit=1)
    except Exception as exc:
        return {"ok": False, "error": f"{exc}"[:200]}
    if not rows:
        return {"ok": False, "error": "unknown dataset version"}
    row = rows[0]
    report = row.get("report") or {}
    status = str(row.get("status") or "building")
    return {
        "ok": True,
        "version": str(row.get("dataset_version") or version),
        "status": status,
        "stage": str(report.get("stage") or status),
        "report": report,
        "data_hash": row.get("data_hash"),
        "assets": row.get("assets") or [],
        "years": row.get("years"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
    }


def load_dataset(db, version: str,
                 assets: list[str] | None = None) -> dict[str, list]:
    """Persisted bars as ``Candle(o,h,l,c,t)`` per asset, oldest-first.

    Plan 2 (portfolio replay / optimizer) must consume ONLY complete
    immutable versions: this returns bars solely when the dataset meta row
    is terminal complete (``status`` in ``("complete", "done")`` AND
    ``report.complete is True``). Building, failed, or unknown versions
    yield ``{}``.

    Actual DB read failures raise :class:`SimMarketDataError` instead of
    returning ``{}`` so callers never mistake an outage for an empty
    dataset. Unknown/incomplete versions still return ``{}``.
    """
    from app.integrations.quotes import Candle

    want = ({str(a).strip().upper() for a in (assets or []) if str(a).strip()}
            or None)
    # -- Meta gate: only terminal-complete versions are loadable. --
    try:
        if hasattr(db, "select_ex"):
            meta_rows = db.select_ex(
                META_TABLE, filters={"dataset_version": str(version)},
                order="started_at", desc=True, limit=1)
        else:
            meta_rows = db.select(
                META_TABLE, filters={"dataset_version": str(version)},
                order="started_at", desc=True, limit=1)
    except Exception as exc:
        raise SimMarketDataError(
            f"{version}: dataset meta read failed ({exc})") from exc
    if not meta_rows:
        return {}
    meta = meta_rows[0] or {}
    report = meta.get("report") or {}
    status = str(meta.get("status") or "")
    if status not in ("complete", "done") or report.get("complete") is not True:
        return {}
    try:
        if hasattr(db, "select_paged"):
            rows = db.select_paged(BARS_TABLE,
                                   filters={"dataset_version": str(version)},
                                   order="bar_date", desc=False)
        else:
            rows = _select_all_bars_fallback(db, str(version))
    except Exception as exc:
        raise SimMarketDataError(
            f"{version}: dataset bars read failed ({exc})") from exc
    out: dict[str, list] = {}
    for r in rows or []:
        asset = str(r.get("asset") or "").upper()
        if want is not None and asset not in want:
            continue
        try:
            day = datetime.fromisoformat(str(r.get("bar_date"))[:10]).date()
        except ValueError:
            continue
        t = int(datetime(day.year, day.month, day.day,
                         tzinfo=timezone.utc).timestamp())
        try:
            cnd = Candle(o=float(r["open"]), h=float(r["high"]),
                         l=float(r["low"]), c=float(r["close"]), t=t)
        except (KeyError, TypeError, ValueError):
            continue
        out.setdefault(asset, []).append(cnd)
    for candles in out.values():
        candles.sort(key=lambda c: c.t)
    return out


def _meta_patch(db, version: str, changes: dict) -> bool:
    """Patch a meta row whose PK is dataset_version, not id.

    Update-in-place first; insert only when the update reports missing.
    Never delete-then-insert: a failed insert after a delete would orphan
    the version (row gone, build stuck at ``building``). Returns True on
    success, False when the row is missing or the write failed (logged).
    """
    try:
        rows = db.select(META_TABLE, filters={"dataset_version": version},
                         order="started_at", desc=True, limit=1)
    except Exception as exc:
        log.warning("sim-data meta patch %s read failed: %s", version, exc)
        return False
    if not rows:
        return False
    row = dict(rows[0])
    row_id = row.get("id")
    # 1. Fake/in-memory path: row carries an ``id`` so Database.update works.
    if row_id is not None and hasattr(db, "update"):
        try:
            if db.update(META_TABLE, row_id, changes):
                return True
        except Exception as exc:
            log.warning("sim-data meta patch %s update failed: %s",
                        version, exc)
            return False
        # Update reported missing -> fall through to insert the merged row.
        log.warning("sim-data meta patch %s update missed; inserting",
                    version)
    else:
        # 2. Real Supabase path: no ``id`` column — update by PK directly
        # so the row is never deleted. Only service_role reaches here.
        client = getattr(db, "_client", None)
        probe = None
        if client is not None:
            try:
                probe = client.table(META_TABLE).update  # type: ignore[attr-defined]
            except Exception:
                probe = None
        if callable(probe):
            try:
                resp = client.table(META_TABLE).update(changes).eq(
                    "dataset_version", version).execute()
                data = getattr(resp, "data", None)
                if data:
                    return True
                log.warning(
                    "sim-data meta patch %s update matched 0 rows; "
                    "inserting", version)
            except Exception as exc:
                log.warning("sim-data meta patch %s update failed: %s",
                            version, exc)
                return False
        else:
            # No usable in-place path and the row exists: do NOT delete it.
            # Merging + inserting would PK-conflict while the row lives, and
            # deleting first risks total loss — report failure instead.
            log.warning("sim-data meta patch %s no in-place path; "
                        "leaving row untouched", version)
            return False
    # 3. Insert-only fallback: the update reported missing, so the row is
    # gone — re-insert the merged row and CHECK the result.
    merged = dict(row)
    merged.update(changes)
    try:
        # Drop the fake-only ``id`` so a re-insert gets a fresh one.
        merged.pop("id", None)
        ok = db.insert(META_TABLE, merged)
    except Exception as exc:
        log.warning("sim-data meta patch %s insert failed: %s",
                    version, exc)
        return False
    if ok is None:
        log.warning("sim-data meta patch %s insert failed (None)", version)
        return False
    return True


def _set_stage(db, version: str, stage: str) -> None:
    try:
        rows = db.select(META_TABLE, filters={"dataset_version": version},
                         order="started_at", desc=True, limit=1)
        report = dict((rows[0].get("report") or {}) if rows else {})
        report["stage"] = stage
        _meta_patch(db, version, {"report": report})
    except Exception as exc:
        log.debug("sim-data stage %s/%s failed: %s", version, stage, exc)


def _run_build(db, version: str, assets: list[str], years: int,
               base_version: str | None = None,
               base_bars: dict | None = None) -> None:
    """Execute one dataset build to a terminal complete/failed row.

    Never raises and never leaves the version `building`: every exit path
    writes a verdict and clears the single-worker slot.

    With ``base_bars`` (snapshot from a terminal-complete ``base_version``),
    fresh Biquote is still fetched for every asset and wins every overlap;
    the Twelve Data fetch is skipped per asset iff base TD bars fully cover
    ``[cutoff, bq_start)``; remaining base bars fill dates neither fresh
    provider covers. All gates apply unchanged to the final set.
    """
    cutoff, today = _date_range(years)
    base_map: dict[str, list[SimBar]] = dict(base_bars or {})
    per_asset: dict[str, dict] = {}
    seam_all: list[dict] = []
    kept: list[SimBar] = []
    failed_assets: list[str] = []
    base_ids = {id(b) for bars in base_map.values() for b in bars}
    fetched_bars = 0
    reused_bars = 0
    reused_per_asset: dict[str, int] = {}
    try:
        _set_stage(db, version, "fetching_biquote")

        async def _fetch_bq() -> dict:
            import httpx as _httpx

            out: dict = {}
            async with _httpx.AsyncClient() as client:
                for asset in assets:
                    try:
                        out[asset] = await fetch_biquote_recent(asset, client)
                        out[asset + ":err"] = None
                    except Exception as exc:
                        out[asset] = []
                        out[asset + ":err"] = (
                            f"{type(exc).__name__}: {exc}"[:200])
            return out

        bq_data = _await_sync(_fetch_bq())
        # Clean Biquote first: its earliest date bounds the TD gap per
        # asset, so the skip decision is made before any TD request.
        clean_bq_map: dict[str, list[SimBar]] = {}
        dup_bq_map: dict[str, list[str]] = {}
        need_td: dict[str, bool] = {}
        for asset in assets:
            cb, dup_bq = _clean_provider_bars(
                bq_data.get(asset) or [], cutoff, today)
            clean_bq_map[asset] = cb
            dup_bq_map[asset] = dup_bq
            bq_start = cb[0].bar_date if cb else None
            need_td[asset] = _td_needed(base_map.get(asset), cutoff,
                                       today, bq_start)

        async def _fetch_td(only: list[str]) -> dict:
            import httpx as _httpx

            out: dict = {}
            async with _httpx.AsyncClient() as client:
                for asset in only:
                    if _td_over_daily_cap():
                        out[asset] = []
                        out[asset + ":err"] = (
                            "twelvedata daily cap reached (750)")
                        continue
                    try:
                        await _td_throttle()
                        out[asset] = await fetch_twelvedata_history(
                            asset, years=years, client=client)
                        out[asset + ":err"] = None
                    except Exception as exc:
                        out[asset] = []
                        out[asset + ":err"] = (
                            f"{type(exc).__name__}: {exc}"[:200])
            return out

        _set_stage(db, version, "fetching_twelvedata")
        td_needed_assets = [a for a in assets if need_td.get(a, True)]
        td_data = _await_sync(_fetch_td(td_needed_assets))
        _set_stage(db, version, "validating")
        fetched = {
            asset: {"bq": bq_data.get(asset) or [],
                    "td": td_data.get(asset) or [],
                    "bq_err": bq_data.get(asset + ":err"),
                    "td_err": td_data.get(asset + ":err")}
            for asset in assets
        }
        for asset in assets:
            pack = fetched.get(asset) or {}
            raw_td = pack.get("td") or []
            clean_bq = clean_bq_map.get(asset) or []
            dup_bq = dup_bq_map.get(asset) or []
            clean_td, dup_td = _clean_provider_bars(raw_td, cutoff, today)
            merged, discrepancies = merge_history(asset, clean_bq, clean_td)
            seam_all.extend(discrepancies)
            # Union base bars for dates neither fresh provider covers.
            # Source priority on collision: fresh-BQ > base-BQ >
            # fresh-TD > base-TD; a Biquote winner over a Twelve Data
            # bar records a seam discrepancy under the current rules.
            base_asset_bars = base_map.get(asset) or []
            if base_asset_bars:
                fresh_by_date = {b.bar_date: b for b in merged}
                base_by_date: dict[str, SimBar] = {}
                for bb in base_asset_bars:
                    base_by_date.setdefault(bb.bar_date, bb)
                for bar_date in sorted(base_by_date):
                    bb = base_by_date[bar_date]
                    f = fresh_by_date.get(bar_date)
                    if f is None:
                        merged.append(bb)
                        fresh_by_date[bar_date] = bb
                    elif (f.source == "biquote"
                          and bb.source == "twelvedata"):
                        discrepancies.append({
                            "asset": asset,
                            "bar_date": bar_date,
                            "biquote": _ohlc(f),
                            "twelvedata": _ohlc(bb),
                            "selected_source": "biquote",
                        })
                        seam_all.append(discrepancies[-1])
                    elif (f.source == "twelvedata"
                          and bb.source == "biquote"):
                        merged[merged.index(f)] = bb
                        fresh_by_date[bar_date] = bb
                        discrepancies.append({
                            "asset": asset,
                            "bar_date": bar_date,
                            "biquote": _ohlc(bb),
                            "twelvedata": _ohlc(f),
                            "selected_source": "biquote",
                        })
                        seam_all.append(discrepancies[-1])
                merged.sort(key=lambda b: b.bar_date)
            fetched_bars += len(clean_bq) + len(clean_td)
            valid: list[SimBar] = []
            invalid: list[dict] = []
            prev: float | None = None
            for b in merged:
                errs = validate_sim_bar(b, prev)
                if errs:
                    invalid.append({"asset": asset, "bar_date": b.bar_date,
                                    "source": b.source, "reasons": errs})
                else:
                    valid.append(b)
                    prev = b.close
            if not valid or len(valid) < MIN_BARS_PER_ASSET:
                coverage_error = (
                    f"insufficient_bars: {len(valid)} "
                    f"< {MIN_BARS_PER_ASSET}")
            elif years > 2:
                td_valid = sum(1 for b in valid
                               if b.source == "twelvedata")
                earliest = valid[0].bar_date if valid else None
                if td_valid < 1:
                    coverage_error = (
                        "twelvedata_missing: years>2 requires at least "
                        "one Twelve Data bar per asset")
                elif (earliest is not None
                      and earliest > _cutoff_plus_30d(cutoff)):
                    coverage_error = (
                        f"history_gap: earliest {earliest} after "
                        f"{cutoff}+30d")
                else:
                    coverage_error = None
            else:
                coverage_error = None
            if coverage_error is not None:
                if asset not in failed_assets:
                    failed_assets.append(asset)
            kept.extend(valid)
            reused_n = sum(1 for b in valid if id(b) in base_ids)
            reused_bars += reused_n
            reused_per_asset[asset] = reused_n
            per_asset[asset] = {
                "start": valid[0].bar_date if valid else None,
                "end": valid[-1].bar_date if valid else None,
                "biquote": len(clean_bq),
                "twelvedata": len(clean_td),
                "twelvedata_valid": sum(
                    1 for b in valid if b.source == "twelvedata"),
                "merged": len(merged),
                "persisted": len(valid),
                "reused": reused_n,
                "td_skipped_reuse": not need_td.get(asset, True),
                "invalid": invalid,
                "duplicate_dates": {"biquote": dup_bq, "twelvedata": dup_td},
                "provider_errors": [e for e in (pack.get("bq_err"),
                                                pack.get("td_err")) if e],
                "coverage_error": coverage_error,
            }
        data_hash = hash_bars(kept)
        complete = not failed_assets
        _set_stage(db, version, "persisting")
        batch: list[SimBar] = []
        for b in kept:
            batch.append(b)
            if len(batch) >= _PERSIST_BATCH_SIZE:
                _persist_batch(db, version, batch)
                batch = []
        if batch:
            _persist_batch(db, version, batch)
        report = {
            "stage": "done" if complete else "failed",
            "version": version,
            "assets": assets,
            "years": years,
            "bars": len(kept),
            "base_version": base_version,
            "reused_bars": reused_bars,
            "reused_per_asset": reused_per_asset,
            "fetched_bars": fetched_bars,
            "per_asset": per_asset,
            "seam_discrepancies": sorted(
                seam_all, key=lambda d: (d.get("asset") or "",
                                         d.get("bar_date") or "")),
            "complete": complete,
            "failed_assets": sorted(failed_assets),
            "data_hash": data_hash,
            "code_version": CODE_VERSION,
        }
        _meta_patch_ok = _meta_patch(db, version, {
            "status": "complete" if complete else "failed",
            "finished_at": _now_iso(),
            "report": report,
            "data_hash": data_hash,
        })
        if not _meta_patch_ok:
            log.error("sim-data build %s: final meta write failed; "
                      "forcing failed verdict", version)
            raise SimMarketDataError(
                f"{version}: final meta write failed")
    except Exception as exc:                       # noqa: BLE001 - verdict, not a crash
        log.exception("sim-data build %s failed", version)
        try:
            _meta_patch(db, version, {
                "status": "failed",
                "finished_at": _now_iso(),
                "report": {"stage": "failed", "version": version,
                           "assets": assets, "years": years,
                           "base_version": base_version,
                           "complete": False,
                           "error": f"{type(exc).__name__}: {exc}"[:300],
                           "per_asset": per_asset,
                           "seam_discrepancies": seam_all[-50:]},
            })
        except Exception:
            pass
    finally:
        _release_active(version)


def _persist_batch(db, version: str, batch: list[SimBar]) -> None:
    """Insert one batch; a failed batch fails the build loudly.

    Bulk by default (``_BULK_PERSIST``): one ``db.insert`` carrying the row
    array — one PostgREST request per batch instead of one per bar. A falsy
    or short response raises :class:`SimMarketDataError`, so a failed batch
    can never leave a silently partial version: the worker marks the
    version failed. ``_BULK_PERSIST = False`` restores the legacy per-row
    loop (identical rows) for test comparison.
    """
    rows = [{
        "dataset_version": version,
        "asset": b.asset,
        "bar_date": b.bar_date,
        "open": b.open,
        "high": b.high,
        "low": b.low,
        "close": b.close,
        "source": b.source,
        "source_timestamp": b.source_timestamp,
    } for b in batch]
    if _BULK_PERSIST:
        try:
            result = db.insert(BARS_TABLE, rows)
        except Exception as exc:
            raise SimMarketDataError(
                f"{version}: persist failed for batch of {len(rows)} "
                f"({type(exc).__name__})") from exc
        if result is None or (isinstance(result, list)
                              and len(result) != len(rows)):
            raise SimMarketDataError(
                f"{version}: persist failed for batch of {len(rows)}")
        return
    for b, row in zip(batch, rows):
        if db.insert(BARS_TABLE, row) is None:
            raise SimMarketDataError(
                f"{version}: persist failed at {b.asset} {b.bar_date}")
