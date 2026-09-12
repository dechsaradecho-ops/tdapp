"use client";

import { useEffect, useRef, useState } from "react";

/** Mobile navigation — bottom dock (< md screens)
 *
 *  เขียนเองทั้งหมด (DOM + CSS + SVG filter) — ไม่มี WebGL, ไม่มี dependency
 *
 *  เดิมใช้ package `webgl-liquid-glass` แต่ pill ของมันถูกวาดใน fragment shader
 *  ด้วย `color = vec3(1.0)` (ขาวล้วน) บวกแถบขอบกว้าง ~2px → ได้ "กรอบขาว"
 *  รอบแท็บที่แก้ไม่ได้ (prop activeColor/inactiveColor ของ package มีผลกับตัวอักษร/
 *  ไอคอนใน DOM เท่านั้น ไม่ใช่สี pill) จึงยึดการวาด pill มาไว้เอง: มี pill เดียว
 *  หน้าตาคุมได้ 100% + สปริง/ยืดบี้/chromatic aberration/บิดด้วย SVG filter
 *  ตอนลาก (แบบ FluidGlass ของ React Bits แต่ไม่ใช้ three/.glb)
 *  ได้แถม: ไม่มี WebGL → ไม่เจอบั๊ก destroy()/loseContext กับ StrictMode
 *
 *  Desktop (md+) renders nothing; the inline nav in the header stays.
 *  หน้าตา/ขนาด/ตำแหน่งอยู่ใน .dock-glass* ที่ globals.css
 */
// Monotone stroke icons — สีจาก currentColor ของแท็บ (active = accent, ปกติ = slate)
const ICON = {
  home: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20a.5.5 0 0 0 .5.5h12a.5.5 0 0 0 .5-.5V9.5" />
      <path d="M9.5 20.5v-6h5v6" />
    </svg>
  ),
  signal: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />
    </svg>
  ),
  monitor: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 20.5h17" />
      <path d="M6.5 20.5V14" />
      <path d="M12 20.5V8.5" />
      <path d="M17.5 20.5V4.5" />
    </svg>
  ),
  logs: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2.5H6.5A1.5 1.5 0 0 0 5 4v16a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 20V7.5L14 2.5Z" />
      <path d="M14 2.5V8h5" />
      <path d="M9 13h6M9 17h6" />
    </svg>
  ),
  setting: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1Z" />
    </svg>
  ),
};

const MENU: { href: string; label: string; icon: keyof typeof ICON }[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/signals", label: "Signal", icon: "signal" },
  { href: "/monitor", label: "Monitor", icon: "monitor" },
  { href: "/logs", label: "Logs", icon: "logs" },
  { href: "/settings", label: "Setting", icon: "setting" },
];

/** static export เขียนไฟล์เป็น /signals.html → เทียบ path ตรง ๆ ไม่ได้
 *  (production host rewrite เป็น clean URL อยู่แล้ว จึงต้องรองรับทั้งสองแบบ) */
const normPath = (p: string) => {
  const s = p.replace(/\/index\.html$/, "/").replace(/\.html$/, "");
  return s === "" ? "/" : s;
};

const isActiveHref = (href: string, p: string) =>
  href === "/" ? p === "/" : p === href || p.startsWith(`${href}/`);

// เปลี่ยนแท็บ = นำทางจริง (static export ไม่มี Next router → ใช้ location)
const go = (href: string) => {
  if (normPath(window.location.pathname) === normPath(href)) return;
  window.location.assign(href);
};

