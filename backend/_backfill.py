"""Bulk backfill price_history_daily for all 28 pairs (one-off).

Same rules as sync_asset (validate first, insert-missing-only, never touch
stored rows) but with bulk inserts — 14k one-by-one upserts would take half
an hour over PostgREST.
"""
import asyncio
import sys
sys.path.insert(0, r'D:\tdapp\backend')
import httpx
from app.integrations import quotes
from app.services.database import Database
from app.services import price_history
from app.services.simulation import DEFAULT_ASSETS

db = Database()


async def fetch_all(assets, days=1095):
    out = {}
    async with httpx.AsyncClient() as client:
        for a in assets:
            try:
                out[a] = await quotes.fetch_candles(a, client, days=days)
            except Exception as exc:
                print(f'  ! {a} fetch failed: {str(exc)[:80]}', flush=True)
    return out


def main():
    fetched = asyncio.run(fetch_all(DEFAULT_ASSETS))
    total_ins = 0
    for asset in DEFAULT_ASSETS:
        candles = fetched.get(asset) or []
        try:
            have = {str(r.get('bar_date')) for r in db.select(
                price_history.TABLE, filters={'asset': asset},
                order='bar_date', desc=False, limit=5000) or []}
        except Exception as exc:
            print(f'  ! {asset} read failed: {str(exc)[:80]}', flush=True)
            continue
        rows, prev, bad = [], None, 0
        for c in candles:
            d = price_history.bar_date_of(getattr(c, 't', 0))
            o, h, l, cc = (getattr(c, 'o', 0), getattr(c, 'h', 0),
                           getattr(c, 'l', 0), getattr(c, 'c', 0))
            if not d or d in have or any(
                    r['bar_date'] == d for r in rows):
                continue
            errs = price_history.validate_bar(asset, d, o, h, l, cc, prev)
            if errs:
                bad += 1
                continue
            try:
                prev = float(cc)
            except (TypeError, ValueError):
                pass
            rows.append({'asset': asset, 'bar_date': d,
                         'open': round(float(o), 6), 'high': round(float(h), 6),
                         'low': round(float(l), 6), 'close': round(float(cc), 6),
                         'source': 'yahoo'})
        ins = 0
        for i in range(0, len(rows), 500):
            try:
                db._client.table(price_history.TABLE) \
                    .insert(rows[i:i + 500]).execute()
                ins += len(rows[i:i + 500])
            except Exception as exc:
                print(f'  ! {asset} batch failed: {str(exc)[:100]}', flush=True)
        total_ins += ins
        print(f'  {asset:8} fetched={len(candles):4} inserted={ins:4} '
              f'quarantined={bad} already_had={len(have)}', flush=True)
    print(f'DONE total_inserted={total_ins}', flush=True)


main()
