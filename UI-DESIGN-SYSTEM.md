# 🧊 tdapp UI Design System — iOS Liquid Glass Dark

> เอกสารนี้สรุป UI ทั้งหมดของ tdapp เพื่อให้เว็บอื่นอ่านแล้วสร้าง UI เดียวกันได้
> ต้นทางจริง: `frontend/src/app/globals.css`, `frontend/tailwind.config.ts`,
> `frontend/src/app/layout.tsx`, `frontend/src/components/MobileNav.tsx`, `frontend/src/components/BackgroundLayer.tsx`
> สถานะล่าสุด: commit `79b4645` (prod hash `DFB879C8F6`, verified 2026-09-06)

---

## 1. ปรัชญา (Design Philosophy)

- **iOS Liquid Glass บนพื้นดำ**: พื้นหลังดำสนิท + aurora glow จาง ๆ, การ์ด/ปุ่ม/ฟอร์มทุกชิ้นเป็น "แก้วฝ้าโปร่งแสง" (blur เบื้องหลังผ่านได้)
- **แก้วใส ไม่ใช่แก้วขุ่น**: blur ต่ำ (5–10px) เพื่อให้เห็นพื้นหลังชัด — ไม่ใช้พื้นทึบเข้ม
- **สื่อสารด้วยสี ไม่ใช้ emoji ในปุ่ม**: ฟ้า=ทำ, แดง=อันตราย, เขียว=ยอมรับ, ขาวใส=ทั่วไป
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

**ภาพพื้นหลังผู้ใช้ (BackgroundLayer):** client component วาด 2 div fixed — รูป z-[-2] + scrim ดำ rgba(0,0,0,0.55) z-[-1] เหนือรูป (ใต้ aurora) · เก็บใน localStorage key `tdapp_bg_image` (data URL, resize canvas ยาวสุด 1600px, JPEG q0.72, cap ~2.8MB) · อัปเดตทันทีด้วย event `tdapp:bg-changed`

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

## 5. Liquid Glass Refraction (SVG feDisplacementMap)

ทำให้เนื้อหาหลังแก้ว "หักเห" แบบเลนส์ (ไม่ใช่แค่เบลอ) — ใช้เฉพาะ header + tab bar (หนักบนมือถือ ห้ามใส่ทุก panel)

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
/* เฉพาะ browser ที่รองรับ (Chromium/Safari ใหม่) — Firefox parse ไม่ผ่าน → fallback blur เดิม */
@supports (backdrop-filter: url("#lg-refract")) {
  .lg-refract::before {
    -webkit-backdrop-filter: url("#lg-refract");
    backdrop-filter: url("#lg-refract");
  }
}
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
  className="lg-refract border-b px-3 sm:px-6 py-2 sm:py-3 flex items-center justify-between safe-top sticky top-0 z-30"
  style={{
    background: "rgba(5, 5, 8, 0.32)",           // แก้วใสมาก
    WebkitBackdropFilter: "blur(6px) saturate(140%)",
    backdropFilter: "blur(6px) saturate(140%)",
    borderBottomColor: "rgba(255,255,255,0.10)",
  }}
>
```

- ซ้าย: `h1` ชื่อเว็บ (มือถือตัวสั้น / desktop ตัวยาว ด้วย `sm:hidden` / `hidden sm:inline`)
- ขวา: nav desktop `hidden md:flex gap-4 text-sm text-slate-400` ลิงก์ `hover:text-accent`
- มือถือ (<md) ซ่อน nav นี้ → ใช้ MobileNav dock ด้านล่างแทน

---

## 7. MobileNav — Floating Glass Dock (เมนูล่าง)

**โครงสร้าง:** 4 แท็บหลัก (หน้าหลัก/ตลาด/สัญญาณ/มอนิเตอร์) + ปุ่ม "เพิ่มเติม" เปิด bottom sheet (Logs, Signal Logs, Risk, Performance, Settings) · แสดงเฉพาะ `md:hidden` · active tab = `text-accent font-semibold`

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
  <div className="grid grid-cols-5 px-1 py-0.5">
    {/* แท็บ: flex flex-col items-center gap-0.5 min-h-[56px] text-[11px]
        active:bg-white/10 rounded-xl, icon text-xl */}
```

**Bottom sheet (เมนูเพิ่มเติม):**

```tsx
// backdrop: fixed inset-0 bg-black/60 + blur(6px) + animate-fade
// sheet: absolute bottom-0 inset-x-0 rounded-t-3xl animate-sheet
style={{
  background: "rgba(18, 18, 24, 0.82)",           // ตัว sheet ทึบกว่า dock (อ่านง่าย)
  WebkitBackdropFilter: "blur(14px) saturate(160%)",
  backdropFilter: "blur(14px) saturate(160%)",
  borderTopColor: "rgba(255,255,255,0.16)",
}}
// รายการใน sheet: grid grid-cols-3 gap-2, การ์ดย่อย rounded-xl border
//   active: border-accent/60 bg-accent/15 text-accent
//   ปกติ:   border-white/10 bg-white/[0.04] text-slate-300
```

**Layout ต้องเว้นที่ให้ dock:** `<main className="... pb-24 md:pb-5">` — ⚠️ ห้ามใช้ `.safe-bottom` บน main (env() มาทีหลัง Tailwind จะ override pb-* เป็น 0 ทำให้ dock ทับเนื้อหา)

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
4. ☐ SVG filter `#lg-refract` ใน body + `.lg-refract` CSS (§5) — ใส่เฉพาะ header/tab bar
5. ☐ Header sticky แก้วใส rgba(5,5,8,0.32) (§6)
6. ☐ MobileNav floating dock แก้วขาว + bottom sheet (§7) + `main pb-24`
7. ☐ ปุ่ม 4 บทบาท tinted + radius 12px + scale(0.97) (§8)
8. ☐ ฟอร์มแก้วฝ้า + focus ring ฟ้า + กัน iOS zoom 16px (§9)
9. ☐ Pill ตาราง: ครอบค่าด้วย `<span>` ใน td เสมอ — ห้าม pill บน td โดยตรง (§10)
10. ☐ ตารางมือถือ `width: auto` + wrapper เลื่อนแนวนอน — `overflow-x-auto` ต้องอยู่ div ลูก ไม่ใช่บน `.panel` (§11)
11. ☐ Animations + stagger + reduced-motion (§12)
12. ☐ Safe-area, tap-highlight, scrollbar, viewport (§13)
