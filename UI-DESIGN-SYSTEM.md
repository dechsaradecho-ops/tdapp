# 🧊 tdapp UI Design System — iOS Liquid Glass Dark

> เอกสารนี้สรุป UI ทั้งหมดของ tdapp เพื่อให้เว็บอื่นอ่านแล้วสร้าง UI เดียวกันได้
> ต้นทางจริง: `frontend/src/app/globals.css`, `frontend/tailwind.config.ts`,
> `frontend/src/app/layout.tsx`, `frontend/src/components/MobileNav.tsx`, `frontend/src/components/BackgroundLayer.tsx`
> สถานะล่าสุด: commit `2d76bca`, verified 2026-09-07 — pill เข้ม /30 + ตัวเลขหนาทั้งหน้า signals/monitor (§10.1–10.2) · ก่อนหน้า: `67a2dfd` (§10.1 pill แบบ component บนหน้า signals — SL/TP, ▲▼, BUY/SELL, PnL + tier badge เลข 1/2/3) · `a13eb36` verified 2026-09-06 (§15 ระบบไอคอน SVG monotone + §16 สวิตช์แจ้งเตือนต่อหมวด — 6 หมวด, migration 020 รันแล้ว — เดิมชื่อ 012 ถูก rename เพราะชนเบอร์กับ 012_min_confidence_gold)
>
> ⚠️ **ห้ามเทียบ prod ด้วย hash ของ index.html หรือชื่อไฟล์ chunk** — Next.js สร้าง buildId/chunk-hash
> ใหม่ทุกครั้งที่ build และ build บน Render ให้ hash ต่างจาก local เสมอ (แม้โค้ดเดียวกัน — ยืนยันแล้ว
> 2026-09-06: prod `page-2c076a551f57135c` vs local `page-061af32ba75f47ea`) — วิธีตรวจ deploy ถึงจริง:
> ดึง **JS chunk ของหน้านั้นจาก prod** (`/_next/static/chunks/app/<page>/page-*.js` ตามที่ HTML อ้าง)
> แล้วสแกนหา **ASCII marker ที่รอดจาก minification** (เช่น `aria-checked`, `notify_trade_opened`,
> `left-[21px]`, `tdapp_chat_history`) — 🚨 ข้อความไทยใน chunk โดน \u-escape หา string ตรงตัวไม่เจอ
> และ prod /settings มี PIN lock หน้าจอ (ยิง API ตรงได้ 401 = ปกติ) → verify ด้วย JS chunk แทนการเข้าหน้า

---

## 1. ปรัชญา (Design Philosophy)

- **iOS Liquid Glass บนพื้นดำ**: พื้นหลังดำสนิท + aurora glow จาง ๆ, การ์ด/ปุ่ม/ฟอร์มทุกชิ้นเป็น "แก้วฝ้าโปร่งแสง" (blur เบื้องหลังผ่านได้)
- **แก้วใส ไม่ใช่แก้วขุ่น**: blur ต่ำ (5–10px) เพื่อให้เห็นพื้นหลังชัด — ไม่ใช้พื้นทึบเข้ม
- **สื่อสารด้วยสี ไม่ใช้ emoji ในปุ่ม**: ฟ้า=ทำ, แดง=อันตราย, เขียว=ยอมรับ, ขาวใส=ทั่วไป
- **ไอคอน SVG monotone ทั้งแอป (fc753ce)**: ห้ามใช้ emoji ใน UI — ใช้ `Icon.tsx` (36 ไอคอน stroke currentColor) ทุกจุด; glyph ขาวธรรมดา ✓ ✗ ✕ • ยังใช้ได้ในข้อความ
- **มือถือเป็นหลัก**: ปุ่มสูง ≥44px, กัน iOS zoom, safe-area notch, ตารางเลื่อนแนวนอน
- **เคารพ OS**: `prefers-reduced-motion`, `color-scheme: dark`, native popup มืด

---

## 2. Color Palette (Tailwind config)

```ts
// tailwind.config.ts
colors: {
  surface: "#000000",              // พื้นหลังดำสนิท
  panel:   "#0a0a0c",              // สำรอง (แทบไม่ได้ใช้เพราะ .panel โปร่ง)
  glass:   "rgba(255,255,255,0.10)",
  accent:  "#0a84ff",              // iOS blue — ปุ่มหลัก/ลิงก์ active
  profit:  "#30d158",              // เขียว iOS — กำไร/สำเร็จ
  loss:    "#ff453a",              // แดง iOS — ขาดทุน/อันตราย
}
```

**Slate overrides (สำคัญมาก — slate มาตรฐานจางเกินบนพื้นดำ):**

```css
.text-slate-500 { color: #cbd5e1; }  /* ยกเป็นโทน slate-300 */
.text-slate-400 { color: #b6c2d3; }  /* สว่างขึ้นอีกขั้น */
/* หมายเหตุ: override ครอบเฉพาะ class สแตนด์อโลน — @apply ใน .panel-title ไม่โดน */
```

**สีข้อความอื่น:** body `text-slate-200`, input `#e2e8f0`, placeholder `#64748b`, option popup `#15151c`/`#e2e8f0`

---

## 3. พื้นหลังร่างกาย (Body Aurora)

