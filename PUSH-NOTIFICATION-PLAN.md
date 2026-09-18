# แผน: Web Push (VAPID) — ยิง notification จากหน้าเว็บเข้าสมาร์ทโฟน

> สถานะ: **รอ confirm** — ยังไม่มีการแก้โค้ดใด ๆ
> เป้าหมาย: ให้แจ้งเตือนเดิม (trade_opened / stop_loss / risk_warning / digest …)
> เด้งเข้าจอมือถือได้ **แม้ปิดแอป/ปิดเบราว์เซอร์** โดยใช้ Web Push มาตรฐาน
> **คง LINE เดิมไว้** เป็นช่องทางคู่กัน (ไม่ตัดทิ้ง)

---

## 1. สถานะปัจจุบัน (ตรวจโค้ดแล้ว)

| ส่วน | สถานะ |
|---|---|
| `frontend/public/manifest.webmanifest` | ✅ มีแล้ว (standalone, icons 192/512) |
| `frontend/public/sw.js` | ⚠️ มีแค่ `install` / `activate` / `fetch` — **ไม่มี `push` + `notificationclick`** |
| `frontend/src/components/PwaRegister.tsx` | ✅ register SW (prod only) |
| การขอ permission | ❌ ยังไม่มีที่ไหนเรียก `Notification.requestPermission()` |
| ช่องทางส่งจริง | LINE เท่านั้น (`NotificationService.push_line` → `line_users` + `line_targets`) |
| ตารางเก็บ subscription | ❌ ยังไม่มี |
| ไลบรารีฝั่งส่ง | ❌ `requirements.txt` ยังไม่มี `pywebpush` |

**สรุป: ยังยิงไม่ด้** ต้องเพิ่ม 3 ชิ้น — SW push handler, ตาราง+API เก็บ subscription, ตัวส่งฝั่ง backend

---

## 2. รองรับแพลตฟอร์มไหน + ข้อจำกัด

### 2.1 ตารางรองรับ (ตรวจ 2026-09-18)

| แพลตฟอร์ม / เบราว์เซอร์ | รับ push | ต้องติดตั้ง PWA ก่อน? | หมายเหตุ |
|---|---|---|---|
| **Android — Chrome / Edge / Samsung Internet / Firefox** | ✅ | **ไม่ต้อง** — เปิดในแท็บปกติก็รับได้ | ง่ายที่สุด, Chrome Android รองรับ Web Push ตั้งแต่ปี 2015 |
| Android — ติดตั้งเป็น PWA (เพิ่มไปยังหน้าจอโฮม) | ✅ | — | เหมือนกัน แต่ได้ไอคอน/ชื่อแอปบน notification แทนชื่อโดเมน |
| **Android — in-app browser** (เปิดลิงก์จาก Facebook / TikTok / IG / LINE) | ❌ | — | in-app webview ไม่มี Push API — ต้องเปิดด้วย Chrome จริง |
| iOS / iPadOS **16.4+** | ✅ | **ต้อง** เพิ่มไปยังหน้าจอโฮมก่อน | เปิดใน Safari เป็นแท็บ **ไม่ได้รับเลย** |
| iOS < 16.4 | ❌ | — | ไม่มีทาง |
| Desktop Chrome / Edge / Firefox | ✅ | ไม่ต้อง | ใช้ทดสอบได้สะดวกสุด |
| จีนแผ่นดินใหญ่ (ไม่มีการเข้าถึง FCM) | ⚠️ | — | Chrome push มักไม่ถึง |

**สรุป: Android ไม่ต้องติดตั้งอะไรเลย** — ต่างจาก iOS ที่บังคับ install

### 2.2 ข้อจำกัดที่ต้องยอมรับก่อนทำ

