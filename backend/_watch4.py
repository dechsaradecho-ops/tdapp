import httpx, time, json
c = httpx.Client(base_url='https://tdapp-api.onrender.com', timeout=180)
tok = c.post('/api/auth/login', json={'pin': '777777'}).json().get('token')
h = {'Authorization': f'Bearer {tok}'}
RUN = 'c18d5af8c65544ffa3b8a063eccdc8f7'
t0 = time.time()
while True:
    time.sleep(10)
    st = c.get(f'/api/system/simulate/{RUN}', headers=h).json()
    print('[%.0fs] %s processed=%s/%s status=%s' % (
        time.time() - t0, st.get('stage'), st.get('processed'),
        st.get('total_events') or st.get('target_events'), st.get('status')),
        flush=True)
    if st.get('status') in ('done', 'failed', 'cancelled'):
        break
    if time.time() - t0 > 1800:
        print('timeout')
        break

res = st.get('result') or {}
print('\n=== OPTIMIZER ===')
print(json.dumps(res.get('optimizer'), ensure_ascii=False, indent=1))
print('\n=== ROUNDS ===')
for r in res.get('rounds') or []:
    w = r.get('winner') or {}
    print('  round %s cells=%s winner=%sxATR/%sR test=%s holds=%s live=%s' % (
        r.get('round'), r.get('grid_cells'), w.get('sl_mult'), w.get('tp_r'),
        r.get('test_mean_r'), r.get('holds_out'), r.get('live_suggested')))
print('\n=== LIVE SUGGESTION ===')
print(json.dumps((res.get('recommendation') or {}).get('live_suggestion'),
                 ensure_ascii=False, indent=1))
