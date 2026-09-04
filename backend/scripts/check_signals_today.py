import sys
sys.path.insert(0, '..')
from app.services.database import Database
from datetime import datetime, timezone

db = Database()

# ดู signals ทั้งหมดวันนี้
today = datetime.now(timezone.utc).date().isoformat()
print(f'=== Signals created today ({today}) ===')
signals = db.select('signals', limit=100)

today_signals = []
for s in signals:
    created = str(s.get('created_at', ''))
    if created.startswith(today):
        today_signals.append(s)

print(f'Total signals today: {len(today_signals)}')
print()

# แสดงรายละเอียด
for s in today_signals:
    asset = s.get('asset')
    approval = s.get('approval')
    created = s.get('created_at')
    print(f'{created} - {asset:8s} approval={approval}')

print()
print('=== Check pending signals (for dedup guard) ===')
pending = db.select('signals', filters={'approval': 'pending'}, limit=200)
print(f'Total pending: {len(pending)}')
for p in pending:
    print(f"  {p.get('asset')} - {p.get('created_at')}")