1. **ต้อง HTTPS** — prod (`tdappstatic.onrender.com`) ผ่านอยู่แล้ว, `localhost` นับเป็น secure context
2. **Permission ต้องมาจากการกดปุ่มของผู้ใช้** — ขออัตโนมัติตอนโหลดจะโดนเบราว์เซอร์บล็อกถาวร
3. **iOS/iPadOS 16.4+ เท่านั้น และต้อง "เพิ่มไปยังหน้าจอโฮม" ก่อน** — เปิดใน Safari เป็นแท็บจะไม่ได้รับ push เลย (ข้อจำกัด Apple ไม่ใช่งานที่แก้ได้ด้วยโค้ด) → UI ต้องมีคำแนะนำนี้
4. **Render free tier**: ตัวส่ง push คือ API service → ถ้า service หลับตาม keepalive เดิม push ก็จะไม่ออก (มี `/ping` cron อยู่แล้ว)
5. **`pywebpush` เป็น blocking sync** → ต้องห่อ `asyncio.to_thread` ไม่ให้บล็อก event loop ของ FastAPI
6. **endpoint ของ subscription = ข้อมูลอ่อนไหว** (ใครได้ไป = ส่ง push หาคนนั้นได้) → ห้ามอยู่ใน response list / log
7. **VAPID private key = ความลับ** → env var (ตรงกับนโยบายเดิมของโปรเจกต์) ห้ามลง `NEXT_PUBLIC_*` และห้าม commit

### 2.3 ข้อควรรู้เฉพาะ Android

1. **ห้าม silent push** — Chrome บังคับ `userVisibleOnly: true` ทุก push ต้องเรียก `showNotification()` ถ้าไม่เรียก จะได้ notification สำรองของ Chrome ว่า "This site has been updated in the background" → `sw.js` ต้องมี fallback นี้เสมอ (ดู §4.5)
2. **ต้องใส่ `tag`** — ถ้าไม่ใส่ notification จะกองซ้อนกันไม่จำกัด (เปิดไม้ 3 ครั้ง = 3 อัน) ใช้ `tag` ต่อประเภท (เช่น `trade_opened`) ให้อันใหม่แทนที่อันเก่า + `renotify: true` ถ้าต้องการให้สั่นซ้ำ
3. **ชื่อบน notification** — ถ้าไม่ติดตั้ง PWA จะขึ้นเป็นชื่อโดเมน (`tdappstatic.onrender.com`) ไม่ใช่ "AI Trading" → ต้องมี `icon` + `badge` ใน `showNotification` เพื่อให้ดูรู้เรื่อง
4. **Battery optimization ของ OEM** (Xiaomi / Huawei / Oppo / Samsung เข้ม) อาจหน่วงหรือตัด push ถ้าผู้ใช้ "Force stop" Chrome → push หยุดจนกว่าจะเปิด Chrome ใหม่ (แก้ฝั่งโค้ดไม่ได้ ต้องบอกผู้ใช้)
5. **FCM endpoint หมดอายุได้** → ต้องมี `pushsubscriptionchange` + re-subscribe ตอนโหลดหน้า (อยู่ในแผน §4.5 แล้ว) ไม่งั้นเครื่องจะเงียบถาวรโดยไม่มี error ที่ไหน
6. **ต้องมี data/Wi-Fi** — เครื่องปิดเน็ต/โหมดเครื่องบิน push จะค้างอยู่ที่ FCM แล้วมาทีหลังตอนต่อเน็ตได้ (ไม่หายไป)
7. **ทดสอบเร็วโดยไม่ต้องมี backend** — Chrome (desktop หรือ Android ผ่าน remote debug) → DevTools → Application → Service Workers → ช่อง **Push** → กดยิง payload ปลอมได้ทันที เหมาะกับการเช็ค `sw.js` ว่าถูกก่อนต่อ VAPID
8. **Android 13+** permission ยังเป็นของ Chrome (ไม่ใช่ของแอป) — ถ้าผู้ใช้ปิด notification ของ **Chrome ทั้งตัว** ใน Settings เครื่อง push จะไม่ขึ้น แม้ permission เว็บยังเป็น granted → `Notification.permission` ยังอ่านได้แค่ระดับเว็บ ตรวจไม่ได้ ต้องมีปุ่ม "ส่งทดสอบ" ให้ผู้ใช้เช็คเอง

---

## 3. สถาปัตยกรรมที่จะทำ

