"use client";

import { useEffect, useState } from "react";

/** Mobile navigation — hamburger drawer for < md screens.
 *
 * Desktop (md+) renders nothing; the inline nav in the header stays.
 * Mobile: fixed header row with a ☰ button that slides down a full-width
 * menu of all 8 pages. Closes on navigation (link click) or backdrop tap.
 */
const LINKS = [
  { href: "/", label: "Dashboard", icon: "🏠" },
  { href: "/market", label: "Market", icon: "📈" },
  { href: "/signals", label: "Signals", icon: "⚡" },
  { href: "/monitor", label: "Monitor", icon: "📊" },
  { href: "/logs", label: "Logs", icon: "📜" },
  { href: "/signal-logs", label: "Signal Logs", icon: "🗂️" },
  { href: "/risk", label: "Risk", icon: "🛡️" },
  { href: "/performance", label: "Performance", icon: "🎯" },
  { href: "/settings", label: "Settings", icon: "⚙️" },
];

export default function MobileNav() {
  const [open, setOpen] = useState(false);
  const [path, setPath] = useState("/");

  // Track current path so the active link is highlighted.
  useEffect(() => {
    setPath(window.location.pathname);
    const onPop = () => setPath(window.location.pathname);
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Lock body scroll while the drawer is open.
  useEffect(() => {
    document.body.style.overflow = open ? "hidden" : "";
    return () => {
      document.body.style.overflow = "";
    };
  }, [open]);

  const isActive = (href: string) =>
    href === "/" ? path === "/" : path.startsWith(href);

  return (
    <div className="md:hidden">
      {/* Hamburger button (lives in the header row) */}
      <button
        onClick={() => setOpen((v) => !v)}
        aria-label={open ? "ปิดเมนู" : "เปิดเมนู"}
        aria-expanded={open}
        className="w-11 h-11 -mr-2 flex items-center justify-center rounded-lg text-2xl text-slate-300 active:bg-slate-800"
      >
        {open ? "✕" : "☰"}
      </button>

      {/* Slide-down drawer */}
      {open && (
        <div className="fixed inset-0 z-40" onClick={() => setOpen(false)}>
          {/* backdrop */}
          <div className="absolute inset-0 bg-slate-950/70" />
          <nav
            className="absolute top-0 inset-x-0 safe-top bg-panel border-b border-slate-800 shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-800">
              <span className="font-bold text-sm">📈 AI Wealth &amp; Trading Advisor</span>
              <button
                onClick={() => setOpen(false)}
                aria-label="ปิดเมนู"
                className="w-11 h-11 -mr-2 flex items-center justify-center rounded-lg text-xl text-slate-400 active:bg-slate-800"
              >
                ✕
              </button>
            </div>
            <div className="grid grid-cols-2 gap-2 p-3">
              {LINKS.map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  onClick={() => setOpen(false)}
                  className={`flex items-center gap-2 rounded-lg border px-3 py-3 text-sm min-h-[48px] active:bg-slate-800 ${
                    isActive(l.href)
                      ? "border-accent/60 bg-accent/10 text-accent font-semibold"
                      : "border-slate-800 text-slate-300"
                  }`}
                >
                  <span className="text-lg">{l.icon}</span>
                  {l.label}
                </a>
              ))}
            </div>
          </nav>
        </div>
      )}
    </div>
  );
}
