"use client";

import { useEffect, useRef, useState } from "react";

/** Mobile navigation — iOS 26 "Liquid Glass" bottom dock (< md screens).
 *
 * Desktop (md+) renders nothing; the inline nav in the header stays.
 * Mobile: floating glass dock 5 แท็บเท่ากัน + เม็ดแก้วเหลว (liquid pill)
 * เลื่อนตามแท็บ active ด้วย spring easing — เลียนแบบ Lottie
 * "iOS 26 inspired tab menu" ด้วย CSS ล้วน (ไม่โหลด lottie-web ~250KB
 * และ pill ใช้ธีมแก้วเดิมของแอป) — สไตล์ pill อยู่ที่ .dock-pill ใน globals.css
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
  const trackRef = useRef<HTMLDivElement>(null);
  const [pill, setPill] = useState<{ x: number; w: number; anim: boolean } | null>(
    null,
  );

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const activeIdx = MENU.findIndex((l) =>
    l.href === "/" ? path === "/" : path.startsWith(l.href),
  );

  // วัดตำแหน่งจริงของแท็บ active → ย้ายเม็ดแก้วไปทับ
  // วัดซ้ำเมื่อ font swap / resize (ความกว้างแท็บเปลี่ยน)
  // ครั้งแรก (pill ยัง null) ไม่ animate — กันเม็ดบินจากซ้ายสุดตอนโหลดหน้า
  useEffect(() => {
    const measure = () => {
      const el =
        trackRef.current?.querySelectorAll<HTMLAnchorElement>("a")[activeIdx];
      if (!el) return;
      setPill((p) => ({ x: el.offsetLeft, w: el.offsetWidth, anim: p !== null }));
    };
    measure();
    document.fonts?.ready?.then(measure).catch(() => {});
    window.addEventListener("resize", measure);
    return () => window.removeEventListener("resize", measure);
  }, [activeIdx]);

  return (
    <div className="md:hidden">
      {/* Bottom tab bar — iOS 26 liquid glass dock (จัดกึ่งกลางจอ) */}
      <nav
        aria-label="เมนูหลัก"
        className="lg-refract fixed z-40 border"
        style={{
          left: "0.75rem",
          right: "0.75rem",
          bottom: "calc(env(safe-area-inset-bottom, 0px) + 0.65rem)",
          maxWidth: "28rem",
          marginInline: "auto",
          background: "rgba(255, 255, 255, 0.06)",
          WebkitBackdropFilter: "blur(8px) saturate(160%)",
          backdropFilter: "blur(8px) saturate(160%)",
          borderColor: "rgba(255,255,255,0.16)",
          borderRadius: "1.4rem",
          boxShadow:
            "0 8px 32px rgba(0, 0, 0, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.14)",
        }}
      >
        {/* เม็ดแก้วเหลว — เลื่อนตามแท็บ active (spring transition ใน .dock-pill) */}
        <span
          aria-hidden="true"
          className="dock-pill"
          style={
            pill
              ? {
                  transform: `translateX(${pill.x}px)`,
                  width: pill.w,
                  opacity: 1,
                  ...(pill.anim ? {} : { transition: "none" }),
                }
              : { opacity: 0 }
          }
        />
        <div ref={trackRef} className="flex px-1 py-0.5">
          {MENU.map((l, i) => {
            const active = i === activeIdx;
            return (
              <a
                key={l.href}
                href={l.href}
                aria-current={active ? "page" : undefined}
                className={`flex flex-1 flex-col items-center justify-center gap-0.5 min-h-[56px] px-1 text-[11px] leading-tight rounded-xl active:bg-white/10 transition-colors ${
                  active ? "text-accent font-semibold" : "text-slate-400"
                }`}
              >
                <span
                  className={`transition-transform duration-300 ${
                    active ? "scale-110 -translate-y-px" : ""
                  }`}
                >
                  {ICON[l.icon]}
                </span>
                {l.label}
              </a>
            );
          })}
        </div>
      </nav>
    </div>
  );
}
