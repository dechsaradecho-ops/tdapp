"use client";

/**
 * Logs > จำลอง — barrier simulation panel.
 *
 * WHY A DEDICATED COMPONENT (owner 2026-10-06)
 * ---------------------------------------------
 * The owner asked for a "run 5,000 samples" button with a live progress bar
 * and a running TP/SL/PnL trace. That is three different concerns — job
 * control, a streaming chart, and a result report — and folding them into the
 * 2,200-line logs page would have made both harder to read, so this is its
 * own file and the page just renders <SimulateTab />.
 *
 * THE HONESTY RULE BAKED IN
 * -------------------------
 * The panel never shows an in-sample number without the out-of-sample one
 * next to it. Ranking the whole grid on a single sample is a multi-hundred-way
 * race and its winner is positive even with no edge at all — the first run of
 * this study produced +0.101R in-sample that collapsed to −0.061R out. When
 * the walk-forward says the ranking was noise, the panel says so in the
 * verdict box instead of burying it under a green number.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import type { SimCellRow, SimEvent, SimLabel, SimRecommendation, SimResult, SimRun } from "@/lib/types";

/** แสดงค่า from → to ของข้อเสนอรอบหน้า (array ย่อให้อ่านง่าย) */
const fmtVal = (v: unknown): string => {
  if (Array.isArray(v)) {
    const s = v.map((x) => String(x)).join(", ");
    return s.length > 80 ? `${v.length} ค่า: ${s.slice(0, 80)}…` : s || "—";
  }
  if (v === null || v === undefined) return "—";
  return String(v);
};

/**
 * เป้าหมาย (R) แต่ละค่าคืออะไร — R คือกำไรเป็นเท่าของความเสี่ยงต่อไม้
 * (เช่น 1.5R = ได้ 1.5 เท่าของที่ยอมเสีย). 2.5R/3.0R ถูกตัดออกจากกริดตั้งต้น
 * เพราะข้อมูล 5,000 ตัวอย่างบอกว่ามีไม่ถึง ~2% ของสัญญาณที่ไปถึง +2R
 */
const TP_R_MEANING: Record<string, string> = {
  "0.5": "เป้าสั้นครึ่งความเสี่ยง — ถึงง่ายสุด แต่ได้ครึ่งเดียวของที่ยอมเสีย",
  "0.75": "เป้าสั้น — ถึงง่าย ได้ 0.75 เท่าของความเสี่ยง",
  "1": "เท่าทุนความเสี่ยง — กำไรเท่าที่เสียได้ (1:1)",
  "1.25": "เป้ากลาง — กำไร 1.25 เท่าของความเสี่ยง",
  "1.5": "เป้ากลาง (ค่าที่ระบบจริงใช้อยู่) — ต้องชนะ ~40% ถึงคุ้มทุน",
  "2": "เป้าไกล — กำไร 2 เท่า แต่มีแค่ ~2% ของสัญญาณที่ไปถึง",
  "2.5": "เป้าไกลมาก — ตัดออกจากกริดตั้งต้น: ไปถึงน้อยมาก",
  "3": "เป้าไกลสุด — ตัดออกจากกริดตั้งต้น: ไปถึงน้อยมาก",
};

const tpMeaning = (v: unknown): string =>
  TP_R_MEANING[String(v)] ?? "";

const POLL_MS = 1000;
const PAGE = 500;

const STAGE_TH: Record<string, string> = {
  queued: "เข้าคิว",
  fetching: "ดึงราคาย้อนหลัง",
  replaying: "เล่นสัญญาณย้อนหลัง",
  labelling: "คำนวณ TP / SL / PnL",
  analysing: "วิเคราะห์ผล",
  running: "กำลังรัน",
  done: "เสร็จแล้ว",
  failed: "ล้มเหลว",
  cancelled: "ถูกหยุด",
};

/** Bar colour + Thai label for each barrier outcome. */
const LABEL_TH: Record<SimLabel, string> = {
  tp: "TP",
  sl: "SL",
  expired: "หมดเวลา",
  pending: "รอข้อมูล",
};

const LABEL_COLOR: Record<SimLabel, string> = {
  tp: "text-profit",
  sl: "text-loss",
  expired: "text-slate-400",
  pending: "text-slate-500",
};

