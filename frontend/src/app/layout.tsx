import type { Metadata, Viewport } from "next";
import AuthGate from "@/components/AuthGate";
import CapitalSync from "@/components/CapitalSync";
import ChatWidget from "@/components/ChatWidget";
import MobileNav from "@/components/MobileNav";
import PwaRegister from "@/components/PwaRegister";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Wealth & Trading Advisor",
  description:
    "Multi-asset trading advisory — goal feasibility, opportunity scoring, risk management. Probabilistic only, never guarantees profit.",
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" },
      { url: "/icons/icon-512.png", sizes: "512x512", type: "image/png" },
    ],
    apple: [{ url: "/icons/icon-192.png", sizes: "192x192", type: "image/png" }],
  },
  appleWebApp: {
    capable: true,
    statusBarStyle: "black-translucent",
    title: "AI Trading",
  },
};

// Mobile browser chrome: dark theme bar + no user zoom (app-like feel).
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 1,
  userScalable: false,
  viewportFit: "cover",
  themeColor: "#000000",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="th">
      <body>
        {/* SVG filter defs สำหรับ Liquid Glass refraction — ใช้โดย .lg-refract::before ใน globals.css
            (backdrop-filter: url(#lg-refract) ทำให้เนื้อหาด้านหลังแก้ว "หักเห" แบบเลนส์ ไม่ใช่แค่เบลอ)
            ต้องอยู่ใน DOM ทุกหน้า — ซ่อนด้วยขนาด 0 (ห้าม display:none เพราะบาง browser จะไม่ resolve filter) */}
        <svg aria-hidden="true" focusable="false" width="0" height="0" style={{ position: "absolute" }}>
          <defs>
            <filter id="lg-refract" x="-20%" y="-20%" width="140%" height="140%" colorInterpolationFilters="sRGB">
              <feTurbulence type="fractalNoise" baseFrequency="0.008 0.012" numOctaves="2" seed="11" result="noise" />
              <feGaussianBlur in="noise" stdDeviation="1.5" result="soft" />
              <feDisplacementMap in="SourceGraphic" in2="soft" scale="20" xChannelSelector="R" yChannelSelector="G" />
            </filter>
          </defs>
        </svg>
        <header
          className="lg-refract border-b px-3 sm:px-6 py-2 sm:py-3 flex items-center justify-between safe-top sticky top-0 z-30"
          style={{
            background: "rgba(5, 5, 8, 0.65)",
            WebkitBackdropFilter: "blur(24px) saturate(160%)",
            backdropFilter: "blur(24px) saturate(160%)",
            borderBottomColor: "rgba(255,255,255,0.10)",
          }}
        >
          <h1 className="font-bold text-base sm:text-lg truncate">
            <span className="sm:hidden">📈 AI Trading</span>
            <span className="hidden sm:inline">📈 AI Wealth &amp; Trading Advisor</span>
          </h1>
          <nav className="hidden md:flex gap-4 text-sm text-slate-400">
            <a href="/" className="hover:text-accent">Dashboard</a>
            <a href="/market" className="hover:text-accent">Market</a>
            <a href="/signals" className="hover:text-accent">Signals</a>
            <a href="/monitor" className="hover:text-accent">Monitor</a>
            <a href="/logs" className="hover:text-accent">Logs</a>
            <a href="/signal-logs" className="hover:text-accent">Signal Logs</a>
            <a href="/risk" className="hover:text-accent">Risk</a>
            <a href="/performance" className="hover:text-accent">Performance</a>
            <a href="/settings" className="hover:text-accent">Settings</a>
          </nav>
        </header>
        {/* pb-24 clears the fixed mobile tab bar (57px) + iOS safe area (≤34px).
            Do NOT add safe-bottom here — .safe-bottom (env(safe-area-inset-bottom))
            appears after Tailwind utilities in globals.css and overrides pb-* to 0,
            which let the bottom nav cover the last content block on every page. */}
        <main className="px-3 py-4 sm:px-6 sm:py-5 max-w-7xl mx-auto pb-24 md:pb-5">
          <AuthGate>{children}</AuthGate>
        </main>
        <MobileNav />
        <CapitalSync />
        <ChatWidget />
        <PwaRegister />
      </body>
    </html>
  );
}
