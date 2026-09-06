"use client";

import { useEffect, useState } from "react";

/** Desktop navigation (md+) — floating "Liquid Glass" pill navbar.
 * อ้างอิงสไตล์ codefronts "Tailwind Liquid Glass Navbar":
 * backdrop-blur 20px + bg-white/10 + ring white/20 + rounded-full
 * brand ซ้าย · ลิงก์กึ่งกลาง · spacer ขวา (grid 1fr auto 1fr ให้ลิงก์ center จริง)
 * มือถือ (< md) ไม่แสดง — ใช้ MobileNav dock แทน
 */
const LINKS = [
  { href: "/", label: "Dashboard" },
  { href: "/signals", label: "Signals" },
  { href: "/monitor", label: "Monitor" },
  { href: "/logs", label: "Logs" },
  { href: "/settings", label: "Settings" },
];

export default function DesktopNav() {
  const [path, setPath] = useState("/");

  // Track current path so the active link is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const isActive = (href: string) =>
    href === "/" ? path === "/" : path.startsWith(href);

  return (
    <header
      className="hidden md:block sticky z-30 px-4"
      style={{ top: "calc(env(safe-area-inset-top, 0px) + 0.9rem)" }}
    >
      <nav
        aria-label="Primary"
        className="lg-refract mx-auto grid grid-cols-[1fr_auto_1fr] items-center gap-2 rounded-full border px-2.5 py-1.5"
        style={{
          maxWidth: "46rem",
          background: "rgba(255, 255, 255, 0.08)",
          WebkitBackdropFilter: "blur(20px) saturate(160%)",
          backdropFilter: "blur(20px) saturate(160%)",
          borderColor: "rgba(255,255,255,0.20)",
          boxShadow:
            "0 8px 32px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.16)",
        }}
      >
        <a
          href="/"
          className="justify-self-start flex items-center gap-1.5 pl-2 text-sm font-semibold text-slate-200"
        >
          📈 AI Trading
        </a>
        <div className="flex items-center gap-0.5">
          {LINKS.map((l) => {
            const active = isActive(l.href);
            return (
              <a
                key={l.href}
                href={l.href}
                aria-current={active ? "page" : undefined}
                className={`rounded-full px-3.5 py-1.5 text-sm transition-colors ${
                  active
                    ? "bg-white/15 text-white font-medium shadow-[inset_0_1px_0_rgba(255,255,255,0.25)]"
                    : "text-slate-300 hover:bg-white/10 hover:text-white"
                }`}
              >
                {l.label}
              </a>
            );
          })}
        </div>
        {/* spacer ขวา — สมดุลกับ brand ซ้าย ให้กลุ่มลิงก์กึ่งกลางจริง */}
        <span aria-hidden="true" className="justify-self-end pr-2" />
      </nav>
    </header>
  );
}