```
worker/route  →  NotificationService.notify(user_id, ntype, message)
                     │
                     ├── (เดิม) line.push()        → line_users + line_targets
                     └── (ใหม่) web_push.push_all() → push_subscriptions
                                                        │  pywebpush + VAPID
                                                        ▼
                                              FCM / Mozilla / APNs (OS push)
                                                        ▼
                                              sw.js  "push" event → showNotification()
```

* ตัวกรองเดิมยังใช้ร่วมกันทั้งหมด — **category toggle** (`notify_trade_opened` ฯลฯ) และ **risk_warning cooldown 30 นาที** ทำงานที่ `notify()` จุดเดียว จึงคุมทั้ง LINE และ Web Push พร้อมกันโดยไม่ต้องเขียนใหม่
* Push เป็น **transport เพิ่ม** ไม่ใช่ระบบใหม่ → ไม่แตะ gate pipeline, auto-trader, scanner

---

## 4. รายการไฟล์ที่จะแตะ

### 4.1 Migration ใหม่ (user ต้องรันใน Supabase SQL Editor)
`database/042_push_subscriptions.sql` (+1 ไฟล์ใหม่)

```sql
create table if not exists push_subscriptions (
  id           bigint generated by default as identity primary key,
  endpoint     text not null unique,
  p256dh       text not null,
  auth         text not null,
  user_agent   text,
  user_id      text,                      -- pseudo-user "demo" → text ไม่ใช่ uuid
                                          -- (บทเรียนจาก 022: uuid FK ทำให้ insert 400 เงียบ)
  enabled      boolean not null default true,
  fail_count   integer not null default 0,
  last_error   text,
  last_ok_at   timestamptz,
  created_at   timestamptz not null default now()
);
alter table push_subscriptions enable row level security;
-- service_role เท่านั้น — ห้าม public select (endpoint เป็นความลับ)
create policy push_subscriptions_service_all on push_subscriptions
  for all to service_role using (true) with check (true);
```

### 4.2 Backend — ไฟล์ใหม่
| ไฟล์ | หน้าที่ |
|---|---|
| `backend/app/integrations/web_push.py` | `enabled()`, `vapid_public_key()`, `push_all(db, title, body, url, tag)` — อ่าน `push_subscriptions` → `await asyncio.to_thread(webpush, ...)`; 404/410 → `enabled=false` (ตายถาวร); error อื่น → `fail_count+1` + `last_error`; ไม่มีคีย์ → no-op เงียบ ๆ (แอปต้องไม่พัง) |
| `backend/app/api/routes/push.py` | mount ที่ `/api/push` (ติด PIN gate อัตโนมัติ เพราะอยู่ใต้ `/api/`) |
| `backend/scripts/gen_vapid_keys.py` | สร้าง VAPID key pair ครั้งเดียว (ใช้ `py-vapid`) |
| `backend/scripts/probe_web_push.py` | ยิงทดสอบจริงจากเครื่องไปยัง subscription ที่ลงทะเบียนไว้ |

Endpoint ใน `push.py`:
| Method | Path | หมายเหตุ |
|---|---|---|
| GET | `/api/push/key` | `{enabled, public_key}` — public key ปลอดภัยที่จะส่งให้ client (private ไม่ออก) |
| POST | `/api/push/subscribe` | `{endpoint, keys:{p256dh, auth}, user_agent}` → upsert by `endpoint` (row เดิม = re-enable + reset fail_count) |
| POST | `/api/push/unsubscribe` | `{endpoint}` → `enabled=false` |
| GET | `/api/push/subscriptions` | สรุปให้ Settings แสดงจำนวน/เครื่องที่ผูก (นับ + user_agent + last_ok_at เท่านั้น) |
| POST | `/api/push/test` | ยิงข้อความทดสอบ |

