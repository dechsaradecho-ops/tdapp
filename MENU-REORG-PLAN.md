# 📋 แผนจัดกลุ่มเมนูใหม่

> วันที่: 2026-09-06 · สถานะ: **✅ นำไปใช้แล้ว 2 รอบ (commit 89f2a51 → 78ba310)** —
> **รอบ 1** (89f2a51): แบบ B ปรับปรุง — รวมหน้าเล็ก แต่ Quote Logs คงเป็นเมนูแยก → 6 เมนู
> **รอบ 2** (78ba310): monitor + performance **รวมเมนูเดียว แยกแท็บภายใน** + ย้ายการ์ด Risk Engine จากบนลง **ล่างสุด** → **5 เมนู**
>
> **โครงสร้างสุดท้าย (5 เมนู):** 🏠 หน้าหลัก · ⚡ สัญญาณ · 📊 มอนิเตอร์ (แท็บ: 📊 มอนิเตอร์ | 🎯 Performance) · 📜 Logs · ⚙️ ตั้งค่า
>
> **สิ่งที่ทำจริง:**
> - หน้าหลัก: + Market Regime Analysis + ตัวเลือกสัญลักษณ์ (XAUUSD/EURUSD/USDJPY/GBPUSD/AUDUSD) + TradingView chart — คง Opportunity Score ไว้ต่อท้าย
> - มอนิเตอร์: แท็บบนหน้า `📊 มอนิเตอร์ | 🎯 Performance` (state แท็บอ่าน `?tab=performance` จาก `window.location.search` ใน useEffect) · เนื้อหา performance ทั้งหมดย้ายเป็น `components/PerformancePanel.tsx` · การ์ด Risk Engine Status ย้ายไป**ล่างสุด**ของแท็บมอนิเตอร์ (หลัง ประวัติ order) ตาม request — guard `capital > 0` กัน 422 ยังอยู่
> - สัญญาณ: แท็บ `⚡ สัญญาณ | 🗂️ บันทึกสัญญาณ` — state แท็บ sync กับ query param `?tab=logs` · ตัว panel แยกเป็น `components/SignalLogsPanel.tsx`
> - route เก่า `/market` `/risk` `/signal-logs` `/performance` = client redirect stubs (`window.location.replace` ชี้ `*.html` เพื่อรองรับ static hosting ทั้ง Render และ preview server) + fallback panel บอกผู้ใช้
> - `MobileNav.tsx` + `layout.tsx`: nav เหลือ **5** เมนู

---

## 1. ปัญหาของโครงสร้างปัจจุบัน (9 เมนูแบน)

| หน้า | ปริมาณเนื้อหา | หน้าที่ | ปัญหา |
|---|---|---|---|
| 🏠 หน้าหลัก `/` | กลาง | กราฟ XAUUSD + Opportunity Score | — |
| 📈 ตลาด `/market` | **น้อย** (2 การ์ด) | Regime Analysis + Opportunity Score | **Opportunity Score ซ้ำกับหน้าหลักเป๊ะ** |
| ⚡ สัญญาณ `/signals` | ใหญ่ | รับ/อนุมัติสัญญาณ | — |
| 🗂️ Signal Logs `/signal-logs` | กลาง | ชีวิตสัญญาณย้อนหลัง | โยงตรงกับ /signals แต่แยกเมนู |
| 📊 มอนิเตอร์ `/monitor` | ใหญ่ | สถิติ + ไม้ค้าง + ประวัติ order | — |
| 🛡️ Risk `/risk` | **น้อยมาก** (การ์ดเดียว) | สถานะ Risk Engine | หน้าเดียวการ์ดเดียว คุ้มเมนู? |
| 🎯 Performance `/performance` | ใหญ่มาก | Equity/Journal/Backtest/Kill Switch | — |
| 📜 Logs `/logs` | กลาง | Quote API Logs (feed health) | เรื่อง "ระบบ" ไม่ใช่ "การเทรด" |
| ⚙️ Settings `/settings` | ใหญ่ | ตั้งค่าทุกอย่าง | — |

- Mobile dock ต้อง**ปัดซ้ายขวา**หาแท็บ (9 ปุ่ม × min-w 64px เกินความกว้างจอ)
- Workflow จริงคือ *ดูตลาด → เทรด → ติดตาม → ตรวจย้อนหลัง → ตั้งค่า* แต่เมนูเรียงไม่ตามนี้

---

## 2. แบบที่แนะนำ — แบบ B: รวมหน้าเล็กเข้าหน้าใหญ่ (เหลือ 5 เมนู)

| เมนูใหม่ | รวมจาก | วิธีรวม |
|---|---|---|
| 🏠 **หน้าหลัก** | หน้าหลัก + ตลาด | การ์ด Regime Analysis วางใต้กราฟ TradingView — ลบ Opportunity Score ที่ซ้ำ |
| ⚡ **สัญญาณ** | สัญญาณ + Signal Logs | แท็บย่อยบนหน้า: `สัญญาณ (สด) \| บันทึกสัญญาณ` |
| 📊 **มอนิเตอร์** | มอนิเตอร์ + Risk | การ์ด Risk Engine Status วาง**บนสุด**ของหน้า (สัมพันธ์กับ pause/ไม้ค้าง) |
| 🎯 **Performance** | (คงเดิม) | ใหญ่อยู่แล้ว |
| ⚙️ **ตั้งค่า** | Settings + Quote Logs | แท็บย่อย: `ตั้งค่า \| Feed Health (Quote Logs)` |

