"use client";

import { useEffect, useState } from "react";
import PortfolioAllocation from "@/components/PortfolioAllocation";
import PinManager from "@/components/PinManager";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import {
  AppSettings, DbCheckResult, DbCounts, PauseStatus, PortfolioRecommendation, RiskProfile,
} from "@/lib/types";

const DEFAULT_SETTINGS: AppSettings = {
  risk_profile: "moderate",
  capital: 10_000,
  min_confidence: 70,
  min_confidence_gold: null,
  min_opportunity: 60,
  max_trades_daily: 6,
  max_trades_weekly: 30,
  max_open_positions: 4,
  risk_per_trade_pct: 1.0,
  min_lot: 0.01,
  min_lot_gold: null,
  breakeven_trigger_r: 1.0,
  trail_atr_mult: 2.0,
  partial_close_pct: 0,
  partial_trigger_r: 1.0,
  paper_spread: 0,
  max_drawdown_pct: 10,
  kill_daily_loss_pct: 2,
  kill_weekly_loss_pct: 5,
  kill_monthly_loss_pct: 8,
  drawdown_throttle_pct: 5,
  news_block_minutes: 30,
  news_caution_minutes: 120,
  correlation_cap: 80,
  order_mode: "auto",
  sl_distance_mode: "medium",
  default_equity: 10_000,
  paper_virtual_capital: 100_000,
  backtest_days: 120,
  backtest_indicator: "EMA",
  backtest_asset: "EURUSD",
};

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

  // --- trading config state (loaded from /api/settings) ---
  const [cfg, setCfg] = useState<AppSettings | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [loadErr, setLoadErr] = useState("");

  // --- execution switch (Phase 1) ---
  const [pause, setPause] = useState<PauseStatus | null>(null);
  const [pauseBusy, setPauseBusy] = useState(false);

  useEffect(() => {
    api.getTradingPause().then(setPause).catch(() => setPause(null));
  }, []);

  const togglePause = async () => {
    setPauseBusy(true);
    try {
      const next = !(pause?.paused ?? false);
      const res = await api.setTradingPause(next, next ? "paused from settings UI" : "");
      setPause(res);
    } finally {
      setPauseBusy(false);
    }
  };

  useEffect(() => {
    api.getSettings()
      .then((s) => setCfg(s))
      .catch((e) => setLoadErr(e instanceof Error ? e.message : String(e)));
  }, []);

  const save = async () => {
    if (!cfg) return;
    setSaving(true);
    setSaveMsg("");
    try {
      const res = await api.saveSettings(cfg);
      setCfg(res.settings);
      setCapital(res.settings.capital); // global store follows saved settings
      setSaveMsg(res.ok ? "✅ บันทึกลง Supabase แล้ว" : `❌ ${res.message}`);
    } catch (e) {
      setSaveMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const reset = async () => {
    setSaving(true);
    setSaveMsg("");
    try {
      const res = await api.resetSettings();
      setCfg(res.settings);
      setSaveMsg("↩️ รีเซ็ตเป็นค่าเริ่มต้นแล้ว");
    } catch (e) {
      setSaveMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSaving(false);
    }
  };

  const set = <K extends keyof AppSettings>(key: K, v: AppSettings[K]) =>
    setCfg((c) => (c ? { ...c, [key]: v } : c));

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
          <PinManager />
          <NumField label="Capital (USD) — ใช้ทั้งระบบ" value={capital}
            onChange={(v) => { setCapital(v); set("capital", v); }} />
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

      {/* ---------------- Trading Configuration ---------------- */}
      <div className="panel md:col-span-2">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">การตั้งค่าระบบเทรด (ใช้จริงทั้งระบบ)</h2>
          <div className="flex items-center gap-2">
            {saveMsg && <span className="text-xs text-slate-400">{saveMsg}</span>}
            <button onClick={reset} disabled={saving || !cfg}
              className="text-xs text-slate-400 border border-slate-700 rounded px-3 min-h-[40px] active:bg-slate-800 disabled:opacity-40">
              ↩️ ค่าเริ่มต้น
            </button>
            <button onClick={save} disabled={saving || !cfg}
              className="bg-accent text-surface font-semibold rounded px-4 min-h-[40px] disabled:opacity-50 active:brightness-90">
              {saving ? "กำลังบันทึก..." : "💾 บันทึกการตั้งค่า"}
            </button>
          </div>
        </div>
        <p className="text-sm text-slate-400 mt-1">
          ค่าที่บันทึกที่นี่จะถูกใช้จริงโดย Frequency Guard, Risk Officer, Kill Switch,
          News Gate, Correlation, Paper Trading และ Extended Analysis ทันที
        </p>

        {loadErr && <p className="text-loss text-sm mt-2">โหลดค่าไม่สำเร็จ: {loadErr}</p>}
        {!cfg && !loadErr && <p className="text-slate-500 text-sm mt-3">กำลังโหลด...</p>}

        {/* --- Execution switch (blocks BOTH auto trader and /approve) --- */}
        <div className={`mt-4 rounded border px-4 py-3 flex items-center justify-between flex-wrap gap-3 ${
          pause?.paused ? "border-loss bg-loss/10" : "border-slate-700 bg-surface/40"}`}>
          <div>
            <p className="text-sm font-semibold">
              {pause?.paused
                ? "🛑 Auto Trading หยุดชั่วคราว — บล็อกทั้ง auto + approve"
                : "✅ Auto Trading ทำงานปกติ"}
            </p>
            <p className="text-xs text-slate-400 mt-0.5">
              {pause?.paused
                ? `เหตุผล: ${pause.reason || "ไม่ระบุ"} — กด Resume เพื่อกลับมาเทรดต่อ`
                : "ระบบจะยิง order ผ่าน gate (pause/kill switch/ข่าว/correlation) ทุกครั้ง"}
            </p>
          </div>
          <button onClick={togglePause} disabled={pauseBusy}
            className={pause?.paused
              ? "bg-profit text-surface font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90"
              : "bg-loss text-surface font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90"}>
            {pauseBusy ? "กำลังส่ง..." : pause?.paused ? "▶️ Resume Auto Trading" : "⏸️ Pause Auto Trading"}
          </button>
        </div>

        {cfg && (
          <div className="mt-4 grid md:grid-cols-4 gap-4">
            {/* --- Profile & signal gates --- */}
            <div className="space-y-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">โปรไฟล์ &amp; Signal Gates</p>
              <label className="block text-sm">
                โหมดเทรด (order_mode)
                <select value={cfg.order_mode}
                  onChange={(e) => set("order_mode", e.target.value)}
                  className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
                  <option value="auto">🤖 Auto — ระบบเทรดเอง</option>
                  <option value="semi_auto">👤 Semi-Auto — รอยืนยันก่อน</option>
                  <option value="manual">✋ Manual — ระบบไม่ยิง order</option>
                </select>
              </label>
              <label className="block text-sm">
                ระยะ SL/TP ที่ใช้เปิด order (sl_distance_mode)
                <select value={cfg.sl_distance_mode}
                  onChange={(e) => set("sl_distance_mode", e.target.value as AppSettings["sl_distance_mode"])}
                  className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
                  <option value="short">สั้น ×1.0 ATR — SL เข้ม ปิดไว</option>
                  <option value="medium">กลาง ×1.5 ATR — ตามสัญญาณ (ค่าเริ่มต้น)</option>
                  <option value="long">ยาว ×2.0 ATR — SL กว้าง ทนผันผวน</option>
                </select>
                <span className="text-xs text-slate-500 mt-1 block">
                  การ์ดสัญญาณเก็บราคากลาง (×1.5) — ตอนยิง order ระบบคำนวณ SL/TP ใหม่ตามระดับนี้
                </span>
              </label>
              <label className="block text-sm">
                Risk Profile
                <select value={cfg.risk_profile}
                  onChange={(e) => set("risk_profile", e.target.value as RiskProfile)}
                  className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2">
                  <option value="conservative">Conservative</option>
                  <option value="moderate">Moderate</option>
                  <option value="aggressive">Aggressive</option>
                </select>
              </label>
              <NumField label="Min Confidence (%)" value={cfg.min_confidence}
                onChange={(v) => set("min_confidence", v)} step={1} />
              <label className="block text-sm">
                Min Confidence (gold) (%)
                <div className="flex items-center gap-2 mt-1">
                  <input type="number"
                    value={cfg.min_confidence_gold ?? ""}
                    placeholder={`ใช้ค่าเดิม ${cfg.min_confidence}`}
                    step={1}
                    onChange={(e) =>
                      set("min_confidence_gold",
                        e.target.value === "" ? null : Number(e.target.value))}
                    className="w-full bg-surface border border-slate-700 rounded px-3 py-2" />
                  {cfg.min_confidence_gold != null && (
                    <button type="button" onClick={() => set("min_confidence_gold", null)}
                      title="ล้างค่า — ใช้ Min Confidence ปกติ"
                      className="shrink-0 text-xs text-slate-400 hover:text-slate-200 border border-slate-700 rounded px-2 py-2">
                      ล้าง
                    </button>
                  )}
                </div>
                <span className="block text-xs text-slate-500 mt-1">
                  เกณฑ์เฉพาะ XAUUSD — เว้นว่างเพื่อใช้ Min Confidence ปกติ
                </span>
              </label>
              <NumField label="Min Opportunity (%)" value={cfg.min_opportunity}
                onChange={(v) => set("min_opportunity", v)} step={1} />
              <NumField label="Capital (USD)" value={cfg.capital}
                onChange={(v) => set("capital", v)} step={100} />
            </div>

            {/* --- Frequency limits --- */}
            <div className="space-y-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">ลิมิตการเทรด</p>
              <NumField label="เทรดสูงสุด/วัน" value={cfg.max_trades_daily}
                onChange={(v) => set("max_trades_daily", v)} step={1} />
              <NumField label="เทรดสูงสุด/สัปดาห์" value={cfg.max_trades_weekly}
                onChange={(v) => set("max_trades_weekly", v)} step={1} />
              <NumField label="ไม้ที่เปิดค้างสูงสุด" value={cfg.max_open_positions}
                onChange={(v) => set("max_open_positions", v)} step={1} />
              <NumField label="Risk ต่อไม้ (%)" value={cfg.risk_per_trade_pct}
                onChange={(v) => set("risk_per_trade_pct", v)} step={0.1} />
              <NumField label="ขนาด Lot ขั้นต่ำ (min_lot)" value={cfg.min_lot}
                onChange={(v) => set("min_lot", v)} step={0.01} />
              <span className="block text-xs text-slate-500 -mt-2">
                ขนาด lot ต่ำสุดของทุกออเดอร์ — ระบบคำนวณจาก Risk ต่อไม้ก่อน แล้วปัดขึ้นเป็นค่านี้ (เช่น 0.02)
              </span>
              <label className="block text-sm">
                ขนาด Lot ขั้นต่ำ (gold) (min_lot_gold)
                <div className="flex items-center gap-2 mt-1">
                  <input type="number"
                    value={cfg.min_lot_gold ?? ""}
                    placeholder={`ใช้ค่าเดิม ${cfg.min_lot}`}
                    step={0.01}
                    onChange={(e) =>
                      set("min_lot_gold",
                        e.target.value === "" ? null : Number(e.target.value))}
                    className="w-full bg-surface border border-slate-700 rounded px-3 py-2" />
                  {cfg.min_lot_gold != null && (
                    <button type="button" onClick={() => set("min_lot_gold", null)}
                      title="ล้างค่า — ใช้ขนาด Lot ขั้นต่ำปกติ"
                      className="shrink-0 text-xs text-slate-400 hover:text-slate-200 border border-slate-700 rounded px-2 py-2">
                      ล้าง
                    </button>
                  )}
                </div>
                <span className="block text-xs text-slate-500 mt-1">
                  เฉพาะ XAUUSD — เว้นว่างเพื่อใช้ขนาด Lot ขั้นต่ำปกติ
                </span>
              </label>
            </div>

            {/* --- Position management (breakeven / trailing / partial) --- */}
            <div className="space-y-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">การจัดการไม้ (Position Management)</p>
              <NumField label="Breakeven Trigger (×R)" value={cfg.breakeven_trigger_r}
                onChange={(v) => set("breakeven_trigger_r", v)} step={0.1} />
              <span className="block text-xs text-slate-500 -mt-2">
                กำไรถึง R กี่เท่า ค่อยย้าย SL ไปที่ราคาเข้า (0 = ปิดการใช้งาน)
              </span>
              <NumField label="Trailing Stop (×ATR)" value={cfg.trail_atr_mult}
                onChange={(v) => set("trail_atr_mult", v)} step={0.1} />
              <span className="block text-xs text-slate-500 -mt-2">
                ระยะ trailing หลัง breakeven — หน่วยเป็นเท่าของ ATR (0 = ปิด)
              </span>
              <NumField label="Partial Close (%)" value={cfg.partial_close_pct}
                onChange={(v) => set("partial_close_pct", v)} step={5} />
              <span className="block text-xs text-slate-500 -mt-2">
                ปิดบางส่วนกี่ % เมื่อกำไรถึง Partial Trigger (0 = ปิดการใช้งาน)
              </span>
              <NumField label="Partial Trigger (×R)" value={cfg.partial_trigger_r}
                onChange={(v) => set("partial_trigger_r", v)} step={0.1} />
              <NumField label="Paper Spread (ราคา)" value={cfg.paper_spread}
                onChange={(v) => set("paper_spread", v)} step={0.00001} />
              <span className="block text-xs text-slate-500 -mt-2">
                สเปรดจำลอง — BUY เข้าแพงขึ้น / SELL เข้าถูกลง (0 = ไม่มีสเปรด)
              </span>
            </div>

            {/* --- Kill switch / drawdown --- */}
            <div className="space-y-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">Kill Switch &amp; Risk</p>
              <NumField label="ขาทุนรายวันสูงสุด (%)" value={cfg.kill_daily_loss_pct}
                onChange={(v) => set("kill_daily_loss_pct", v)} step={0.5} />
              <NumField label="ขาทุนรายสัปดาห์ (%)" value={cfg.kill_weekly_loss_pct}
                onChange={(v) => set("kill_weekly_loss_pct", v)} step={0.5} />
              <NumField label="ขาทุนรายเดือน (%)" value={cfg.kill_monthly_loss_pct}
                onChange={(v) => set("kill_monthly_loss_pct", v)} step={0.5} />
              <NumField label="Max Drawdown — Kill (%)" value={cfg.max_drawdown_pct}
                onChange={(v) => set("max_drawdown_pct", v)} step={0.5} />
              <NumField label="DD เริ่มลดความถี่ (%)" value={cfg.drawdown_throttle_pct}
                onChange={(v) => set("drawdown_throttle_pct", v)} step={0.5} />
            </div>

            {/* --- News / correlation / order / backtest --- */}
            <div className="space-y-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">ข่าว / Correlation / Backtest</p>
              <NumField label="บล็อกข่าวก่อน event (นาที)" value={cfg.news_block_minutes}
                onChange={(v) => set("news_block_minutes", v)} step={5} />
              <NumField label="ระวังข่าวก่อน event (นาที)" value={cfg.news_caution_minutes}
                onChange={(v) => set("news_caution_minutes", v)} step={5} />
              <NumField label="Correlation Cap (0-100)" value={cfg.correlation_cap}
                onChange={(v) => set("correlation_cap", v)} step={1} />
              <NumField label="Paper Capital เสมือน" value={cfg.paper_virtual_capital}
                onChange={(v) => set("paper_virtual_capital", v)} step={10_000} />
              <NumField label="Backtest: จำนวนวัน" value={cfg.backtest_days}
                onChange={(v) => set("backtest_days", v)} step={10} />
            </div>
          </div>
        )}
      </div>

      <div className="panel md:col-span-2">
        <h2 className="panel-title">Database Connection Test</h2>
        <p className="text-sm text-slate-400 mb-3">
          ทดสอบการอ่าน/เขียนจริงกับ Supabase (insert → select → delete ในตาราง db_probe)
          พร้อมแสดงจำนวน row จริงในตาราง worker ทั้งหมด
        </p>
        <button onClick={runDbCheck} disabled={dbTesting}
          className="bg-accent text-surface font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90">
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