```css
:root { color-scheme: dark; }

body {
  @apply bg-surface text-slate-200;
  -webkit-text-size-adjust: 100%;      /* กัน iOS zoom */
  overscroll-behavior-y: none;         /* กันแถบขาวตอน pull-to-refresh */
  background-image:
    radial-gradient(60rem 40rem at 85% -10%, rgba(10, 132, 255, 0.16), transparent 60%),
    radial-gradient(50rem 36rem at -15% 30%, rgba(94, 92, 230, 0.13), transparent 60%),
    radial-gradient(55rem 40rem at 50% 115%, rgba(48, 209, 88, 0.07), transparent 60%);
  background-attachment: fixed;        /* aurora ไม่ขยับตาม scroll */
}
```

**ภาพพื้นหลังผู้ใ้ตอง (BackgroundLayer):** client component วาด 2 div fixed — รูป z-[-2] + scrim ดำ rgba(0,0,0,0.55) z-[-1] เหนือรูป (ใต้ aurora) · เก็บใน localStorage key `tdapp_bg_image` (data URL, resize canvas ยาวสุด 1600px, JPEG q0.72, cap ~2.8MB) · อัปเดตทันทีด้วย event `tdapp:bg-changed`

⚠️ **ห้ามเบลอรูปพื้นหลังทั้งใบ** — เดิมเคยใส่ `filter: blur(14px)` บนตัวรูปเพื่อชดเชย backdrop-filter ที่ดับบน Android แต่ user ต้องการเห็นรูปชัด → ลดเหลือ `blur(2px)` + `scale(1.03)` (กันขอบรูปขาวเพราะเบลอ) เท่านั้น ความฝ้าของแก้วให้มาจาก backdrop-filter ของ `.panel`/dock เอง + scrim ดำ

---

## 4. การ์ดแก้ว — `.panel` (สูตรหลักของทั้งเว็บ)

```css
.panel {
  background: linear-gradient(160deg,
    rgba(255, 255, 255, 0.085),
    rgba(255, 255, 255, 0.03) 45%,
    rgba(255, 255, 255, 0.055));
  -webkit-backdrop-filter: blur(10px) saturate(150%);
  backdrop-filter: blur(10px) saturate(150%);
  border: 1px solid rgba(255, 255, 255, 0.12);
  border-top-color: rgba(255, 255, 255, 0.22);   /* ขอบบนสว่าง = แสงสะท้อน */
  border-radius: 1rem;
  box-shadow:
    0 8px 32px rgba(0, 0, 0, 0.45),
    inset 0 1px 0 rgba(255, 255, 255, 0.12);
  padding: 1rem;
  min-width: 0;   /* กัน grid blowout จากเนื้อหายาว */
}

/* lens sheen — แสงวาบบนขอบบนแก้ว */
.panel:not(.fixed):not(.sticky):not(.absolute) { position: relative; }
.panel::after {
  content: ""; position: absolute; inset: 0; border-radius: inherit;
  pointer-events: none;
  background: linear-gradient(180deg, rgba(255,255,255,0.14), rgba(255,255,255,0.02) 28%, transparent 55%);
  mix-blend-mode: screen; opacity: 0.35;
}

.panel-title {
  @apply text-sm font-semibold text-slate-400 uppercase tracking-wide mb-3;
}
```

---

## 5. Liquid Glass Refraction (SVG feDisplacementMap) — ⛔ DISABLED

ทำให้เนื้อหาหลังแก้ว "หักเห" แบบเลนส์ (ไม่ใช่แค่เบลอ) — เคยใช้เฉพาะ header + tab bar

**⛔ ปิดใช้งานถาวร (commit 9c355bd):** Samsung Internet **parse `@supports (backdrop-filter: url(...))` ผ่าน แต่ render พัง** → dock เมนูล่างบน Samsung วาดเป็น "กรอบซ้อน" เพี้ยน ๆ (อาการเดียวกับที่ Samsung ไม่ render backdrop-filter บน pseudo-element)

บทเรียน: **`@supports` ผ่าน ≠ render ถูก** บน Samsung Internet — ฟีเจอร์ CSS ทดลองบน pseudo-element/backdrop ต้องทดสอบเครื่องจริงก่อนขึ้น prod เสมอ

สถานะโค้ดปัจจุบัน: `.lg-refract` เหลือแค่ `isolation: isolate` + `::after` specular gradient (ทำงานปกติทุก browser) — `::before` โปร่งใส ไม่มี filter; rule `@supports (backdrop-filter: url(...))` ถูก comment out ไว้ใน `globals.css` (ห้ามเปิดกลับจนกว่าจะทดสอบ Samsung เครื่องจริง)

เอกสารอ้างอิงเดิม (เผื่อนำกลับมา desktop-only):

**SVG defs (ใส่ใน body ทุกหน้า, ซ่อนด้วยขนาด 0 — ห้าม display:none):**

```html
<svg aria-hidden="true" focusable="false" width="0" height="0" style={{ position: "absolute" }}>
  <defs>
    <filter id="lg-refract" x="-20%" y="-20%" width="140%" height="140%" colorInterpolationFilters="sRGB">
      <feTurbulence type="fractalNoise" baseFrequency="0.008 0.012" numOctaves="2" seed="11" result="noise" />
      <feGaussianBlur in="noise" stdDeviation="1.5" result="soft" />
      <feDisplacementMap in="SourceGraphic" in2="soft" scale="20" xChannelSelector="R" yChannelSelector="G" />
    </filter>
  </defs>
</svg>
```

**CSS:**