**ผลลัพธ์:** dock มือถือเหลือ 5 ปุ่ม = **ไม่ต้องเลื่อนแถว** · desktop nav สั้นลงครึ่งหนึ่ง · ทุกเมนู = หนึ่งภารกิจ

### สิ่งที่ต้องแก้เมื่อลงมือ (checklist)
1. `MobileNav.tsx` — MENU เหลือ 5 แถว (dock ไม่ต้อง scroll แล้ว แต่คงโครงสไลด์ไว้ได้)
2. `layout.tsx` — header nav (desktop) เหลือ 5 ลิงก์
3. รวมหน้า:
   - `market/page.tsx` → ย้ายการ์ด Regime ไป `page.tsx` (หน้าหลัก) แล้ว**ลบ route /market**
   - `risk/page.tsx` → ย้าย RiskPanel ไปบนสุดของ `monitor/page.tsx` แล้ว**ลบ route /risk** (จบปัญหา 422 ด้วย — guard เดิมย้ายไปด้วย)
   - `signal-logs/page.tsx` → กลายเป็นแท็บใน `signals/page.tsx` (state แท็บเก็บ query param `?tab=logs` ให้ deep link ยังใช้ได้)
   - `logs/page.tsx` → กลายเป็นแท็บใน `settings/page.tsx`
4. Redirect เดิม: static export ไม่มี server redirect — เก็บหน้าเก่าเป็น "หน้าเปลี่ยนเส้นทาง" (client redirect ไปหน้าใหม่) 1 release กัน bookmark เก่าพัง หรือตัดทิ้งเลยถ้า user ไม่มี bookmark
5. อัปเดตเอกสาร: `UI-DESIGN-SYSTEM.md` §7 (dock), `README.md` (รายชื่อหน้า)
6. ตรวจ `sw.js` (PWA) — network-first HTML อยู่แล้ว ไม่ต้องแก้ แต่เช็ค cache ของ chunk เก่า

### ความเสี่ยง/ข้อควรระวัง
- ลิงก์จาก LINE notification ถ้ามีชี้ `/risk` หรือ `/signal-logs` ต้องตามแก้ (ต้องค้น backend notification_service ก่อนลงมือ)
- หน้า monitor จะยาวขึ้น (Risk การ์ดบน + สถิติ + ตาราง) — จัดลำดับให้ Risk อยู่เหนือสถิติ
- แท็บย่อยต้องเป็น client state ไม่ใช่ route ใหม่ (static export) — ใช้ query param

---

## 3. ทางเลือกอื่น

### แบบ C — เหลือ 6 เมนู (ประนีประนี)
รวมเฉพาะที่ซ้ำ/บางจริง ๆ: ตลาด→หน้าหลัก, Risk→มอนิเตอร์, **Logs รวมเป็นหน้าเดียว 2 แท็บ** (Signal Logs | Quote Logs) แยกจากสัญญาณ
เหลือ: หน้าหลัก · สัญญาณ · มอนิเตอร์ · Performance · Logs · ตั้งค่า — dock 6 ปุ่มพอดีจอโดยลด min-w แท็บนิดเดียว
เลือกเมื่อ: ไม่อยากให้ Settings บวมขึ้นด้วย Quote Logs

### แบบ A — กลุ่ม 2 ระดับ (ไม่รวมหน้า)
เมนูหลัก 4 กลุ่ม: 📈 ตลาด (หน้าหลัก, ตลาด) · ⚡ เทรด (สัญญาณ, Signal Logs) · 📊 ผลลัพธ์ (มอนิเตอร์, Performance) · ⚙️ ระบบ (Risk, Logs, Settings)
แรงงานน้อยสุด แต่ต้องกด 2 ชั้นถึงเนื้อหา และหน้าเล็กก็ยังเล็ก — **ไม่แนะนำ** เว้นแต่จะเพิ่มหน้าใหม่อีกเร็ว ๆ นี้

---

## 4. สรุปคำแนะนำ

**แบบ B ปรับปรุง (ที่นำไปใช้จริง)** — รวม 3 หน้าบางเข้าหน้าใหญ่ตามแบบ B แต่ Quote Logs คงเป็นเมนูแยกก่อนตั้งค่า (ตามแนวคิดแบบ C เรื่อง "ไม่อยากให้ Settings บวม") — จบด้วย 6 เมนู · dock 6 ปุ่มพอดีจอ

**รอบ 2 (2026-09-06, commit 78ba310)** — ผู้ใช้ขอรวมต่อ: monitor + performance **เมนูเดียวแยกแท็บภายใน** (`📊 มอนิเตอร์ | 🎯 Performance`) และย้ายการ์ด Risk Engine จากบนสุดลง **ล่างสุด** (หลังประวัติ order) — จบด้วย **5 เมนู** · dock 5 ปุ่ม:
- `PerformancePanel.tsx` = สกัดเนื้อหา performance ทั้งหมดเป็น component (state + loadAll 10 API + Backtest Center + Equity Curve SVG)
- `monitor/page.tsx` = แท็บ 2 ปุ่มด้านบน + `{tab === "performance" && <PerformancePanel />}` + เนื้อหาเดิมครอบ `{tab === "monitor" && (<>...</>)}`
- `/performance` = redirect stub ชี้ `/monitor.html?tab=performance`
- ตรวจ prod แล้ว: monitor chunk มี "Performance Dashboard" + "Risk Engine Status" + logic `?tab=performance` · performance chunk (876B) มี redirect string · nav ทุกหน้าไม่มีลิงก์ `/performance`
