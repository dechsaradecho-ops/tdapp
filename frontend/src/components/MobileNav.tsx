"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { LiquidGlassNav } from "webgl-liquid-glass";
import type { NavItem } from "webgl-liquid-glass";

/** Mobile navigation — bottom dock (< md screens)
 *
 * ใช้ framework `webgl-liquid-glass` (github.com/clayharmon/webgl-liquid-glass)
 * แทน dock + เม็ดแก้ว .dock-pill ที่เขียนด้วย CSS เองเมื่อก่อน:
 * <LiquidGlassNav> = CSS backdrop-filter + WebGL canvas (specular highlight,
 * chromatic aberration ที่ขอบ pill, motion shimmer) + spring physics
 * (ลาก pill ไปแท็บอื่น / กดค้างเพื่อพองเป็นบับเบิลได้)
 *
 * Desktop (md+) renders nothing; the inline nav in the header stays.
 * ความกว้าง/ระยะ padding ของแท็บถูกปรับให้พอดีจอมือถือที่ .dock-glass ใน globals.css
 * (คอมโพเนนต์ตั้ง padding ด้วย inline style → override ต้องใช้ !important)
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

export default function MobileNav() {
  const [path, setPath] = useState("/");
  const wrapRef = useRef<HTMLDivElement>(null);
  // เลเยอร์ "เลนส์เหลว" (FluidGlass-style) — ตรรกะทั้งหมดอยู่ใน useEffect ด้านล่าง
  const layerRef = useRef<HTMLDivElement>(null);
  const lensRef = useRef<HTMLDivElement>(null);
  const wakeRef = useRef<HTMLDivElement>(null);
  const caRRef = useRef<HTMLSpanElement>(null);
  const caBRef = useRef<HTMLSpanElement>(null);

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // a11y: package ยังไม่ตั้ง aria-label ให้ <nav> ของตัวเอง (0.3.0 ที่ยังไม่
  // publish บน npm เพิ่งเพิ่มส่วนนี้) → เติมเองหลัง mount ให้เทียบเท่า dock เดิม
  useEffect(() => {
    wrapRef.current
      ?.querySelector("nav")
      ?.setAttribute("aria-label", "เมนูหลัก");
  }, []);

  const active = MENU.find((l) =>
    l.href === "/" ? path === "/" : path.startsWith(l.href),
  );
  const activeIcon = active?.icon ?? "home";

  // ---- Fluid drag lens (FluidGlass-style, ทำเอง ไม่ใช้ three/.glb) ---------
  // ล้อตามสิ่งที่ผู้ใช้อ้าง (reactbits.dev/components/fluid-glass → "แก้วเหลว") แต่
  // FluidGlass ต้องใช้ three + @react-three/fiber + drei (peer React 19) และไฟล์
  // .glb ที่ไม่ได้แจกมาใน registry — จะทำให้ bundle โต ~500KB และบังคับอัปเกรด React
  // จึงทำเฉพาะ "ความรู้สึก" ที่เป็นหัวใจ: แก้วที่ตามนิ้วแบบหนืด (spring underdamped)
  // + ยืด/บี้ตามความเร็ว + ขอบ chromatic aberration
  //
  // ทำไมไม่แตะ DOM ของ package: pill ถูกวาดด้วย WebGL canvas ล้วน (ไม่มี element
  // ให้ขยับ) และ package ยึด pointer ไว้เอง → ชั้นนี้อยู่ "นอก" <nav> ที่ z-index 41
  // + pointer-events: none จึงได้ผลโดยไม่เสี่ยงทำ drag เดิมพัง
  useEffect(() => {
    const nav = wrapRef.current?.querySelector("nav") as HTMLElement | null;
    const layer = layerRef.current;
    const lens = lensRef.current;
    const wake = wakeRef.current;
    const caR = caRRef.current;
    const caB = caBRef.current;
    if (!nav || !layer || !lens || !wake || !caR || !caB) return;

    // ผู้ใช้ที่ปิดอนิเมชัน → ไม่ต้องมีเลนส์เลย (dock ยังทำงานปกติทุกอย่าง)
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    // ต้องตรงกับ width/height ของ .dock-liquid__lens ใน globals.css
    const W = 84;
    const H = 48;

    // สปริงตัวหลัก: underdamped (ζ = 0.88) → ตามนิ้วช้ากว่า pill แล้วส่ายคืนตัว
    // นิดหนึ่ง = ความ "เหลว/หนืด" แบบ FluidGlass แต่เฟรมเรตอิสระ
    const K = 289; // ω² = 17²
    const C = 29.92; // 2ζω = 2 * 0.88 * 17
    // สปริงตัวตาม (wake): over-damped (ζ = 1.05) → ไม่ส่าย ตามหลังเสมอ = หางของเหลว
    const KW = 81; // 9²
    const CW = 18.9; // 2 * 1.05 * 9

    const main = { p: 0, v: 0 };
    const mainY = { p: 0, v: 0 };
    const tail = { p: 0, v: 0 };
    const tailY = { p: 0, v: 0 };

    let rect = nav.getBoundingClientRect();
    let restX = rect.width / 2;
    let targetX = restX;
    let targetY = rect.height / 2;
    let alpha = 0;
    let targetAlpha = 0;
    let dragging = false;
    let raf = 0;
    let last = 0;

    const clamp = (v: number, lo: number, hi: number) =>
      v < lo ? lo : v > hi ? hi : v;

    // ตำแหน่งพัก = กลางแท็บที่ active (วัดจาก <button> จริงของ package)
    const measureRest = () => {
      const btns = nav.querySelectorAll("button");
      const i = Math.max(0, MENU.findIndex((m) => m.icon === activeIcon));
      const b = btns[i] ?? btns[0];
      if (!b) return;
      const r = b.getBoundingClientRect();
      restX = r.left - rect.left + r.width / 2;
      if (!dragging) targetX = restX;
    };

    // ให้ layer ทับกล่อง <nav> พอดี — วัดจริง จึงไม่ผูกกับ CSS ของ package
    // (bottom/safe-area เปลี่ยนเมื่อหมุนจอหรือ URL bar โผล่ → sync ใหม่)
    const sync = () => {
      rect = nav.getBoundingClientRect();
      layer.style.left = `${rect.left}px`;
      layer.style.top = `${rect.top}px`;
      layer.style.width = `${rect.width}px`;
      layer.style.height = `${rect.height}px`;
      targetY = rect.height / 2;
      measureRest();
      if (!dragging) {
        main.p = restX;
        mainY.p = targetY;
        tail.p = restX;
        tailY.p = targetY;
      }
    };
    sync();

    const startLoop = () => {
      if (!raf) {
        last = 0;
        raf = requestAnimationFrame(tick);
      }
    };

    const onDown = (e: PointerEvent) => {
      rect = nav.getBoundingClientRect();
      dragging = true;
      targetAlpha = 1;
      targetX = clamp(e.clientX - rect.left, W / 2 - 8, rect.width - W / 2 + 8);
      targetY = clamp(e.clientY - rect.top, H / 2, rect.height - H / 2);
      startLoop();
    };

    const onMove = (e: PointerEvent) => {
      if (!dragging) return;
      targetX = clamp(e.clientX - rect.left, W / 2 - 8, rect.width - W / 2 + 8);
      targetY = clamp(e.clientY - rect.top, H / 2, rect.height - H / 2);
    };

    const onUp = () => {
      if (!dragging) return;
      dragging = false;
      targetAlpha = 0;
      targetX = restX;
      targetY = rect.height / 2;
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

      // squash & stretch ตามความเร็ว (จำกัดเพดานไม่ให้บิดเกิน) — หัวใจของ "ของเหลว"
      const spd = Math.min(Math.abs(main.v) / 3200, 0.25);
      const sx = 1 + spd * 0.8;
      const sy = 1 - spd * 0.42;

      // chromatic aberration: ขอบแดง/น้ำเงินเยื้องออกตามความเร็วการลาก
      const ca = (0.6 + spd * 11).toFixed(2);
      caR.style.transform = `translate3d(-${ca}px,0,0)`;
      caB.style.transform = `translate3d(${ca}px,0,0)`;

      lens.style.transform =
        `translate3d(${(main.p - W / 2).toFixed(2)}px,${(mainY.p - H / 2).toFixed(2)}px,0)` +
        ` scale(${sx.toFixed(3)},${sy.toFixed(3)})`;
      lens.style.opacity = alpha.toFixed(3);

      wake.style.transform = `translate3d(${(tail.p - W / 2).toFixed(2)}px,${(tailY.p - H / 2).toFixed(2)}px,0) scale(0.62)`;
      wake.style.opacity = (alpha * 0.32).toFixed(3);

      // หยุด rAF เมื่อนิ่งแล้ว (ไม่กินแบตเตอรี่ตอนไม่ได้ลาก)
      const settled =
        !dragging &&
        alpha === 0 &&
        Math.abs(main.p - restX) < 0.6 &&
        Math.abs(main.v) < 6 &&
        Math.abs(tail.p - restX) < 1.5 &&
        Math.abs(tail.v) < 6;
      if (settled) {
        lens.style.opacity = "0";
        wake.style.opacity = "0";
        raf = 0;
        return;
      }
      raf = requestAnimationFrame(tick);
    };

    // capture + passive: อ่านตำแหน่งนิ้วเท่านั้น ไม่ block และไม่แย่ง pointer
    // ของ package (drag/เปลี่ยนแท็บยังเป็นของ package ทั้งหมด)
    nav.addEventListener("pointerdown", onDown, { capture: true, passive: true });
    window.addEventListener("pointermove", onMove, { capture: true, passive: true });
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    // ต้อง observe <html> ด้วย ไม่ใช่แค่ nav: ตำแหน่งของ nav เปลี่ยนได้โดยขนาดคงเดิม
    // (เช่น scrollbar ของ layout โผล่ → viewport แคบลง 4px → nav ที่จัดกึ่งกลางเลื่อนตาม)
    // ถ้าดูแค่ขนาดของ nav จะไม่ sync → เลนส์เยื้องจากแท็บจริง
    const ro =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(sync) : null;
    ro?.observe(nav);
    ro?.observe(document.documentElement);
    // เลย์เอาต์นิ่งช้า (font/รูปโหลดเสร็จ) → sync อีกครั้งหลังโหลดครบ
    window.addEventListener("load", sync);

    return () => {
      if (raf) cancelAnimationFrame(raf);
      nav.removeEventListener("pointerdown", onDown, true);
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("pointerup", onUp, true);
      window.removeEventListener("pointercancel", onUp, true);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
      window.removeEventListener("load", sync);
      ro?.disconnect();
    };
  }, [activeIcon]);

  // ต้อง useMemo: package ใช้ `items` เป็น dependency ของ useCallback
  // (measureAndTarget) ถ้าสร้าง array ใหม่ทุก render จะสั่งวัด/ย้าย pill ซ้ำ
  const items = useMemo<NavItem[]>(
    () => MENU.map((l) => ({ id: l.icon, label: l.label, icon: ICON[l.icon] })),
    [],
  );

  // เปลี่ยนแท็บ = นำทางจริง (static export ไม่มี Next router → ใช้ location)
  const onChange = (id: string) => {
    const target = MENU.find((l) => l.icon === id);
    if (!target || target.href === path) return;
    window.location.assign(target.href);
  };

  return (
    <div ref={wrapRef} className="md:hidden">
      {/* Bottom dock — จัดกึ่งกลางจอ, fixed (สไตล์แก้ว/เงา/ตำแหน่งมาจาก style
          ที่ spread ทับค่าดีฟอลต์ของ package ได้ทั้งหมด) */}
      <LiquidGlassNav
        items={items}
        activeItem={active?.icon ?? "home"}
        onItemChange={onChange}
        activeColor="#7cc4ff"
        inactiveColor="rgba(148, 163, 184, 0.92)"
        className="dock-glass"
        style={{
          // ค่าดีฟอลต์ของ package = bottom 24px + z-index 9999 (ลอยทับ modal
          // z-50 / GlassSelect z-48) → ลด z กลับเป็น 40 เท่า dock เดิม เพื่อให้
          // AuthGate overlay (z-50) ยังคลุม dock ตอนล็อก PIN และ bottom sheet
          // ยังเปิดทับได้
          bottom: "calc(env(safe-area-inset-bottom, 0px) + 0.75rem)",
          width: "min(28rem, calc(100vw - 1.5rem))",
          zIndex: 40,
          background: "rgba(255, 255, 255, 0.08)",
          WebkitBackdropFilter: "blur(20px) saturate(160%)",
          backdropFilter: "blur(20px) saturate(160%)",
          boxShadow:
            "0 8px 32px rgba(0, 0, 0, 0.35), inset 0 0 0 0.5px rgba(255, 255, 255, 0.2), inset 0 1px 0 rgba(255, 255, 255, 0.16)",
          // package ตั้ง touch-action: none → ลากนิ้วจากโซน dock แล้วหน้าไม่เลื่อน
          // ตั้ง pan-y แทน: เลื่อนหน้าจอแนวตั้งได้ (คนมักปัดจากก้นจอ) แต่แนว
          // horizontal ยังถูกกันไว้ให้ drag เปลี่ยนแท็บของ component ทำงาน
          touchAction: "pan-y",
        }}
      />

      {/* เลนส์เหลว (FluidGlass-style) — อยู่ "นอก" <nav> ที่ z-index 41
          (สูงกว่า dock 40 แต่ต่ำกว่า GlassSelect 48 / AuthGate 50) และ
          pointer-events: none ทั้งชั้น → ไม่บังการแตะของ dock เลย
          ห้ามใช้ backdrop-filter: url() ที่นี่ — Samsung Internet render dock พัง
          (ดูบทเรียนใน globals.css) จึงบิด artwork ของเลนส์เองด้วย filter: url() */}
      <div
        ref={layerRef}
        aria-hidden="true"
        className="dock-liquid md:hidden"
        style={{
          position: "fixed",
          left: 0,
          top: 0,
          width: 0,
          height: 0,
          zIndex: 41,
          pointerEvents: "none",
        }}
      >
        {/* filter defs ต้องอยู่ใน DOM จริง (ห้าม display:none) — เหมือน #lg-refract
            ใน layout.tsx ไม่งั้นบาง browser จะไม่ resolve filter ให้ */}
        <svg
          aria-hidden="true"
          focusable="false"
          width="0"
          height="0"
          style={{ position: "absolute" }}
        >
          <defs>
            <filter
              id="dock-liquid-warp"
              x="-50%"
              y="-80%"
              width="200%"
              height="260%"
              colorInterpolationFilters="sRGB"
            >
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.016 0.022"
                numOctaves="2"
                seed="7"
                result="noise"
              />
              <feGaussianBlur in="noise" stdDeviation="1.2" result="soft" />
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="7"
                xChannelSelector="R"
                yChannelSelector="G"
              />
            </filter>
          </defs>
        </svg>
        {/* wake ก่อน → อยู่ใต้เลนส์หลักเสมอ (หางของเหลวที่ตามหลัง) */}
        <div ref={wakeRef} className="dock-liquid__wake" />
        <div ref={lensRef} className="dock-liquid__lens">
          <span ref={caRRef} className="dock-liquid__ca dock-liquid__ca--r" />
          <span ref={caBRef} className="dock-liquid__ca dock-liquid__ca--b" />
        </div>
      </div>
    </div>
  );
}
