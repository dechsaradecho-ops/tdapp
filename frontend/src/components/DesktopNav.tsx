"use client";

import { useEffect, useState } from "react";

/** Desktop navigation (md+) — floating "Liquid Glass" pill navbar.
 * อ้างอิงสไตล์ codefronts "Tailwind Liquid Glass Navbar":
 * backdrop-blur 20px + bg-white/10 + ring white/20 + rounded-full
 * ชิดขวาจอ · brand ซ้ายใน pill · ลิงก์ถัดไป · icon monotone SVG (ไม่ใช้ emoji)
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
      className="hidden md:flex sticky z-30 justify-end px-4 mb-6"
      style={{ top: "calc(env(safe-area-inset-top, 0px) + 0.9rem)" }}
    >
      <nav
        aria-label="Primary"
        className="lg-refract flex items-center gap-2 rounded-full border px-2.5 py-1.5"
        style={{
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
          className="flex items-center gap-1.5 pl-2 pr-1 text-sm font-semibold text-slate-200"
        >
          <svg
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.8"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polyline points="22 7 13.5 15.5 8.5 10.5 2 17" />
            <polyline points="16 7 22 7 22 13" />
          </svg>
          AI Trading
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
        {/* ชิดขวา — ไม่ต้องมี spacer สมดุลซ้าย */}
      </nav>
    </header>
  );
}
