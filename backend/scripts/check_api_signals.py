import urllib.request
import json

url = 'https://tdapp-api.onrender.com/api/signals/latest'
resp = urllib.request.urlopen(url)
data = json.loads(resp.read())

print(f'Total signals: {len(data)}')
print()

for i, s in enumerate(data[:10], 1):
    asset = s.get('asset')
    direction = s.get('direction')
    conf = s.get('confidence_pct')
    approval = s.get('approval')
    created = str(s.get('created_at'))[:19]
    print(f'{i:2d}. {created} - {asset:8s} {direction:4s} conf={conf}% approval={approval}')