```css
.lg-refract { isolation: isolate; }
.lg-refract::before { content: ""; position: absolute; inset: 0; z-index: -1; pointer-events: none; }
/* ⛔ DISABLED — Samsung Internet parse @supports ผ่านแต่ render พัง (กรอบซ้อนบน dock)
@supports (backdrop-filter: url("#lg-refract")) {
  .lg-refract::before {
    -webkit-backdrop-filter: url("#lg-refract");
    backdrop-filter: url("#lg-refract");
  }
}
*/
/* ขอบ specular ด้านบน — ทำงานทุก browser */
.lg-refract::after {
  content: ""; position: absolute; inset: 0; z-index: 1; pointer-events: none;
  border-radius: inherit;
  background: linear-gradient(180deg, rgba(255,255,255,0.18), rgba(255,255,255,0.02) 30%, transparent 60%);
  mix-blend-mode: screen; opacity: 0.5;
}
```

⚠️ ห้ามใส่ `position: relative` ที่ `.lg-refract` — จะ override `.sticky` ของ header (target ทุกตัวเป็น sticky/fixed/absolute อยู่แล้ว)

---

## 6. Header (sticky บนสุด)

```tsx
<header
  className="lg-refract border-b px-3 sm:px-6 py-2 sm:py-3 flex items-center justify-between safe-top sticky top-0 z-30 hidden md:flex"
  style={{
    background: "rgba(5, 5, 8, 0.32)",           // แก้วใสมาก
    WebkitBackdropFilter: "blur(6px) saturate(140%)",
    backdropFilter: "blur(6px) saturate(140%)",
    borderBottomColor: "rgba(255,255,255,0.10)",
  }}
>
```

- **ไม่มี `h1` ชื่อเว็บ** — เอาออกตาม request (commit ef6e244) เหลือแค่ nav ลิงก์
- **มือถือซ่อน header ทั้งแถบ** (`hidden md:flex`) — มือถือใช้ MobileNav dock อย่างเดียว

---

## 7. MobileNav — Floating Glass Dock (เมนูล่าง, สไลด์ซ้ายขวา)

**โครงสร้าง (78ba310):** ทุกหน้า **5 แท็บในแถวเดียว** (จัดเมนูใหม่รอบ 2 — รอบ 1 89f2a51 รวม market→หน้าหลัก, risk→มอนิเตอร์, signal-logs→แท็บในสัญญาณ; รอบ 2 รวม performance→แท็บในมอนิเตอร์; dock ไม่ต้องปัดเลื่อนแล้ว แต่คงโครงสไลด์ไว้) · ลำดับ: หน้าหลัก · สัญญาณ · มอนิเตอร์ · Logs · ตั้งค่า · แสดงเฉพาะ `md:hidden` · active tab = `text-accent font-semibold` · แท็บ active ถูกเลื่อนมากึ่งกลางอัตโนมัติ

**Tab bar = dock ลอยโค้งมน แก้วขาวใส (ไม่ใช้พื้นเข้ม):**

```tsx
<nav
  aria-label="เมนูหลัก"
  className="lg-refract fixed z-40 border"
  style={{
    left: "0.75rem",
    right: "0.75rem",
    bottom: "calc(env(safe-area-inset-bottom, 0px) + 0.65rem)",
    background: "rgba(255, 255, 255, 0.06)",      // แก้วขาวใส
    WebkitBackdropFilter: "blur(8px) saturate(160%)",
    backdropFilter: "blur(8px) saturate(160%)",
    borderColor: "rgba(255,255,255,0.16)",
    borderRadius: "1.4rem",                        // ~22.4px
    boxShadow: "0 8px 32px rgba(0, 0, 0, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.14)",
  }}
>
  {/* สไลด์ซ้ายขวา — no-scrollbar util (globals.css) */}
  <div ref={scrollRef} className="no-scrollbar flex overflow-x-auto px-1 py-0.5"
       style={{ overscrollBehaviorX: "contain" }}>
    {/* แท็บ: flex flex-col items-center gap-0.5 min-h-[56px] min-w-[64px] px-1
        shrink-0 text-[11px] active:bg-white/10 rounded-xl, icon text-xl
        data-active={isActive(href) || undefined} */}
```

**Auto-center แท็บ active (สำคัญ — timing บน prod):**

```tsx
// คำนวณ synchronous ใน effect — ห้ามใช้ requestAnimationFrame หรือ scrollIntoView smooth
// เพราะบน prod hydration/font ช้ากว่า local → RAF callback รันเมื่อค่าเปลี่ยนแล้ว = ไม่เลื่อน
effect: sc.scrollLeft = Math.max(0, el.offsetLeft + el.offsetWidth/2 - sc.clientWidth/2)
document.fonts?.ready?.then(center)  // center ซ้ำหลัง font swap (ความกว้างแท็บเปลี่ยน)
```

แท็บแรก/สุดท้าย (หน้าหลัก/Settings) เลื่อนชนขอบเพราะ clamp ที่ maxScroll — พฤติกรรมปกติของ scroll container

**Layout ต้องเว้นที่ให้ dock:** `<main className="... pb-24 md:pb-5">` — ⚠️ ห้ามใช้ `.safe-bottom` บน main (env() มาทีหลัง Tailwind จะ override pb-* เป็น 0 ทำให้ dock ทับเนื้อหา)

⚠️ **ห้ามใส่ `.lg-refract` กลับบน dock โดยไม่ทดสอบ Samsung เครื่องจริง** — เดิม dock ใช้ `.lg-refract` (SVG displacement บน ::before) แล้วบน Samsung Internet วาดเป็น "กรอบซ้อน" เพี้ยน ๆ (commit 9c355bd เอาออก) — dock ปัจจุบันใช้ inline blur บนตัว nav เอง ซึ่ง Samsung รองรับปกติ

