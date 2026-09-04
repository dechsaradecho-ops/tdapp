"use client";

import { useEffect, useState } from "react";
import OpportunityScore from "@/components/OpportunityScore";
import TradingViewChart from "@/components/TradingViewChart";
import { api } from "@/lib/api";
import { MarketSummary } from "@/lib/types";

const SYMBOLS: Record<string, string> = {
  XAUUSD: "OANDA:XAUUSD",
  EURUSD: "OANDA:EURUSD",
  USDJPY: "OANDA:USDJPY",
  GBPUSD: "OANDA:GBPUSD",
  AUDUSD: "OANDA:AUDUSD",
};

export default function MarketPage() {
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [selected, setSelected] = useState("XAUUSD");

  useEffect(() => {
    api.marketSummary().then(setSummary).catch(() => setSummary(null));
  }, []);

  return (
    <div className="space-y-6">
      <section className="panel">
        <h2 className="panel-title">Market Regime Analysis</h2>
        {summary ? (
          <div className="grid md:grid-cols-3 gap-4">
            <div>
              <p className="text-xs text-slate-500">Regime</p>
              <p className="text-xl font-bold">{summary.regime.replace(/_/g, " ").toUpperCase()}</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Confidence</p>
              <p className="text-xl font-bold text-accent">{summary.confidence}%</p>
            </div>
            <div>
              <p className="text-xs text-slate-500">Sentiment</p>
              <p className={`text-xl font-bold ${summary.sentiment === "bullish" ? "text-profit" : summary.sentiment === "bearish" ? "text-loss" : ""}`}>
                {summary.sentiment.toUpperCase()}
              </p>
            </div>
            <p className="md:col-span-3 text-sm text-slate-400">{summary.explanation}</p>
          </div>
        ) : (
          <p className="text-slate-500 text-sm">ไม่มีข้อมูล — ตรวจสอบว่า backend รันอยู่</p>
        )}
      </section>

      <section className="grid md:grid-cols-3 gap-4">
        <div className="panel md:col-span-2">
          <div className="flex flex-wrap gap-2 mb-3">
            {Object.keys(SYMBOLS).map((a) => (
              <button key={a} onClick={() => setSelected(a)}
                className={`px-3 py-2 min-h-[40px] rounded text-sm border ${selected === a ? "border-accent text-accent" : "border-slate-700 text-slate-400 active:bg-slate-800"}`}>
                {a}
              </button>
            ))}
          </div>
          <TradingViewChart symbol={SYMBOLS[selected]} />
        </div>
        <div className="panel">
          <h2 className="panel-title">Opportunity Score (0–100)</h2>
          <OpportunityScore opportunities={summary?.opportunities ?? []} />
        </div>
      </section>
    </div>
  );
}
