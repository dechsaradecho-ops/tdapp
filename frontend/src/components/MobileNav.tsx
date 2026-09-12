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
    </div>
  );
}