---

## 8. ระบบปุ่ม (4 บทบาท — แก้วใส tinted ทั้งหมด)

| บทบาท | Class trigger | พื้น | ขอบ | ใช้เมื่อ |
|---|---|---|---|---|
| PRIMARY | `button.bg-accent` | rgba(10,132,255,0.32) | rgba(10,132,255,0.55) | บันทึก/ส่ง/รัน/เพิ่ม |
| DANGER | `button.bg-loss` | rgba(255,69,58,0.3) | rgba(255,69,58,0.55) | ปิดไม้/ลบ/Pause/Reject |
| SUCCESS | `button.bg-profit` | rgba(48,209,88,0.3) | rgba(48,209,88,0.55) | Resume/Approve |
| NORMAL | `button.border-slate-700` | rgba(255,255,255,0.05) | rgba(255,255,255,0.14) | รีเฟรช/ยกเลิก/ทั่วไป |

```css
/* โครงร่วม */
button.bg-accent, button.bg-loss, button.bg-profit {
  color: #ffffff;
  box-shadow: 0 4px 16px <สี tint 0.2>, inset 0 1px 0 rgba(255,255,255,0.2);
  -webkit-backdrop-filter: blur(6px) saturate(160%);
  backdrop-filter: blur(6px) saturate(160%);
}
button { border-radius: 0.75rem !important; }        /* โค้ง 12px ทุกปุ่ม */
button.rounded-full { border-radius: 999px !important; }  /* ยกเว้นปุ่มกลม */

/* hover: ทึบขึ้น + ลอยขึ้น 1px (desktop) */
button.bg-accent:hover:not(:disabled)  { background: rgba(10,132,255,0.45); }
button.bg-loss:hover:not(:disabled)    { background: rgba(255,69,58,0.42); }
button.bg-profit:hover:not(:disabled)  { background: rgba(48,209,88,0.42); }
@media (hover: hover) { /* tinted buttons: transform: translateY(-1px) */ }

/* NORMAL hover/active */
button.border-slate-700:hover:not(:disabled)  { background: rgba(255,255,255,0.09); border-color: rgba(255,255,255,0.3); }
button.border-slate-700:active:not(:disabled) { background: rgba(255,255,255,0.12); }

/* กดทุกปุ่ม: หดเล็กน้อยแบบ iOS */
button { transition: transform .12s ease, background-color .15s ease, border-color .15s ease, box-shadow .2s ease, opacity .15s ease, filter .15s ease; }
button:not(:disabled):active { transform: scale(0.97); }
```

**Class ประกอบ (Tailwind):**

```css
.btn-primary  { @apply inline-flex items-center justify-center gap-1 bg-accent text-white font-semibold rounded-xl px-4 py-2.5 text-sm min-h-[44px] active:brightness-90 disabled:opacity-50 transition; }
.btn-secondary{ @apply inline-flex items-center justify-center gap-1 text-slate-200 rounded-xl px-4 py-2.5 text-sm min-h-[44px] active:bg-white/10 disabled:opacity-50 transition; background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.14); backdrop-filter: blur(6px); }
.btn-sm       { @apply inline-flex items-center justify-center gap-1 rounded-lg px-3 min-h-[40px] text-sm active:opacity-80 disabled:opacity-50 transition; }
```

---

## 9. ฟอร์ม (input/select/textarea — แก้วฝ้าทั้งแอป)

```css
input:not([type="checkbox"]):not([type="radio"]):not([type="range"]):not([type="color"]),
select, textarea {
  background: rgba(255, 255, 255, 0.05);
  border: 1px solid rgba(255, 255, 255, 0.14);
  border-radius: 0.75rem !important;   /* โค้งเท่าปุ่ม */
  color: #e2e8f0;
  -webkit-backdrop-filter: blur(5px);
  backdrop-filter: blur(5px);
  transition: border-color .15s ease, box-shadow .2s ease;
}
/* focus: ขอบฟ้า + ring */
:focus {
  border-color: rgba(10, 132, 255, 0.7);
  outline: none;
  box-shadow: 0 0 0 3px rgba(10, 132, 255, 0.22);
}
::placeholder { color: #64748b; }
option { background-color: #15151c; color: #e2e8f0; }

/* select: ซ่อนลูกศร native วาดเอง (SVG chevron สี #94a3b8, ขวา 0.7rem) + padding-right 2.2rem */
```

**กัน iOS zoom:** `@media (max-width: 639px) { input, select, textarea { font-size: 16px !important; } }` + body `-webkit-text-size-adjust: 100%`

**Select = liquid glass เต็มรูปแบบ (อัปเดต 2026-09-06):** ห้ามใส่ `bg-surface` หรือ bg ทึบทับ select อีกต่อไป — ทุก dropdown ในแอป (10 จุด: monitor/signals refresh, settings ×4, performance, GoalForm, ฯลฯ) ใช้พื้นหลังโปร่ง `rgba(255,255,255,0.05)` + `backdrop-filter: blur(5px)` จาก rule กลางด้านบน ถ้าเผลอใส่ `bg-surface` (สีพื้นทึบ) ทับ จะทำให้ select เป็นก้อนทึบตัดกับ card แก้วรอบ ๆ ทันที ตรวจง่าย ๆ: เปิด dropdown แล้วต้องมองทะลุเห็น aurora เบลอ ๆ ด้านหลัง

