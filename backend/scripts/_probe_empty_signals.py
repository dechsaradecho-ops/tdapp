import json, urllib.request
BASE='https://tdapp-api.onrender.com'
def req(path, token=None, method='GET', body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE+path, data=data, method=method)
    if token: r.add_header('Authorization', f'Bearer {token}')
    if data: r.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(r, timeout=60) as resp:
        return json.loads(resp.read().decode())
tok = req('/api/auth/login', method='POST', body={'pin':'777777'})
token = tok.get('token') or tok.get('access_token')
print('login ok:', bool(token))
counts = req('/api/system/counts', token=token)
print('COUNTS:', json.dumps(counts, ensure_ascii=False))
st = req('/api/settings', token=token)
keys = ['allowed_assets','min_confidence','min_confidence_gold','gold_breakout_only','order_mode','max_open_positions','max_trades_daily','max_trades_weekly','capital','risk_per_trade_pct']
print('SETTINGS:', json.dumps({k: st.get(k) for k in keys}, ensure_ascii=False))
sigs = req('/api/signals/latest', token=token)
print('LATEST len:', len(sigs))
for s in sigs:
    print(' -', s.get('asset'), s.get('direction'), 'conf=', s.get('confidence'), 'approval=', s.get('approval'), 'created=', s.get('created_at'), 'entry=', s.get('entry'), 'blocked=', (s.get('order_blocked') or '')[:80])
mkt = req('/api/market/summary', token=token)
opps = mkt.get('opportunities') or []
print('MARKET opps:', len(opps))
for o in opps[:15]:
    print(' -', o.get('asset'), 'score=', o.get('score'))
logs = req('/api/system/signal-logs?limit=30', token=token)
rows = logs.get('logs') or []
print('SIGNAL-LOGS:', len(rows))
for x in rows[:15]:
    print(' -', x.get('created_at'), x.get('event'), x.get('asset'), (x.get('reason') or '')[:100])
