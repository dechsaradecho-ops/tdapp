"use client";

import { useEffect, useState } from "react";
import PortfolioAllocation from "@/components/PortfolioAllocation";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import { DbCheckResult, DbCounts, PortfolioRecommendation } from "@/lib/types";

export default function SettingsPage() {
  const { capital, setCapital } = usePortfolio();
  const [target, setTarget] = useState(3);
  const [profile, setProfile] = useState("moderate");
  const [maxDd, setMaxDd] = useState(10);
  const [rec, setRec] = useState<PortfolioRecommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [dbCheck, setDbCheck] = useState<DbCheckResult | null>(null);
  const [dbCounts, setDbCounts] = useState<DbCounts | null>(null);
  const [dbTesting, setDbTesting] = useState(false);

  const recommend = async () => {
    setLoading(true);
    try {
      setRec(await api.recommendPortfolio({
        capital, target_return_pct: target, max_drawdown_pct: maxDd, risk_profile: profile,
      }));
    } finally {
      setLoading(false);
    }
  };

  const runDbCheck = async () => {
    setDbTesting(true);
    setDbCheck(null);
    try {
      const [check, counts] = await Promise.all([api.dbCheck(), api.dbCounts()]);
      setDbCheck(check);
      setDbCounts(counts);
    } catch (e) {
      setDbCheck({ client: "unavailable", verdict: "fail",
        error: e instanceof Error ? e.message : String(e) });
    } finally {
      setDbTesting(false);
    }
  };

  useEffect(() => { recommend(); /* eslint-disable-next-line */ }, []);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div className="panel">
        <h2 className="panel-title">Portfolio Settings</h2>
        <div className="space-y-3">
          <NumField label="Capital (THB)" value={capital} onChange={setCapital} />
          <NumField label="Target Return (%/month)" value={target} onChange={setTarget} step={0.5} />
          <NumField label="Max Drawdown (%)" value={maxDd} onChange={setMaxDd} step={0.5} />
          <label className="block text-sm">
            Risk Profile
            <select value={profile} onChange={(e) => setProfile(e.target.value)}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
              <option value="conservative">Conservative</option>
              <option value="moderate">Moderate</option>
              <option value="aggressive">Aggressive</option>
            </select>
          </label>
          <button onClick={recommend} disabled={loading}
            className="w-full bg-accent text-surface font-semibold rounded py-2 disabled:opacity-50">
            {loading ? "กำลังคำนวณ..." : "ขอ Portfolio Recommendation"}
          </button>
        </div>
      </div>

      <div className="panel">
        <h2 className="panel-title">Portfolio Recommendation</h2>
        {rec ? (
          <div className="space-y-4">
            <PortfolioAllocation allocation={rec.allocation} />
            <div className="text-sm flex justify-between border-t border-slate-800 pt-3">
              <span className="text-slate-400">Expected Return</span>
              <span className="text-profit font-semibold">{fmtPct(rec.expected_monthly_return_pct, 2)}</span>
            </div>
            <div className="text-sm flex justify-between">
              <span className="text-slate-400">Expected Drawdown</span>
              <span className="text-amber-400 font-semibold">{fmtPct(rec.expected_drawdown_pct, 2)}</span>
            </div>
            <div>
              <p className="panel-title">เหตุผล</p>
              <ul className="list-disc list-inside text-sm space-y-1 text-slate-300">
                {rec.reasoning.map((r, i) => <li key={i}>{r}</li>)}
              </ul>
            </div>
          </div>
        ) : (
          <p className="text-slate-500 text-sm">กำลังโหลด...</p>
        )}
      </div>

      <div className="panel md:col-span-2">
        <h2 className="panel-title">Database Connection Test</h2>
        <p className="text-sm text-slate-400 mb-3">
          ทดสอบการอ่าน/เขียนจริงกับ Supabase (insert → select → delete ในตาราง db_probe)
          พร้อมแสดงจำนวน row จริงในตาราง worker ทั้งหมด
        </p>
        <button onClick={runDbCheck} disabled={dbTesting}
          className="bg-accent text-surface font-semibold rounded px-4 py-2 disabled:opacity-50">
          {dbTesting ? "กำลังทดสอบ..." : "🧪 ทดสอบ อ่าน/เขียน DB"}
        </button>

        {dbCheck && (
          <div className="mt-4 space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <span className="text-slate-400">ผลทดสอบ:</span>
              <span className={dbCheck.verdict === "pass"
                ? "text-profit font-bold" : "text-loss font-bold"}>
                {dbCheck.verdict === "pass" ? "✅ ผ่านทั้งหมด" : `❌ ${dbCheck.verdict}`}
              </span>
              <span className="text-slate-500">
                (client: {dbCheck.client})
              </span>
            </div>
            <div className="grid grid-cols-3 gap-2 max-w-md">
              <StepBadge label="Insert" status={dbCheck.insert} />
              <StepBadge label="Select" status={dbCheck.select} />
              <StepBadge label="Delete" status={dbCheck.delete} />
            </div>
            {dbCheck.worker_table && (
              <div className="pt-1">
                <p className="text-xs text-slate-500 mb-1">
                  INSERT แบบเดียวกับ scanner → {dbCheck.worker_table}:
                </p>
                <StepBadge label="Worker insert" status={dbCheck.worker_insert} />
                {dbCheck.worker_insert_error && (
                  <p className="text-loss text-xs mt-1 break-all">
                    error: {dbCheck.worker_insert_error}
                  </p>
                )}
                {dbCheck.worker_insert_hint && (
                  <p className="text-amber-400 text-xs mt-1">{dbCheck.worker_insert_hint}</p>
                )}
              </div>
            )}
            {dbCheck.error && <p className="text-loss">error: {dbCheck.error}</p>}
            {dbCheck.insert_hint && <p className="text-amber-400">{dbCheck.insert_hint}</p>}
            {dbCheck.select_hint && <p className="text-amber-400">{dbCheck.select_hint}</p>}
          </div>
        )}

        {dbCounts && dbCounts.verdict === "ok" && (
          <div className="mt-4">
            <p className="text-sm text-slate-400 mb-2">Row counts (worker tables):</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <CountCard label="market_analysis" count={dbCounts.market_analysis} latest={dbCounts.market_analysis_latest} />
              <CountCard label="signals" count={dbCounts.signals} latest={dbCounts.signals_latest} />
              <CountCard label="news_analysis" count={dbCounts.news_analysis} latest={dbCounts.news_analysis_latest} />
              <CountCard label="trades" count={dbCounts.trades} latest={dbCounts.trades_latest} />
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function StepBadge({ label, status }:
  { label: string; status?: "ok" | "FAIL" }) {
  if (!status) return <div className="bg-surface rounded p-2 text-center text-slate-500">{label}: —</div>;
  return (
    <div className={`bg-surface rounded p-2 text-center ${status === "ok" ? "text-profit" : "text-loss font-bold"}`}>
      {label}: {status === "ok" ? "✅" : "❌"}
    </div>
  );
}

function CountCard({ label, count, latest }:
  { label: string; count?: number; latest?: string }) {
  return (
    <div className="bg-surface rounded p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-lg font-bold ${count ? "text-profit" : "text-slate-400"}`}>
        {count ?? 0} rows
      </p>
      {latest && <p className="text-xs text-slate-500">ล่าสุด: {new Date(latest).toLocaleString("th-TH")}</p>}
    </div>
  );
}

function NumField({ label, value, onChange, step = 1 }:
  { label: string; value: number; onChange: (v: number) => void; step?: number }) {
  return (
    <label className="block text-sm">
      {label}
      <input type="number" value={value} step={step}
        onChange={(e) => onChange(Number(e.target.value))}
        className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
    </label>
  );
}