const num = (v: unknown, d = 2): string => {
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "—";
};
const signed = (v: unknown, d = 3): string => {
  const n = Number(v);
  return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(d)}` : "—";
};
const int = (v: unknown): string => {
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("th-TH") : "—";
};

export default function SimulateTab() {
  const [runs, setRuns] = useState<SimRun[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<SimRun | null>(null);
  const [events, setEvents] = useState<SimEvent[]>([]);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");
  const [hint, setHint] = useState("");
  const [target, setTarget] = useState(5000);
  const [cooldown, setCooldown] = useState(1);
  const [days, setDays] = useState(1095);
  const [follow, setFollow] = useState(true);
  const [exporting, setExporting] = useState(false);
  const logRef = useRef<HTMLDivElement | null>(null);

  const seqRef = useRef(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const runIdRef = useRef<string | null>(null);
  runIdRef.current = runId;
  // Which run the events on screen belong to. seq counts alone cannot tell
  // runs apart (two finished runs both end at seq 5000), so without this a
  // switch between two completed runs would keep showing the old trace.
  const loadedIdRef = useRef<string | null>(null);

  // ---- running aggregates, derived from the stream -----------------------
  // Kept client-side on purpose: the server would have to re-read every event
  // row each poll to produce the same numbers.
  const stats = useMemo(() => {
    let tp = 0, sl = 0, ex = 0, wins = 0, sum = 0;
    const curve: number[] = [];
    for (const e of events) {
      const r = Number(e.r_multiple || 0);
      if (e.label === "tp") tp++;
      else if (e.label === "sl") sl++;
      else if (e.label === "expired") ex++;
      sum += r;
      if (r > 0) wins++;
      curve.push(sum);
    }
    const n = events.length;
    return {
      n, tp, sl, ex, wins, sum,
      meanR: n ? sum / n : 0,
      wr: n ? (100 * wins) / n : 0,
      curve,
    };
  }, [events]);

  const loadRuns = useCallback(async () => {
    try {
      const r = await api.simRuns(20);
      setRuns(r.runs || []);
      setHint(r.setup_required ? r.hint || "" : "");
      if (r.active_run_id) setRunId((cur) => cur ?? r.active_run_id!);
      if (!r.active_run_id && r.runs?.length && !runIdRef.current) {
        setRunId(r.runs[0].id);
      }
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => { void loadRuns(); }, [loadRuns]);

  // ---- poll the run + stream events --------------------------------------
  const pull = useCallback(async () => {
    const id = runIdRef.current;
    if (!id) return;
    try {
      const st = await api.simRun(id);
      // The user may have clicked another run while this request was in
      // flight — dropping a late response beats showing run A's verdict
      // under run B's header.
      if (runIdRef.current !== id) return;
      setRun({ ...st, result: st.result || {} });

      // Reset the trace when switching to a different run.
      if (loadedIdRef.current !== id) {
        loadedIdRef.current = id;
        seqRef.current = 0;
        setEvents([]);
      }

      let guard = 0;
      // Drain in pages so a fast run cannot outrun a single request.
      for (;;) {
        const page = await api.simEvents(id, seqRef.current, PAGE);
        if (runIdRef.current !== id) return;
        if (!page.events?.length) break;
        setEvents((prev) => [...prev, ...page.events].slice(-6000));
        seqRef.current = page.events[page.events.length - 1].seq;
        if (!page.more || guard++ > 40) break;
      }

      if (st.status === "done" || st.status === "failed" || st.status === "cancelled") {
        void loadRuns();
      }
    } catch (e) {
      setErr(String(e));
    }
  }, [loadRuns]);

  const selectRun = useCallback((id: string) => {
    if (id === runIdRef.current && loadedIdRef.current === id) return;
    seqRef.current = 0;
    setEvents([]);
    setRun(null);
    setRunId(id);
  }, []);

  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (!runId) return;
    void pull();
    const live = run && (run.status === "pending" || run.status === "running");
    if (live) timerRef.current = setInterval(() => { void pull(); }, POLL_MS);
    return () => { if (timerRef.current) clearInterval(timerRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, run?.status]);

  // Auto-scroll the live trade log to the newest row.
  useEffect(() => {
    if (follow && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [events, follow]);

  const start = async () => {
    setStarting(true);
    setErr("");
    try {
      const res = await api.simStart({
        target_events: target,
        cooldown,
        days,
      });
      if (!res.ok) {
        setErr(res.error || "เริ่มไม่สำเร็จ");
        return;
      }
      seqRef.current = 0;
      setEvents([]);
      setRun(null);
      setRunId(res.run_id!);
      await loadRuns();
    } catch (e) {
      setErr(String(e));
    } finally {
      setStarting(false);
    }
  };

  const cancel = async () => {
    if (!runId) return;
    try {
      const res = await api.simCancel(runId);
      if (!res.ok) setErr(res.error || "หยุดไม่สำเร็จ");
    } catch (e) {
      setErr(String(e));
    }
  };

  // Download this run's samples as CSV — the same rows the chart streams.
  const downloadCsv = async () => {
    if (!runId) return;
    setExporting(true);
    setErr("");
    try {
      const blob = await api.simExport(runId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `simulation-${runId.slice(0, 8)}.csv`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 5000);
    } catch (e) {
      setErr(String(e));
    } finally {
      setExporting(false);
    }
  };

  // Apply the run's own proposal to the NEXT simulation run. The payload
  // carries only simulation parameters (grid / gate / assets) — there is no
  // code path here that can reach live trading settings.
  const applyRec = async (next: SimRecommendation["next_config"]) => {
    setStarting(true);
    setErr("");
    try {
      const res = await api.simStart({
        target_events: run?.target_events ?? target,
        cooldown,
        days: run?.config?.days ?? days,
        ...next,
      });
      if (!res.ok) {
        setErr(res.error || "เริ่มไม่สำเร็จ");
        return;
      }
      selectRun(res.run_id!);
      await loadRuns();
    } catch (e) {
      setErr(String(e));
    } finally {
      setStarting(false);
    }
  };

  const busy = run?.status === "pending" || run?.status === "running";
  const pct = run && run.target_events
    ? Math.min(100, Math.round((100 * run.processed) / run.target_events))
    : run?.total_events
      ? Math.min(100, Math.round((100 * run.processed) / run.total_events))
      : 0;

  const res = run?.result || {};
  const wf = res.walk_forward;

  return (
    <div className="space-y-3">
      {/* ---------------- controls ---------------- */}
      <section className="panel p-4">
        <div className="flex items-center justify-between gap-2 mb-3">
          <h2 className="panel-title mb-0">
            <Icon n="bot" size={14} className="inline mr-1" />
            จำลองสัญญาณด้วย Triple Barrier
          </h2>
        </div>
        <p className="text-xs text-slate-400 mb-3">
          ดึงราคาย้อนหลังจริง เล่นเครื่องสร้างสัญญาณตัวเดียวกับตอนเทรดจริง
          แล้ววางกำแพง TP/SL หลายขนาดลงไปดูว่าตลาดแตะกำแพงไหนก่อน
          {" "}— เพื่อหาว่า SL กว้างพอดีเท่าไหร่ และเป้าหมายที่กี่ R
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
          <Field label="จำนวนตัวอย่าง" value={target} min={50} max={20000}
                 onChange={setTarget} />
          <Field label="คั่งกันซ้ำ (แท่ง)" value={cooldown} min={0} max={30}
                 onChange={setCooldown} />
          <Field label="ย้อนหลัง (วัน)" value={days} min={180} max={3650}
                 onChange={setDays} />
          <div className="flex items-end">
            <button onClick={start} disabled={starting || !!busy}
              className="w-full px-4 py-2 rounded bg-accent text-white font-bold disabled:opacity-50 min-h-[40px]">
              {starting ? "กำลังเริ่ม..." : busy ? "กำลังรันอยู่" : "รันจำลอง"}
            </button>
          </div>
        </div>
        {busy && (
          <button onClick={cancel}
            className="px-3 py-1.5 rounded border border-loss text-loss text-xs min-h-[36px]">
            หยุด
          </button>
        )}
        {hint && (
          <p className="mt-3 text-xs text-amber-300 rounded border border-amber-400/25 bg-amber-400/10 p-2">
            {hint}
          </p>
        )}
        {err && <p className="mt-2 text-loss text-sm">{err}</p>}
        {run?.error && (
          <p className="mt-2 text-loss text-xs">รันไม่สำเร็จ: {run.error}</p>
        )}
      </section>

      {/* ---------------- progress + live counters ---------------- */}
      {run && (
        <section className="panel p-4">
          {/* Which run is on screen — past results load here when picked
              from the history below, so the header has to say so. */}
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="text-xs text-slate-400">
              {runs.length > 1 && runId !== runs[0]?.id
                ? `กำลังดูผลรันเก่า (${run.created_at
                    ? String(run.created_at).slice(5, 16).replace("T", " ")
                    : run.id.slice(0, 8)}) — ไม่ใช่งานล่าสุด`
                : `ผลรัน (${run.created_at
                    ? String(run.created_at).slice(5, 16).replace("T", " ")
                    : run.id.slice(0, 8)})`}
            </span>
            <div className="flex gap-2">
              <button onClick={downloadCsv} disabled={exporting || !runId}
                title="ดาวน์โหลดตัวอย่างทั้งหมดของรันนี้เป็น CSV (แถวเดียวกับที่เห็นในกราฟ)"
                className="text-[11px] px-2 py-1 rounded border border-slate-700 text-slate-300 disabled:opacity-50 min-h-[32px]">
                {exporting ? "กำลังเตรียม..." : "ดาวน์โหลด CSV"}
              </button>
              {runs.length > 1 && runId !== runs[0]?.id && (
                <button onClick={() => selectRun(runs[0].id)}
                  className="text-[11px] px-2 py-1 rounded border border-slate-700 text-slate-300 min-h-[32px]">
                  ← กลับไปงานล่าสุด
                </button>
              )}
            </div>
          </div>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-slate-300">
              ขั้นตอน: <span className="text-white font-bold">
                {STAGE_TH[run.stage] || run.stage}
              </span>
              {run.status === "running" && (
                <span className="text-accent ml-2 animate-pulse">●</span>
              )}
            </span>
            <span className="text-slate-400">
              {int(run.processed)}
              {run.target_events ? ` / ${int(run.target_events)}` : ""} ตัวอย่าง
              {run.total_events ? ` (มีประวัติให้ ${int(run.total_events)})` : ""}
            </span>
          </div>
          <div className="h-2.5 rounded bg-slate-800 overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                run.status === "failed" ? "bg-loss"
                  : run.status === "cancelled" ? "bg-slate-500"
                  : "bg-accent"}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="text-[10px] text-slate-500 mt-1">{pct}%</div>

          <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mt-3">
            <Metric label="ตัวอย่าง" value={int(stats.n)} />
            <Metric label="TP" value={int(stats.tp)} tone="text-profit" />
            <Metric label="SL" value={int(stats.sl)} tone="text-loss" />
            <Metric label="หมดเวลา" value={int(stats.ex)} />
            <Metric label="Win rate" value={`${num(stats.wr, 1)}%`}
                    tone={stats.wr >= 50 ? "text-profit" : "text-loss"} />
            <Metric label="สะสม R" value={signed(stats.sum, 1)} bold
                    tone={stats.sum > 0 ? "text-profit"
                      : stats.sum < 0 ? "text-loss" : ""} />
          </div>
          <p className="text-[10px] text-slate-500 mt-2">
            เซลล์ที่นับสด: SL {num(run.config?.sl_multiples?.[0] ?? 1.5, 2)}×ATR
            {" · "}เป้า {num(run.config?.tp_rs?.[0] ?? 1.5, 2)}R
            {" · "}ถือสูงสุด {int(run.config?.max_bars?.[2] ?? 20)} แท่ง
          </p>
        </section>
      )}

      {/* ---------------- live chart + trade log ---------------- */}
      {run && stats.n > 0 && (
        <section className="panel p-4">
          <h3 className="panel-title">ผลสด — สะสม R ต่อเหตุการณ์</h3>
          <EquityCurve points={stats.curve} tp={stats.tp} sl={stats.sl} ex={stats.ex} />
          <div className="flex items-center justify-between mt-3 mb-1">
            <h3 className="panel-title mb-0">เหตุการณ์ล่าสุด (เข้า/ออก/TP/SL/PnL)</h3>
            <label className="text-[10px] text-slate-400 flex items-center gap-1 cursor-pointer">
              <input type="checkbox" checked={follow}
                     onChange={(e) => setFollow(e.target.checked)} />
              เลื่อนตาม
            </label>
          </div>
          <div ref={logRef}
               className="overflow-y-auto scroll-x-thin text-xs font-mono max-h-72 rounded border border-slate-800">
            {events.slice(-300).reverse().map((e) => (
              <div key={e.seq}
                   className="flex gap-2 px-2 py-0.5 border-b border-slate-800/50 whitespace-nowrap">
                <span className="text-slate-600 w-12 text-right">{e.seq}</span>
                <span className="text-slate-300 w-16">{e.asset}</span>
                <span className={e.direction === "BUY" ? "text-profit w-10"
                  : "text-loss w-10"}>
                  {e.direction === "BUY" ? "▲" : "▼"}
                </span>
                <span className="text-slate-400 w-20">
                  {num(e.entry, e.entry && e.entry > 10 ? 2 : 5)}
                </span>
                <span className={`w-10 font-bold ${LABEL_COLOR[e.label]}`}>
                  {LABEL_TH[e.label]}
                </span>
                <span className="text-slate-500 w-10">{e.bars_held ?? "-"}d</span>
                <span className={Number(e.r_multiple) > 0 ? "text-profit w-14"
                  : Number(e.r_multiple) < 0 ? "text-loss w-14" : "text-slate-400 w-14"}>
                  {signed(e.r_multiple, 2)}R
                </span>
                {e.ambiguous && (
                  <span className="text-amber-400" title="แท่งเดียวแตะทั้ง TP และ SL — นับเป็น SL ( conservative )">
                    ⚠
                  </span>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ---------------- verdict ---------------- */}
      {run && (run.status === "done" || run.status === "cancelled") && (
        <Verdict res={res} status={run.status} busy={!!busy}
                 starting={starting} onApply={applyRec} />
      )}

      {/* ---------------- run history ---------------- */}
      {/* Every past run stays here with its verdict summary; tapping a row
          loads the FULL result (chart + trade trace + verdict tables) into
          the sections above. */}
      {runs.length > 0 && (
        <section className="panel p-4">
          <h3 className="panel-title">
            ประวัติการจำลอง ({runs.length}) — แตะแถวเพื่อดูผลเต็ม
          </h3>
          <div className="overflow-x-auto scroll-x-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500 border-b border-slate-800">
                  <th className="py-2 pr-3">เวลา</th>
                  <th className="py-2 pr-3">สถานะ</th>
                  <th className="py-2 pr-3">ตัวอย่าง</th>
                  <th className="py-2 pr-3">ช่องดีสุด</th>
                  <th className="py-2 pr-3">เป้าหมาย</th>
                  <th className="py-2 pr-3">ผลนอกตัว</th>
                  <th className="py-2">ดูผล</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const w = r.result?.walk_forward;
                  const top = r.result?.top_paid?.[0];
                  const selected = r.id === runId;
                  return (
                    <tr key={r.id}
                        onClick={() => selectRun(r.id)}
                        className={`border-b border-slate-800/50 hover:bg-white/[0.04] cursor-pointer ${
                          selected ? "bg-white/[0.06]" : ""}`}>
                      <td className="py-1.5 pr-3 text-slate-400 whitespace-nowrap">
                        {r.created_at ? String(r.created_at).slice(5, 16).replace("T", " ") : "—"}
                        {selected && <span className="text-accent"> ●</span>}
                      </td>
                      <td className="py-1.5 pr-3">
                        <span className={r.status === "done" ? "text-profit"
                          : r.status === "failed" ? "text-loss" : "text-slate-400"}>
                          {STAGE_TH[r.status] || r.status}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3">{int(r.processed)}</td>
                      <td className="py-1.5 pr-3">
                        {top ? `${num(top.sl_pct, 2)}×ATR` : "—"}
                      </td>
                      <td className="py-1.5 pr-3">{top ? `${num(top.tp_r, 2)}R` : "—"}</td>
                      <td className="py-1.5 pr-3">
                        {w && w.holds_out !== undefined
                          ? (w.holds_out
                            ? <span className="text-profit">รอด {signed(w.test_mean_r, 3)}R</span>
                            : <span className="text-loss">พัง {signed(w.test_mean_r, 3)}R</span>)
                          : "—"}
                      </td>
                      <td className="py-1.5">
                        <span className="px-2 py-1 rounded border border-slate-700 text-slate-300 whitespace-nowrap">
                          {selected ? "กำลังดู" : "ดูผล"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </section>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* pieces                                                             */
/* ------------------------------------------------------------------ */

function Field({ label, value, min, max, onChange }: {
  label: string; value: number; min: number; max: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="text-slate-300">{label}</span>
      <input
        type="number" value={value} min={min} max={max}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) onChange(Math.max(min, Math.min(max, n)));
        }}
        className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2"
      />
    </label>
  );
}

function Metric({ label, value, tone = "", bold }: {
  label: string; value: string; tone?: string; bold?: boolean;
}) {
  return (
    <div className="rounded border border-slate-700/60 bg-surface/40 p-2">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`text-base ${bold ? "font-bold" : "font-semibold"} ${tone}`}>
        {value}
      </div>
    </div>
  );
}

/**
 * Cumulative-R curve as inline SVG.
 *
 * Hand-rolled because the project ships no chart library, and because this
 * needs exactly one thing: a polyline that grows, a zero line, and dots
 * coloured by which barrier was hit. The zero line is not decoration — a
 * curve sitting above it is the only honest read of "this made money", and
 * without it a viewer cannot tell +0.05R from -0.05R on a flat axis.
 */
function EquityCurve({ points, tp, sl, ex }: {
  points: number[]; tp: number; sl: number; ex: number;
}) {
  const W = 900, H = 180, PAD = 24;
  if (points.length < 2) {
    return (
      <p className="text-xs text-slate-500 py-6 text-center">
        กำลังสะสมข้อมูลเพื่อวาดกราฟ...
      </p>
    );
  }
  // Downsample: 5,000 points in an SVG is a 40KB attribute string for a
  // shape the eye cannot resolve.
  const step = Math.max(1, Math.floor(points.length / 300));
  const pts = points.filter((_, i) => i % step === 0);
  if (pts[pts.length - 1] !== points[points.length - 1]) {
    pts.push(points[points.length - 1]);
  }

  const min = Math.min(0, ...pts);
  const max = Math.max(0, ...pts);
  const span = (max - min) || 1;
  const x = (i: number) => PAD + (i / (pts.length - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2);

  const d = pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const last = pts[pts.length - 1];
  const up = last >= 0;

  return (
    <div className="mt-1">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto rounded bg-surface/40"
           preserveAspectRatio="none" role="img"
           aria-label={`สะสม R จบที่ ${last.toFixed(2)}R`}>
        <line x1={PAD} x2={W - PAD} y1={y(0)} y2={y(0)}
              stroke="#475569" strokeWidth="1" strokeDasharray="4 4" />
        <text x={PAD + 2} y={y(0) - 4} fill="#64748b" fontSize="10">0R</text>
        <text x={PAD + 2} y={y(max) + 11} fill="#64748b" fontSize="10">
          {max.toFixed(1)}
        </text>
        <text x={PAD + 2} y={y(min) - 3} fill="#64748b" fontSize="10">
          {min.toFixed(1)}
        </text>
        <path d={d} fill="none"
              stroke={up ? "#22c55e" : "#ef4444"} strokeWidth="1.6" />
        <circle cx={x(pts.length - 1)} cy={y(last)} r="3"
                fill={up ? "#22c55e" : "#ef4444"} />
      </svg>
      <div className="flex gap-3 text-[10px] text-slate-400 mt-1">
        <span>🟢 TP {int(tp)}</span>
        <span>🔴 SL {int(sl)}</span>
        <span>⚪ หมดเวลา {int(ex)}</span>
        <span className="ml-auto">
          ตัวอย่างสุดท้าย {int(points.length)} · จบที่ {signed(last, 2)}R
        </span>
      </div>
    </div>
  );
}

function Verdict({ res, status, busy, starting, onApply }: {
  res: SimResult; status: string; busy: boolean; starting: boolean;
  onApply: (next: SimRecommendation["next_config"]) => void;
}) {
  const top = res.top_paid || [];
  const wf = res.walk_forward;
  const per = res.per_asset || [];
  const mfe = res.mfe_mae;
  const holds = wf?.holds_out;
  const testR = wf?.test_mean_r;
  const gridCells = res.grid_cells ?? 0;
  const gate = res.gate_sweep;
  const bestGate = res.best_gate;
  const pb = res.production_benchmark;
  const rec = res.recommendation;

  return (
    <>
      <section className="panel p-4">
        <h3 className="panel-title">ผลการวิเคราะห์</h3>
        {status === "cancelled" && (
          <p className="text-xs text-slate-400 mb-2">
            งานถูกหยุดก่อนครบ — ตัวเลขข้างล่างมาจากเฉพาะส่วนที่ประมวลผลแล้ว
          </p>
        )}
        {res.error && <p className="text-loss text-sm">{res.error}</p>}

        {/* The verdict box comes FIRST and states the out-of-sample result,
            because that is the only number that has been tested. */}
        {wf && !wf.error && (
          <div className={`rounded border p-3 mb-3 text-xs ${
            holds
              ? "border-amber-400/25 bg-amber-400/10"
              : "border-loss/30 bg-loss/10"}`}>
            <div className="font-bold mb-1">
              {holds
                ? "⚠️ อันดับ 1 รอดนอกตัวอย่าง — แต่ยังไม่ถือว่ายืนยันแล้ว"
                : "❌ อันดับ 1 พังนอกตัวอย่าง — ตัวเลขที่ได้เป็นสัญญาณรบกวน"}
            </div>
            <div className="text-slate-300">
              เรียงอันดับช่วงแรก (ในตัว):{" "}
              <span className="font-bold text-white">
                {signed(wf.winner?.train_mean_r ?? 0, 3)}R
              </span>{" "}
              → วัดช่วงหลัง (นอกตัว):{" "}
              <span className={`font-bold ${holds ? "text-profit" : "text-loss"}`}>
                {signed(testR ?? 0, 3)}R
              </span>{" "}
              (win rate {num(wf.test_win_rate_pct, 1)}%, n={int(wf.test_n)})
            </div>
            <p className="text-slate-400 mt-1">
              การจัดอันดับ {int(gridCells)} ช่องกำแพง
              {res.gate_candidates ? ` × ${int(res.gate_candidates)} เกณฑ์กรอง` : ""}
              {" "}บนข้อมูลชุดเดียวกัน — ผู้ชนะจะบวกเสมอแม้ไม่มี edge
              {wf?.candidates_checked
                ? ` · จากที่ตรวจ ${int(wf.candidates_checked)} อันดับ ยังบวกนอกตัวอย่าง ${int(wf.survivors ?? 0)} อัน`
                : ""}
            </p>
          </div>
        )}
        {wf?.error && (
          <p className="text-xs text-slate-400 mb-2">{wf.error}</p>
        )}

        {/* ---- next round: what changes (from -> to), applied to the NEXT
                SIMULATION run only. Live trading settings are never touched
                here; a live suggestion is text that must be confirmed by hand
                in Settings. ---- */}
        {rec && (rec.has_plan || rec.note) && (
          <div className="rounded border border-accent/30 bg-accent/5 p-3 mb-3">
            <div className="text-xs font-bold text-slate-200 mb-1">
              รอบหน้า: ปรับอะไร (เฉพาะการจำลอง — ไม่แตะเทรดจริง)
            </div>
            {rec.changes.map((c, i) => (
              <div key={i} className="text-[11px] text-slate-300 mb-2">
                <span className="font-semibold text-white">{c.field_th}</span>
                {": "}
                <span className="line-through text-loss/80">{fmtVal(c.from)}</span>
                {" → "}
                <span className="font-bold text-profit">{fmtVal(c.to)}</span>
                <div className="text-slate-500">{c.reason}</div>
                {c.field === "tp_rs" && Array.isArray(c.to) && (
                  <ul className="mt-1 space-y-0.5">
                    {c.to.map((v) => (
                      <li key={String(v)} className="text-slate-400">
                        <span className="font-semibold text-slate-200">
                          {String(v)}R
                        </span>
                        {tpMeaning(v) ? ` — ${tpMeaning(v)}` : ""}
                      </li>
                    ))}
                    {Array.isArray(c.from) &&
                      c.from.filter((v) => !(c.to as unknown[]).includes(v))
                        .map((v) => (
                          <li key={String(v)} className="text-slate-500">
                            <span className="line-through">
                              {String(v)}R — ตัดออก
                            </span>
                            {tpMeaning(v) ? ` (${tpMeaning(v)})` : ""}
                          </li>
                        ))}
                  </ul>
                )}
              </div>
            ))}
            {rec.note && !rec.has_plan && (
              <p className="text-[11px] text-slate-400">{rec.note}</p>
            )}
            {rec.has_plan && (
              <button onClick={() => onApply(rec.next_config)}
                disabled={starting || busy}
                className="mt-2 px-4 py-2 rounded bg-accent text-white text-xs font-bold disabled:opacity-50 min-h-[40px]">
                {starting ? "กำลังเริ่ม..." : "ใช้ค่านี้รันรอบหน้า"}
              </button>
            )}
          </div>
        )}
        {rec?.live_suggestion && (
          <div className="rounded border border-amber-400/25 bg-amber-400/10 p-3 mb-3">
            <div className="text-xs font-bold text-amber-200 mb-1">
              ข้อเสนอสำหรับ setting จริง — ยังไม่เปลี่ยน
            </div>
            {rec.live_suggestion.changes.map((c, i) => (
              <div key={i} className="text-[11px] text-slate-300">
                <span className="font-semibold">{c.field_th}</span>
                {": "}
                <span className="line-through text-loss/80">{fmtVal(c.from)}</span>
                {" → "}
                <span className="font-bold">{fmtVal(c.to)}</span>
              </div>
            ))}
            <p className="text-[10px] text-slate-400 mt-1">
              ตัวเต็งนอกตัว {signed(rec.live_suggestion.candidate_test_r, 3)}R
              เทียบของจริง {signed(rec.live_suggestion.production_test_r, 3)}R
              {" · "}{rec.live_suggestion.note}
            </p>
          </div>
        )}

        {mfe && mfe.mfe_median !== undefined && (
          <div className="rounded border border-slate-700/60 bg-surface/40 p-3 mb-3">
            <div className="text-xs font-bold text-slate-300 mb-1">
              เป้าหมายแตะได้จริงไหม (MFE — กำไรสูงสุดที่สัญญาณเสนอ)
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px]">
              <Kv k="มัธยฐาน" v={signed(mfe.mfe_median, 2) + "R"} />
              <Kv k="p75" v={signed(mfe.mfe_p75, 2) + "R"} />
              <Kv k="p95" v={signed(mfe.mfe_p95, 2) + "R"} />
              <Kv k="สูงสุด" v={signed(mfe.mfe_max, 2) + "R"} />
            </div>
            <div className="grid grid-cols-3 gap-2 text-[11px] mt-2">
              <Kv k="ไปถึง ≥1R" v={`${num(mfe.reached_1r_pct, 0)}%`} />
              <Kv k="ไปถึง ≥1.5R" v={`${num(mfe.reached_1_5r_pct, 0)}%`} />
              <Kv k="ไปถึง ≥2R" v={`${num(mfe.reached_2r_pct, 0)}%`} />
            </div>
            {mfe.cell && (
              <p className="text-[10px] text-slate-500 mt-1">คำนวณจากเซลล์ {mfe.cell}</p>
            )}
            <p className="text-[10px] text-slate-500 mt-1">
              ถ้าสัดส่วนที่ไปถึง ≥2R น้อยมาก แปลว่าเป้าหมายที่ตั้งไว้อยู่ไกลเกินสิ่งที่
              สัญญาณให้จริง — ไม้ที่หมดเวลาโดยไม่ได้อะไรคือการผูกเงินทิ้งฟรี
            </p>
          </div>
        )}

        {/* ---- production benchmark: the config that is running right now,
                scored on exactly the same data and split as every candidate.
                Without this the table below is a list with nothing to compare
                it to. ---- */}
        {pb?.available && pb.train && pb.test && (
          <div className="rounded border border-slate-700/60 bg-surface/40 p-3 mb-3">
            <div className="text-xs font-bold text-slate-300 mb-1">
              ค่าที่ระบบจริงใช้อยู่ตอนนี้ (เทียบบนข้อมูลชุดเดียวกัน)
            </div>
            <div className="text-[10px] text-slate-500 mb-1">
              SL {pb.config?.sl_distance_mode} ({num(pb.config?.sl_atr_mult, 2)}×ATR,
              clamp {num(pb.config?.sl_min_pct, 2)}–{num(pb.config?.sl_max_pct, 2)}%)
              · เป้า {num(pb.config?.rr_target, 2)}R · gate
              opp ≥{num(pb.config?.min_opportunity, 0)} conf ≥{num(pb.config?.min_confidence, 0)}
            </div>
            <div className="grid grid-cols-3 gap-2 text-[11px]">
              <Kv k="ช่วงแรก" v={`${signed(pb.train.mean_r, 3)}R (n=${int(pb.train.n)})`} />
              <Kv k="ช่วงหลัง" v={
                <span className={pb.test.mean_r > 0 ? "text-profit" : "text-loss"}>
                  {signed(pb.test.mean_r, 3)}R (n={int(pb.test.n)})
                </span>} />
              <Kv k="WR ช่วงหลัง" v={`${num(pb.test.win_rate_pct, 1)}%`} />
            </div>
          </div>
        )}
        {pb && pb.available === false && (
          <p className="text-[11px] text-slate-500 mb-2">
            อ่านค่าของระบบจริงเพื่อใช้เทียบไม่ได้: {pb.reason}
          </p>
        )}

        {/* ---- gate sweep: the simulation's own thresholds, never inherited
                from production ---- */}
        {gate && gate.length > 0 && (
          <>
            <h4 className="text-xs font-bold text-slate-300 mb-1">
              เกณฑ์กรองสัญญาณ (เลือกจากช่วงแรกเท่านั้น — ไม่ใช่ค่าของระบบจริง)
            </h4>
            <div className="overflow-x-auto scroll-x-thin mb-3">
              <table className="w-full text-xs">
                <thead>
                  <tr className="text-left text-slate-500 border-b border-slate-800">
                    <th className="py-1.5 pr-3">minOpp</th>
                    <th className="py-1.5 pr-3">minConf</th>
                    <th className="py-1.5 pr-3">n ช่วงแรก</th>
                    <th className="py-1.5 pr-3">R ช่วงแรก</th>
                    <th className="py-1.5 pr-3">n ช่วงหลัง</th>
                    <th className="py-1.5 pr-3">R ช่วงหลัง</th>
                    <th className="py-1.5">WR ช่วงหลัง</th>
                  </tr>
                </thead>
                <tbody>
                  {gate.map((g, i) => (
                    <tr key={i}
                        className={`border-b border-slate-800/50 ${
                          i === 0 ? "bg-white/[0.05]" : ""}`}>
                      <td className="py-1 pr-3">{num(g.min_opp, 0)}</td>
                      <td className="py-1 pr-3">{num(g.min_conf, 0)}</td>
                      <td className="py-1 pr-3">{int(g.train_n)}</td>
                      <td className={`py-1 pr-3 font-bold ${
                        g.train_mean_r > 0 ? "text-profit" : "text-loss"}`}>
                        {signed(g.train_mean_r, 3)}
                      </td>
                      <td className="py-1 pr-3">{int(g.test_n)}</td>
                      <td className={`py-1 pr-3 font-bold ${
                        g.test_mean_r > 0 ? "text-profit" : "text-loss"}`}>
                        {signed(g.test_mean_r, 3)}
                      </td>
                      <td className="py-1">{num(g.test_win_rate_pct, 1)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {bestGate && (
              <p className="text-[11px] text-slate-400 mb-3">
                เลือกได้: opp ≥{num(bestGate.min_opp, 0)} conf ≥{num(bestGate.min_conf, 0)}
                {" "}(เลือกจากช่วงแรก) → ช่วงหลัง {signed(bestGate.test_mean_r, 3)}R
                {" "}{bestGate.test_mean_r !== null && bestGate.test_mean_r <= 0
                  ? "— ยังไม่รอด"
                  : "— รอด"}
              </p>
            )}
          </>
        )}

        <h4 className="text-xs font-bold text-slate-300 mb-1">
          10 ช่องที่จ่ายดีที่สุด (ในตัวอย่าง)
        </h4>
        {/* เป้า (R) แต่ละค่าคืออะไร — R คือกำไรเป็นเท่าของความเสี่ยงต่อไม้ */}
        <ul className="text-[10px] text-slate-500 mb-2 space-y-0.5">
          {["0.5", "0.75", "1", "1.25", "1.5", "2"].map((v) => (
            <li key={v}>
              <span className="font-semibold text-slate-300">{v}R</span>
              {` — ${TP_R_MEANING[v]}`}
            </li>
          ))}
        </ul>
        <div className="overflow-x-auto scroll-x-thin">
          <table className="w-full text-xs">
            <thead>
              <tr className="text-left text-slate-500 border-b border-slate-800">
                <th className="py-1.5 pr-3">SL×ATR</th>
                <th className="py-1.5 pr-3"
                    title="เป้าหมายกำไรเป็นเท่าของความเสี่ยง (R) — ดูความหมายแต่ละค่าด้านบน">
                  เป้า
                </th>
                <th className="py-1.5 pr-3">นาฬิกา</th>
                <th className="py-1.5 pr-3">n</th>
                <th className="py-1.5 pr-3">WR%</th>
                <th className="py-1.5 pr-3">mean R</th>
                <th className="py-1.5 pr-3">เฉพาะที่แตะราคา</th>
                <th className="py-1.5 pr-3">payoff</th>
                <th className="py-1.5">กำกวม</th>
              </tr>
            </thead>
            <tbody>
              {top.map((r, i) => (
                <tr key={i} className="border-b border-slate-800/50">
                  <td className="py-1 pr-3">{num(r.sl_pct, 2)}</td>
                  <td className="py-1 pr-3" title={tpMeaning(r.tp_r)}>
                    {num(r.tp_r, 2)}R
                  </td>
                  <td className="py-1 pr-3">{int(r.n)}</td>
                  <td className="py-1 pr-3">{int(r.n)}</td>
                  <td className="py-1 pr-3">{num(r.win_rate_pct, 1)}</td>
                  <td className={`py-1 pr-3 font-bold ${r.mean_r > 0 ? "text-profit" : "text-loss"}`}>
                    {signed(r.mean_r, 3)}
                  </td>
                  <td className="py-1 pr-3">{signed(r.mean_r_resolved, 3)}</td>
                  <td className="py-1 pr-3">{num(r.payoff, 2)}</td>
                  <td className="py-1 text-slate-500">{num(r.ambiguous_pct, 1)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {per.length > 0 && (
        <section className="panel p-4">
          <h3 className="panel-title">รายสินทรัพย์ (คัดจากช่วงแรกเท่านั้น)</h3>
          <div className="overflow-x-auto scroll-x-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500 border-b border-slate-800">
                  <th className="py-1.5 pr-3">คู่</th>
                  <th className="py-1.5 pr-3">ช่วงแรก R</th>
                  <th className="py-1.5 pr-3">WR%</th>
                  <th className="py-1.5 pr-3">n</th>
                  <th className="py-1.5 pr-3">ช่วงหลัง R</th>
                  <th className="py-1.5 pr-3">WR%</th>
                  <th className="py-1.5">ตัดสิน</th>
                </tr>
              </thead>
              <tbody>
                {per.map((r) => (
                  <tr key={r.asset} className="border-b border-slate-800/50">
                    <td className="py-1 pr-3 font-semibold">{r.asset}</td>
                    <td className={`py-1 pr-3 ${r.train_mean_r > 0 ? "text-profit" : "text-loss"}`}>
                      {signed(r.train_mean_r, 3)}
                    </td>
                    <td className="py-1 pr-3">{num(r.train_win_rate_pct, 1)}</td>
                    <td className="py-1 pr-3">{int(r.train_n)}</td>
                    <td className={`py-1 pr-3 ${r.test_mean_r > 0 ? "text-profit" : "text-loss"}`}>
                      {signed(r.test_mean_r, 3)}
                    </td>
                    <td className="py-1 pr-3">{num(r.test_win_rate_pct, 1)}</td>
                    <td className="py-1">
                      {r.keep
                        ? <span className="text-amber-400">คงไว้เฝ้าดู</span>
                        : <span className="text-loss">ตัดออก</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-[10px] text-slate-500 mt-2">
            "คงไว้เฝ้าดู" = ผลบวกในช่วงแรกเท่านั้น — ยังเป็นการเลือกจากหลายคู่
            จึงต้องทดสอบต่อหน้าทำงานจริงก่อนเชื่อถือ
          </p>
        </section>
      )}
    </>
  );
}

function Kv({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-500">{k}</span>
      <span className="font-semibold text-slate-200">{v}</span>
    </div>
  );
}
