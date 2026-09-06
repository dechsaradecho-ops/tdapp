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
const MENU = [
  { href: "/", label: "หน้าหลัก", icon: "🏠" },
  { href: "/signals", label: "สัญญาณ", icon: "⚡" },
  { href: "/monitor", label: "มอนิเตอร์", icon: "📊" },
  { href: "/logs", label: "Logs", icon: "📜" },
  { href: "/settings", label: "ตั้งค่า", icon: "⚙️" },
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
                  className={`text-xl transition-transform duration-300 ${
                    active ? "scale-110 -translate-y-px" : ""
                  }`}
                >
                  {l.icon}
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