**GlassSelect = standard ใหม่ของ dropdown ทุกตัว (อัปเดต 2026-09-07):** native `<select>` ถูกแทนด้วย component `frontend/src/components/GlassSelect.tsx` ทั้ง 10 จุดแล้ว (monitor/signals refresh, settings ×4, GoalForm ×2, PerformancePanel ×2) — เหตุผล: popup รายการของ native `<select>` เป็น **OS-drawn** (Windows/Android วาดดำทึบเสมอ ปรับ CSS ไม่ได้) จึงไม่มีทางเป็นแก้วฝ้าได้ GlassSelect วาด trigger + popup เอง: trigger = `.glass-select-trigger` (โปร่ง blur(5px) + chevron SVG หมุนตอนเปิด), popup = `.glass-select-popup` (**แก้วขาวใส white frosted 2026-09-07**: gradient ขาวโปร่ง 0.22→0.10 + blur(20px) saturate(160%) + border ขาวจาง 0.30 + shadow เหมือน iOS popover, animation glass-pop 0.14s — ตัวหนังสือขาว #f8fafc อ่านชัดบนพื้นโปร่ง), item = `.glass-select-item` + `.is-active` (hover/keyboard) + `.is-selected` (ฟ้าอ่อน + ✓) รองรับ keyboard ครบ (ArrowUp/Down, Enter, Escape) + aria (listbox/option/expanded) + ปิดเมื่อคลิกนอก **กันการ์ดอื่นทับ popup:** backdrop-filter ของ `.panel` สร้าง stacking context → z-index ของ popup มีผลแค่ภายในการ์ดตัวเอง จึงใช้ `.panel:has(.glass-select-popup) { z-index: 25 }` ยกการ์ดเจ้าของ dropdown ขึ้นเหนือการ์ดพี่น้องเฉพาะตอน popup เปิด (25 ยังต่ำกว่า nav z-30/z-40 และ modal z-50 — popup ไม่ทับ chrome แอป) **กฎ: dropdown ใหม่ทุกตัวต่อจากนี้ใช้ `<GlassSelect>` เท่านั้น — ห้ามกลับไปใช้ native `<select>`** (CSS rule เดิมของ select/option ยังเก็บไว้เป็น fallback เท่านั้น)

---

## 10. Pill ตัวเลขเขียว/แดงในตาราง ⚠️ (จุดพังง่ายที่สุด)

**กฎเดียว: pill ต้องเป็น `<span>` ครอบค่าข้างใน td เสมอ — ห้ามใส่ pill บน td โดยตรง**
(span เป็น inline-block จะหดขนาดพอดีตัวหนังสือ; ถ้าใส่บน td พื้นจะกินทั้ง cell และถ้าเผลอใส่ display จะทำคอลัมน์เลื่อนทับกัน)

```tsx
{/* ✅ ถูก */}
<td className="py-2 pr-4"><span className="text-loss">2390.00</span></td>
<td className="py-2 pr-4 font-bold">
  <span className={pnl >= 0 ? "text-profit" : "text-loss"}>+55.00</span>
</td>

{/* ❌ ผิด — bg กินทั้ง cell, เสี่ยงคอลัมน์เลื่อน */}
<td className="py-2 pr-4 text-loss">2390.00</td>
```

```css
/* pill span ใน td — inline-block หดพอดีตัวหนังสือ */
td .text-profit, td .text-loss, td .text-emerald-400, td .text-red-400 {
  background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.03));
  backdrop-filter: blur(4px);
  border: 1px solid rgba(255,255,255,0.14);
  border-radius: 999px;
  padding: 0.05rem 0.3rem;
  display: inline-block;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

/* tint ฝั่ง — เขียว/แดงจาง ๆ อ่านง่ายไม่ฉูดฉาด */
td .text-profit, td .text-emerald-400 {
  background: linear-gradient(180deg, rgba(48,209,88,0.16), rgba(48,209,88,0.07));
  border-color: rgba(48,209,88,0.3);
}
td .text-loss, td .text-red-400 {
  background: linear-gradient(180deg, rgba(255,69,58,0.16), rgba(255,69,58,0.07));
  border-color: rgba(255,69,58,0.3);
}
```

> 🚨 **บทเรียน (bug จริง 2 ครั้ง):** (1) ใส่ `display: inline-block` บน td โดยตรง → cell หลุดจากกริดตาราง คอลัมน์ SL/TP เลื่อนทับกันทั้งแถว (2) ใส่ pill bg บน td → พื้นกินทั้ง cell ไม่พอดีตัวหนังสือ ทางแก้สุดท้ายคือ span wrapper เท่านั้น ตรวจเสมอด้วยการเทียบ `getBoundingClientRect().x` ของ thead vs tbody ทุกคอลัมน์ + span width < td width

### 10.1 Pill แบบ component (หน้า signals — commit 67a2dfd)

นอกจาก pill อัตโนมัติจาก CSS ด้านบน (ครอบ class `text-profit`/`text-loss` ลอย ๆ ใน td) หน้า signals ใช้ **pill แบบเขียน class ตรง** — คุมพื้น tint 30% + ตัวอักษรโทนเดียวกัน + น้ำหนักใน class เดียว (ตัวเลขเป็นตัวหนาเสมอ):

```tsx
// pill มาตรฐาน — SL/TP, PnL, ▲▼ % (เข้ม /30 + ตัวหนา ตาม request 2026-09-07 "pill เข้มอีก" และ "ตัวเลขให้เป็นตัวหนา")
<span className="inline-flex items-center rounded-full bg-profit/30 text-profit px-2 py-0.5 font-bold">TP 0.73313</span>
<span className="inline-flex items-center rounded-full bg-loss/30 text-loss px-2 py-0.5 font-bold">SL 0.71268</span>

// tier badge วงกลม — เลขล้วน 1/2/3 (ผู้ใช้ขอ 2026-09-07: "ระดับ 1,2,3 ให้เหลือแค่ 1.2.3")
<span className={`inline-flex items-center justify-center min-w-[22px] h-[22px] rounded-full px-1.5 text-xs font-bold ${
  buy ? "bg-profit/30 text-profit" : "bg-loss/30 text-loss"}`}>1</span>
```

**จุดที่ใช้แล้ว (67a2dfd):**
- `LimitLevels.tsx` — tier badge วงกลม 1/2/3 + แถว SL/TP เป็น pill
- `SltpLevels.tsx` — SL/TP ทุก tier เป็น pill
- `SignalCard.tsx` — ▲▼ % ราคาตลาดเทียบ entry เป็น pill (เขียวขึ้น/แดงลง)
- `SignalLogsPanel.tsx` — คอลัมน์ ทิศทาง (BUY/SELL), SL, TP, PnL เป็น pill

**กฎ:** ข้อความสีใหม่บนหน้า signals ต่อจากนี้ใช้ pill แบบนี้เสมอ — อย่าปล่อยเป็นตัวอักษรสีลอย ๆ (ยกเว้นตารางที่ pill CSS §10 จัดการให้อยู่แล้ว) และยังคงห้ามใส่ pill บน td โดยตรง

### 10.2 ตัวเลขเป็นตัวหนา (หน้า signals + monitor — request 2026-09-07 "หน้า signal และ monitor ตัวเลขให้เป็นตัวหนา")

- ทุกตัวเลข (ราคา, lots, PnL, confidence, %) ใช้ `font-bold` — ไม่ใช้ `font-semibold` กับตัวเลขอีกต่อไป
- **Monitor:** ตารางไม้ค้าง (Lots/Entry/ราคาปัจจุบัน/SL/TP/PnL) + ตารางประวัติ order (Lots/Entry/Exit/PnL) ใส่ `font-bold` ที่ td หรือ span ข้างใน
- **Signals:** Field ใน SignalCard (`font-bold`), ราคาใน LimitLevels (`font-bold text-sm`), pill SL/TP เปลี่ยน `font-semibold` → `font-bold`, ตาราง SignalLogsPanel (confidence/entry/exit/volume) เป็น `font-mono font-bold`

### 10.3 Badge "ระดับถูกขยับ" (SL/TP move indicator — monitor, 2026-09-07)

- **จุดประสงค์:** บอกผู้ใช้ว่า SL/TP ของไม้ค้างถูกขยับจากค่าตั้งต้น (breakeven / trailing / ปรับมือ) — กดค้าง/ชี้เพื่อดู tooltip
- **Pattern:** `<span title={...} className="ml-1 inline-flex cursor-help align-middle">` ครอบ `<Icon n="arrowsH" size={12} className="text-accent" />` — ไอคอนลูกศรซ้ายขวาเล็ก ๆ สี accent ต่อท้ายตัวเลข SL/TP
- **Tooltip:** native `title` (ไม่ใช้ portal) — รูปแบบ `"SL ถูกขยับ: {initial} → {current}\nเมื่อ: {th-TH short}\nเหตุผล: {reasonLabel}"` (breakeven→"ทุนคืน (Breakeven)", trailing→"Trailing Stop", manual*→"ปรับด้วยมือ")
- **เงื่อนไขแสดง:** `moved_at != null` **หรือ** `initial != null && |current - initial| > 1e-9` (กันกรณี migration 021 ยังไม่ apply → initial เป็น null แต่ค่าต่างกันจริง)
- **Component:** `LevelMovedBadge` ใน `frontend/src/app/monitor/page.tsx` — return `null` เมื่อไม่ moved; ใช้ได้กับตารางอื่นที่มีค่าระดับเปลี่ยนแปลงตามเวลา

---

## 11. ตารางบนมือถือ

```css
@media (max-width: 767px) {
  .overflow-x-auto > table { width: auto; min-width: 100%; }
}
```

> 🚨 **บทเรียน (bug จริง):** ห้าม `width: max-content` + `table-layout: fixed` — Chrome จัดกริดหัวตารางกับเนื้อหาไม่ตรงกัน (หัว SL ทาบช่อง TP) ใช้ auto layout เท่านั้น ให้ wrapper `.overflow-x-auto` เลื่อนแนวนอนแทน

> 🚨 **บทเรียน (bug จริง):** ห้ามใส่ `overflow-x-auto` บน `.panel` เดียวกัน (`className="panel overflow-x-auto"`) — เมื่อตารางโหลดแล้วกว้างขึ้น panel กลายเป็น scroll container เอง ทำให้ `backdrop-filter` พังใน Chromium (blur หาย) ต้องแยกเป็น `.panel` นอก + `<div className="overflow-x-auto">` ใน

---

## 12. Animations (iOS-like, เบามือ)

```css
@keyframes lg-fade-in  { from { opacity: 0; } to { opacity: 1; } }
@keyframes lg-sheet-up { from { transform: translateY(100%); } to { transform: translateY(0); } }
@keyframes lg-pop-in   { from { opacity: 0; transform: scale(0.94) translateY(10px); } to { opacity: 1; transform: scale(1) translateY(0); } }
@keyframes lg-rise-in  { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes lg-slide-down { from { opacity: 0; transform: translateY(-12px); } to { opacity: 1; transform: translateY(0); } }
@keyframes lg-slide-up   { from { opacity: 0; transform: translateY(14px); } to { opacity: 1; transform: translateY(0); } }

.animate-fade  { animation: lg-fade-in .2s ease-out both; }
.animate-sheet { animation: lg-sheet-up .32s cubic-bezier(0.32, 0.72, 0, 1) both; }  /* iOS spring */
.animate-pop   { animation: lg-pop-in .25s cubic-bezier(0.32, 0.72, 0, 1) both; }

/* Site-wide entrance stagger */
header        { animation: lg-slide-down .4s cubic-bezier(0.32,0.72,0,1) backwards; }
main > *      { animation: lg-slide-up .45s cubic-bezier(0.32,0.72,0,1) backwards; }
main .grid > *{ animation: lg-rise-in .4s ease-out backwards; }
/* grid children: nth-child(1..8) delay 0.05s→0.4s (step .05s), n+9 คงที่ 0.4s */
nav[aria-label="เมนูหลัก"] > div:first-child { animation: lg-slide-up .4s cubic-bezier(0.32,0.72,0,1) .1s backwards; }

/* ตาราง: แถวโผล่ทีละแถว — tbody tr nth-child(1..8) delay 0.02s→0.16s (step .02s), n+9 = 0.18s */
/* แชท: ฟองข้อความ (.max-w-\[85\%\]) animate lg-pop-in .3s */

/* แชท — "กำลังคิด" indicator (commit 2c5e717): Tailwind built-ins ไม่ใช้ lg-* keyframes
   จุดเด้ง 3 จุด + ข้อความ pulse + นับวินาที (thinkSecs = setInterval 1s ระหว่าง loading) */
/* 🚨 ต้องแสดงตลอดระหว่าง loading — ห้ามผูกกับเงื่อนไข "ยังไม่มีข้อความตอบ"
   (เดิม {loading && !last?.content} ทำให้ indicator หายพอ AI เริ่มพิมพ์ทีละ chunk) */

/* accordion: details summary + * lg-rise-in .3s, summary:active scale(0.99) */
/* เคารพ OS */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important; animation-delay: 0s !important;
    animation-iteration-count: 1 !important; transition-duration: 0.01ms !important;
  }
  html { scroll-behavior: auto; }
}
```

**ตัวอย่าง "กำลังคิด" indicator (ทั้ง `/chat` + `ChatWidget`):**

```tsx
{loading && (
  <div className="flex items-center gap-2 text-slate-400 text-xs animate-pulse">
    <span className="inline-flex gap-1" aria-hidden="true">
      <span className="w-1.5 h-1.5 rounded-full bg-accent animate-bounce [animation-delay:0ms]" />
      <span className="w-1.5 h-1.5 rounded-full bg-accent animate-bounce [animation-delay:150ms]" />
      <span className="w-1.5 h-1.5 rounded-full bg-accent animate-bounce [animation-delay:300ms]" />
    </span>
    💭 AI กำลังคิด... ({thinkSecs}s)
  </div>
)}
```

---

## 13. Mobile Quirks & Utilities

```css
/* safe area (notch) */
.safe-top    { padding-top: env(safe-area-inset-top, 0px); }
.safe-bottom { padding-bottom: env(safe-area-inset-bottom, 0px); }

/* แตะ: ลบ highlight ฟ้า + ตอบสนองทันที */
* { -webkit-tap-highlight-color: transparent; }
button, a, select, input[type="checkbox"] { touch-action: manipulation; }

/* scrollbar มืดบาง ๆ (desktop) */
::-webkit-scrollbar { width: 8px; height: 8px; }
::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.14); border-radius: 999px; }
::-webkit-scrollbar-track { background: transparent; }
```

**Viewport (layout.tsx):** `width: device-width, initialScale: 1, maximumScale: 1, userScalable: false, viewportFit: "cover", themeColor: "#000000"` + `appleWebApp: { capable: true, statusBarStyle: "black-translucent" }`

**Modal/overlay มาตรฐาน:** backdrop `rgba(0,0,0,0.6–0.75)` + `blur(12–20px) saturate(140%)`, กล่องใช้ `.panel` + `.animate-pop`

---

## 14. Checklist สร้างเว็บใหม่ด้วย UI นี้

1. ☐ Tailwind config: palette 6 สี (§2) + override `.text-slate-400/500`
2. ☐ Body: ดำ + aurora 3 radial-gradient + `color-scheme: dark` (§3)
3. ☐ คัดลอก `.panel` + `::after` sheen (§4) — ใช้เป็นการ์ดหลักทุกหน้า
4. ☐ `.lg-refract` CSS (§5) — ⛔ refraction filter ปิดอยู่ (Samsung พัง) เหลือ isolation + specular ::after เท่านั้น — ห้ามเปิดกลับโดยไม่ทดสอบ Samsung เครื่องจริง
5. ☐ Header sticky แก้วใส rgba(5,5,8,0.32) (§6)
6. ☐ MobileNav floating dock สไลด์ซ้ายขวา + auto-center active tab (§7) + `main pb-24`
7. ☐ ปุ่ม 4 บทบาท tinted + radius 12px + scale(0.97) (§8)
8. ☐ ฟอร์มแก้วฝ้า + **select โปร่ง (ห้ามใส่ bg-surface ทับ)** + focus ring ฟ้า + กัน iOS zoom 16px (§9)
9. ☐ Pill ตาราง: ครอบค่าด้วย `<span>` ใน td เสมอ — ห้าม pill บน td โดยตรง (§10) · ข้อความสีบนหน้า signals ใช้ pill แบบ component (§10.1)
10. ☐ ตารางมือถือ `width: auto` + wrapper เลื่อนแนวนอน — `overflow-x-auto` ต้องอยู่ div ลูก ไม่ใช่บน `.panel` (§11)
11. ☐ Animations + stagger + reduced-motion (§12)
12. ☐ Safe-area, tap-highlight, scrollbar, viewport (§13)
13. ☐ ไอคอนทุกจุดใช้ `Icon.tsx` — ห้าม emoji ใน UI (§15)
14. ☐ สวิตช์ on/off ใช้ pattern iOS toggle (§16)

---

## 15. ระบบไอคอน SVG monotone — `frontend/src/components/Icon.tsx` (fc753ce)

**กฎ: ห้ามใช้ emoji ใน UI ทุกจุด** — ไอคอนทั้งแอปมาจาก component เดียว สีตาม `currentColor` (สืบทอดจาก text-* ของ parent) จึง tint ตามบริบทได้ (accent/profit/loss/slate)

```tsx
// Icon.tsx — 36 ไอคอน: bot, user, users, home, hand, check, checkCircle, x, xCircle,
// warning, ban, help, target, trendUp, trendDown, bolt, chart, waves, archive, scroll,
// news, inbox, lock, bulb, flask, coins, clock, hourglass, octagon, pause, play,
// arrowsH, message, arrowDown, undo, shield
import Icon from "@/components/Icon";
<Icon n="checkCircle" size={16} className="text-profit" />
```

- viewBox `0 0 24 24`, `stroke="currentColor"`, `strokeWidth 1.8`, fill none — เส้นบางสไตล์ iOS
- className เริ่มต้น `inline-block shrink-0 align-[-0.15em]` — วางใน flow ข้อความได้โดยไม่เบย baseline
- export `IconName` type — ใช้ type-safe ใน metadata arrays (เช่น `NOTIFY_CATEGORIES` ใน settings page)
- glyph ขาวธรรมดา ✓ ✗ ✕ • ในข้อความยังใช้ได้ (ไม่ใช่ emoji) — แต่ปุ่ม/หัวข้อ/แถวสถานะใช้ Icon เสมอ
- select `<option>` วาด JSX ไม่ได้ → ตัด emoji ออกเหลือข้อความล้วน
- ตรวจ emoji หลุดเหลือ: grep `[\x{1F300}-\x{1FAFF}\x{2600}-\x{27BF}\x{FE0F}]` (JS: `/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE0F}]/gu`)

**DesktopNav (desktop pill navbar):** แถวเดียว justify-end ลอยเหนือเนื้อหา (mb-6) — แบรนด์ SVG + ลิงก์ 5 หน้า, active = `text-accent font-semibold`, ทุกไอคอนใช้ Icon.tsx

---

## 16. iOS Toggle Switch (สวิตช์ on/off — ใช้ใน Settings หมวดแจ้งเตือน)

สวิตช์มาตรฐานของแอป — ปุ่ม `<button role="switch">` + ลูกดอกขาวเลื่อนซ้าย/ขวา ไม่ใช้ checkbox native

```tsx
<button role="switch" aria-checked={on} aria-label={label}
  disabled={busy}
  onClick={() => toggle(!on)}
  className={`relative w-[46px] h-[28px] rounded-full transition-colors shrink-0 disabled:opacity-40 ${
    on ? "bg-profit" : "bg-slate-700"}`}>
  <span className={`absolute top-[3px] w-[22px] h-[22px] rounded-full bg-white shadow transition-all ${
    on ? "left-[21px]" : "left-[3px]"}`} />
</button>
```

- **on = `bg-profit` (เขียว iOS), off = `bg-slate-700`** — สีสื่อสถานะตาม palette §2
- ขนาด 46×28 ลูกดอก 22px — แตะง่ายบนมือถือ, `disabled:opacity-40` ระหว่างบันทึก
- `role="switch"` + `aria-checked` + `aria-label` — screen reader อ่านเป็นสวิตช์
- **บันทึกทันทีเมื่อกด** (instant save ผ่าน `api.saveSettings({key: value})`) — ไม่ต้องกดปุ่ม Save รวม; แสดง feedback ใน `lineMsg`
- ตัวอย่างการใช้จริง: หมวดหมู่การแจ้งเตือน LINE ในหน้า Settings — แถวละ 1 หมวด (icon วงกลม tint + ชื่อหมวด + คำอธิบาย + สวิตช์), ค่าเก็บใน `trading_settings` (`notify_*` booleans) ตามหมวด
- ✅ **Verified บน prod 2026-09-06 (a13eb36)**: 6 สวิตช์แสดงครบ โหลดค่าจาก API ถูกต้อง (เริ่มต้น ON ทุกหมวด), toggle ปิด/เปิด "ไม้เปิด" บันทึกสำเร็จทั้งสองครั้ง (feedback "บันทึกการแจ้งเตือนแล้ว"), migration 020 รันแล้ว (เดิมชื่อ 012 — rename เพราะชนเบอร์) — PUT `notify_*` ผ่าน (ถ้ายังไม่รัน PostgREST จะ error หาคอลัมน์)
- 🚨 บทเรียน Playwright: กดสวิตช์แล้ว re-render ขณะ feedback หาย ทำให้ click ปกติ timeout ("waiting for element to be visible, enabled and stable") — ใช้ `click({ force: true })` แก้ได้ และอ่านสถานะจาก `aria-checked` เสมอ
