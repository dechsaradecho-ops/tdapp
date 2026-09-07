"use client";

import { useEffect, useState } from "react";
import AutoTradeReadinessCard from "@/components/AutoTradeReadinessCard";
import GoalForm from "@/components/GoalForm";
import OpportunityScore from "@/components/OpportunityScore";
import TradingViewChart from "@/components/TradingViewChart";
import { api } from "@/lib/api";
import { fmtMoney, scoreColor } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import { MarketSummary } from "@/lib/types";

// เดิมอยู่หน้า /market (รวมเข้าหน้าหลักตามแผนจัดเมนูใหม่ Plan B)
const SYMBOLS: Record<string, string> = {
  XAUUSD: "OANDA:XAUUSD",
  EURUSD: "OANDA:EURUSD",
  USDJPY: "OANDA:USDJPY",
  GBPUSD: "OANDA:GBPUSD",
  AUDUSD: "OANDA:AUDUSD",
};
// Pairs ที่ไม่มีใน SYMBOLS (เช่น CHFJPY จาก allowed_assets) ใช้ generic OANDA mapping
const tvSymbol = (a: string) => SYMBOLS[a] ?? `OANDA:${a}`;

export default function DashboardPage() {
  const [summary, setSummary] = useState<MarketSummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(true);
  const [summaryErr, setSummaryErr] = useState(false);
  const [selected, setSelected] = useState("XAUUSD");
  const { capital, equity, pnl } = usePortfolio();

  useEffect(() => {
    api.marketSummary()
      .then((s) => { setSummary(s); setSummaryLoading(false); })
      .catch(() => { setSummaryErr(true); setSummaryLoading(false); });
  }, []);

  // ปุ่มสัญลักษณ์ = ทุก asset ใน summary.opportunities (follows allowed_assets)
  // fallback เป็น SYMBOLS เดิมถ้ายังไม่มีข้อมูล
  const symbolList = summary?.opportunities.length
    ? summary.opportunities.map((o) => o.asset)
    : Object.keys(SYMBOLS);
  const confByAsset = new Map(
    (summary?.opportunities ?? []).map((o) => [o.asset, o.score]),
  );

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <Stat label="Capital" value={fmtMoney(capital)} />
        <Stat label="Current Equity" value={fmtMoney(equity)} positive={pnl >= 0} />
        <Stat label="Current PnL" value={fmtMoney(pnl)} positive={pnl >= 0} />
        <Stat label="Monthly Goal" value="3%" />
      </section>

      {/* ---------- สถานะการเทรดอัตโนมัติ: เปิดได้/ไม่ได้ เพราะปัจจัยอะไร ---------- */}
      <AutoTradeReadinessCard />

      <GoalForm />

      {/* ---------- Market Regime Analysis (จาก /market เดิม) ---------- */}
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
          <p className="text-slate-500 text-sm">
            {summaryLoading ? "กำลังโหลด..." : "ไม่มีข้อมูล — ตรวจสอบว่า backend รันอยู่"}
          </p>
        )}
      </section>

      <section className="grid md:grid-cols-3 gap-4">
        <div className="panel md:col-span-2">
          {/* ตัวเลือกสัญลักษณ์ (จาก /market เดิม) — XAUUSD ค่าเริ่มต้น, badge Confidence % ต่อสัญลักษณ์ */}
          <div className="flex flex-wrap gap-2 mb-3">
            {symbolList.map((a) => {
              const conf = confByAsset.get(a);
              return (
                <button key={a} onClick={() => setSelected(a)}
                  className={`px-3 py-2 min-h-[40px] rounded-xl text-sm border inline-flex items-center gap-1.5 ${selected === a ? "border-accent text-accent bg-accent/10" : "border-white/15 bg-white/[0.04] text-slate-400 active:bg-white/10"}`}>
                  {a}
                  {conf != null && (
                    <span className={`text-xs font-bold ${selected === a ? "" : scoreColor(conf)}`}>
                      {conf.toFixed(0)}%
                    </span>
                  )}
                </button>
              );
            })}
          </div>
          <TradingViewChart symbol={tvSymbol(selected)} />
        </div>
        <div className="panel">
          <h2 className="panel-title">Opportunity Score</h2>
          <OpportunityScore
            opportunities={summary?.opportunities ?? []}
            loading={summaryLoading}
            error={summaryErr}
            minConfidence={summary?.min_confidence}
            minConfidenceGold={summary?.min_confidence_gold}
          />
        </div>
      </section>
    </div>
  );
}

function Stat({ label, value, positive }: { label: string; value: string; positive?: boolean }) {
  return (
    <div className="panel">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-xl font-bold ${positive ? "text-profit" : ""}`}>{value}</p>
    </div>
  );
}
