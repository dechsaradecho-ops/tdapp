import sys
sys.path.insert(0, '..')
from app.services.database import Database
from datetime import datetime, timezone

db = Database()

# ดู signals ทั้งหมดล่าสุด
print('=== All recent signals (last 20) ===')
signals = db.select('signals', limit=20)

print(f'Total signals in DB: {len(signals)}')
print()

# แสดงรายละเอียดทั้งหมด
for i, s in enumerate(signals[:20], 1):
    asset = s.get('asset')
    approval = s.get('approval')
    created = s.get('created_at')
    direction = s.get('direction')
    confidence = s.get('confidence_pct')
    print(f'{i:2d}. {created} - {asset:8s} {direction:4s} conf={confidence}% approval={approval}')

print()
print('=== Check UTC now ===')
print(f'UTC now: {datetime.now(timezone.utc)}')
print(f'Local now: {datetime.now()}')

# Check signals from last 24 hours
print()
print('=== Signals in last 24 hours ===')
from datetime import timedelta
cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
recent = [s for s in signals if s.get('created_at') and s.get('created_at') > cutoff]
print(f'Count: {len(recent)}')
for s in recent:
    print(f"  {s.get('created_at')} - {s.get('asset')} {s.get('approval')}")
