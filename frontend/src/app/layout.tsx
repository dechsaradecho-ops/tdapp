import type { Metadata } from "next";
import AuthGate from "@/components/AuthGate";
import CapitalSync from "@/components/CapitalSync";
import ChatWidget from "@/components/ChatWidget";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Wealth & Trading Advisor",
  description:
    "Multi-asset trading advisory — goal feasibility, opportunity scoring, risk management. Probabilistic only, never guarantees profit.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="th">
      <body>
        <header className="border-b border-slate-800 px-6 py-3 flex items-center justify-between">
          <h1 className="font-bold text-lg">📈 AI Wealth &amp; Trading Advisor</h1>
          <nav className="flex gap-4 text-sm text-slate-400">
            <a href="/" className="hover:text-accent">Dashboard</a>
            <a href="/market" className="hover:text-accent">Market</a>
            <a href="/signals" className="hover:text-accent">Signals</a>
            <a href="/monitor" className="hover:text-accent">Monitor</a>
            <a href="/risk" className="hover:text-accent">Risk</a>
            <a href="/performance" className="hover:text-accent">Performance</a>
            <a href="/settings" className="hover:text-accent">Settings</a>
          </nav>
        </header>
        <main className="p-6 max-w-7xl mx-auto">
          <AuthGate>{children}</AuthGate>
        </main>
        <CapitalSync />
        <ChatWidget />
      </body>
    </html>
  );
}
