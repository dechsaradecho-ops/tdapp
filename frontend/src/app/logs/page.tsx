"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import Icon from "@/components/Icon";
import LoadingGraphic from "@/components/LoadingGraphic";
import {
  NewsLog,
  NewsLogsResponse,
  QuoteApiLog,
  QuoteLogSummary,
  QuoteTestResult,
  SchedulerLog,
  SchedulerLogsResponse,
  SignalLog,
  SignalLogsResponse,
} from "@/lib/types";

/** สีของ badge ตามสถานะ call */
function statusBadge(status: string) {
  return status === "success"
    ? "bg-emerald-500/15 text-emerald-400"
    : "bg-red-500/15 text-red-400";
}

/** แยกตัวเลขจาก guard detail "checked=2, closed=0, moved_sl=1, ..." (never throws) */
function parseGuardDetail(detail: string): Record<string, number> {
  const out: Record<string, number> = {};
  try {
    for (const part of String(detail || "").split(",")) {
      const m = part.trim().match(/^([a-z_]+)\s*=\s*(-?\d+)/i);
      if (m) out[m[1]] = parseInt(m[2], 10) || 0;
    }
  } catch { /* ignore */ }
  return out;
}

/**
 * ข้อความ empty-state ของตาราง scheduler_runs (scheduler / guard tab)
 *
 * เดิมขึ้น "ต้องรัน migration 030_scheduler_logs.sql" เสมอ ซึ่งทำให้เข้าใจผิด:
 * ตารางอาจมีอยู่และทำงานปกติ แต่ job ยังไม่ "เสร็จแล้วบันทึก log" — prod
 * 2026-09-11: position_guard ใช้เวลา ~55 วิ/รอบ เกิน interval 1 นาที จึงถูก
 * ข้ามด้วย max_instances=1 ตลอด → ไม่มี row ทั้งที่ตารางปกติดี
 *
 * แยก 2 กรณีด้วย summary.total ของทุก job: ถ้ามี log ของ job อื่นอยู่ =
 * ตารางทำงาน (ไม่ใช่ปัญหา migration) → บอกให้รอ/กดรันเดี๋ยวนี้
 */
function schedulerEmptyMessage(
  jobLabel: string,
  summary: SchedulerLogsResponse["summary"] | null,
) {
  if ((summary?.total ?? 0) > 0) {
    return `ยังไม่มีรอบ ${jobLabel} ที่บันทึกได้ — ตาราง scheduler_runs ทำงานปกติ (job อื่นมี log) แต่รอบนี้ยังไม่เสร็จหรือถูกข้าม รอรอบถัดไป (~1 นาที)`;
  }
  return "ยังไม่มีประวัติ scheduler เลย — ตรวจสอบว่ารัน migration 030_scheduler_logs.sql บน Supabase แล้ว และ backend ตั้ง ENABLE_WORKERS=1";
}

function BucketCard({ label, bucket }: { label: string; bucket?: { total: number; success: number; error: number } | null }) {
  if (!bucket) return null;
  return (
    <div className="panel">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-2xl font-bold">{bucket.total.toLocaleString()}</p>
      <p className="text-xs mt-1">
        <span className="text-emerald-400">✓ {bucket.success}</span>
        {"  "}
        <span className="text-red-400">✗ {bucket.error}</span>
      </p>
    </div>
  );
}

