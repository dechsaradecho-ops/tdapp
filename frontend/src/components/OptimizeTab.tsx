"use client";

/**
 * Logs > Optimize — sandbox settings optimizer panel.
 *
 * WHY A SEPARATE COMPONENT
 * --------------------------
 * Job control (start/poll/cancel), a live iteration log, overlaid equity
 * curves, and a guarded recommendation card are four concerns that would
 * drown the 2,200-line logs page, so this lives in its own file and the
 * page just renders <OptimizeTab />.
 *
 * THE HONESTY RULE BAKED IN
 * -------------------------
 * The panel never presents a train-only number as a result: the headline
 * is always the validation net PnL of the current best, and the Apply
 * button stays disabled until the run is `complete` with a recommendation.
 * Apply is a two-click explicit confirmation, never automatic, and a
 * stale-baseline refusal is shown verbatim with a "rerun" hint.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import type {
  OptEquityPoint,
  OptEquityResponse,
  OptIterationRow,
  OptMetaFilterSummary,
  OptRunLogEntry,
  OptRunStatus,
  SimDataReport,
} from "@/lib/types";

const POLL_MS = 1000;
/** Consecutive transient poll failures tolerated before an update is
 *  abandoned — a flaky network must not orphan a live backend job. */
const DS_MAX_RETRIES = 5;
const ITER_PAGE = 100;
/** Client-side run-log view cap — oldest rows drop first; the server
 *  keeps every row (clear/paging here never deletes anything). */
const LOG_MAX = 500;
const TERMINAL = ["complete", "failed", "cancelled"];

/** Two headline settings + RR target for the parameter-movement multiples. */
const PARAM_DIMS = ["min_confidence", "risk_per_trade_pct", "rr_target"] as const;

const STAGE_TH: Record<string, string> = {
  queued: "เข้าคิว",
  loading: "โหลดข้อมูล",
  searching: "ค้นหาค่า",
  done: "เสร็จแล้ว",
  failed: "ล้มเหลว",
  cancelled: "ถูกหยุด",
};

const numF = (v: unknown, d = 2): string => {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toFixed(d) : "—";
};
const signedF = (v: unknown, d = 1): string => {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? `${n >= 0 ? "+" : ""}${n.toFixed(d)}` : "—";
};
const intF = (v: unknown): string => {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString("th-TH") : "—";
};
/** profit_factor may be null (Plan 2/3 convention) — never crash, show "—". */
const pfF = (v: number | null | undefined): string =>
  v === null || v === undefined || !Number.isFinite(Number(v))
    ? "—"
    : Number(v).toFixed(2);
const usdF = (v: unknown): string => {
  if (v === null || v === undefined) return "—";
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return `${n < 0 ? "-" : "+"}$${Math.abs(n).toLocaleString("en-US", {
    maximumFractionDigits: 0,
  })}`;
};

/** Compact from → to for one config-diff entry (arrays shortened). */
const fmtDiffVal = (v: unknown): string => {
  if (v === null || v === undefined) return "—";
  if (Array.isArray(v)) {
    const s = v.map((x) => String(x)).join(", ");
    return s.length > 40 ? `${v.length} ค่า` : s || "—";
  }
  if (typeof v === "boolean") return v ? "เปิด" : "ปิด";
  if (typeof v === "number") {
    const r = Math.round(v * 10000) / 10000;
    return String(r);
  }
  const s = String(v);
  return s.length > 24 ? `${s.slice(0, 24)}…` : s;
};