export default function MobileNav() {
  const [path, setPath] = useState("/");
  const navRef = useRef<HTMLElement>(null);
  // pill = ตัวแก้วตอนพัก / orb = วงกลมแก้วตอนลาก / wake = หางของเหลว
  // rim = ขอบ chromatic aberration (อยู่ใน orb — โผล่เฉพาะตอนลาก)
  const pillRef = useRef<HTMLDivElement>(null);
  const orbRef = useRef<HTMLSpanElement>(null);
  const wakeRef = useRef<HTMLDivElement>(null);
  const rimRRef = useRef<HTMLSpanElement>(null);
  const rimGRef = useRef<HTMLSpanElement>(null);
  const rimBRef = useRef<HTMLSpanElement>(null);
  // disperse = ชั้นแยกสี (แดง/เขียว/น้าเงิน คนละรัศมี) = การหักเหของสี
  const dispRef = useRef<HTMLSpanElement>(null);
  // ให้ effect หลัก (deps []) วัดตำแหน่งใหม่ได้เมื่อแท็บ active เปลี่ยน
  const syncRef = useRef<(() => void) | null>(null);

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(normPath(window.location.pathname));
    const onPop = () => setPath(normPath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const activeIdx = Math.max(
    0,
    MENU.findIndex((l) => isActiveHref(l.href, path)),
  );

  // แท็บ active เปลี่ยน → ย้าย pill ไปแท็บใหม่ (position พักเปลี่ยน)
  useEffect(() => {
    syncRef.current?.();
  }, [activeIdx]);

  // ---- ฟิสิกส์ของเหลวของ pill/orb (สปริง + ยืดบี้ + chromatic aberration + warp) ----
  // หัวใจของ "แก้วเหลว" แบบ FluidGlass: pill ไม่ได้กระโดดตามนิ้ว แต่ "ไหล" ตาม
  // ด้วยสปริง underdamped (ตามช้าแล้วส่ายเข้าที่) + ยืดตามความเร็ว + ขอบสีเพี้ยน
  // คิดเป็น px/วินาที ทั้งหมด → เฟรมเรตเท่าไรก็ให้ความรู้สึกเดียวกัน
  // ตอนลาก pill แคปซูลจะจางหาย แล้วมี "วงกลมแก้ว" (orb) โผล่แทน: ขอบฟุ้ง
  // + backdrop-filter หักเหฉากหลัง + แถบแสงหักเห/ประกายบิดด้วย SVG filter 2 ความถี่
  //
  // ทำไมไม่ใช้ FluidGlass ตรง ๆ: มันไม่มี drag interaction เลย (mode = lens/cube/
  // bar) และต้องใช้ three + @react-three/fiber (peer React 19) + ไฟล์ .glb ที่
  // ไม่ได้แจกมา → ทำเฉพาะ "ความรู้สึก" ที่เป็นหัวใจด้วยสปริง + SVG filter แทน
  useEffect(() => {
    const nav = navRef.current;
    const pill = pillRef.current;
    const orb = orbRef.current;
    const wake = wakeRef.current;
    const rimR = rimRRef.current;
    const rimG = rimGRef.current;
    const rimB = rimBRef.current;
    const disp = dispRef.current;
    if (!nav || !pill || !orb || !wake || !rimR || !rimG || !rimB || !disp) return;

    // ผู้ใช้ที่ปิดอนิเมชัน → pill ยัง mark แท็บ active แต่นิ่ง ไม่มีสปริง/การลาก
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // เลนส์บิดเบี้ยวภาพหลังแก้ว (backdrop-filter: url()) เปิดเฉพาะ browser
    // ที่รองรับจริง และ "ไม่ใช่ Samsung Internet"
    // (บทเรียนใน globals.css: Samsung render dock พังเมื่อเจอ backdrop-filter: url())
    // ถอดคลาสออกเมื่อไร วงกลมกลับไปใช้ blur() ธรรมดาทันที ไม่มีอะไรเสียหาย
    const lensOk =
      typeof CSS !== "undefined" &&
      typeof CSS.supports === "function" &&
      (CSS.supports("backdrop-filter", 'url("#dock-glass-lens")') ||
        CSS.supports("-webkit-backdrop-filter", 'url("#dock-glass-lens")')) &&
      !/SamsungBrowser/i.test(navigator.userAgent);
    if (lensOk) nav.classList.add("dock-glass--lens");

    // เผย pill (CSS ซ่อนไว้กัน flash ก่อน JS วัดตำแหน่งเสร็จ)
    pill.style.opacity = "1";
    orb.style.opacity = "0";
    wake.style.opacity = "0";
    rimR.style.opacity = "0";
    rimG.style.opacity = "0";
    rimB.style.opacity = "0";
    disp.style.opacity = "0";

    const H = 44; // ต้องตรงกับ height ของ .dock-glass__pill ใน globals.css
    const INSET = 3; // ระยะห่างซ้าย/ขวาของ pill จากขอบแท็บ
    const ORB = 84; // ต้องตรงกับ --dock-orb ของ .dock-glass
    // ยอมให้วงกลมล้นขอบ dock ได้นิดหน่อย: แนวนอนเพื่อให้ไปถึงกลางแท็บสุดท้าย
    // (pill กว้าง ~65 → ครึ่งวง 42 จะเลยขอบ) แนวตั้งให้ "ไหล" ตามนิ้วออกนอกได้
    const OVER_X = 18;
    const OVER_Y = 34;

    // สปริงตัวหลัก: underdamped (ζ = 0.88) → ตามนิ้วช้าแล้วส่ายเข้าที่นิดหนึ่ง
    // = ความ "เหลว/หนืด" แบบ FluidGlass แต่เฟรมเรตอิสระ
    const K = 289; // ω² = 17²
    const C = 29.92; // 2ζω = 2 × 0.88 × 17
    // สปริงตัวตาม (wake): over-damped (ζ = 1.05) → ไม่ส่าย ตามหลังเสมอ = หางของเหลว
    const KW = 81; // 9²
    const CW = 18.9; // 2 × 1.05 × 9

    const main = { p: 0, v: 0 };
    const mainY = { p: 0, v: 0 };
    const tail = { p: 0, v: 0 };
    const tailY = { p: 0, v: 0 };

    let navH = 56;
    let pillW = 56;
    let restX = 0; // จุดกึ่งกลาง pill (พิกัดใน <nav>)
    let restY = 28; // จุดกึ่งกลางแนวตั้ง — pill ใช้ top:0 จึงต้องเป็น "กลาง" ไม่ใช่ offset
    let targetX = 0;
    let targetY = 28;
    let alpha = 0; // ความเข้มของ layer ของเหลว (wake/rim/warp) ตอนลาก
    let targetAlpha = 0;
    let dragging = false;
    let moved = false;
    let startX = 0;
    let startY = 0;
    let pressIdx = 0;
    let raf = 0;
    let last = 0;
    let clickTimer = 0;

    const clamp = (v: number, lo: number, hi: number) =>
      v < lo ? lo : v > hi ? hi : v;

    const tabs = () =>
      Array.from(nav.querySelectorAll<HTMLButtonElement>(".dock-glass__tab"));

    const centreOf = (i: number) => {
      const b = tabs()[i];
      if (!b) return restX;
      const nr = nav.getBoundingClientRect();
      const r = b.getBoundingClientRect();
      return r.left - nr.left + r.width / 2;
    };

    const activeTabIdx = () =>
      tabs().findIndex((b) => b.getAttribute("aria-current") === "page");

    // วัดกล่อง <nav> + ความกว้างแท็บจริง → pill ตรงแท็บทุกขนาดจอโดยไม่ต้อง
    // hard-code ความกว้าง (nav จัดกึ่งกลางจอ ขนาดเปลี่ยนตาม viewport)
    const measure = () => {
      const nr = nav.getBoundingClientRect();
      navH = nr.height;
      restY = navH / 2;
      const btn =
        nav.querySelector<HTMLButtonElement>('[aria-current="page"]') ??
        tabs()[0];
      if (btn) {
        const r = btn.getBoundingClientRect();
        pillW = Math.max(36, r.width - INSET * 2);
        pill.style.width = `${pillW}px`;
        // wake/orb เป็นวงกลมขนาดคงที่ (var(--dock-orb)) — ไม่ตั้งขนาดจาก JS
        restX = r.left - nr.left + r.width / 2;
      }
      return nr;
    };

    // เขียนผลลง DOM — เรียกจาก rAF, ตอน snap และตอน resize
    const render = () => {
      // squash & stretch ตามความเร็ว (จำกัดเพดานไม่ให้บิดเกิน) — หัวใจของ "ของเหลว"
      const spd = Math.min(Math.abs(main.v) / 3200, 0.25);
      // chromatic aberration: ขอบแดง/เขียว/น้าเงินเยื้องออกตามความเร็วการลาก
      // (การหักเหของสี — ยิ่งลากเร็ว สีแยกออกจากกันยิ่งชัด)
      const ca = 0.8 + spd * 16;

      // pill (แคปซูล) ยืด/บี้ชัด ๆ ตอนลาก แล้วจางหายไปให้วงกลมแทนที่
      const sx = 1 + spd * 0.8;
      const sy = 1 - spd * 0.42;
      pill.style.transform =
        `translate3d(${(main.p - pillW / 2).toFixed(2)}px,${(mainY.p - H / 2).toFixed(2)}px,0)` +
        ` scale(${sx.toFixed(3)},${sy.toFixed(3)})`;
      pill.style.opacity = (1 - alpha).toFixed(3);

      // orb (วงกลม) — ยืดบี้น้อยกว่า pill เพราะโจทย์คือ "ให้เป็นวงกลม"
      const osx = 1 + spd * 0.42;
      const osy = 1 - spd * 0.26;
      orb.style.transform =
        `translate3d(${(main.p - ORB / 2).toFixed(2)}px,${(mainY.p - ORB / 2).toFixed(2)}px,0)` +
        ` scale(${osx.toFixed(3)},${osy.toFixed(3)})`;
      orb.style.opacity = alpha.toFixed(3);

      // wake = หางของเหลวกลมตามหลัง (over-damped → ตามหลังเสมอ)
      wake.style.transform =
        `translate3d(${(tail.p - ORB / 2).toFixed(2)}px,${(tailY.p - ORB / 2).toFixed(2)}px,0)` +
        ` scale(${(0.5 + spd * 0.7).toFixed(3)})`;
      wake.style.opacity = (alpha * 0.36).toFixed(3);

      const rimA = Math.min(alpha * 1.5, 1).toFixed(3);
      rimR.style.opacity = rimA;
      rimG.style.opacity = rimA;
      rimB.style.opacity = rimA;
      // แยกสีแบบปริซึม: แดงเยื้องซ้าย / น้าเงินเยื้องขวา / เขียวขยายวงตรงกลาง
      rimR.style.transform = `translate3d(${(-ca * 1.3).toFixed(2)}px,0,0)`;
      rimG.style.transform = `scale(${(1 + ca * 0.0035).toFixed(4)})`;
      rimB.style.transform = `translate3d(${(ca * 1.3).toFixed(2)}px,0,0)`;

      // ชั้นหักเหของสี: จาง-เข้มตาม alpha และ "แยกสี" ตามความเร็ว (--sp 0→1)
      disp.style.opacity = Math.min(alpha * 1.15, 1).toFixed(3);
      disp.style.setProperty("--sp", Math.min(spd / 0.25, 1).toFixed(3));

      // บิดเฉพาะตอนลาก (toggle = no-op ถ้าสถานะเดิม → ไม่ repaint ซ้ำทุกเฟรม)
      pill.classList.toggle("is-fluid", alpha > 0.02);
    };

    const tick = (t: number) => {
      const dt = last ? Math.min((t - last) / 1000, 0.05) : 1 / 60;
      last = t;

      main.v += (K * (targetX - main.p) - C * main.v) * dt;
      main.p += main.v * dt;
      mainY.v += (K * (targetY - mainY.p) - C * mainY.v) * dt;
      mainY.p += mainY.v * dt;

      tail.v += (KW * (targetX - tail.p) - CW * tail.v) * dt;
      tail.p += tail.v * dt;
      tailY.v += (KW * (targetY - tailY.p) - CW * tailY.v) * dt;
      tailY.p += tailY.v * dt;

      alpha += (targetAlpha - alpha) * (1 - Math.exp(-9 * dt));
      if (alpha < 0.004) alpha = 0;

      render();

      // หยุด rAF เมื่อนิ่งแล้ว (ไม่กินแบตเตอรี่ตอนไม่ได้ลาก)
      const settled =
        !dragging &&
        alpha === 0 &&
        Math.abs(main.v) < 4 &&
        Math.abs(tail.v) < 5 &&
        Math.abs(mainY.v) < 4 &&
        Math.abs(tailY.v) < 4;
      if (settled) {
        main.v = 0;
        tail.v = 0;
        mainY.v = 0;
        tailY.v = 0;
        raf = 0;
        return;
      }
      raf = requestAnimationFrame(tick);
    };

    const startLoop = () => {
      if (!raf) {
        last = 0;
        raf = requestAnimationFrame(tick);
      }
    };

    // วาง pill นิ่ง ๆ ที่ตำแหน่งพัก (ตอน mount / resize / แท็บ active เปลี่ยน)
    const snap = () => {
      main.p = restX;
      main.v = 0;
      mainY.p = restY;
      mainY.v = 0;
      tail.p = restX;
      tail.v = 0;
      tailY.p = restY;
      tailY.v = 0;
      alpha = 0;
      targetAlpha = 0;
      targetX = restX;
      targetY = restY;
      render();
    };

    const sync = () => {
      measure();
      if (!dragging) snap();
    };
    syncRef.current = sync;
    sync();

    // แท็บที่จุดกึ่งกลางใกล้ x ที่สุด (พิกัดใน <nav>)
    const idxAtX = (localX: number) => {
      const nr = nav.getBoundingClientRect();
      let best = 0;
      let bestD = Infinity;
      tabs().forEach((b, i) => {
        const r = b.getBoundingClientRect();
        const d = Math.abs(r.left - nr.left + r.width / 2 - localX);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      });
      return best;
    };

    // กัน click ที่ browser ยิงหลังลาก (pointerdown/pointerup คนละแท็บ) ไม่ให้
    // นำทางซ้ำ/นำทางผิดแท็บ — ถอน listener ใน 400ms กันค้าง
    const onSwallowClick = (ev: Event) => {
      ev.preventDefault();
      ev.stopPropagation();
    };
    const suppressClick = () => {
      window.addEventListener("click", onSwallowClick, true);
      window.clearTimeout(clickTimer);
      clickTimer = window.setTimeout(() => {
        window.removeEventListener("click", onSwallowClick, true);
        clickTimer = 0;
      }, 400);
    };

    const onDown = (e: PointerEvent) => {
      if (reduce) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const nr = measure();
      dragging = true;
      moved = false;
      startX = e.clientX;
      startY = e.clientY;
      pressIdx = idxAtX(e.clientX - nr.left);
      targetAlpha = 1;
      targetX = clamp(e.clientX - nr.left, ORB / 2 - OVER_X, nr.width - ORB / 2 + OVER_X);
      targetY = clamp(e.clientY - nr.top, ORB / 2 - OVER_Y, nr.height - ORB / 2 + OVER_Y);
      startLoop();
    };

    const onMove = (e: PointerEvent) => {
      if (!dragging) return;
      if (Math.abs(e.clientX - startX) > 6 || Math.abs(e.clientY - startY) > 6) {
        moved = true;
      }
      const nr = nav.getBoundingClientRect();
      targetX = clamp(e.clientX - nr.left, ORB / 2 - OVER_X, nr.width - ORB / 2 + OVER_X);
      targetY = clamp(e.clientY - nr.top, ORB / 2 - OVER_Y, nr.height - ORB / 2 + OVER_Y);
    };

    const onUp = (e: PointerEvent) => {
      if (!dragging) return;
      dragging = false;
      targetAlpha = 0;
      targetY = restY;

      const nr = nav.getBoundingClientRect();
      const idx = moved ? idxAtX(e.clientX - nr.left) : pressIdx;

      if (moved && idx !== activeTabIdx() && MENU[idx]) {
        // ปล่อยนิ้วเหนือแท็บอื่น → เปลี่ยนแท็บเอง (browser ไม่ยิง click ของแท็บ
        // เพราะ pointerdown/up คนละ element) แล้วหยุดสปริงที่แท็บนั้นเลย
        restX = centreOf(idx);
        targetX = restX;
        suppressClick();
        go(MENU[idx].href);
      } else {
        // แตะเฉย ๆ: pill ตามไปแท็บที่กด แล้วปล่อยให้ click ของแท็บนำทางปกติ
        targetX = moved ? restX : centreOf(pressIdx);
      }
      startLoop();
    };

    nav.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    // ต้อง observe <html> ด้วย ไม่ใช่แค่ nav: ตำแหน่งของ nav เปลี่ยนได้โดยขนาดคงเดิม
    // (เช่น scrollbar ของ layout โผล่ → viewport แคบลง → nav ที่จัดกึ่งกลางเลื่อนตาม)
    const ro =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(sync) : null;
    ro?.observe(nav);
    ro?.observe(document.documentElement);
    // เลย์เอาต์นิ่งช้า (font/รูปโหลดเสร็จ) → sync อีกครั้งหลังโหลดครบ
    window.addEventListener("load", sync);

    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.clearTimeout(clickTimer);
      window.removeEventListener("click", onSwallowClick, true);
      nav.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
      window.removeEventListener("load", sync);
      nav.classList.remove("dock-glass--lens");
      ro?.disconnect();
      if (syncRef.current === sync) syncRef.current = null;
    };
  }, []);

  return (
    <div className="md:hidden">
      <nav ref={navRef} aria-label="เมนูหลัก" className="dock-glass">
        {/* filter defs ต้องอยู่ใน DOM จริง (ห้าม display:none) เหมือน #lg-refract
            ใน layout.tsx ไม่งั้นบาง browser จะไม่ resolve filter ให้ */}
        <svg
          aria-hidden="true"
          focusable="false"
          width="0"
          height="0"
          style={{ position: "absolute" }}
        >
          <defs>
            {/* คลื่นยาว (baseFrequency ต่ำ) + แอมพลิจูดน้อย (scale ต่ำ) → บิดนุ่ม
                Glow ให้เส้นรอบวงยังเท่ากันทั้งวง ไม่หยักเป็นหย่อม ๆ */}
            <filter
              id="dock-glass-warp"
              x="-40%"
              y="-40%"
              width="180%"
              height="180%"
              colorInterpolationFilters="sRGB"
            >
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.012 0.016"
                numOctaves="2"
                seed="71"
                result="noise"
              />
              <feGaussianBlur in="noise" stdDeviation="6" result="soft" />
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="6"
                xChannelSelector="R"
                yChannelSelector="G"
              />
            </filter>

            {/* คลื่นประกายในเนื้อแก้ว + "การหักเหของสี": แยกช่องสี R/G/B
                แล้วดิสเพลสคนละสเกล (แดงน้อยสุด → น้าเงินมากสุด เหมือนแก้วจริง)
                แล้ว screen กลับ — ได้ประกายที่มีขอบสีรุ้งในเนื้อแก้ว
                ใช้กับ "เนื้อใน" เท่านั้น จึงไม่ทําให้เส้นรอบวงหยัก */}
            <filter
              id="dock-glass-caustic"
              x="-30%"
              y="-30%"
              width="160%"
              height="160%"
              colorInterpolationFilters="sRGB"
            >
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.05 0.06"
                numOctaves="2"
                seed="53"
                result="noise"
              />
              <feGaussianBlur in="noise" stdDeviation="2.6" result="soft" />
              {/* red */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="3"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispR"
              />
              <feColorMatrix
                in="dispR"
                type="matrix"
                values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chR"
              />
              {/* green */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="6"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispG"
              />
              <feColorMatrix
                in="dispG"
                type="matrix"
                values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chG"
              />
              {/* blue */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="9"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispB"
              />
              <feColorMatrix
                in="dispB"
                type="matrix"
                values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0"
                result="chB"
              />
              <feBlend
                in="chR"
                in2="chG"
                mode="screen"
                result="chRG"
              />
              <feBlend in="chRG" in2="chB" mode="screen" />
            </filter>

            {/* เลนส์บิดเบี้ยว "ภาพหลังแก้ว": คนละหน้าที่กับ 2 ตัวบน
                - dock-glass-warp/caustic = บิด "แถบแสงที่เราวาดเอง" (filter:)
                - dock-glass-lens        = บิด "ภาพจริงที่อยู่ข้างหลังวงกลม"
                                           (ใช้ผ่าน backdrop-filter: ของ orb)

                ⚠️⚠️ backdrop-filter ต้องมี url() "ตัวเดียว" เท่านั้น
                   วัดแล้ว (harness + diff พิกเซล): ถ้ามี blur()/saturate() ปนอยู่
                   Chromium จะทิ้ง url() ทั้งตัว → ได้แค่ blur เฉย ๆ ไม่มีการบิด
                   (cfg sweep 5 ค่า baseFrequency + สลับลำดับ blur/lens → เหมือนเดิม)
                   ดังนั้นความฟุ้ง/อิ่มสี/ความสว่างของ "ฉากหลัง" ต้องทำในฟิลเตอร์นี้
                   ด้วย feGaussianBlur/feColorMatrix/feComponentTransfer แทน

                ลำดับ: เตรียมฉากหลัง (เบลอ→อิ่มสี→สว่าง) แล้วค่อยดิสเพลส
                (เบลอทีหลังจะทับรอยบิดให้หายไป)

                สนามดิสเพลส: feTurbulence คลื่นยาว (คาบ ~1/0.014 ≈ 70px ≈ 1 รอบ
                ต่อวง 84px) → blur ให้เป็นสนามนุ่ม → เป็น "โป่ง" ก้อนเดียว
                scale 28 → ดึงได้สูงสุด ±14px (≈ 17% ของเส้นผ่านศูนย์กลางวง)

                ⚠️ เปลี่ยน baseFrequency/numOctaves เมื่อไร → bump seed ด้วย
                ⚠️ feImage ใช้เป็นแผนที่ไม่ได้ (Chromium ให้ผล uniform) จึงต้อง
                   ใช้ feTurbulence เป็นตัวสร้างสนามแทน */}
            <filter
              id="dock-glass-lens"
              x="-20%"
              y="-20%"
              width="140%"
              height="140%"
              colorInterpolationFilters="sRGB"
            >
              {/* 1) ฉากหลังหลังผ่านแก้ว: เบลอ 4px (เทียบเท่า blur(7px) เดิมที่ตา
                    มองเพราะการบิดช่วยพรางรอยคม) → อิ่มสี 1.45 → สว่าง 1.18 */}
              <feGaussianBlur
                in="SourceGraphic"
                stdDeviation="4"
                result="bg0"
              />
              <feColorMatrix
                in="bg0"
                type="saturate"
                values="1.45"
                result="bg1"
              />
              <feComponentTransfer in="bg1" result="bg2">
                <feFuncR type="linear" slope="1.18" />
                <feFuncG type="linear" slope="1.18" />
                <feFuncB type="linear" slope="1.18" />
              </feComponentTransfer>
              {/* 2) สนามดิสเพลส (ต้อง "ไม่สม่ำเสมอ" ในวง — ถ้าสนามนิ่งเกินไป
                    จะกลายเป็นเลื่อนภาพทั้งวง ซึ่งมองไม่เห็นบนฉากหลังที่เบลอ) */}
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.014 0.017"
                numOctaves="2"
                seed="93"
                result="bulge"
              />
              <feGaussianBlur in="bulge" stdDeviation="9" result="soft" />
              <feDisplacementMap
                in="bg2"
                in2="soft"
                scale="28"
                xChannelSelector="R"
                yChannelSelector="G"
              />
            </filter>
          </defs>
        </svg>

        {/* wake มาก่อน orb/pill → วาดใต้ทั้งคู่ (หางของเหลวที่ตามหลัง) */}
        <div ref={wakeRef} className="dock-glass__wake" aria-hidden="true" />

        {/* pill = ตัวแก้วตอนพัก (แคปซูล) — ตอนลากจะจางหายไปให้ orb แทนที่ */}
        <div ref={pillRef} className="dock-glass__pill" aria-hidden="true" />

        {/* orb = วงกลมแก้วตอนลาก: ขอบฟุ้ง + หักเหฉากหลัง + แถบแสงหักเห/ประกาย
            + 2 rim สีเพี้ยน (rim อยู่ข้างในวง → ใช้ border-radius: inherit = 50%) */}
        <span ref={orbRef} className="dock-glass__orb" aria-hidden="true">
          <span className="dock-glass__orb-sheen" />
          <span className="dock-glass__orb-caustic" />
          {/* แยกสีที่ขอบวง: แดงในสุด → เขียวกึ่งกลาง → น้าเงินนอกสุด */}
          <span ref={dispRef} className="dock-glass__disperse">
            <span className="dock-glass__disperse--r" />
            <span className="dock-glass__disperse--g" />
            <span className="dock-glass__disperse--b" />
          </span>
          <span ref={rimRRef} className="dock-glass__rim dock-glass__rim--r" />
          <span ref={rimGRef} className="dock-glass__rim dock-glass__rim--g" />
          <span ref={rimBRef} className="dock-glass__rim dock-glass__rim--b" />
        </span>

        {MENU.map((l, i) => (
          <button
            key={l.href}
            type="button"
            className="dock-glass__tab"
            aria-current={i === activeIdx ? "page" : undefined}
            style={{
              color: i === activeIdx ? "#7cc4ff" : "rgba(148, 163, 184, 0.92)",
            }}
            onClick={() => go(l.href)}
          >
            <span className="dock-glass__icon">{ICON[l.icon]}</span>
            <span className="dock-glass__label">{l.label}</span>
          </button>
        ))}
      </nav>
    </div>
  );
}