export default function LogsPage() {
  const [tab, setTab] = useState<"quotes" | "news" | "scheduler" | "guard" | "gate">("quotes");
  const [logs, setLogs] = useState<QuoteApiLog[]>([]);
  const [summary, setSummary] = useState<QuoteLogSummary | null>(null);
  const [newsLogs, setNewsLogs] = useState<NewsLog[]>([]);
  const [newsSummary, setNewsSummary] = useState<NewsLogsResponse["summary"] | null>(null);
  const [schedLogs, setSchedLogs] = useState<SchedulerLog[]>([]);
  const [schedSummary, setSchedSummary] = useState<SchedulerLogsResponse["summary"] | null>(null);
  const [ttlDays, setTtlDays] = useState(7);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [loading, setLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<QuoteTestResult | null>(null);
  const [filter, setFilter] = useState<"all" | "forex" | "gold">("all");
  const [page, setPage] = useState(1);
  // refs ให้ callbacks เสถียร (ไม่ต้องใส่ tab/filter ใน deps → ไม่โหลดซ้ำวน)
  const tabRef = useRef<"quotes" | "news" | "scheduler" | "guard" | "gate">("quotes");
  const filterRef = useRef<"all" | "forex" | "gold">("all");
  // server paging: ขอทีละหน้า (500 แถว/ครั้ง) แล้วแบ่งแสดง 50/หน้า —
  // ตาราง 7 วันโตเกิน 500 ได้ จึงต้องเดิน offset ไปเรื่อย ๆ ไม่ใช่ดึงแค่ 500 ล่าสุด
  const [quoteTotal, setQuoteTotal] = useState(0);
  const [quoteHasMore, setQuoteHasMore] = useState(false);
  const [newsTotal, setNewsTotal] = useState(0);
  const [newsHasMore, setNewsHasMore] = useState(false);
  const [schedTotal, setSchedTotal] = useState(0);
  const [schedHasMore, setSchedHasMore] = useState(false);
  // แท็บ Guard = scheduler_runs ที่ job_id=position_guard (server filter)
  const [guardLogs, setGuardLogs] = useState<SchedulerLog[]>([]);
  const [guardTotal, setGuardTotal] = useState(0);
  const [guardHasMore, setGuardHasMore] = useState(false);
  // แท็บ Gate = signal_logs ฝั่ง execution gate (order_blocked = gate ปัดตก,
  // order_opened = gate ผ่าน) — server filter ด้วย event
  const [gateLogs, setGateLogs] = useState<SignalLog[]>([]);
  const [gateSummary, setGateSummary] = useState<SignalLogsResponse["summary"] | null>(null);
  const [gateTotal, setGateTotal] = useState(0);
  const [gateHasMore, setGateHasMore] = useState(false);
  const [gateFilter, setGateFilter] = useState<"order_blocked" | "order_opened" | "all">("all");
  const gateFilterRef = useRef<"order_blocked" | "order_opened" | "all">("all");
  const PAGE_SIZE = 50;
  const SERVER_PAGE = 500;

  const loadQuotes = useCallback(async (flt: "all" | "forex" | "gold", pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    const qres = await api.quoteLogs(SERVER_PAGE, offset, flt);
    return qres;
  }, []);

  const loadNews = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.newsLogs(SERVER_PAGE, offset);
  }, []);

  const loadSched = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.schedulerLogs(SERVER_PAGE, offset);
  }, []);

  const loadGuard = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.schedulerLogs(SERVER_PAGE, offset, "position_guard");
  }, []);

  const loadGate = useCallback(async (pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.signalLogs(SERVER_PAGE, offset, gateFilterRef.current);
  }, []);

  // โหลดเฉพาะแท็บที่เปิดอยู่ (lazy) — เข้าหน้าครั้งแรกยิงแค่ 1 request
  // แทน 5 requests พร้อมกัน (quotes+news+scheduler+guard+gate) ทำให้หน้าแรกไวขึ้น ~5 เท่า
  const loadedRef = useRef<Set<string>>(new Set());
  const loadOne = useCallback(async (t: "quotes" | "news" | "scheduler" | "guard" | "gate", flt?: "all" | "forex" | "gold") => {
    setLoading(true);
    try {
      if (t === "quotes") {
        const f = flt ?? filterRef.current;
        const qres = await loadQuotes(f, 1);
        setLogs(qres.logs ?? []);
        setSummary(qres.summary ?? null);
        setTtlDays(qres.ttl_days ?? 7);
        setQuoteTotal(qres.total ?? (qres.logs ?? []).length);
        setQuoteHasMore(qres.has_more ?? false);
      } else if (t === "news") {
        const nres = await loadNews(1);
        setNewsLogs(nres.logs ?? []);
        setNewsSummary(nres.summary ?? null);
        setNewsTotal(nres.total ?? (nres.logs ?? []).length);
        setNewsHasMore(nres.has_more ?? false);
      } else if (t === "guard") {
        const gres = await loadGuard(1);
        setGuardLogs(gres.logs ?? []);
        setGuardTotal(gres.total ?? (gres.logs ?? []).length);
        setGuardHasMore(gres.has_more ?? false);
        // summary guard อาศัย schedSummary — โหลด scheduler summary แบบเบา (limit 1) ครั้งเดียว
        if (!loadedRef.current.has("scheduler")) {
          try {
            const sres = await api.schedulerLogs(1, 0);
            setSchedSummary(sres.summary ?? null);
          } catch { /* ignore */ }
        }
      } else if (t === "gate") {
        const gates = await loadGate(1);
        setGateLogs(gates.logs ?? []);
        setGateSummary(gates.summary ?? null);
        setGateTotal(gates.total ?? (gates.logs ?? []).length);
        setGateHasMore(gates.has_more ?? false);
      } else {
        const sres = await loadSched(1);
        setSchedLogs(sres.logs ?? []);
        setSchedSummary(sres.summary ?? null);
        setSchedTotal(sres.total ?? (sres.logs ?? []).length);
        setSchedHasMore(sres.has_more ?? false);
      }
      loadedRef.current.add(t);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadGate, loadGuard, loadNews, loadQuotes, loadSched]);

  // เปลี่ยน server page (ทุก 10 หน้า UI = 500 แถว) — ดึง chunk ถัดไปจาก backend
  const gotoServerPage = useCallback(async (pg: number) => {
    setLoading(true);
    try {
      if (tabRef.current === "quotes") {
        const qres = await loadQuotes(filterRef.current, pg);
        setLogs(qres.logs ?? []);
        setQuoteTotal(qres.total ?? 0);
        setQuoteHasMore(qres.has_more ?? false);
      } else if (tabRef.current === "news") {
        const nres = await loadNews(pg);
        setNewsLogs(nres.logs ?? []);
        setNewsTotal(nres.total ?? 0);
        setNewsHasMore(nres.has_more ?? false);
      } else if (tabRef.current === "guard") {
        const gres = await loadGuard(pg);
        setGuardLogs(gres.logs ?? []);
        setGuardTotal(gres.total ?? 0);
        setGuardHasMore(gres.has_more ?? false);
      } else if (tabRef.current === "gate") {
        const gates = await loadGate(pg);
        setGateLogs(gates.logs ?? []);
        setGateTotal(gates.total ?? 0);
        setGateHasMore(gates.has_more ?? false);
      } else {
        const sres = await loadSched(pg);
        setSchedLogs(sres.logs ?? []);
        setSchedTotal(sres.total ?? 0);
        setSchedHasMore(sres.has_more ?? false);
      }
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadGate, loadGuard, loadNews, loadQuotes, loadSched]);

  useEffect(() => {
    loadOne("quotes");
  }, [loadOne]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.quoteTest();
      setTestResult(r);
      // ทดสอบสร้าง quote log ใหม่ — ล้างแคช quotes แล้วโหลดแท็บปัจจุบัน
      // (ถ้าอยู่แท็บอื่น quotes จะโหลดใหม่ตอนกดเข้ามา)
      loadedRef.current.delete("quotes");
      await loadOne(tabRef.current);
    } catch (e) {
      setTestResult({
        verdict: "fail",
        prices: {},
        failures: {},
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      setTesting(false);
    }
  };

  // ตารางคือ server page ปัจจุบัน (500 แถว) — filter ราคาทำฝั่ง server แล้ว
  const shown = logs;
  // จำนวนหน้าทั้งหมดอ้างจาก total จริง (count=exact) ไม่ใช่ความยาว chunk
  const totalPages = Math.max(1, Math.ceil((quoteTotal || shown.length) / PAGE_SIZE));
  const uiPagesPerChunk = Math.max(1, Math.ceil(SERVER_PAGE / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const chunkPage = ((safePage - 1) % uiPagesPerChunk) + 1;
  const pageRows = shown.slice((chunkPage - 1) * PAGE_SIZE, chunkPage * PAGE_SIZE);
  const newsChunkTotal = newsLogs.length;
  const newsTotalPages = Math.max(1, Math.ceil((newsTotal || newsChunkTotal) / PAGE_SIZE));
  const newsSafePage = Math.min(page, newsTotalPages);
  const newsChunkPage = ((newsSafePage - 1) % uiPagesPerChunk) + 1;
  const newsPageRows = newsLogs.slice((newsChunkPage - 1) * PAGE_SIZE, newsChunkPage * PAGE_SIZE);
  const schedChunkTotal = schedLogs.length;
  const schedTotalPages = Math.max(1, Math.ceil((schedTotal || schedChunkTotal) / PAGE_SIZE));
  const schedSafePage = Math.min(page, schedTotalPages);
  const schedChunkPage = ((schedSafePage - 1) % uiPagesPerChunk) + 1;
  const schedPageRows = schedLogs.slice((schedChunkPage - 1) * PAGE_SIZE, schedChunkPage * PAGE_SIZE);
  // แท็บ Guard ใช้ chunk ตัวเอง (server filter job=position_guard แล้ว)
  const guardChunkTotal = guardLogs.length;
  const guardTotalPages = Math.max(1, Math.ceil((guardTotal || guardChunkTotal) / PAGE_SIZE));
  const guardSafePage = Math.min(page, guardTotalPages);
  const guardChunkPage = ((guardSafePage - 1) % uiPagesPerChunk) + 1;
  const guardPageRows = guardLogs.slice((guardChunkPage - 1) * PAGE_SIZE, guardChunkPage * PAGE_SIZE);
  const guardServerPage = Math.floor((guardSafePage - 1) / uiPagesPerChunk) + 1;
  const guardServerPages = Math.max(1, Math.ceil(guardTotalPages / uiPagesPerChunk));
  // แท็บ Gate ใช้ chunk ตัวเอง (server filter event แล้ว)
  const gateChunkTotal = gateLogs.length;
  const gateTotalPages = Math.max(1, Math.ceil((gateTotal || gateChunkTotal) / PAGE_SIZE));
  const gateSafePage = Math.min(page, gateTotalPages);
  const gateChunkPage = ((gateSafePage - 1) % uiPagesPerChunk) + 1;
  const gatePageRows = gateLogs.slice((gateChunkPage - 1) * PAGE_SIZE, gateChunkPage * PAGE_SIZE);
  const gateServerPage = Math.floor((gateSafePage - 1) / uiPagesPerChunk) + 1;
  const gateServerPages = Math.max(1, Math.ceil(gateTotalPages / uiPagesPerChunk));
  const serverPage = Math.floor((safePage - 1) / uiPagesPerChunk) + 1;
  const newsServerPage = Math.floor((newsSafePage - 1) / uiPagesPerChunk) + 1;
  const schedServerPage = Math.floor((schedSafePage - 1) / uiPagesPerChunk) + 1;
  const serverPages = Math.max(1, Math.ceil(totalPages / uiPagesPerChunk));
  const newsServerPages = Math.max(1, Math.ceil(newsTotalPages / uiPagesPerChunk));
  const schedServerPages = Math.max(1, Math.ceil(schedTotalPages / uiPagesPerChunk));

  return (
    <div className="space-y-4">
      {/* ---------- Header + test button ---------- */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold flex items-center gap-2"><Icon n="scroll" size={19} /> Logs</h2>
          <p className="text-xs text-slate-500">
            {tab === "quotes"
              ? `บันทึกการดึงราคาทุกครั้งจาก API ภายนอก — เก็บ ${ttlDays} วัน ลบเกินอายุอัตโนมัติ`
              : tab === "news"
                ? "ประวัติการวิเคราะห์ข่าว (worker ทุก 15 นาที) — พาดหัวจริง + sentiment จาก AI"
                : tab === "guard"
                  ? "การทำงานของ Guard ทุกรอบ (ทุก 1 นาที) — เช็ค SL/TP, ขยับ SL, ปิดไม้ (เก็บ 7 วัน)"
                  : tab === "gate"
                    ? "Gate อนุมัติ/ปัดตกออเดอร์ — เหตุผลทุกครั้งที่เปิดหรือบล็อก (เก็บ 7 วัน)"
                    : "ประวัติการทำงาน scheduler ทุก job — พิสูจน์ว่า worker ยังรันอยู่ (เก็บ 7 วัน)"}
            {updatedAt && ` · อัปเดต ${updatedAt}`}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={() => loadOne(tabRef.current)} disabled={loading}
            aria-busy={loading} aria-live="polite"
            title={loading ? "กำลังโหลดข้อมูล..." : "รีเฟรชข้อมูลตอนนี้"}
            className="btn-secondary disabled:opacity-50">
            <span className="inline-flex items-center gap-1.5">
              {loading && <Icon n="spinner" size={14} className="animate-spin" />}
              รีเฟรช
            </span>
          </button>
          <button onClick={runTest} disabled={testing} className="btn-primary">
            <span className="inline-flex items-center gap-1.5">
              {testing && <Icon n="spinner" size={14} className="animate-spin" />}
              {testing ? "กำลังทดสอบ..." : "ทดสอบดึงราคา"}
            </span>
          </button>
        </div>
      </section>

      {/* ---------- Tabs: quotes / news / scheduler ---------- */}
      <div className="flex gap-2 text-xs">
        <button
          onClick={() => { setTab("quotes"); tabRef.current = "quotes"; setPage(1); if (!loadedRef.current.has("quotes")) loadOne("quotes"); }}
          className={`px-3 py-1 rounded ${tab === "quotes" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          ราคา (Quote API)
        </button>
        <button
          onClick={() => { setTab("news"); tabRef.current = "news"; setPage(1); if (!loadedRef.current.has("news")) loadOne("news"); }}
          className={`px-3 py-1 rounded ${tab === "news" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          ข่าว (News{newsSummary ? ` ${newsSummary.total}` : ""})
        </button>
        <button
          onClick={() => { setTab("scheduler"); tabRef.current = "scheduler"; setPage(1); if (!loadedRef.current.has("scheduler")) loadOne("scheduler"); }}
          className={`px-3 py-1 rounded ${tab === "scheduler" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Scheduler{schedSummary ? ` ${schedSummary.total}` : ""}
        </button>
        <button
          onClick={() => { setTab("guard"); tabRef.current = "guard"; setPage(1); if (!loadedRef.current.has("guard")) loadOne("guard"); }}
          className={`px-3 py-1 rounded ${tab === "guard" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Guard{schedSummary?.by_job?.position_guard ? ` ${schedSummary.by_job.position_guard.total}` : guardTotal ? ` ${guardTotal}` : ""}
        </button>
        <button
          onClick={() => { setTab("gate"); tabRef.current = "gate"; setPage(1); if (!loadedRef.current.has("gate")) loadOne("gate"); }}
          className={`px-3 py-1 rounded ${tab === "gate" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
          Gate{gateSummary ? ` ${gateSummary.blocked + gateSummary.opened}` : gateTotal ? ` ${gateTotal}` : ""}
        </button>
      </div>

      {err && <p className="text-loss text-sm">{err}</p>}

      {/* ---------- Test result ---------- */}
      {testResult && (
        <section className={`panel ${testResult.verdict === "ok" ? "border-profit" : "border-loss"}`}>
          <p className="text-sm font-bold mb-2 flex items-center gap-1.5">
            {testResult.verdict === "ok"
              ? <><Icon n="checkCircle" size={15} className="text-profit" /> ทดสอบสำเร็จ</>
              : <><Icon n="xCircle" size={15} className="text-loss" /> ทดสอบล้มเหลว</>}
            {testResult.hint && <span className="text-xs font-normal text-slate-400"> — {testResult.hint}</span>}
          </p>
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2 text-sm">
            {Object.entries(testResult.prices).map(([asset, price]) => (
              <div key={asset} className="bg-slate-800/60 rounded px-3 py-2">
                <p className="text-xs text-slate-500">{asset}</p>
                <p className="font-bold text-profit">{fmtNum(price, 4)}</p>
              </div>
            ))}
            {Object.entries(testResult.failures).map(([asset, msg]) => (
              <div key={asset} className="bg-slate-800/60 rounded px-3 py-2">
                <p className="text-xs text-slate-500">{asset}</p>
                <p className="text-xs text-loss truncate" title={msg}>{msg}</p>
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ---------- Summary card: forex vs gold ---------- */}
      {tab === "quotes" && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <BucketCard label="ยิงทั้งหมด (7 วัน)" bucket={summary} />
        <BucketCard label="Forex" bucket={summary?.forex} />
        <BucketCard label="Gold" bucket={summary?.gold} />
        <div className="panel">
          <p className="text-xs text-slate-500">สถานะรวม</p>
          <p className="text-2xl font-bold">
            <span className="text-emerald-400">{summary?.success ?? 0}</span>
            <span className="text-slate-500 text-base"> / </span>
            <span className="text-red-400">{summary?.error ?? 0}</span>
          </p>
          <p className="text-xs text-slate-500 mt-1">success / error</p>
        </div>
      </section>
      )}

      {/* ---------- News summary ---------- */}
      {tab === "news" && newsSummary && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">วิเคราะห์ทั้งหมด</p>
          <p className="text-2xl font-bold">{newsSummary.total}</p>
          <p className="text-xs mt-1">
            <span className="text-emerald-400">พาดหัวจริง {newsSummary.real}</span>
            {"  "}
            <span className="text-amber-400">heuristic {newsSummary.heuristic}</span>
          </p>
        </div>
        {Object.entries(newsSummary.by_event).map(([ev, n]) => (
          <div key={ev} className="panel">
            <p className="text-xs text-slate-500">{ev}</p>
            <p className="text-2xl font-bold">{n}</p>
          </div>
        ))}
      </section>
      )}

      {/* ---------- Scheduler summary ---------- */}
      {tab === "scheduler" && schedSummary && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">รันทั้งหมด (7 วัน)</p>
          <p className="text-2xl font-bold">{schedSummary.total}</p>
          <p className="text-xs mt-1">
            <span className="text-emerald-400">ok {schedSummary.ok}</span>
            {"  "}
            <span className="text-red-400">error {schedSummary.error}</span>
          </p>
        </div>
        {Object.entries(schedSummary.by_job).map(([job, b]) => (
          <div key={job} className="panel">
            <p className="text-xs text-slate-500">{job}</p>
            <p className="text-2xl font-bold">{b.total}</p>
            <p className="text-xs mt-1">
              <span className="text-emerald-400">ok {b.ok}</span>
              {b.error > 0 && <span className="text-red-400"> ✗{b.error}</span>}
            </p>
          </div>
        ))}
      </section>
      )}

      {/* ---------- Guard summary (position_guard เท่านั้น) ---------- */}
      {tab === "guard" && (() => {
        const b = schedSummary?.by_job?.position_guard;
        const latest = guardLogs[0];
        const latestKv = parseGuardDetail(latest?.detail ?? "");
        let moved500 = 0;
        let closed500 = 0;
        for (const g of guardLogs) {
          const kv = parseGuardDetail(g.detail ?? "");
          moved500 += kv.moved_sl ?? 0;
          closed500 += kv.closed ?? 0;
        }
        return (
        <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="panel">
            <p className="text-xs text-slate-500">รอบ guard (7 วัน)</p>
            <p className="text-2xl font-bold">{b ? b.total.toLocaleString() : guardTotal.toLocaleString()}</p>
            <p className="text-xs mt-1">
              {b ? (<><span className="text-emerald-400">ok {b.ok}</span>{"  "}<span className="text-red-400">error {b.error}</span></>) : (<span className="text-slate-500">ทุก 1 นาที/รอบ</span>)}
            </p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">รอบล่าสุด</p>
            <p className="text-sm font-bold mt-1">{latest?.created_at ? new Date(latest.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}</p>
            <p className="text-xs mt-1 text-slate-400">
              ตรวจ {latestKv.checked ?? "—"} ไม้ · ขยับ SL {latestKv.moved_sl ?? 0} · ปิด {latestKv.closed ?? 0}
            </p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">ขยับ SL (500 รอบล่าสุด)</p>
            <p className="text-2xl font-bold text-amber-400">{moved500}</p>
            <p className="text-xs text-slate-500 mt-1">ครั้งที่ guard เลื่อน stop</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">ปิดไม้ (500 รอบล่าสุด)</p>
            <p className="text-2xl font-bold">{closed500}</p>
            <p className="text-xs text-slate-500 mt-1">SL/TP + smart + time-stop</p>
          </div>
        </section>
        );
      })()}

      {/* ---------- Gate summary (order_blocked vs order_opened) ---------- */}
      {tab === "gate" && gateSummary && (
      <section className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <div className="panel">
          <p className="text-xs text-slate-500">Gate ปัดตก (7 วัน)</p>
          <p className="text-2xl font-bold text-amber-400">{gateSummary.blocked.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">order_blocked — pause/limit/news/corr/heat</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">Gate ผ่าน (7 วัน)</p>
          <p className="text-2xl font-bold text-emerald-400">{gateSummary.opened.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">order_opened — เปิดออเดอร์จริง</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">หมดอายุ (7 วัน)</p>
          <p className="text-2xl font-bold">{gateSummary.expired.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">expired — pending เกิน 30 นาที</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">ปิดไม้ (7 วัน)</p>
          <p className="text-2xl font-bold">{gateSummary.closed.toLocaleString()}</p>
          <p className="text-xs text-slate-500 mt-1">closed — SL/TP/manual</p>
        </div>
      </section>
      )}

      {/* ---------- Provider breakdown ---------- */}
      {tab === "quotes" && summary && Object.keys(summary.by_provider).length > 0 && (
        <section className="panel">
          <p className="text-xs text-slate-500 mb-2">แยกตาม Provider</p>
          <div className="flex flex-wrap gap-2 text-xs">
            {Object.entries(summary.by_provider).map(([prov, b]) => (
              <span key={prov} className="bg-slate-800/60 rounded px-3 py-1">
                <b>{prov}</b>: {b.total} calls
                <span className="text-emerald-400"> ✓{b.success}</span>
                {b.error > 0 && <span className="text-red-400"> ✗{b.error}</span>}
              </span>
            ))}
          </div>
        </section>
      )}

      {/* ---------- Filter + log table ---------- */}
      {/* หมายเหตุ: overflow-x-auto ต้องอยู่ div ลูก ไม่ใช่บน .panel — backdrop-filter
          บนตัว scroll container เองจะพังใน Chromium (blur หายเมื่อตารางโหลด/เลื่อน) */}
      {tab === "quotes" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex gap-2 mb-3 text-xs">
          {(["all", "forex", "gold"] as const).map((f) => (
            <button
              key={f}
              onClick={() => { setFilter(f); filterRef.current = f; setPage(1); loadOne("quotes", f); }}
              className={`px-3 py-1 rounded ${filter === f ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}
            >
              {f === "all" ? "ทั้งหมด" : f === "forex" ? "Forex" : "Gold"}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Asset</th>
              <th className="py-2 pr-3">ประเภท</th>
              <th className="py-2 pr-3">Provider</th>
              <th className="py-2 pr-3">URL</th>
              <th className="py-2 pr-3">API Key</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">HTTP</th>
              <th className="py-2 pr-3">ราคา</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && logs.length === 0 && (
              <tr><td colSpan={11} className="py-6">
                <LoadingGraphic message="กำลังโหลดบันทึก API — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && shown.length === 0 && (
              <tr><td colSpan={11} className="py-6 text-center text-slate-500">
                ยังไม่มี log — กด &quot;ทดสอบดึงราคา&quot; เพื่อสร้างรายการแรก
              </td></tr>
            )}
            {pageRows.map((l) => (
              <tr key={l.id} className="border-b border-slate-800/50 hover:bg-white/[0.04]">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {new Date(l.created_at).toLocaleString("th-TH", { hour12: false })}
                </td>
                <td className="py-2 pr-3 font-bold">{l.asset}</td>
                <td className="py-2 pr-3">
                  <span className={l.category === "gold" ? "text-amber-400" : "text-sky-400"}>
                    {l.category}
                  </span>
                </td>
                <td className="py-2 pr-3">{l.provider}</td>
                <td className="py-2 pr-3 max-w-[260px] truncate text-slate-400" title={l.url}>{l.url}</td>
                <td className="py-2 pr-3 font-mono text-slate-500">{l.api_key_hint || "—"}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded ${statusBadge(l.status)}`}>{l.status}</span>
                </td>
                <td className="py-2 pr-3">{l.http_status ?? "—"}</td>
                <td className="py-2 pr-3 font-mono">{l.price != null ? fmtNum(l.price, 4) : "—"}</td>
                <td className="py-2 pr-3 text-slate-400">{l.duration_ms ?? "—"}</td>
                <td className="py-2 max-w-[200px] truncate" title={l.error ?? ""}><span className="text-loss">{l.error || "—"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {(logs.length > 0 || quoteTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {quoteTotal.toLocaleString()} รายการ
              {serverPages > 1 && ` · ชุดที่ ${serverPage}/${serverPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {totalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (chunkPage > 1 || safePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(serverPage - 1).then(() => setPage((serverPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={safePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{safePage} / {totalPages}</span>
                <button onClick={() => {
                    if (chunkPage < uiPagesPerChunk && chunkPage * PAGE_SIZE < shown.length) { setPage((p) => Math.min(totalPages, p + 1)); return; }
                    if (!quoteHasMore && serverPage >= serverPages) { setPage((p) => Math.min(totalPages, p + 1)); return; }
                    gotoServerPage(serverPage + 1).then(() => setPage(serverPage * uiPagesPerChunk + 1));
                  }}
                  disabled={safePage >= totalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- News history table ---------- */}
      {tab === "news" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Event</th>
              <th className="py-2 pr-3">Sentiment</th>
              <th className="py-2 pr-3">สินทรัพย์</th>
              <th className="py-2 pr-3">ความมั่นใจ</th>
              <th className="py-2 pr-3">ที่มา</th>
              <th className="py-2">บทวิเคราะห์</th>
            </tr>
          </thead>
          <tbody>
            {loading && newsLogs.length === 0 && (
              <tr><td colSpan={7} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติข่าว — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && newsLogs.length === 0 && (
              <tr><td colSpan={7} className="py-6 text-center text-slate-500">
                ยังไม่มีประวัติข่าว
              </td></tr>
            )}
            {newsPageRows.map((n) => {
              const isHeuristic = !n.analysis || n.analysis.includes("heuristic");
              const s = n.sentiment ?? 0;
              return (
              <tr key={n.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {n.created_at ? new Date(n.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{n.event}</td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  <span className={s > 0.2 ? "text-emerald-400" : s < -0.2 ? "text-red-400" : "text-slate-400"}>
                    {s > 0 ? `+${s.toFixed(2)}` : s.toFixed(2)}
                  </span>
                </td>
                <td className="py-2 pr-3 whitespace-nowrap text-slate-300">
                  {(n.affected_assets ?? []).join(", ") || "—"}
                </td>
                <td className="py-2 pr-3 text-slate-300">
                  {n.confidence != null ? `${Number(n.confidence).toFixed(0)}%` : "—"}
                </td>
                <td className="py-2 pr-3 whitespace-nowrap">
                  <span className={`px-2 py-0.5 rounded ${isHeuristic ? "bg-amber-500/15 text-amber-400" : "bg-emerald-500/15 text-emerald-400"}`}>
                    {isHeuristic ? "heuristic" : "พาดหัวจริง"}
                  </span>
                </td>
                <td className="py-2 max-w-[420px] text-slate-300" style={{ whiteSpace: "pre-wrap" }}>
                  {n.analysis || "—"}
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(newsLogs.length > 0 || newsTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {newsSafePage}/{newsTotalPages} · แสดง {newsPageRows.length} จาก {newsTotal.toLocaleString()} รายการ
              {newsServerPages > 1 && ` · ชุดที่ ${newsServerPage}/${newsServerPages}`}
            </p>
            {newsTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (newsChunkPage > 1 || newsSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(newsServerPage - 1).then(() => setPage((newsServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={newsSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{newsSafePage} / {newsTotalPages}</span>
                <button onClick={() => {
                    if (newsChunkPage < uiPagesPerChunk && newsChunkPage * PAGE_SIZE < newsChunkTotal) { setPage((p) => Math.min(newsTotalPages, p + 1)); return; }
                    if (!newsHasMore && newsServerPage >= newsServerPages) { setPage((p) => Math.min(newsTotalPages, p + 1)); return; }
                    gotoServerPage(newsServerPage + 1).then(() => setPage(newsServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={newsSafePage >= newsTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Scheduler run table ---------- */}
      {tab === "scheduler" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Job</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2 pr-3">ผลลัพธ์</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && schedLogs.length === 0 && (
              <tr><td colSpan={6} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ scheduler — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && schedLogs.length === 0 && (
              <tr><td colSpan={6} className="py-6 text-center text-slate-500">
                {schedulerEmptyMessage("scheduler", schedSummary)}
              </td></tr>
            )}
            {schedPageRows.map((s) => (
              <tr key={s.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {s.created_at ? new Date(s.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{s.job_id}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded ${s.status === "ok" ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"}`}>{s.status}</span>
                </td>
                <td className="py-2 pr-3 text-slate-400">{s.duration_ms ?? "—"}</td>
                <td className="py-2 pr-3 max-w-[320px] truncate text-slate-300" title={s.detail || ""}>{s.detail || "—"}</td>
                <td className="py-2 max-w-[280px] truncate" title={s.error || ""}><span className="text-loss">{s.error || "—"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
        {(schedLogs.length > 0 || schedTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {schedSafePage}/{schedTotalPages} · แสดง {schedPageRows.length} จาก {schedTotal.toLocaleString()} รายการ
              {schedServerPages > 1 && ` · ชุดที่ ${schedServerPage}/${schedServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {schedTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (schedChunkPage > 1 || schedSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(schedServerPage - 1).then(() => setPage((schedServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={schedSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{schedSafePage} / {schedTotalPages}</span>
                <button onClick={() => {
                    if (schedChunkPage < uiPagesPerChunk && schedChunkPage * PAGE_SIZE < schedChunkTotal) { setPage((p) => Math.min(schedTotalPages, p + 1)); return; }
                    if (!schedHasMore && schedServerPage >= schedServerPages) { setPage((p) => Math.min(schedTotalPages, p + 1)); return; }
                    gotoServerPage(schedServerPage + 1).then(() => setPage(schedServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={schedSafePage >= schedTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Gate run table (order_blocked / order_opened) ---------- */}
      {tab === "gate" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex gap-2 mb-3 text-xs">
          {(["order_blocked", "order_opened", "all"] as const).map((f) => (
            <button
              key={f}
              onClick={async () => {
                setGateFilter(f); gateFilterRef.current = f; setPage(1);
                setLoading(true);
                try {
                  const res = await api.signalLogs(SERVER_PAGE, 0, f);
                  setGateLogs(res.logs ?? []);
                  setGateTotal(res.total ?? (res.logs ?? []).length);
                  setGateHasMore(res.has_more ?? false);
                  if (res.summary) setGateSummary(res.summary);
                } finally { setLoading(false); }
              }}
              className={`px-3 py-1 rounded ${gateFilter === f ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}>
              {f === "all" ? "ทั้งหมด" : f === "order_blocked" ? "ปัดตก" : "ผ่าน"}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">Asset</th>
              <th className="py-2 pr-3">ทิศ</th>
              <th className="py-2 pr-3">ผล</th>
              <th className="py-2 pr-3">Conf</th>
              <th className="py-2 pr-3">Entry</th>
              <th className="py-2 pr-3">เหตุผล gate</th>
              <th className="py-2">Ticket</th>
            </tr>
          </thead>
          <tbody>
            {loading && gateLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ gate — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && gateLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6 text-center text-slate-500">
                ยังไม่มีประวัติ gate ในช่วงนี้
              </td></tr>
            )}
            {gatePageRows.map((g) => {
              const blocked = g.event === "order_blocked";
              return (
              <tr key={g.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {g.created_at ? new Date(g.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3 font-bold whitespace-nowrap">{g.asset || "—"}</td>
                <td className="py-2 pr-3 whitespace-nowrap">{(g.direction || "—").toUpperCase()}</td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded whitespace-nowrap ${blocked ? "bg-amber-500/15 text-amber-400" : "bg-emerald-500/15 text-emerald-400"}`}>
                    {blocked ? "ปัดตก" : g.event === "order_opened" ? "ผ่าน" : g.event}
                  </span>
                </td>
                <td className="py-2 pr-3 text-slate-300">{g.confidence != null ? `${Number(g.confidence).toFixed(0)}` : "—"}</td>
                <td className="py-2 pr-3 font-mono text-slate-300">{g.entry != null ? fmtNum(g.entry, 4) : "—"}</td>
                <td className="py-2 pr-3 max-w-[420px] text-slate-300" style={{ whiteSpace: "pre-wrap" }} title={g.reason || ""}>{g.reason || "—"}</td>
                <td className="py-2 font-mono text-slate-500">{g.ticket || "—"}</td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(gateLogs.length > 0 || gateTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {gateSafePage}/{gateTotalPages} · แสดง {gatePageRows.length} จาก {gateTotal.toLocaleString()} รายการ
              {gateServerPages > 1 && ` · ชุดที่ ${gateServerPage}/${gateServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {gateTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (gateChunkPage > 1 || gateSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(gateServerPage - 1).then(() => setPage((gateServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={gateSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{gateSafePage} / {gateTotalPages}</span>
                <button onClick={() => {
                    if (gateChunkPage < uiPagesPerChunk && gateChunkPage * PAGE_SIZE < gateChunkTotal) { setPage((p) => Math.min(gateTotalPages, p + 1)); return; }
                    if (!gateHasMore && gateServerPage >= gateServerPages) { setPage((p) => Math.min(gateTotalPages, p + 1)); return; }
                    gotoServerPage(gateServerPage + 1).then(() => setPage(gateServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={gateSafePage >= gateTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}

      {/* ---------- Guard run table (position_guard เท่านั้น) ---------- */}
      {tab === "guard" && (
      <section className="panel">
        <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">สถานะ</th>
              <th className="py-2 pr-3">ตรวจ</th>
              <th className="py-2 pr-3">ขยับ SL</th>
              <th className="py-2 pr-3">ปิดไม้</th>
              <th className="py-2 pr-3">ms</th>
              <th className="py-2 pr-3">รายละเอียด</th>
              <th className="py-2">Error</th>
            </tr>
          </thead>
          <tbody>
            {loading && guardLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6">
                <LoadingGraphic message="กำลังโหลดประวัติ guard — Render cold start อาจใช้เวลาสักครู่" compact />
              </td></tr>
            )}
            {!loading && guardLogs.length === 0 && (
              <tr><td colSpan={8} className="py-6 text-center text-slate-500">
                {schedulerEmptyMessage("guard", schedSummary)}
              </td></tr>
            )}
            {guardPageRows.map((g) => {
              const kv = parseGuardDetail(g.detail ?? "");
              const moved = kv.moved_sl ?? 0;
              const closed = kv.closed ?? 0;
              return (
              <tr key={g.id} className="border-b border-slate-800/50 hover:bg-white/[0.04] align-top">
                <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                  {g.created_at ? new Date(g.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                </td>
                <td className="py-2 pr-3">
                  <span className={`px-2 py-0.5 rounded ${g.status === "ok" ? "bg-emerald-500/15 text-emerald-400" : "bg-red-500/15 text-red-400"}`}>{g.status}</span>
                </td>
                <td className="py-2 pr-3 text-slate-300">{kv.checked ?? "—"}</td>
                <td className="py-2 pr-3 font-bold">
                  {moved > 0 ? <span className="text-amber-400">{moved}</span> : <span className="text-slate-500">0</span>}
                </td>
                <td className="py-2 pr-3">
                  {closed > 0 ? <span className="text-sky-400 font-bold">{closed}</span> : <span className="text-slate-500">0</span>}
                </td>
                <td className="py-2 pr-3 text-slate-400">{g.duration_ms ?? "—"}</td>
                <td className="py-2 pr-3 max-w-[320px] truncate text-slate-300" title={g.detail || ""}>{g.detail || "—"}</td>
                <td className="py-2 max-w-[280px] truncate" title={g.error || ""}><span className="text-loss">{g.error || "—"}</span></td>
              </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(guardLogs.length > 0 || guardTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {guardSafePage}/{guardTotalPages} · แสดง {guardPageRows.length} จาก {guardTotal.toLocaleString()} รอบ
              {guardServerPages > 1 && ` · ชุดที่ ${guardServerPage}/${guardServerPages}`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {guardTotalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => {
                    if (guardChunkPage > 1 || guardSafePage <= 1) { setPage((p) => Math.max(1, p - 1)); return; }
                    gotoServerPage(guardServerPage - 1).then(() => setPage((guardServerPage - 2) * uiPagesPerChunk + uiPagesPerChunk));
                  }}
                  disabled={guardSafePage <= 1 || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{guardSafePage} / {guardTotalPages}</span>
                <button onClick={() => {
                    if (guardChunkPage < uiPagesPerChunk && guardChunkPage * PAGE_SIZE < guardChunkTotal) { setPage((p) => Math.min(guardTotalPages, p + 1)); return; }
                    if (!guardHasMore && guardServerPage >= guardServerPages) { setPage((p) => Math.min(guardTotalPages, p + 1)); return; }
                    gotoServerPage(guardServerPage + 1).then(() => setPage(guardServerPage * uiPagesPerChunk + 1));
                  }}
                  disabled={guardSafePage >= guardTotalPages || loading}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ถัดไป
                </button>
              </div>
            )}
          </div>
        )}
      </section>
      )}
    </div>
  );
}
