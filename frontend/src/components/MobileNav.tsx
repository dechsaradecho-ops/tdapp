"use client";

import { useEffect, useState } from "react";

/** Mobile navigation — bottom tab bar for < md screens.
 *
 * Desktop (md+) renders nothing; the inline nav in the header stays.
 * Mobile: fixed bottom bar with 4 primary tabs + "เพิ่มเติม" that opens a
 * bottom sheet with the remaining pages. Closes on navigation (link click)
 * or backdrop tap.
 */
const PRIMARY = [
  { href: "/", label: "หน้าหลัก", icon: "🏠" },
  { href: "/market", label: "ตลาด", icon: "📈" },
  { href: "/signals", label: "สัญญาณ", icon: "⚡" },
  { href: "/monitor", label: "มอนิเตอร์", icon: "📊" },
];

const MORE = [
  { href: "/logs", label: "Logs", icon: "📜" },
  { href: "/signal-logs", label: "Signal Logs", icon: "🗂️" },
  { href: "/risk", label: "Risk", icon: "🛡️" },
  { href: "/performance", label: "Performance", icon: "🎯" },
  { href: "/settings", label: "Settings", icon: "⚙️" },
];

export default function MobileNav() {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("/");

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Lock body scroll while the sheet is open.
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  const isActive = (href: string) =>
    href === "/" ? path === "/" : path.startsWith(href);

  const tabClass = (href: string) =>
    `flex flex-col items-center justify-center gap-0.5 min-h-[56px] text-[11px] leading-tight active:bg-white/10 rounded-xl ${
      isActive(href) ? "text-accent font-semibold" : "text-slate-400"
    }`;

  return (
    <div className="md:hidden">
      {/* Bottom tab bar */}
      <nav
        aria-label="เมนูหลัก"
        className="fixed bottom-0 inset-x-0 z-40 border-t safe-bottom"
        style={{
          background: "rgba(10, 10, 14, 0.72)",
          WebkitBackdropFilter: "blur(24px) saturate(160%)",
          backdropFilter: "blur(24px) saturate(160%)",
          borderTopColor: "rgba(255,255,255,0.14)",
        }}
      >
        <div className="grid grid-cols-5 px-1">
          {PRIMARY.map((l) => (
            <a key={l.href} href={l.href} className={tabClass(l.href)}>
              <span className="text-xl">{l.icon}</span>
              {l.label}
            </a>
          ))}
          <button
            onClick={() => setOpen(true)}
            aria-label="เปิดเมนูเพิ่มเติม"
            aria-expanded={open}
            className={tabClass("__more__")}
          >
            <span className="text-xl">⋯</span>
            เพิ่มเติม
          </button>
        </div>
      </nav>

      {/* Bottom sheet — remaining pages */}
      {open && (
        <div className="fixed inset-0 z-50" onClick={() => setOpen(false)}>
          <div className="absolute inset-0 bg-black/60" style={{ WebkitBackdropFilter: "blur(6px)", backdropFilter: "blur(6px)" }} />
          <div
            className="absolute bottom-0 inset-x-0 border-t rounded-t-3xl shadow-2xl safe-bottom"
            style={{
              background: "rgba(18, 18, 24, 0.82)",
              WebkitBackdropFilter: "blur(28px) saturate(160%)",
              backdropFilter: "blur(28px) saturate(160%)",
              borderTopColor: "rgba(255,255,255,0.16)",
            }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 pt-3 pb-2">
              <span className="font-bold text-sm">เมนูทั้งหมด</span>
              <button
                onClick={() => setOpen(false)}
                aria-label="ปิดเมนู"
                className="w-11 h-11 -mr-2 flex items-center justify-center rounded-lg text-xl text-slate-400 active:bg-slate-800"
              >
                ✕
              </button>
            </div>
            <div className="grid grid-cols-3 gap-2 p-3 pt-0">
              {MORE.map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  onClick={() => setOpen(false)}
                  className={`flex flex-col items-center gap-1 rounded-xl border px-2 py-3 text-xs text-center min-h-[64px] justify-center active:bg-white/10 ${
                    isActive(l.href)
                      ? "border-accent/60 bg-accent/15 text-accent font-semibold"
                      : "border-white/10 bg-white/[0.04] text-slate-300"}
                  }`}
                >
                  <span className="text-xl">{l.icon}</span>
                  {l.label}
                </a>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
