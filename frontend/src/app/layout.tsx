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
            which let the bottom nav cover the last content block on every page.

            pt-0 บนมือถือ (< md): มือถือไม่มี header (DesktopNav เป็น `hidden md:flex`)
            จึงไม่มีอะไรกินพื้นที่บรรทัดแรก — เหลือแต่ py-4 = 16px ที่เห็นเป็น "ช่องว่าง
            ข้างบน" ทึบ ๆ (ด้านหลังคือขอบบนของ .zoom-hero ที่เกือบดำจาก vignette)
            จึงดันเนื้อหาขึ้นชิดขอบบนสุด ให้ตรงกับที่ DesktopNav ชิดขอบบนบนเดสก์ท็อป
            ใช้ md ไม่ใช่ sm เพราะ breakpoint ที่หัวเว็บโผล่คือ md (768px) — มือถือ
            แนวนอน (เช่น 844×390) กว้างเกิน 640 จึงยังต้องได้ pt-0 ด้วย
            (ยังคง py/pb รอบข้าง: px-3 กันการ์ดชนขอบจอ, pb-24 กัน dock ทับ)
            ลำดับคลาสสำคัญ: `pt-0` ต้องชนะ `py-4` (Tailwind ออก CSS ของ pt หลัง py)
            และ `md:py-5` (อยู่ใน media block ซึ่งมาหลัง base) คืน pt ให้แท็บเล็ต/เดสก์ท็อป */}
        <main className="px-3 py-4 pt-0 sm:px-6 md:py-5 max-w-7xl mx-auto pb-24 md:pb-5">
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