### 4.3 Backend — ไฟล์ที่แก้
| ไฟล์ | แก้อะไร |
|---|---|
| `requirements.txt` | + `pywebpush>=2.0.0` (ดึง `py-vapid`, `cryptography`, `http-ece` มาด้วย) |
| `app/core/config.py` | + `vapid_public_key`, `vapid_private_key`, `vapid_subject` (env; default "" = ปิดฟีเจอร์) |
| `app/main.py` | `include_router(push.router, prefix="/api/push")` |
| `app/services/notification_service.py` | `notify()`: หลังผ่าน category + cooldown → ยิง LINE และ Web Push **แยกกัน**; critical → `status="sent"` ถ้าอย่างใดอย่างหนึ่งสำเร็จ; เพิ่ม `async def push_web(...)` (ชื่ออิง `push_line` ให้เทสต์เดิมไม่พัง) |
| `app/workers/notification_worker.py` | `dispatch_pending()`: หลัง `push_line` สำเร็จ → เรียก `push_web` ต่อ (ประเภท non-critical อย่าง `drawdown_warning`/digest จะได้เด้งมือถือด้วย) |

### 4.4 Frontend — ไฟล์ใหม่
| ไฟล์ | หน้าที่ |
|---|---|
| `src/lib/push.ts` | `pushSupported()`, `permissionState()`, `subscribePush()`, `unsubscribePush()`, `isIosNeedsInstall()` (ตรวจ iOS + `navigator.standalone`) |
| `src/components/PushNotificationCard.tsx` | การ์ดในหน้า ตั้งค่า: สถานะ (รองรับ/ไม่รองรับ · ได้ permission/ถูกบล็อก · ผูกแล้ว N เครื่อง) + ปุ่ม เปิดใช้ / ปิด / ส่งทดสอบ + คำแนะนำ iOS + ข้อความสาเหตุที่กดไม่ได้ |

### 4.5 Frontend — ไฟล์ที่แก้
| ไฟล์ | แก้อะไร |
|---|---|
| `public/sw.js` | + `push` handler (`showNotification` พร้อม `icon`/`badge`/`tag`/`renotify`/`data.url` + **fallback notification เสมอเมื่อไม่มี payload** — Android บังคับ ดู §2.3) · + `notificationclick` (focus หน้าที่เปิดอยู่ หรือเปิดใหม่) · + `pushsubscriptionchange` (พยายาม subscribe ใหม่) · **bump `CACHE_VERSION` v1 → v2** เพื่อบังคับ SW อัปเดต |
| `src/lib/api.ts` | + `pushKey()`, `pushSubscribe()`, `pushUnsubscribe()`, `pushSubscriptions()`, `pushTest()` |
| `src/lib/types.ts` | + `PushKeyInfo`, `PushSubscriptionInfo`, `PushTestResult` |
| `src/app/settings/page.tsx` | วาง `PushNotificationCard` ในหมวดการแจ้งเตือน (ใช้ `CollapsePanel` เดิม) — ไม่ต้องมี toggle ใหม่ ซ้ำกับ 6 category switch ที่มีอยู่ |

> หมายเหตุ: `output: "export"` + `sw.js` เป็นไฟล์ static → การเปลี่ยน SW มีดีเลย์ตาม cache ของ host, ต้อง bump `CACHE_VERSION` ทุกครั้งที่แก้

### 4.6 Tests
`backend/tests/test_web_push.py` (ใหม่) — โดยไม่ยิง network จริง (`monkeypatch` ตัวส่ง):
1. `subscribe` upsert — endpoint เดิมซ้ำ → update ไม่เพิ่ม row
2. 410/404 → `enabled=false` (ไม่ลบทิ้ง เผื่อ debug)
3. error ทั่วไป → `fail_count` +1 + `last_error` ถูกบันทึก
4. ไม่มี VAPID key → `push_all` คืน 0 และ **ไม่ raise**
5. category ปิด → `notify()` ไม่ยิงทั้ง LINE และ push
6. `notify()` critical → เรียก push
7. `dispatch_pending` ประเภท queued → ยิง push ด้วย (และไม่ DOUBLE-push กับ critical)
8. `GET /api/push/key` ไม่มีคีย์ → `enabled=false` (frontend แสดง "ยังไม่ตั้งค่า")

