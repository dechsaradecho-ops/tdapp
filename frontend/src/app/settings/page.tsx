"use client";

import { useEffect, useState } from "react";
import PortfolioAllocation from "@/components/PortfolioAllocation";
import PinManager from "@/components/PinManager";
import { api } from "@/lib/api";
import { fmtPct } from "@/lib/format";
import { usePortfolio } from "@/lib/portfolio";
import {
  AppSettings, DbCheckResult, DbCounts, LineDiag, LineEventsResponse,
  LineSimulateResult, LineTargetsResponse, LineTestResult, PauseStatus,
  PortfolioRecommendation, RiskProfile,
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

  // --- LINE notification test + registered targets ---
  const [lineTargets, setLineTargets] = useState<LineTargetsResponse | null>(null);
  const [lineTestRes, setLineTestRes] = useState<LineTestResult | null>(null);
  const [lineBusy, setLineBusy] = useState(false);
  const [lineMsg, setLineMsg] = useState("");
  const [newGroupId, setNewGroupId] = useState("");
  const [diag, setDiag] = useState<LineDiag | null>(null);

  const loadLineTargets = () =>
    api.lineTargets().then(setLineTargets).catch(() => setLineTargets(null));

  useEffect(() => { loadLineTargets(); }, []);

  const runLineTest = async () => {
    setLineBusy(true);
    setLineMsg("");
    try {
      const res = await api.lineTest();
      setLineTestRes(res);
      setLineMsg(res.ok ? "✅ ส่งข้อความทดสอบสำเร็จ — เช็คกลุ่ม LINE" : "❌ ส่งไม่สำเร็จ — ดู error รายกลุ่มด้านล่าง");
      loadLineTargets(); // refresh last_seen_at
    } catch (e) {
      setLineMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLineBusy(false);
    }
  };

  const runDiag = async () => {
    setLineBusy(true);
    setLineMsg("");
    try {
      setDiag(await api.lineDiag());
    } catch (e) {
      setLineMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLineBusy(false);
    }
  };

  const addGroup = async () => {
    const id = newGroupId.trim();
    if (!id) return;
    setLineBusy(true);
    setLineMsg("");
    try {
      const res = await api.lineAddTarget(id);
      setLineMsg(res.message);
      if (res.ok) setNewGroupId("");
      loadLineTargets();
    } catch (e) {
      setLineMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLineBusy(false);
    }
  };

  const removeGroup = async (id: string) => {
    setLineBusy(true);
    try {
      const res = await api.lineRemoveTarget(id);
      setLineMsg(res.message);
      loadLineTargets();
    } finally {
      setLineBusy(false);
    }
  };

  // --- webhook debug: event log + simulate ---
  const [events, setEvents] = useState<LineEventsResponse | null>(null);
  const [simText, setSimText] = useState("วันนี้ควรเทรดทองไหม");
  const [simRes, setSimRes] = useState<LineSimulateResult | null>(null);

  const loadEvents = () =>
    api.lineEvents().then(setEvents).catch(() => setEvents(null));

  const runSimulate = async () => {
    setLineBusy(true);
    setSimRes(null);
    try {
      const res = await api.lineSimulate({ text: simText });
      setSimRes(res);
      loadEvents();
    } catch (e) {
      setLineMsg(`❌ ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setLineBusy(false);
    }
  };

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
            className="w-full bg-accent text-white font-semibold rounded py-2 disabled:opacity-50">
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
              ค่าเริ่มต้น
            </button>
            <button onClick={save} disabled={saving || !cfg}
              className="bg-accent text-white font-semibold rounded px-4 min-h-[40px] disabled:opacity-50 active:brightness-90">
              {saving ? "กำลังบันทึก..." : "บันทึกการตั้งค่า"}
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
              ? "bg-profit text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90"
              : "bg-loss text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90"}>
            {pauseBusy ? "กำลังส่ง..." : pause?.paused ? "Resume Auto Trading" : "Pause Auto Trading"}
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
          className="bg-accent text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90">
          {dbTesting ? "กำลังทดสอบ..." : "ทดสอบ อ่าน/เขียน DB"}
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

      {/* ---------------- LINE Notifications ---------------- */}
      <div className="panel md:col-span-2">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">การแจ้งเตือน LINE</h2>
          <div className="flex items-center gap-2">
            <button onClick={runDiag} disabled={lineBusy}
              className="text-xs text-slate-400 border border-slate-700 rounded px-3 min-h-[44px] active:bg-slate-800 disabled:opacity-40">
              วินิจฉัย
            </button>
            <button onClick={runLineTest} disabled={lineBusy}
              className="bg-accent text-white font-semibold rounded px-4 py-2.5 min-h-[44px] disabled:opacity-50 active:brightness-90">
              {lineBusy ? "กำลังส่ง..." : "ทดสอบการแจ้งเตือน"}
            </button>
          </div>
        </div>
        <p className="text-sm text-slate-400 mt-1">
          กลุ่มจะถูกลงทะเบียนอัตโนมัติเมื่อบอทได้รับ event จากกลุ่ม (@mention บอท 1 ครั้ง)
          หรือวาง Group ID ด้วยมือด้านล่าง
        </p>
        {lineMsg && <p className="text-sm mt-2">{lineMsg}</p>}

        {diag && (
          <div className="mt-3 rounded border border-slate-700 bg-surface/40 p-3 text-sm space-y-1">
            <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1">ผลวินิจฉัย</p>
            <DiagRow label="LINE_CHANNEL_ACCESS_TOKEN" ok={diag.token_set} />
            <DiagRow label="LINE_CHANNEL_SECRET" ok={diag.secret_set} />
            <DiagRow label="LINE_BOT_USER_ID" ok={diag.bot_user_id_set}
              note={diag.bot_user_id || "ไม่จำเป็นถ้าไม่ใช้ @mention"} />
            <DiagRow label="token ใช้ได้จริง (LINE API)" ok={diag.token_valid}
              note={diag.display_name ? `บอท: ${diag.display_name}` : undefined} />
            <DiagRow label="ตาราง line_targets" ok={diag.targets_table_ok}
              note={diag.table_error ? diag.table_error.slice(0, 120) : undefined} />
            <p className="text-xs text-slate-400">
              กลุ่มที่ลงทะเบียน: {diag.targets_count} · personal: {diag.users_count}
            </p>
            {diag.bot_user_id_from_api && !diag.bot_user_id_set && (
              <p className="text-amber-400 text-xs">
                💡 LINE API บอกว่า Bot user ID ของคุณคือ <code>{diag.bot_user_id_from_api}</code> —
                คัดลอกไปใส่ env LINE_BOT_USER_ID บน Render เพื่อเปิด @mention detection
              </p>
            )}
            {diag.hint && <p className="text-amber-400 text-xs">{diag.hint}</p>}
          </div>
        )}

        {/* --- webhook debug: simulate + event log --- */}
        <div className="mt-3 rounded border border-slate-700 bg-surface/40 p-3">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">
            🧪 Debug webhook — จำลองข้อความ (ไม่ต้องมี LINE)
          </p>
          <div className="flex flex-wrap gap-2">
            <input value={simText} onChange={(e) => setSimText(e.target.value)}
              placeholder="พิมพ์ข้อความที่จะทดสอบ เช่น /risk หรือ วันนี้ควรเทรดไหม"
              className="flex-1 min-w-[240px] bg-surface border border-slate-700 rounded px-3 py-2 text-sm" />
            <button onClick={runSimulate} disabled={lineBusy || !simText.trim()}
              className="bg-accent text-white font-semibold rounded px-4 min-h-[44px] disabled:opacity-50 active:brightness-90">
              {lineBusy ? "กำลังรัน..." : "จำลอง"}
            </button>
            <button onClick={loadEvents} disabled={lineBusy}
              className="text-xs text-slate-400 border border-slate-700 rounded px-3 min-h-[44px] active:bg-slate-800 disabled:opacity-40">
              ดู event log
            </button>
          </div>

          {simRes && (
            <div className="mt-3 space-y-1 text-sm">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide">ผลจำลอง (pipeline เดียวกับ webhook จริง)</p>
              {simRes.steps.map((s, i) => (
                <div key={i} className="flex items-start gap-2 text-xs">
                  <span>{s.ok ? "✅" : "❌"}</span>
                  <span className="text-slate-300">{s.step}</span>
                  {s.note && <span className="text-slate-500 break-all">— {s.note}</span>}
                </div>
              ))}
              {simRes.reply && (
                <div className="mt-2 bg-surface rounded p-2">
                  <p className="text-xs text-slate-500 mb-1">
                    บอทจะตอบ ({simRes.via === "command" ? "คำสั่ง" : "AI"}):
                  </p>
                  <p className="text-xs text-slate-200 whitespace-pre-wrap break-all">{simRes.reply}</p>
                </div>
              )}
              {simRes.note && <p className="text-amber-400 text-xs">{simRes.note}</p>}
            </div>
          )}

          {events && events.events.length > 0 && (
            <div className="mt-3">
              <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-1">
                Event log ล่าสุด ({events.events.length} รายการ — 403/skipped จะโผล่ตรงนี้)
              </p>
              <div className="space-y-0.5 max-h-48 overflow-y-auto">
                {events.events.slice().reverse().map((ev, i) => (
                  <div key={i} className="text-xs flex gap-2">
                    <span className="text-slate-500 shrink-0">
                      {new Date(ev.at).toLocaleTimeString("th-TH")}
                    </span>
                    <span className={ev.kind.includes("rejected") || ev.kind.startsWith("skipped")
                      ? "text-loss" : "text-profit"}>{ev.kind}</span>
                    <span className="text-slate-500 break-all">
                      {Object.entries(ev)
                        .filter(([k]) => k !== "at" && k !== "kind")
                        .map(([k, v]) => `${k}=${String(v).slice(0, 40)}`)
                        .join(" · ")}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {events && events.events.length === 0 && (
            <p className="text-xs text-slate-500 mt-2">
              ยังไม่มี event — ถ้าเพิ่งกด Verify ใน LINE console แล้ว log ว่าง แปลว่า
              request ไม่ถึง API เลย (เช็ค webhook URL ว่าเป็น
              https://tdapp-api.onrender.com/api/line/webhook)
            </p>
          )}
        </div>

        {lineTestRes && (
          <div className="mt-3 space-y-2 text-sm">
            <div className="flex items-center gap-2">
              <span className="text-slate-400">ผลทดสอบ:</span>
              <span className={lineTestRes.ok ? "text-profit font-bold" : "text-loss font-bold"}>
                {lineTestRes.ok ? "✅ ส่งสำเร็จ" : "❌ ส่งไม่สำเร็จ"}
              </span>
              <span className="text-slate-500">
                ส่งได้ {lineTestRes.sent} / ล้มเหลว {lineTestRes.failed}
              </span>
            </div>
            {lineTestRes.results.length > 0 && (
              <div className="space-y-1">
                {lineTestRes.results.map((r) => (
                  <div key={r.target_id} className="text-xs">
                    <div className="flex items-center gap-2">
                      <span>{r.ok ? "✅" : "❌"}</span>
                      <span className="text-slate-400">{r.target_type}</span>
                      <code className="text-slate-300 break-all">{r.target_id}</code>
                    </div>
                    {r.error && <p className="text-loss ml-6 break-all">{r.error}</p>}
                  </div>
                ))}
              </div>
            )}
            {lineTestRes.hint && <p className="text-amber-400 text-xs">{lineTestRes.hint}</p>}
          </div>
        )}

        {/* --- manual groupId registration --- */}
        <div className="mt-4 rounded border border-slate-700 bg-surface/40 p-3">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">
            เพิ่ม Group ID ด้วยมือ (ถ้า auto-registration ยังไม่ทำงาน)
          </p>
          <div className="flex flex-wrap gap-2">
            <input value={newGroupId}
              onChange={(e) => setNewGroupId(e.target.value)}
              placeholder="C1234… (groupId จาก LINE console / webhook log)"
              className="flex-1 min-w-[240px] bg-surface border border-slate-700 rounded px-3 py-2 text-sm" />
            <button onClick={addGroup} disabled={lineBusy || !newGroupId.trim()}
              className="bg-accent text-white font-semibold rounded px-4 min-h-[44px] disabled:opacity-50 active:brightness-90">
              เพิ่มกลุ่ม
            </button>
          </div>
          <p className="text-xs text-slate-500 mt-1">
            groupId หาได้จาก LINE Developers Console (Webhook event log → source.groupId)
            หรือดูใน log ของ API หลัง @mention บอท
          </p>
        </div>

        <div className="mt-4">
          <p className="text-xs font-semibold text-slate-400 uppercase tracking-wide mb-2">
            กลุ่ม/แชทที่ลงทะเบียน (event.source.groupId จาก webhook)
          </p>
          {!lineTargets ? (
            <p className="text-slate-500 text-sm">กำลังโหลด... (หรือยังไม่มีข้อมูล)</p>
          ) : lineTargets.targets.length === 0 && lineTargets.users.length === 0 ? (
            <p className="text-slate-500 text-sm">
              ยังไม่มี — เพิ่มบอทเข้ากลุ่ม LINE แล้วพิมพ์ @บอท 1 ครั้ง หรือวาง Group ID ด้วยมือด้านบน
            </p>
          ) : (
            <div className="space-y-2">
              {lineTargets.targets.map((t) => (
                <div key={t.target_id}
                  className="bg-surface rounded p-3 flex items-start justify-between flex-wrap gap-2">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-semibold">
                        {t.target_type === "group" ? "👥 Group" : t.target_type === "room" ? "🏠 Room" : "👤 User"}
                      </span>
                      <span className={t.notification_enabled ? "text-profit text-xs" : "text-loss text-xs"}>
                        {t.notification_enabled ? "เปิดรับแจ้งเตือน" : "ปิดรับแจ้งเตือน"}
                      </span>
                    </div>
                    <code className="text-xs text-slate-300 break-all">{t.target_id}</code>
                    <p className="text-xs text-slate-500 mt-0.5">
                      {t.last_seen_at
                        ? `รับ event ล่าสุด: ${new Date(t.last_seen_at).toLocaleString("th-TH")}`
                        : "ยังไม่เคยรับ event"}
                    </p>
                  </div>
                  <button onClick={() => removeGroup(t.target_id)} disabled={lineBusy}
                    className="bg-loss text-white text-xs font-semibold rounded px-3 min-h-[36px] disabled:opacity-40">
                    ลบ
                  </button>
                </div>
              ))}
              {lineTargets.users.map((u) => (
                <div key={u.target_id} className="bg-surface rounded p-3">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-semibold">👤 Personal</span>
                    <span className={u.notification_enabled ? "text-profit text-xs" : "text-loss text-xs"}>
                      {u.notification_enabled ? "เปิดรับแจ้งเตือน" : "ปิดรับแจ้งเตือน"}
                    </span>
                  </div>
                  <code className="text-xs text-slate-300 break-all">{u.target_id}</code>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function DiagRow({ label, ok, note }:
  { label: string; ok?: boolean; note?: string }) {
  return (
    <div className="flex items-center gap-2 text-xs">
      <span>{ok === undefined ? "❔" : ok ? "✅" : "❌"}</span>
      <span className="text-slate-300">{label}</span>
      {note && <span className="text-slate-500 truncate">— {note}</span>}
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
