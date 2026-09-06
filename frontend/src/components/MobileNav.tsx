"use client";

import { useEffect, useRef, useState } from "react";

/** Mobile navigation — bottom tab bar for < md screens.
 *
 * Desktop (md+) renders nothing; the inline nav in the header stays.
 * Mobile: fixed bottom bar with ALL pages in one horizontally swipeable row
 * (ปัดซ้าย-ขวา) — ไม่มีปุ่ม "เพิ่มเติม"/bottom sheet แล้ว
 * แท็บที่ active จะถูกเลื่อนมากึ่งกลางอัตโนมัติเมื่อเปลี่ยนหน้า
 */
const MENU = [
  { href: "/", label: "หน้าหลัก", icon: "🏠" },
  { href: "/market", label: "ตลาด", icon: "📈" },
  { href: "/signals", label: "สัญญาณ", icon: "⚡" },
  { href: "/monitor", label: "มอนิเตอร์", icon: "📊" },
  { href: "/logs", label: "Logs", icon: "📜" },
  { href: "/signal-logs", label: "Signal Logs", icon: "🗂️" },
  { href: "/risk", label: "Risk", icon: "🛡️" },
  { href: "/performance", label: "Performance", icon: "🎯" },
  { href: "/settings", label: "Settings", icon: "⚙️" },
];

export default function MobileNav() {
  const [path, setPath] = useState("/");
  const scrollRef = useRef<HTMLDivElement>(null);

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // สไลด์แถวให้แท็บ active อยู่กึ่งกลาง
  // ใช้ scrollLeft ตรง ๆ (ไม่ใช่ scrollIntoView/scrollTo smooth) — เชื่อถือได้ทุก browser
  // (smooth ถูก disable เมื่อ prefers-reduced-motion ทำให้ไม่เลื่อนเลยบางเครื่อง)
  // และ clamp เอง (จบแถวซ้าย/ขวา ให้ยึดขอบ ไม่พยายามกลางเกิน max)
  useEffect(() => {
    const sc = scrollRef.current;
    const el = sc?.querySelector<HTMLElement>('[data-active="true"]');
    if (!sc || !el) return;
    const raf = requestAnimationFrame(() => {
      sc.scrollLeft = Math.max(0, el.offsetLeft + el.offsetWidth / 2 - sc.clientWidth / 2);
    });
    return () => cancelAnimationFrame(raf);
  }, [path]);

  const isActive = (href: string) =>
    href === "/" ? path === "/" : path.startsWith(href);

  const tabClass = (href: string) =>
    `flex flex-col items-center justify-center gap-0.5 min-h-[56px] min-w-[64px] px-1 shrink-0 text-[11px] leading-tight active:bg-white/10 rounded-xl ${
      isActive(href) ? "text-accent font-semibold" : "text-slate-400"
    }`;

  return (
    <div className="md:hidden">
      {/* Bottom tab bar — floating rounded glass dock */}
      <nav
        aria-label="เมนูหลัก"
        className="lg-refract fixed z-40 border"
        style={{
          left: "0.75rem",
          right: "0.75rem",
          bottom: "calc(env(safe-area-inset-bottom, 0px) + 0.65rem)",
          background: "rgba(255, 255, 255, 0.06)",
          WebkitBackdropFilter: "blur(8px) saturate(160%)",
          backdropFilter: "blur(8px) saturate(160%)",
          borderColor: "rgba(255,255,255,0.16)",
          borderRadius: "1.4rem",
          boxShadow:
            "0 8px 32px rgba(0, 0, 0, 0.32), inset 0 1px 0 rgba(255, 255, 255, 0.14)",
        }}
      >
        {/* สไลด์ซ้ายขวา — ทุกหน้าในแถวเดียว (no-scrollbar util ใน globals.css) */}
        <div
          ref={scrollRef}
          className="no-scrollbar flex overflow-x-auto px-1 py-0.5"
          style={{ overscrollBehaviorX: "contain" }}
        >
          {MENU.map((l) => (
            <a
              key={l.href}
              href={l.href}
              data-active={isActive(l.href) || undefined}
              className={tabClass(l.href)}
            >
              <span className="text-xl">{l.icon}</span>
              {l.label}
            </a>
          ))}
        </div>
      </nav>
    </div>
  );
}