เกณฑ์ผ่าน: suite เดิม (621+) ต้องเขียวทั้งหมด

---

## 5. ขั้นตอน deploy (สิ่งที่ user ต้องทำเอง)

1. `py -m venv` เดิม → `pip install -r requirements.txt` (ได้ `pywebpush`)
2. รัน `python scripts/gen_vapid_keys.py` → ได้ public/private key
3. เพิ่ม env บน Render → **tdapp-api** → Environment
   - `VAPID_PUBLIC_KEY` (base64url)
   - `VAPID_PRIVATE_KEY` (ความลับ)
   - `VAPID_SUBJECT` = `mailto:you@example.com`
   - และใส่ `backend/.env` สำหรับรัน local
4. รัน `database/042_push_subscriptions.sql` ใน Supabase SQL Editor
5. Redeploy tdapp-api (public key ต้องพร้อมก่อน frontend กด subscribe)
6. มือถือ **Android**: เปิดด้วย **Chrome** (ห้ามเปิดผ่าน in-app browser ของ Facebook/LINE/IG) → ตั้งค่า → **เปิดการแจ้งเตือน** → กด **ส่งทดสอบ** (ไม่ต้องติดตั้ง PWA)
7. มือถือ **iPhone**: Safari → แชร์ → **เพิ่มไปยังหน้าจอโฮม** → เปิดจากไอคอนที่เพิ่งเพิ่ม → ตั้งค่า → เปิดการแจ้งเตือน
8. ก่อนมี VAPID key: ทดสอบ `sw.js` เปล่า ๆ ได้ด้วย Chrome DevTools → Application → Service Workers → ปุ่ม **Push** (§2.3 ข้อ 7)

---

## 6. จุดตัดสินใจที่ขอความเห็น

| # | เรื่อง | ตัวเลือก |
|---|---|---|
| A | `notifications.channel` (enum มี `line`/`email`/`in_app`) จะเพิ่มค่า `push` ไหม | **(แนะนำ) ไม่เพิ่ม** — push ใช้ transport เดียวกันแต่ไม่สร้าง row ซ้ำ; เก็บสถานะสุขภาพไว้ที่ `push_subscriptions.last_error`/`fail_count` แล้ว (เลี่ยงความเสี่ยง ALTER TYPE) · หรือเพิ่ม `alter type notification_channel add value if not exists 'push'` (มี precedent ที่ 022) |
| B | ข้อความบน push | ใช้ `message` เดิมทั้งก้อน (มี emoji/`\n` ได้ แต่ OS จะตัดยาว) หรือย่อหัวข้อสั้น (`trade_opened` → "เปิดไม้ EURUSD") + body 1 บรรทัด |
| C | ค่า default ของฟีเจอร์นี้ | **ปิดไว้ก่อน** (ยังไม่ตั้ง VAPID = เงียบ ไม่พัง) จนกว่าจะตั้ง env — ยืนยันว่าเป็นแบบนี้ |
| D | iPAD/iPhone ที่ยังไม่ install | แสดงเป็น "ยังไม่รองรับ" หรือ "ต้องเพิ่มไปยังหน้าจอโฮมก่อน" (แนะนำอย่างหลัง เพราะกดแก้ได้เอง) |

---

## 7. ประมาณการงาน + ความเสี่ยง

| งาน | จำนวน |
|---|---|
| ไฟล์ใหม่ | 7 (migration 1 · backend 4 · frontend 2) |
| ไฟล์แก้ | 8 (backend 5 · frontend 3) |
| จุดเสี่ยงสูง | SW cache ไม่ยอมอัปเดต · iOS เงียบโดยไม่ error · `pywebpush` block event loop · endpoint รั่ว |

**ความเสี่ยงที่ใหญ่ที่สุด: iOS** — ถ้าผู้ใช้ใช้ iPhone และไม่ยอม "เพิ่มไปยังหน้าจอโฮม" จะไม่มีทางได้ push เลยไม่ว่าจะเขียนโค้ดอย่างไร นี่คือเหตุผลที่ **ไม่ควรตัด LINE ทิ้ง**
