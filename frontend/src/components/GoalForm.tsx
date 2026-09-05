"use client";

import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import { fmtMoney, probabilityLabel } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import { GoalAssessment, GoalRealityContext } from "@/lib/types";

const SCENARIO_LABELS: Record<string, string> = {
  best_case: "Best Case",
  normal_case: "Normal Case",
  worst_case: "Worst Case",
};

const REGIME_LABELS: Record<string, string> = {
  strong_bull_trend: "🐂 Bull แรง",
  bull_trend: "📈 Bull Trend",
  sideway: "↔️ Sideway",
  high_volatility: "⚡ ผันผวนสูง",
  bear_trend: "📉 Bear Trend",
  strong_bear_trend: "🐻 Bear แรง",
  news_driven_market: "📰 ขับเคลื่อนด้วยข่าว",
};

export default function GoalForm() {
  const { capital, setCapital } = usePortfolio();
  const [target, setTarget] = useState(3);
  const [profile, setProfile] = useState("moderate");
  const [maxDd, setMaxDd] = useState(10);
  const [mode, setMode] = useState("manual");
  const [result, setResult] = useState<GoalAssessment | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setLoading(true);
    setError(null);
    try {
      setResult(await api.assessGoal({
        capital, target_return_pct: target,
        risk_profile: profile, max_drawdown_pct: maxDd, trading_mode: mode,
      }));
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  };

  const probColor = useMemo(() => {
    switch (result?.probability) {
      case "high_probability": return "text-profit";
      case "moderate_probability": return "text-amber-400";
      case "low_probability": return "text-loss";
      default: return "";
    }
  }, [result]);

  return (
    <div className="grid md:grid-cols-2 gap-4">
      <div className="panel">
        <h2 className="panel-title">Goal Engine — ประเมินความเป็นไปได้ของเป้าหมาย</h2>
        <div className="space-y-3">
          <label className="block text-sm">
            Capital (USD)
            <input type="number" value={capital} min={1}
              onChange={(e) => setCapital(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Target Return (% monthly)
            <input type="number" value={target} step={0.5} min={0.5} max={100}
              onChange={(e) => setTarget(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Risk Profile
            <select value={profile} onChange={(e) => setProfile(e.target.value)}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
              <option value="conservative">Conservative</option>
              <option value="moderate">Moderate</option>
              <option value="aggressive">Aggressive</option>
            </select>
          </label>
          <label className="block text-sm">
            Max Drawdown (%)
            <input type="number" value={maxDd} step={0.5} min={1} max={100}
              onChange={(e) => setMaxDd(Number(e.target.value))}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2" />
          </label>
          <label className="block text-sm">
            Trading Mode
            <select value={mode} onChange={(e) => setMode(e.target.value)}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
              <option value="auto">AUTO</option>
              <option value="semi_auto">SEMI-AUTO</option>
              <option value="manual">MANUAL</option>
            </select>
          </label>
          <button onClick={submit} disabled={loading}
            className="w-full bg-accent text-surface font-semibold rounded py-2 hover:brightness-110 disabled:opacity-50">
            {loading ? "กำลังประเมิน..." : "ประเมินเป้าหมาย"}
          </button>
          {error && <p className="text-loss text-sm">{error}</p>}
        </div>
      </div>

      <div className="panel">
        <h2 className="panel-title">ผลการประเมิน</h2>
        {!result && <p className="text-slate-500 text-sm">กรอกข้อมูลแล้วกดปุ่มเพื่อประเมิน</p>}
        {result && (
          <div className="space-y-4">
            {result.reality && <RealityPanel reality={result.reality} />}
            <div>
              <p className="text-sm text-slate-400">Expected Profit</p>
              <p className="text-2xl font-bold">{fmtMoney(result.expected_profit)}</p>
            </div>
            <div>
              <p className="text-sm text-slate-400">Probability</p>
              <p className={`text-xl font-bold ${probColor}`}>{probabilityLabel(result.probability)}</p>
            </div>
            {result.risk_warning && (
              <div className="border border-loss/40 bg-loss/10 rounded p-3 text-sm">
                ⚠️ {result.risk_warning}
              </div>
            )}
            <div className="space-y-2">
              {result.scenarios.map((s) => (
                <div key={s.label} className="flex justify-between text-sm border-b border-slate-800 pb-1">
                  <span>{SCENARIO_LABELS[s.label]}</span>
                  <span className={s.expected_profit >= 0 ? "text-profit" : "text-loss"}>
                    {s.expected_return_pct.toFixed(1)}% ({fmtMoney(s.expected_profit)})
                  </span>
                </div>
              ))}
            </div>
            <div>
              <p className="panel-title">AI Reasoning</p>
              <ol className="list-decimal list-inside text-sm space-y-1 text-slate-300">
                {result.reasoning.map((r, i) => <li key={i}>{r}</li>)}
              </ol>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/** Live trading state the assessment was adjusted by — "ประเมินจากสถานะจริงของคุณ". */
function RealityPanel({ reality }: { reality: GoalRealityContext }) {
  if (!reality.data_available) {
    return (
      <div className="border border-white/10 bg-white/[0.03] rounded-xl p-3 text-sm text-slate-400">
        📭 ยังไม่มีข้อมูลการเทรดจริง — ผลนี้คำนวณจากสูตรทฤษฎีล้วน
        (เริ่มเทรดแล้วระบบจะปรับผลตาม PnL / Win Rate / ตลาดจริงให้อัตโนมัติ)
      </div>
    );
  }
  const regime = REGIME_LABELS[reality.market_regime] ?? reality.market_regime;
  const pnlColor = reality.pnl_total > 0 ? "text-profit" : reality.pnl_total < 0 ? "text-loss" : "";
  const blocked = reality.kill_switch_engaged || reality.trading_paused;
  return (
    <div className={`border rounded p-3 text-sm space-y-2 ${blocked ? "border-loss/40 bg-loss/10" : "border-accent/30 bg-accent/5"}`}>
      <p className="font-semibold text-slate-200">🎯 ประเมินจากสถานะจริงของคุณ</p>
      <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-slate-300">
        <span>PnL รวม (ปิดแล้ว)</span>
        <span className={`text-right font-semibold ${pnlColor}`}>
          {reality.pnl_total >= 0 ? "+" : ""}{fmtMoney(reality.pnl_total)}
        </span>
        <span>Win Rate</span>
        <span className="text-right font-semibold">{reality.win_rate.toFixed(0)}% ({reality.closed_count} ไม้)</span>
        <span>ไม้เปิดค้าง</span>
        <span className="text-right font-semibold">{reality.open_positions}</span>
        <span>ตลาดตอนนี้</span>
        <span className="text-right font-semibold">{regime}</span>
      </div>
      {reality.kill_switch_engaged && (
        <p className="text-loss">🛑 Kill Switch: {reality.kill_triggers.join("; ")}</p>
      )}
      {reality.trading_paused && (
        <p className="text-amber-400">⏸️ หยุดเทรดด้วยตนเอง{reality.pause_reason ? ` — ${reality.pause_reason}` : ""}</p>
      )}
    </div>
  );
}