export default function OptimizeTab() {
  const [runs, setRuns] = useState<OptRunStatus[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [run, setRun] = useState<OptRunStatus | null>(null);
  const [rows, setRows] = useState<OptIterationRow[]>([]);
  const [itTotal, setItTotal] = useState(0);
  const [itOffset, setItOffset] = useState(0);
  const [followLatest, setFollowLatest] = useState(true);
  const [equityBest, setEquityBest] = useState<OptEquityResponse | null>(null);
  const [equityBase, setEquityBase] = useState<OptEquityResponse | null>(null);
  const [starting, setStarting] = useState(false);
  const [err, setErr] = useState("");
  const [applyErr, setApplyErr] = useState("");
  const [applied, setApplied] = useState<Record<string, { from: unknown; to: unknown }> | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [applying, setApplying] = useState(false);

  // ---- run log (per-run events; diagnostic only) ----
  // Appended by last-seq inside pull() under the same no-overlap guard;
  // pause only stops appending (nothing is lost — resume polls from the
  // same seq), clear only empties this view (server rows stay, next poll
  // re-pages from the start).
  const [logs, setLogs] = useState<OptRunLogEntry[]>([]);
  const [logOpen, setLogOpen] = useState(true);
  const [logPaused, setLogPaused] = useState(false);

  // ---- run controls (capital fixed for the run, never optimized) ----
  const [datasetVersion, setDatasetVersion] = useState("");
  const [capital, setCapital] = useState(1000);
  const [assetsText, setAssetsText] = useState("");
  const [maxIter, setMaxIter] = useState(100);

  // ---- dataset update (sim-data v2; never auto-starts optimization) ----
  // The version textbox is auto-resolved, never mandatory typing: on mount
  // (and on demand via the ↻ affordance) the newest COMPLETE version
  // prefills it; the user may still override it manually.
  const [dsUpdating, setDsUpdating] = useState(false);
  const [dsTarget, setDsTarget] = useState<string | null>(null);
  const [dsMsg, setDsMsg] = useState("");
  const [dsResolving, setDsResolving] = useState(false);

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const runIdRef = useRef<string | null>(null);
  runIdRef.current = runId;
  const inFlightRef = useRef(false);
  const offRef = useRef(0);
  offRef.current = itOffset;
  const followRef = useRef(true);
  followRef.current = followLatest;
  const loadedIdRef = useRef<string | null>(null);
  const eqKeyRef = useRef<string>("");
  const logsRef = useRef<OptRunLogEntry[]>([]);
  logsRef.current = logs;
  const logPausedRef = useRef(false);
  logPausedRef.current = logPaused;
  const confirmingRef = useRef(false);
  confirmingRef.current = confirming;
  // Dataset-update poll guard — mirrors the run poll's inFlightRef: never
  // two GET /sim-data/{version} in flight, and the interval dies with the
  // update (or unmount) so a late response can't flip dsUpdating back on.
  const dsInFlightRef = useRef(false);
  const dsTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Consecutive transient poll-error count — reset on every successful
  // poll and on each new update; abort only past DS_MAX_RETRIES.
  const dsErrRef = useRef(0);
  const dsTargetRef = useRef<string | null>(null);
  dsTargetRef.current = dsTarget;
  const dsResolveRef = useRef(false);

  const live = !!run && !TERMINAL.includes(run.status);
  const done = run?.status === "complete";
  const canApply = !!done && !!run?.recommendation && !live && !applying;

  const loadRuns = useCallback(async () => {
    try {
      const r = await api.optRuns(20);
      if (!r.ok) {
        setErr(r.error || "โหลดประวัติรันไม่สำเร็จ");
        return;
      }
      setRuns(r.runs || []);
      const liveRun = (r.runs || []).find((x) => !TERMINAL.includes(x.status));
      if (liveRun) {
        setRunId((cur) => cur ?? liveRun.run_id);
      } else if ((r.runs || []).length && !runIdRef.current) {
        setRunId((r.runs || [])[0].run_id);
      }
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  // ---- dataset version auto-resolve (newest COMPLETE, never mandatory) --
  // Fail-soft by design: no complete dataset yet (or DB down) just leaves
  // the textbox for manual typing — the missing-version inline error only
  // fires at start time, when the field is still empty.
  const resolveLatest = useCallback(async (force: boolean) => {
    if (dsResolveRef.current) return;
    dsResolveRef.current = true;
    setDsResolving(true);
    try {
      const r = await api.simDataLatest();
      if (!r.ok) return;
      const v = (r.version || "").trim();
      if (!v) return;
      setDatasetVersion((cur) => (force || !cur.trim() ? v : cur));
    } catch {
      // Leave the field alone — manual typing still works.
    } finally {
      dsResolveRef.current = false;
      setDsResolving(false);
    }
  }, []);

  useEffect(() => {
    void resolveLatest(false);
  }, [resolveLatest]);

  // ---- dataset-update poll (no overlap, mirrors the run pull guard) -----
  useEffect(() => {
    if (!dsUpdating || !dsTarget) return;
    const target = dsTarget;
    // Abort helper: the tracked version is provably gone or terminally
    // refused — stop polling and surface the backend string verbatim.
    const abortDs = (msg: string) => {
      setErr(msg);
      setDsMsg("");
      setDsUpdating(false);
      setDsTarget(null);
    };
    // Retry helper: transient failure (network throw, DB outage) — keep
    // polling through DS_MAX_RETRIES consecutive errors, with the retry
    // count visible so a flaky network is not silent.
    const retryDs = (msg: string) => {
      dsErrRef.current += 1;
      if (dsErrRef.current > DS_MAX_RETRIES) {
        abortDs(msg);
        return;
      }
      setDsMsg(`กำลังอัปเดต dataset ${target}… (ติดต่อไม่ได้ ครั้งที่ ${dsErrRef.current}/${DS_MAX_RETRIES} — ยังรอต่อ)`);
    };
    const tick = async () => {
      if (dsInFlightRef.current) return;
      if (dsTargetRef.current !== target) return;
      dsInFlightRef.current = true;
      try {
        const st = await api.simDataStatus(target);
        if (dsTargetRef.current !== target) return;
        if (!st.ok) {
          // Only "unknown dataset version" proves the run is gone — abort
          // at once. Anything else may be transient: keep polling.
          if (/unknown dataset version/i.test(st.error || "")) {
            abortDs(st.error || "unknown dataset version");
            return;
          }
          retryDs(st.error || "อ่านสถานะ dataset ไม่สำเร็จ");
          return;
        }
        dsErrRef.current = 0;
        const rep = (st.report || {}) as SimDataReport;
        const stage = st.stage || st.status || "";
        if (stage === "done" || st.status === "complete" || rep.complete === true) {
          setDatasetVersion(st.version || target);
          const reused = typeof rep.reused_bars === "number" ? intF(rep.reused_bars) : "—";
          const fetched = typeof rep.fetched_bars === "number" ? intF(rep.fetched_bars) : "—";
          const base = rep.base_version ? ` (ฐาน ${rep.base_version})` : "";
          setDsMsg(`อัปเดตเสร็จ ${st.version || target} — ใช้ซ้ำ ${reused} bars · ดึงใหม่ ${fetched} bars${base}`);
          setDsUpdating(false);
          setDsTarget(null);
          return;
        }
        if (stage === "failed" || st.status === "failed") {
          // Verbatim: the report's own error, never paraphrased.
          const msg = rep.error || st.error || `dataset ${target} ล้มเหลว`;
          setErr(msg);
          setDsMsg("");
          setDsUpdating(false);
          setDsTarget(null);
          return;
        }
        setDsMsg(`กำลังอัปเดต dataset ${target}… (${stage || "building"})`);
      } catch (e) {
        if (dsTargetRef.current !== target) return;
        retryDs(String(e));
      } finally {
        dsInFlightRef.current = false;
      }
    };
    void tick();
    if (dsTimerRef.current) clearInterval(dsTimerRef.current);
    dsTimerRef.current = setInterval(() => void tick(), POLL_MS);
    return () => {
      if (dsTimerRef.current) clearInterval(dsTimerRef.current);
    };
  }, [dsUpdating, dsTarget]);

  const fetchEquity = useCallback(async (id: string, bestIter: number | null) => {
    try {
      // Baseline (iteration 0) vs current best — two overlaid curves.
      const [base, best] = await Promise.all([
        api.optEquity(id, 0),
        bestIter === 0 ? null : await api.optEquity(id),
      ]);
      if (runIdRef.current !== id) return;
      if (base.ok) setEquityBase(base);
      if (best && best.ok) setEquityBest(best);
      else if (bestIter === 0 && base.ok) setEquityBest(base);
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  // ---- poll status + page the iteration log without overlap -------------
  const pull = useCallback(async () => {
    const id = runIdRef.current;
    if (!id || inFlightRef.current) return;
    inFlightRef.current = true;
    try {
      const st = await api.optRun(id);
      // The user may have clicked another run while this request was in
      // flight — dropping a late response beats showing run A's verdict
      // under run B's header.
      if (runIdRef.current !== id) return;
      if (!st.ok) {
        setErr(st.error || "อ่านสถานะรันไม่สำเร็จ");
        return;
      }
      setRun(st);
      if (loadedIdRef.current !== id) {
        loadedIdRef.current = id;
        setRows([]);
        setItOffset(0);
        offRef.current = 0;
        logsRef.current = [];
        setLogs([]);
        setEquityBest(null);
        setEquityBase(null);
        eqKeyRef.current = "";
        setApplied(null);
        setApplyErr("");
        setConfirming(false);
        void loadRuns();
      }
      // Iteration log pages through offset/limit; while live and following,
      // jump to the last page so new rows stream in.
      let off = offRef.current;
      if (followRef.current && !TERMINAL.includes(st.status)) {
        const pg = await api.optIterations(id, 0, 1);
        if (runIdRef.current !== id) return;
        if (pg.ok && pg.total > ITER_PAGE) {
          off = Math.floor((pg.total - 1) / ITER_PAGE) * ITER_PAGE;
          if (off !== offRef.current) {
            offRef.current = off;
            setItOffset(off);
          }
        } else {
          off = 0;
          if (offRef.current !== 0) {
            offRef.current = 0;
            setItOffset(0);
          }
        }
      }
      const page = await api.optIterations(id, off, ITER_PAGE);
      if (runIdRef.current !== id) return;
      if (page.ok) {
        setRows(page.rows || []);
        setItTotal(page.total ?? (page.rows || []).length);
      }
      // Run-log streams under this same no-overlap guard. Gap-safe
      // `seq > after_seq` filtering (not positional offset — event seq
      // advances even on failed inserts, so positions overshoot after a
      // gap). Late/duplicate rows (seq already present) are dropped, the
      // view is capped at LOG_MAX, and a `more` page drains inline so a
      // terminal run shows the full log after one pull.
      // Paused = skip the fetch; resume continues from the same seq.
      if (!logPausedRef.current) {
        let seen = logsRef.current;
        // At the LOG_MAX cap the window still slides: append + slice drops
        // the oldest. An empty batch returns `seen` unchanged → break.
        for (let p = 0; p < 3; p++) {
          const lg = await api.optLogs(id, nextOptLogAfterSeq(seen), 200);
          if (runIdRef.current !== id) return;
          if (!lg.ok) break;
          const merged = appendOptLogRows(seen, lg.rows || []);
          if (merged === seen) break;
          seen = merged;
          if (!lg.more) break;
        }
        if (seen !== logsRef.current) {
          logsRef.current = seen;
          setLogs(seen);
        }
      }
      // Equity curves move only when the best iteration (or terminal
      // state) changes — refetching every second would churn for nothing.
      const key = `${id}:${st.best?.iteration ?? "-"}:${st.status}`;
      if (key !== eqKeyRef.current) {
        eqKeyRef.current = key;
        await fetchEquity(id, st.best?.iteration ?? null);
      }
      if (TERMINAL.includes(st.status)) void loadRuns();
    } catch (e) {
      setErr(String(e));
    } finally {
      inFlightRef.current = false;
    }
  }, [fetchEquity, loadRuns]);

  const selectRun = useCallback((id: string) => {
    if (id === runIdRef.current && loadedIdRef.current === id) return;
    setRows([]);
    setItTotal(0);
    setItOffset(0);
    offRef.current = 0;
    logsRef.current = [];
    setLogs([]);
    setRun(null);
    setEquityBest(null);
    setEquityBase(null);
    eqKeyRef.current = "";
    setApplied(null);
    setApplyErr("");
    setConfirming(false);
    setRunId(id);
  }, []);

  useEffect(() => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (!runId) return;
    void pull();
    if (live) timerRef.current = setInterval(() => void pull(), POLL_MS);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, run?.status]);

  // ---- dataset update: mint a new version, then poll it ----------------
  // Same assets textbox as optimization (blank = omit = reuse the newest
  // complete version's set) and a fixed 5-year span — no new years
  // control. Never auto-starts optimization; the user presses start
  // explicitly once the version textbox holds the new version.
  const updateDataset = async () => {
    if (dsUpdating) return;
    setDsMsg("");
    setErr("");
    try {
      const assets = assetsText
        .split(",")
        .map((a) => a.trim().toUpperCase())
        .filter(Boolean);
      const res = await api.simDataUpdate({
        ...(assets.length ? { assets } : {}),
        years: 5,
      });
      if (!res.ok) {
        // Refusal (a build/update already live, bad years, …) verbatim.
        setErr(res.error || "อัปเดต dataset ไม่สำเร็จ");
        return;
      }
      const v = (res.version || "").trim();
      if (!v) {
        setErr("อัปเดต dataset ไม่สำเร็จ (ไม่ได้ version กลับมา)");
        return;
      }
      setDsUpdating(true);
      setDsTarget(v);
      dsErrRef.current = 0;
      setDsMsg(`เริ่มอัปเดต dataset ${v}…`);
    } catch (e) {
      setErr(String(e));
    }
  };

  const start = async () => {
    if (!datasetVersion.trim()) {
      setErr("ต้องระบุ dataset version ก่อนเริ่ม (ประวัติราคาที่จะ replay)");
      return;
    }
    setStarting(true);
    setErr("");
    setApplyErr("");
    setApplied(null);
    setConfirming(false);
    try {
      const assets = assetsText
        .split(",")
        .map((a) => a.trim().toUpperCase())
        .filter(Boolean);
      const res = await api.optStart({
        dataset_version: datasetVersion.trim(),
        ...(assets.length ? { assets } : {}),
        starting_capital: capital,
        search_config: { max_iterations: maxIter },
      });
      if (!res.ok) {
        setErr(res.error || "เริ่มไม่สำเร็จ (ไม่ได้ run_id กลับมา)");
        return;
      }
      setRows([]);
      setItTotal(0);
      setItOffset(0);
      offRef.current = 0;
      logsRef.current = [];
      setLogs([]);
      setRun(null);
      setEquityBest(null);
      setEquityBase(null);
      eqKeyRef.current = "";
      setRunId(res.run_id);
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
      const res = await api.optCancel(runId);
      if (!res.ok) setErr(res.error || "หยุดไม่สำเร็จ");
      else await pull();
    } catch (e) {
      setErr(String(e));
    }
  };

  const apply = async () => {
    if (!runId || !canApply) return;
    // First click arms an explicit inline confirmation; second click sends
    // {confirm: true}. Never auto-applies, never retries on failure.
    if (!confirmingRef.current) {
      setConfirming(true);
      return;
    }
    setApplying(true);
    setApplyErr("");
    try {
      const res = await api.optApply(runId, true);
      if (!res.ok) {
        // Stale-baseline (and every other) refusal renders verbatim with a
        // rerun hint. No retry — the user must rerun or inspect live drift.
        const msg = res.error || "Apply ไม่สำเร็จ";
        setApplyErr(
          `${msg} — แนะนำให้ rerun optimization ใหม่บนข้อมูล/settings ปัจจุบัน แล้วค่อย Apply อีกครั้ง`
        );
        setConfirming(false);
        return;
      }
      setApplied(res.applied ?? {});
      setConfirming(false);
      await pull();
    } catch (e) {
      setApplyErr(String(e));
      setConfirming(false);
    } finally {
      setApplying(false);
    }
  };

  const best = run?.best ?? null;
  const rec = run?.recommendation ?? null;
  const warnings = run?.warnings ?? [];
  const ambPct = best?.validation_metrics?.ambiguous_pct ?? 0;

  const pct = run ? Math.min(100, Math.max(0, Math.round(run.progress * 100))) : 0;

  const totalPages = Math.max(1, Math.ceil(itTotal / ITER_PAGE));
  const curPage = Math.floor(itOffset / ITER_PAGE) + 1;

  return (
    <div className="space-y-3">
      {/* ---------------- controls ---------------- */}
      <section className="panel p-4">
        <div className="flex items-center justify-between gap-2 mb-3">
          <h2 className="panel-title mb-0">
            <Icon n="flask" size={14} className="inline mr-1" />
            Sandbox Optimizer — ค้นหา settings ที่ดีที่สุดบนข้อมูลย้อนหลัง
          </h2>
        </div>
        <p className="text-xs text-slate-400 mb-3">
          เปลี่ยน settings ทีละชุดใน sandbox แล้ว replay พอร์ตบนประวัติราคาเดิมซ้ำๆ
          เก็บทุก iteration ว่าดีขึ้นหรือแย่ลง — ไม่แตะเทรดจริง ไม่วางออเดอร์
          จนกว่าจะกด Apply เองหลังรันจบ
        </p>
        <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-3">
          <label className="block text-sm">
            <span className="text-slate-300">Dataset version (ประวัติราคา)</span>
            <input
              type="text" value={datasetVersion}
              onChange={(e) => setDatasetVersion(e.target.value)}
              placeholder={dsResolving ? "กำลังดึง version ล่าสุด…" : "เช่น v2026-11-20-5y"}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2"
            />
            <span className="mt-1 flex gap-1">
              <button type="button" onClick={() => void updateDataset()}
                disabled={dsUpdating || live}
                title={live
                  ? "ปิดการอัปเดตระหว่าง optimization รันอยู่ — รอให้รันจบก่อน"
                  : "ดึงราคาล่าสุดต่อจาก dataset ที่ใหม่สุด (สินทรัพย์ตามช่องข้างๆ ว่าง = ชุดเดิม, ย้อนหลัง 5 ปี)"}
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 text-xs disabled:opacity-50 min-h-[32px]">
                {dsUpdating ? "กำลังอัปเดต…" : "อัปเดต dataset"}
              </button>
              <button type="button" onClick={() => void resolveLatest(true)}
                disabled={dsResolving || dsUpdating}
                title="ดึง dataset version ล่าสุดที่ complete มาใส่ช่อง"
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 text-xs disabled:opacity-50 min-h-[32px]">
                {dsResolving ? "…" : "↻ ล่าสุด"}
              </button>
            </span>
            {dsMsg && <span className="mt-1 block text-[11px] text-slate-400">{dsMsg}</span>}
          </label>
          <label className="block text-sm">
            <span className="text-slate-300">ทุนตั้งต้น (USD)</span>
            <input
              type="number" value={capital} min={1}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) setCapital(Math.max(1, n));
              }}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2"
            />
          </label>
          <label className="block text-sm">
            <span className="text-slate-300">สินทรัพย์ (ว่าง = ทั้ง universe)</span>
            <input
              type="text" value={assetsText}
              onChange={(e) => setAssetsText(e.target.value)}
              placeholder="EURUSD, XAUUSD"
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2"
            />
          </label>
          <label className="block text-sm">
            <span className="text-slate-300">รอบค้นสูงสุด</span>
            <input
              type="number" value={maxIter} min={1} max={500}
              onChange={(e) => {
                const n = Number(e.target.value);
                if (Number.isFinite(n)) setMaxIter(Math.max(1, Math.min(500, n)));
              }}
              className="mt-1 w-full bg-surface border border-slate-700 rounded px-3 py-2"
            />
          </label>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={start} disabled={starting || live || dsUpdating || !datasetVersion.trim()}
            title={dsUpdating ? "ปิดการเริ่มระหว่างอัปเดต dataset — รอให้อัปเดตเสร็จก่อน" : undefined}
            className="px-4 py-2 rounded bg-accent text-white font-bold disabled:opacity-50 min-h-[40px]">
            {starting ? "กำลังเริ่ม..." : live ? "กำลังรันอยู่" : dsUpdating ? "รออัปเดต dataset…" : "เริ่ม optimization"}
          </button>
          {live && (
            <button onClick={cancel}
              className="px-3 py-1.5 rounded border border-loss text-loss text-xs min-h-[36px]">
              หยุด
            </button>
          )}
        </div>
        {dsUpdating && (
          <p className="mt-2 text-xs text-slate-400">
            ปิดการเริ่ม optimization ระหว่างอัปเดต dataset — รอให้เสร็จก่อน แล้วค่อยกดเริ่มเอง
          </p>
        )}
        {err && <p className="mt-2 text-loss text-sm">{err}</p>}
        {run?.status === "failed" && run?.error && (
          <p className="mt-2 text-loss text-xs">รันไม่สำเร็จ: {run.error}</p>
        )}
      </section>

      {/* ---------------- progress + current best ---------------- */}
      {run && (
        <section className="panel p-4">
          <div className="flex items-center justify-between gap-2 mb-2">
            <span className="text-xs text-slate-400">
              {runs.length > 1 && runId !== runs[0]?.run_id
                ? "กำลังดูผลรันเก่า — ไม่ใช่งานล่าสุด"
                : `รัน ${String(run.run_id || "").slice(0, 8)} · seed ${intF(run.seed)}`}
            </span>
            <span className="text-xs text-slate-400">
              ทุน {usdF(run.starting_capital).replace("+", "")} · {run.dataset_version || "—"}
            </span>
          </div>
          <div className="flex items-center justify-between text-xs mb-1">
            <span className="text-slate-300">
              ขั้นตอน: <span className="text-white font-bold">
                {STAGE_TH[run.stage] || run.stage}
              </span>
              {live && <span className="text-accent ml-2 animate-pulse">●</span>}
            </span>
            <span className="text-slate-400">
              ดีสุด (train) {run.best_train_pnl !== null && run.best_train_pnl !== undefined
                ? usdF(run.best_train_pnl) : "—"}
            </span>
          </div>
          <div className="h-2.5 rounded bg-slate-800 overflow-hidden">
            <div
              className={`h-full transition-all duration-500 ${
                run.status === "failed" ? "bg-loss"
                  : run.status === "cancelled" ? "bg-slate-500" : "bg-accent"}`}
              style={{ width: `${pct}%` }}
            />
          </div>
          <div className="text-[10px] text-slate-500 mt-1">{pct}%</div>

          {best && (
            <div className="grid grid-cols-3 sm:grid-cols-6 gap-2 mt-3">
              <Metric label="รอบดีสุด" value={`#${intF(best.iteration)}`} />
              <Metric label="Val net PnL" value={usdF(best.validation_metrics?.net_pnl)}
                tone={(best.validation_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"} bold />
              <Metric label="Train net PnL" value={usdF(best.train_metrics?.net_pnl)}
                tone={(best.train_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"} />
              <Metric label="Val drawdown" value={`${numF(best.validation_metrics?.max_drawdown_pct, 1)}%`}
                tone="text-loss" />
              <Metric label="Val trades" value={intF(best.validation_metrics?.trades)} />
              <Metric label="Val win rate" value={`${numF(best.validation_metrics?.win_rate_pct, 1)}%`} />
            </div>
          )}

          {(warnings.length > 0 || ambPct > 0) && (
            <div className="rounded border border-amber-400/25 bg-amber-400/10 p-3 mt-3">
              <div className="text-xs font-bold text-amber-200 mb-1">
                <Icon n="warning" size={13} className="inline mr-1" />
                ข้อควรระวังของรันนี้ (อ่านก่อนเชื่อตัวเลข)
              </div>
              <ul className="text-[11px] text-slate-300 space-y-0.5">
                {warnings.map((w, i) => (
                  <li key={i}>• {w}</li>
                ))}
                {ambPct > 0 && (
                  <li>• มีเทรด {numF(ambPct, 1)}% ที่แท่งเดียวแตะทั้ง TP/SL (ambiguous — นับแบบ conservative)</li>
                )}
              </ul>
            </div>
          )}
        </section>
      )}

      {/* ---------------- run log (per-run events, diagnostic) ------ */}
      {run && (
        <section className="panel p-4">
          <div className="flex items-center justify-between gap-2 mb-1">
            <button type="button" onClick={() => setLogOpen((o) => !o)}
              className="panel-title mb-0 cursor-pointer select-none"
              aria-expanded={logOpen}
              title="พับ/ขยาย log ของรันนี้">
              {logOpen ? "▾" : "▸"} Run log ({intF(logs.length)})
              {logs.length >= LOG_MAX && (
                <span className="text-[10px] text-slate-500 font-normal ml-1">
                  — เก็บ {LOG_MAX} แถวล่าสุด (แถวเก่ากว่าถูกปัดทิ้งเฉพาะหน้าจอ)
                </span>
              )}
            </button>
            <div className="flex gap-2">
              <button type="button" onClick={() => setLogPaused((p) => !p)}
                title={logPaused
                  ? "กลับมารับ log ใหม่ต่อจาก seq เดิม (ไม่สูญหาย)"
                  : "หยุดรับ log ชั่วคราว (เซิร์ฟเวอร์ยังบันทึกต่อ)"}
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 text-xs min-h-[32px]">
                {logPaused ? "▶ ต่อ" : "⏸ พัก"}
              </button>
              <button type="button"
                onClick={() => { logsRef.current = []; setLogs([]); }}
                title="ล้างเฉพาะหน้าจอฝั่งนี้ — แถวบนเซิร์ฟเวอร์ไม่ถูกลบ (poll ถัดไปดึงกลับมา)"
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 text-xs min-h-[32px]">
                ล้างหน้าจอ
              </button>
            </div>
          </div>
          {logPaused && (
            <p className="text-[11px] text-amber-200/80 mb-1">
              พักไว้ — เซิร์ฟเวอร์ยังบันทึกต่อ กด “ต่อ” เพื่อรับ log ใหม่จาก seq เดิม
            </p>
          )}
          {logOpen && (
            logs.length ? (
              <div className="overflow-x-auto scroll-x-thin max-h-72 overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface">
                    <tr className="text-left text-slate-500 border-b border-slate-800">
                      <th className="py-1.5 pr-3">เวลา</th>
                      <th className="py-1.5 pr-3">ระดับ</th>
                      <th className="py-1.5 pr-3">ขั้นตอน</th>
                      <th className="py-1.5 pr-3">รอบ</th>
                      <th className="py-1.5 pr-3">เหตุการณ์</th>
                      <th className="py-1.5">ข้อความ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {logs.map((l) => (
                      <tr key={l.seq} className="border-b border-slate-800/50">
                        <td className="py-1 pr-3 text-slate-500 whitespace-nowrap"
                          title={String(l.created_at || "")}>
                          {fmtLogTime(l.created_at)}
                        </td>
                        <td className="py-1 pr-3">
                          <span className={`px-1.5 py-0.5 rounded text-[10px] font-bold ${logLevelTone(l.level)}`}>
                            {String(l.level || "info").toUpperCase()}
                          </span>
                        </td>
                        <td className="py-1 pr-3 text-slate-300 whitespace-nowrap">{l.stage || "—"}</td>
                        <td className="py-1 pr-3 text-slate-400">
                          {l.iteration === null || l.iteration === undefined ? "—" : `#${intF(l.iteration)}`}
                        </td>
                        <td className="py-1 pr-3 text-slate-200 whitespace-nowrap">{l.event || "—"}</td>
                        <td className="py-1 text-slate-300">{l.message || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-[11px] text-slate-500 py-2">
                {live ? "รอ log แรกจาก worker…" : "รันนี้ยังไม่มี log"}
              </p>
            )
          )}
        </section>
      )}

      {/* ---------------- iteration log ---------------- */}
      {run && rows.length > 0 && (
        <section className="panel p-4">
          <div className="flex items-center justify-between mb-1">
            <h3 className="panel-title mb-0">
              Iteration log ({intF(itTotal)}) — เปลี่ยนอะไร ดีขึ้นหรือแย่ลง
            </h3>
            <label className="text-[10px] text-slate-400 flex items-center gap-1 cursor-pointer">
              <input type="checkbox" checked={followLatest}
                onChange={(e) => setFollowLatest(e.target.checked)} />
              ตามล่าสุด
            </label>
          </div>
          <div className="overflow-x-auto scroll-x-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500 border-b border-slate-800">
                  <th className="py-1.5 pr-3">#</th>
                  <th className="py-1.5 pr-3">เปลี่ยน</th>
                  <th className="py-1.5 pr-3">Train net PnL</th>
                  <th className="py-1.5 pr-3">Val net PnL</th>
                  <th className="py-1.5 pr-3">Train DD%</th>
                  <th className="py-1.5 pr-3">Trades</th>
                  <th className="py-1.5 pr-3">Win%</th>
                  <th className="py-1.5 pr-3">PF</th>
                  <th className="py-1.5">ผล</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const diffKeys = Object.keys(r.config_diff || {});
                  const shown = diffKeys.slice(0, 2);
                  const extra = diffKeys.length - shown.length;
                  const tip = diffKeys
                    .map((k) => {
                      const e = r.config_diff[k] as { from: unknown; to: unknown };
                      return `${k}: ${fmtDiffVal(e?.from)} → ${fmtDiffVal(e?.to)}`;
                    })
                    .join("\n");
                  return (
                    <tr key={r.iteration}
                      className={`border-b border-slate-800/50 ${
                        best && r.iteration === best.iteration ? "bg-white/[0.05]" : ""}`}>
                      <td className="py-1 pr-3 text-slate-400">
                        {intF(r.iteration)}
                        {best && r.iteration === best.iteration &&
                          <span className="text-accent"> ●</span>}
                      </td>
                      <td className="py-1 pr-3 max-w-[220px]" title={tip || "baseline (ไม่มี diff)"}>
                        {r.iteration === 0 ? (
                          <span className="text-slate-500">baseline</span>
                        ) : shown.length ? (
                          <span className="text-slate-200">
                            {shown.map((k) => {
                              const e = r.config_diff[k] as { from: unknown; to: unknown };
                              return (
                                <span key={k} className="block truncate">
                                  {k}: {fmtDiffVal(e?.from)} → {fmtDiffVal(e?.to)}
                                </span>
                              );
                            })}
                            {extra > 0 && <span className="text-slate-500">+{extra} ช่อง</span>}
                          </span>
                        ) : (
                          <span className="text-slate-500">—</span>
                        )}
                      </td>
                      <td className={`py-1 pr-3 font-bold ${
                        (r.train_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"}`}>
                        {usdF(r.train_metrics?.net_pnl)}
                      </td>
                      <td className={`py-1 pr-3 font-bold ${
                        (r.validation_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"}`}>
                        {r.validation_metrics ? usdF(r.validation_metrics.net_pnl) : "—"}
                      </td>
                      <td className="py-1 pr-3">{numF(r.train_metrics?.max_drawdown_pct, 1)}</td>
                      <td className="py-1 pr-3">{intF(r.train_metrics?.trades)}</td>
                      <td className="py-1 pr-3">{numF(r.train_metrics?.win_rate_pct, 1)}</td>
                      <td className="py-1 pr-3">{pfF(r.train_metrics?.profit_factor)}</td>
                      <td className="py-1">
                        {r.iteration === 0
                          ? <span className="text-slate-500">ฐาน</span>
                          : r.improved
                            ? <span className="text-profit font-bold">ดีขึ้น</span>
                            : <span className="text-loss">แย่ลง</span>}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="flex items-center justify-between mt-2 text-xs">
            <span className="text-slate-500">หน้า {intF(curPage)} / {intF(totalPages)}</span>
            <div className="flex gap-2">
              <button
                onClick={() => { const o = Math.max(0, itOffset - ITER_PAGE); setItOffset(o); offRef.current = o; void pull(); }}
                disabled={itOffset <= 0}
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 disabled:opacity-50 min-h-[32px]">
                ← ก่อนหน้า
              </button>
              <button
                onClick={() => { const o = itOffset + ITER_PAGE; setItOffset(o); offRef.current = o; void pull(); }}
                disabled={itOffset + ITER_PAGE >= itTotal}
                className="px-2 py-1 rounded border border-slate-700 text-slate-300 disabled:opacity-50 min-h-[32px]">
                ถัดไป →
              </button>
            </div>
          </div>
        </section>
      )}

      {/* ---------------- charts ---------------- */}
      {run && (equityBest || equityBase || rows.length > 1) && (
        <section className="panel p-4">
          <h3 className="panel-title">กราฟ — equity / PnL / drawdown / การขยับพารามิเตอร์</h3>
          {(equityBest || equityBase) && (
            <OptEquityChart
              base={equityBase && equityBase.ok ? equityBase.rows : []}
              best={equityBest && equityBest.ok ? equityBest.rows : []}
              capital={run?.starting_capital ?? null}
            />
          )}
          {rows.length > 1 && (
            <>
              <PnlBars rows={rows} />
              <ParamMoves rows={rows} />
            </>
          )}
          {equityBest && equityBest.ok && equityBest.rows.length > 0 && (
            <DrawdownCurve
              points={equityBest.rows.map((p) => ({
                v: Number(p.drawdown_pct) || 0,
              }))}
            />
          )}
        </section>
      )}

      {/* ---------------- recommendation + guarded apply ---------------- */}
      {run && (done || rec) && rec && (
        <section className="panel p-4 border-accent/30">
          <h3 className="panel-title">
            <Icon n="shield" size={14} className="inline mr-1" />
            ข้อเสนอหลังรันจบ — รอบ #{intF(rec.iteration)}
          </h3>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 text-[11px] mb-3">
            <Kv k="Train net PnL" v={usdF(best?.train_metrics?.net_pnl)} />
            <Kv k="Val net PnL"
              v={<span className={(rec.validation_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"}>
                {usdF(rec.validation_metrics?.net_pnl)}
              </span>} />
            <Kv k="Val drawdown" v={`${numF(rec.validation_metrics?.max_drawdown_pct, 1)}%`} />
            <Kv k="Val trades / win%"
              v={`${intF(rec.validation_metrics?.trades)} / ${numF(rec.validation_metrics?.win_rate_pct, 1)}%`} />
          </div>
          <div className="text-[11px] text-slate-300 mb-1">
            profit factor (val): <b>{pfF(rec.validation_metrics?.profit_factor)}</b>
            {" · "}avg R: {numF(rec.validation_metrics?.average_r, 2)}
          </div>
          <MetaFilterBlock mf={run?.meta_filter ?? rec?.meta_filter ?? null} />
          {rec.note && <p className="text-[11px] text-slate-400 mb-2">{rec.note}</p>}
          {best && Object.keys(best.config_diff || {}).length > 0 && (
            <div className="rounded border border-slate-700/60 bg-surface/40 p-3 mb-3">
              <div className="text-xs font-bold text-slate-300 mb-1">
                Diff ที่จะเขียน (เฉพาะช่องที่เปลี่ยน — ที่เหลือคงเดิม)
              </div>
              <ul className="text-[11px] space-y-0.5">
                {Object.entries(best.config_diff || {}).map(([k, e]) => {
                  const en = e as { from: unknown; to: unknown };
                  return (
                    <li key={k} className="text-slate-300">
                      <span className="font-semibold text-white">{k}</span>
                      {": "}
                      <span className="line-through text-loss/80">{fmtDiffVal(en?.from)}</span>
                      {" → "}
                      <span className="font-bold text-profit">{fmtDiffVal(en?.to)}</span>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
          {applied && (
            <p className="text-xs text-profit mb-2">
              Apply สำเร็จ — เขียน {Object.keys(applied).length} ช่องผ่าน guarded path
              (source=simulation_apply)
            </p>
          )}
          {applyErr && (
            <p className="text-xs text-loss mb-2 whitespace-pre-wrap">{applyErr}</p>
          )}
          <button onClick={apply} disabled={!canApply}
            className={`px-4 py-2 rounded font-bold text-xs min-h-[40px] ${
              confirming ? "bg-loss text-white" : "bg-accent text-white disabled:opacity-50"}`}>
            {applying ? "กำลังเขียน..." : confirming
              ? "กดอีกครั้งเพื่อยืนยันเขียน settings จริง"
              : "Apply — เขียน settings จริง"}
          </button>
          {!done && (
            <p className="text-[11px] text-slate-500 mt-1">
              ปุ่ม Apply เปิดเฉพาะเมื่อรันจบ (complete) และมี recommendation
            </p>
          )}
        </section>
      )}

      {/* ---------------- run history ---------------- */}
      {runs.length > 0 && (
        <section className="panel p-4">
          <h3 className="panel-title">
            ประวัติ optimization ({intF(runs.length)}) — แตะแถวเพื่อดูผลเต็ม
          </h3>
          <div className="overflow-x-auto scroll-x-thin">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-slate-500 border-b border-slate-800">
                  <th className="py-2 pr-3">รัน</th>
                  <th className="py-2 pr-3">สถานะ</th>
                  <th className="py-2 pr-3">ทุน</th>
                  <th className="py-2 pr-3">Val ดีสุด</th>
                  <th className="py-2 pr-3">รอบ</th>
                  <th className="py-2">ดูผล</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => {
                  const selected = r.run_id === runId;
                  return (
                    <tr key={r.run_id}
                      onClick={() => selectRun(r.run_id)}
                      className={`border-b border-slate-800/50 hover:bg-white/[0.04] cursor-pointer ${
                        selected ? "bg-white/[0.06]" : ""}`}>
                      <td className="py-1.5 pr-3 text-slate-400 whitespace-nowrap">
                        {String(r.run_id || "").slice(0, 8)}
                        {selected && <span className="text-accent"> ●</span>}
                      </td>
                      <td className="py-1.5 pr-3">
                        <span className={r.status === "complete" ? "text-profit"
                          : r.status === "failed" ? "text-loss" : "text-slate-400"}>
                          {STAGE_TH[r.stage] || r.status}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3">{usdF(r.starting_capital).replace("+", "")}</td>
                      <td className={`py-1.5 pr-3 font-bold ${
                        (r.best?.validation_metrics?.net_pnl ?? 0) >= 0 ? "text-profit" : "text-loss"}`}>
                        {r.best?.validation_metrics ? usdF(r.best.validation_metrics.net_pnl) : "—"}
                      </td>
                      <td className="py-1.5 pr-3">
                        {r.best ? `#${intF(r.best.iteration)}` : "—"}
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

/** Last seen seq for the next gap-safe poll (null = first page).
 *  Exported for the mocked render trace — pull() uses this verbatim. */
export function nextOptLogAfterSeq(rows: OptRunLogEntry[]): number | null {
  return rows.length ? rows[rows.length - 1].seq : null;
}

/** Append one fetched page: drop late/duplicate rows (seq already
 *  present), cap the view at LOG_MAX (oldest first). Returns `seen`
 *  unchanged when the batch adds nothing. Clearing the view is just
 *  `appendOptLogRows([], [])` → `[]`; server rows are never touched. */
export function appendOptLogRows(
  seen: OptRunLogEntry[], batch: OptRunLogEntry[]): OptRunLogEntry[] {
  const fresh = (batch || []).filter(
    (r) => !seen.length || r.seq > seen[seen.length - 1].seq);
  if (!fresh.length) return seen;
  return [...seen, ...fresh].slice(-LOG_MAX);
}

/** HH:MM:SS (MM-DD) from an ISO timestamp; raw string when unparseable. */
export function fmtLogTime(v: string | null | undefined): string {
  const t = String(v || "");
  if (t.length >= 19) return `${t.slice(5, 10)} ${t.slice(11, 19)}`;
  return t || "—";
}

/** Level badge tone — backend emits info/error today; unknown stays neutral. */
export function logLevelTone(level: string | null | undefined): string {
  const l = String(level || "info").toLowerCase();
  if (l === "error") return "bg-loss/20 text-loss";
  if (l === "warn" || l === "warning") return "bg-amber-400/20 text-amber-200";
  return "bg-slate-700/60 text-slate-300";
}

/**
 * Post-run meta-filter diagnostic inside the recommendation card.
 *
 * Diagnostic only — the tally never affected ranking, eligibility, or
 * replay (backend `ranking_effect: "none"`). Renders `unavailable`
 * honestly when there is no report at all (run predates the field),
 * when the backend marks it unavailable (no valid artifact, e.g. never
 * trained, or no recommendation to score), or when the counts are absent.
 */
export function MetaFilterBlock({ mf }: { mf: OptMetaFilterSummary | null | undefined }) {
  const hasCounts = !!mf && typeof mf.evaluated === "number";
  if (!hasCounts || mf?.status === "unavailable") {
    const reason = (mf && mf.reason)
      || "ยังไม่มีรายงาน (ไม่มี recommendation/artifact ที่ใช้ได้)";
    return (
      <div className="rounded border border-slate-700/60 bg-surface/40 p-3 mb-3">
        <div className="text-xs font-bold text-slate-300 mb-1">
          Meta-filter <span className="font-normal text-slate-500">(วินิจฉัยหลังรัน — ไม่มีผลต่อการจัดอันดับ)</span>
        </div>
        <p className="text-[11px] text-slate-400">ไม่พร้อมใช้งาน: {reason}</p>
      </div>
    );
  }
  const art = mf.artifact_run_id ? String(mf.artifact_run_id).slice(0, 8) : "—";
  const trained = mf.trained_at ? String(mf.trained_at).slice(0, 10) : "—";
  return (
    <div className="rounded border border-slate-700/60 bg-surface/40 p-3 mb-3">
      <div className="text-xs font-bold text-slate-300 mb-1">
        Meta-filter <span className="font-normal text-slate-500">(วินิจฉัยหลังรัน — ไม่มีผลต่อการจัดอันดับ)</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mb-2">
        <Kv k="ประเมิน" v={intF(mf.evaluated)} />
        <Kv k="Take" v={<span className="text-profit">{intF(mf.take)}</span>} />
        <Kv k="Skip" v={<span className="text-loss">{intF(mf.skip)}</span>} />
        <Kv k="Unavailable" v={intF(mf.unavailable)} />
      </div>
      <div className="text-[11px] text-slate-300">
        skip rate: <b>{numF(mf.skip_rate_pct, 1)}%</b>
        {" · "}threshold: <b>{mf.threshold === null || mf.threshold === undefined ? "—" : numF(mf.threshold, 3)}</b>
        {" · "}artifact: <b>{art}</b>
        {" · "}trained: <b>{trained}</b>
      </div>
      {mf.note && <p className="text-[10px] text-slate-500 mt-1">{mf.note}</p>}
    </div>
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

function Kv({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="rounded border border-slate-700/60 bg-surface/40 p-2">
      <div className="text-[10px] text-slate-500">{k}</div>
      <div className="text-sm font-semibold">{v}</div>
    </div>
  );
}

/**
 * Baseline vs current-best equity, overlaid in one inline SVG.
 *
 * Hand-rolled inline SVG equity curve (no chart library shipped):
 * downsampled to ≤300 rendered nodes per curve, and a legend that says
 * which curve is which. Two curves matter here — the gap between
 * baseline and best IS the optimization result.
 *
 * DEVIATION FROM THE BRIEF (documented): the brief ruled a "zero line",
 * but these curves are absolute equity denominated in starting capital
 * (~1000 USD), so y=0 would render far off-chart and carry no
 * information. The dashed starting-capital reference line serves the
 * same honesty purpose instead: curve above = profit, below = loss.
 */
function OptEquityChart({ base, best, capital }: {
  base: OptEquityPoint[]; best: OptEquityPoint[]; capital: number | null;
}) {
  const W = 900, H = 200, PAD = 28;
  const basePts = useMemo(
    () => downsample(base.map((p) => Number(p.equity) || 0)),
    [base]);
  const bestPts = useMemo(
    () => downsample(best.map((p) => Number(p.equity) || 0)),
    [best]);
  if (basePts.length < 2 && bestPts.length < 2) {
    return (
      <p className="text-xs text-slate-500 py-4 text-center">
        ยังไม่มีเส้น equity (รันต้องมี iteration ที่เก็บ evidence แล้ว)
      </p>
    );
  }
  const all = [...basePts, ...bestPts];
  // The capital reference line must always be visible, so the capital
  // value joins the domain (curves alone may never touch it).
  const cap = capital !== null && Number.isFinite(Number(capital))
    ? Number(capital) : null;
  const min = Math.min(...all, ...(cap !== null ? [cap] : []));
  const max = Math.max(...all, ...(cap !== null ? [cap] : []));
  const span = (max - min) || 1;
  const x = (i: number, len: number) =>
    PAD + (len <= 1 ? 0 : (i / (len - 1)) * (W - PAD * 2));
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2);
  const toPath = (pts: number[]) =>
    pts.map((v, i) => `${i === 0 ? "M" : "L"}${x(i, pts.length).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const lastBest = bestPts.length ? bestPts[bestPts.length - 1] : NaN;
  return (
    <div className="mt-1 mb-3">
      <div className="text-[11px] font-bold text-slate-300 mb-1">
        Equity — baseline (เทา) vs ดีสุดปัจจุบัน (เขียว/แดง)
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto rounded bg-surface/40"
        preserveAspectRatio="none" role="img"
        aria-label="equity baseline เทียบกับ best">
        {cap !== null && (
          <>
            <line x1={PAD} x2={W - PAD} y1={y(cap)} y2={y(cap)}
              stroke="#94a3b8" strokeWidth="1" strokeDasharray="4 4" />
            <text x={PAD + 2} y={y(cap) - 4} fill="#94a3b8" fontSize="10">
              ทุน {usdF(cap).replace("+", "")}
            </text>
          </>
        )}
        {basePts.length > 1 && (
          <path d={toPath(basePts)} fill="none" stroke="#647b8b" strokeWidth="1.4" />
        )}
        {bestPts.length > 1 && (
          <path d={toPath(bestPts)} fill="none"
            stroke={lastBest >= (basePts.length ? basePts[basePts.length - 1] : lastBest) ? "#22c55e" : "#ef4444"}
            strokeWidth="1.6" />
        )}
        {bestPts.length > 0 && Number.isFinite(lastBest) && (
          <circle cx={x(bestPts.length - 1, bestPts.length)} cy={y(lastBest)} r="3"
            fill="#22c55e" />
        )}
      </svg>
      <div className="flex gap-3 text-[10px] text-slate-400 mt-1">
        <span>⬜ baseline {basePts.length ? intF(basePts.length) : "—"} จุด</span>
        <span>🟢 best {bestPts.length ? intF(bestPts.length) : "—"} จุด</span>
        <span className="ml-auto">
          best สุดท้าย {Number.isFinite(lastBest) ? usdF(lastBest).replace("+", "") : "—"}
        </span>
      </div>
    </div>
  );
}

/** Downsample to ≤300 rendered nodes for the inline SVG equity curve. */
function downsample(pts: number[]): number[] {
  if (pts.length <= 300) return pts;
  const step = Math.max(1, Math.floor(pts.length / 300));
  const out = pts.filter((_, i) => i % step === 0);
  if (out[out.length - 1] !== pts[pts.length - 1]) out.push(pts[pts.length - 1]);
  return out;
}

/**
 * Per-iteration train net-PnL bars — the search movement at a glance:
 * green bars climbing means the search is finding better configs.
 */
function PnlBars({ rows }: { rows: OptIterationRow[] }) {
  const W = 900, H = 140, PAD = 28;
  const vals = rows.map((r) => Number(r.train_metrics?.net_pnl) || 0);
  if (!vals.length) return null;
  const min = Math.min(0, ...vals);
  const max = Math.max(...vals);
  const span = (max - min) || 1;
  const bw = (W - PAD * 2) / vals.length;
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2);
  const zero = y(0);
  return (
    <div className="mt-1 mb-3">
      <div className="text-[11px] font-bold text-slate-300 mb-1">
        Train net PnL ต่อ iteration (เขียว = บวก, แดง = ลบ)
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto rounded bg-surface/40"
        preserveAspectRatio="none" role="img" aria-label="net PnL ต่อ iteration">
        <line x1={PAD} x2={W - PAD} y1={zero} y2={zero} stroke="#475569" strokeWidth="1" />
        {vals.map((v, i) => {
          const bx = PAD + i * bw + 1;
          const top = Math.min(y(v), zero);
          const h = Math.max(1, Math.abs(y(v) - zero));
          return (
            <rect key={i} x={bx.toFixed(1)} y={top.toFixed(1)}
              width={Math.max(1, bw - 2).toFixed(1)} height={h.toFixed(1)}
              fill={v >= 0 ? "#22c55e" : "#ef4444"} opacity={0.8} />
          );
        })}
      </svg>
      <div className="text-[10px] text-slate-500 mt-1">
        {intF(vals.length)} iterations · สูงสุด {usdF(Math.max(...vals))} · ต่ำสุด {usdF(Math.min(...vals))}
      </div>
    </div>
  );
}

/**
 * Drawdown curve of the current-best equity (%, deeper = worse).
 */
function DrawdownCurve({ points }: { points: { v: number }[] }) {
  const W = 900, H = 110, PAD = 28;
  const vals = downsample(points.map((p) => p.v));
  if (vals.length < 2) return null;
  const max = Math.max(0, ...vals);
  const span = max || 1;
  const x = (i: number) => PAD + (i / (vals.length - 1)) * (W - PAD * 2);
  const y = (v: number) => PAD + (v / span) * (H - PAD * 2);
  const d = vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <div className="mt-1 mb-3">
      <div className="text-[11px] font-bold text-slate-300 mb-1">
        Drawdown% ของเส้น best (ยิ่งลึกยิ่งแย่)
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto rounded bg-surface/40"
        preserveAspectRatio="none" role="img" aria-label="drawdown ของ best">
        <path d={d} fill="none" stroke="#f59e0b" strokeWidth="1.6" />
      </svg>
      <div className="text-[10px] text-slate-500 mt-1">
        drawdown สูงสุด {numF(max, 1)}%
      </div>
    </div>
  );
}

/**
 * Parameter-movement small multiples for 2–3 headline settings: where the
 * search walked. Non-numeric or missing values break the line (gap, not 0).
 */
function ParamMoves({ rows }: { rows: OptIterationRow[] }) {
  return (
    <div className="mt-1">
      <div className="text-[11px] font-bold text-slate-300 mb-1">
        การขยับพารามิเตอร์ (ค่าที่แต่ละ iteration ใช้)
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-2">
        {PARAM_DIMS.map((dim) => (
          <ParamMini key={dim} dim={dim} rows={rows} />
        ))}
      </div>
    </div>
  );
}

function ParamMini({ dim, rows }: { dim: string; rows: OptIterationRow[] }) {
  const W = 280, H = 90, PAD = 26;
  const vals: (number | null)[] = rows.map((r) => {
    const cfg = (r.config || {}) as Record<string, unknown>;
    const n = Number(cfg[dim]);
    return Number.isFinite(n) ? n : null;
  });
  const nums = vals.filter((v): v is number => v !== null);
  if (nums.length < 2) {
    return (
      <div className="rounded border border-slate-700/60 bg-surface/40 p-2">
        <div className="text-[10px] text-slate-400">{dim}</div>
        <div className="text-[11px] text-slate-500">ข้อมูลไม่พอวาดกราฟ</div>
      </div>
    );
  }
  const min = Math.min(...nums);
  const max = Math.max(...nums);
  const span = (max - min) || 1;
  const x = (i: number) => PAD + (i / (vals.length - 1)) * (W - PAD * 2);
  const y = (v: number) => H - PAD - ((v - min) / span) * (H - PAD * 2);
  // Segments split on nulls so missing values read as gaps, not zeroes.
  const segs: string[] = [];
  let cur = "";
  vals.forEach((v, i) => {
    if (v === null) {
      if (cur) segs.push(cur);
      cur = "";
      return;
    }
    cur += `${cur ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)} `;
  });
  if (cur) segs.push(cur);
  return (
    <div className="rounded border border-slate-700/60 bg-surface/40 p-2">
      <div className="text-[10px] text-slate-400 mb-1">{dim}</div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto" role="img"
        aria-label={`การขยับ ${dim}`}>
        {segs.map((d, i) => (
          <path key={i} d={d} fill="none" stroke="#38bdf8" strokeWidth="1.4" />
        ))}
      </svg>
      <div className="text-[10px] text-slate-500 mt-0.5">
        {numF(min, 3)} → {numF(max, 3)}
      </div>
    </div>
  );
}
