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
        <header className="border-b border-slate-800 px-3 sm:px-6 py-2 sm:py-3 flex items-center justify-between safe-top sticky top-0 z-30 bg-surface/95 backdrop-blur">
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
        <main className="px-3 py-4 sm:px-6 sm:py-5 max-w-7xl mx-auto safe-bottom pb-24 md:pb-5">
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
