import type { Metadata, Viewport } from "next";
import AuthGate from "@/components/AuthGate";
import BackgroundLayer from "@/components/BackgroundLayer";
import CapitalSync from "@/components/CapitalSync";
import ChatWidget from "@/components/ChatWidget";
import DesktopNav from "@/components/DesktopNav";
import HomeHero from "@/components/HomeHero";
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
        <BackgroundLayer />
        {/* แบ็กกราวด์ฮีโร่หน้าหลัก (GSAP scroll zoom) — ต้องอยู่นอก <main> และ
            ก่อน DesktopNav เพื่อให้แถบเริ่มที่ขอบบนสุดของหน้าไปจนถึง header
            (HomeHero เช็ค pathname === "/" เอง; หน้าอื่นไม่แสดง) */}
        <HomeHero />
        {/* Desktop (md+): floating liquid glass pill navbar (codefronts style —
            blur 20px + bg-white/10 + ring white/20 + rounded-full) — sticky ลอยเหนือเนื้อหา
            มือถือ (< md) ไม่แสดง — ใช้ MobileNav dock ล่างแทน */}
        <DesktopNav />
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
