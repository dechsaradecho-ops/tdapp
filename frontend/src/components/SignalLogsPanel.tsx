"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import Icon from "@/components/Icon";
import LoadingGraphic from "@/components/LoadingGraphic";
import { SignalLog, SignalLogSummary } from "@/lib/types";

/** badge สี/ข้อความของแต่ละ lifecycle event */
const EVENT_META: Record<string, { label: string; cls: string }> = {
  created: { label: "เกิดสัญญาณ", cls: "bg-sky-500/15 text-sky-400" },
  order_opened: { label: "เปิดออเดอร์", cls: "bg-emerald-500/15 text-emerald-400" },
  order_blocked: { label: "ไม่เปิดออเดอร์", cls: "bg-amber-500/15 text-amber-400" },
  rejected: { label: "ถูกปฏิเสธ", cls: "bg-red-500/15 text-red-400" },
  expired: { label: "หมดอายุ", cls: "bg-slate-500/20 text-slate-300" },
  closed: { label: "ปิดไม้", cls: "bg-violet-500/15 text-violet-300" },
};

function eventMeta(ev: string) {
  return EVENT_META[ev] ?? { label: ev, cls: "bg-slate-500/20 text-slate-300" };
}

function StatCard({ label, value, cls = "" }: { label: string; value: number | null; cls?: string }) {
  return (
    <div className="panel">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-2xl font-bold ${cls}`}>{value == null ? "—" : value.toLocaleString()}</p>
    </div>
  );
}

/** แท็บ "บันทึกสัญญาณ" ในหน้าสัญญาณ — เดิมอยู่หน้า /signal-logs
 *  (รวมเข้าหน้าสัญญาณตามแผนจัดเมนูใหม่ Plan B) */
export default function SignalLogsPanel() {
  const [logs, setLogs] = useState<SignalLog[]>([]);
  const [summary, setSummary] = useState<SignalLogSummary | null>(null);
  const [ttlDays, setTtlDays] = useState(7);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const [loading, setLoading] = useState(false);
  // server paging: ขอทีละชุด (500 แถว/ครั้ง) แล้วแบ่งแสดง 50/หน้า —
  // ตาราง 7 วันโตเกิน 500 ได้ จึงต้องเดิน offset ไปเรื่อย ๆ (pattern เดียวกับ logs page)
  const [page, setPage] = useState(1);
  const [sigTotal, setSigTotal] = useState(0);
  const [sigHasMore, setSigHasMore] = useState(false);
  const filterRef = useRef<string>("all");
  const PAGE_SIZE = 50;
  const SERVER_PAGE = 500;

  const loadChunk = useCallback(async (flt: string, pg: number) => {
    const offset = (pg - 1) * SERVER_PAGE;
    return api.signalLogs(SERVER_PAGE, offset, flt);
  }, []);

  const load = useCallback(async (flt?: string) => {
    setLoading(true);
    try {
      const f = flt ?? filterRef.current;
      const res = await loadChunk(f, 1);
      setLogs(res.logs ?? []);
      setSummary(res.summary ?? null);
      setTtlDays(res.ttl_days ?? 7);
      setSigTotal(res.total ?? (res.logs ?? []).length);
      setSigHasMore(res.has_more ?? false);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadChunk]);

  // เปลี่ยน server chunk (ทุก 10 หน้า UI = 500 แถว) — ดึงชุดถัดไปจาก backend
  const gotoServerPage = useCallback(async (pg: number) => {
    setLoading(true);
    try {
      const res = await loadChunk(filterRef.current, pg);
      setLogs(res.logs ?? []);
      setSigTotal(res.total ?? 0);
      setSigHasMore(res.has_more ?? false);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [loadChunk]);

  useEffect(() => {
    load();
  }, [load]);

  const changeFilter = (f: string) => {
    setFilter(f);
    filterRef.current = f;
    setPage(1);
    load(f);
  };

  // ตารางคือ server chunk ปัจจุบัน (500 แถว) — filter ทำฝั่ง server แล้ว
  const shown = logs;
  // จำนวนหน้าทั้งหมดอ้างจาก total จริง (count=exact) ไม่ใช่ความยาว chunk
  const totalPages = Math.max(1, Math.ceil((sigTotal || shown.length) / PAGE_SIZE));
  const uiPagesPerChunk = Math.max(1, Math.ceil(SERVER_PAGE / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const chunkPage = ((safePage - 1) % uiPagesPerChunk) + 1;
  const pageRows = shown.slice((chunkPage - 1) * PAGE_SIZE, chunkPage * PAGE_SIZE);
  const serverPage = Math.floor((safePage - 1) / uiPagesPerChunk) + 1;
  const serverPages = Math.max(1, Math.ceil(totalPages / uiPagesPerChunk));

  return (
    <div className="space-y-4">
      {/* ---------- Header ---------- */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold flex items-center gap-2"><Icon n="archive" size={19} /> Signal Logs — บันทึกชีวิตสัญญาณ</h2>
          <p className="text-xs text-slate-500">
            ตั้งแต่เกิดสัญญาณ → เปิด/ไม่เปิดออเดอร์ → ปิดไม้ พร้อมเหตุผล — เก็บ {ttlDays} วัน ลบเกินอายุอัตโนมัติ
            {updatedAt && ` · อัปเดต ${updatedAt}`}
          </p>
        </div>
        <button onClick={() => load()} disabled={loading}
          aria-busy={loading} aria-live="polite"
          title={loading ? "กำลังโหลดข้อมูล..." : "รีเฟรชข้อมูลตอนนี้"}
          className="btn-secondary disabled:opacity-50">
          <span className="inline-flex items-center gap-1.5">
            {loading && <Icon n="spinner" size={14} className="animate-spin" />}
            รีเฟรช
          </span>
        </button>
      </section>

      {err && <p className="text-loss text-sm">{err}</p>}

      {/* ---------- Summary cards ---------- */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard label="เหตุการณ์ทั้งหมด (7 วัน)" value={summary?.total ?? null} />
        <StatCard label="เปิดออเดอร์" value={summary?.opened ?? null} cls="text-emerald-400" />
        <StatCard label="ไม่เปิดออเดอร์" value={summary?.blocked ?? null} cls="text-amber-400" />
        <StatCard label="หมดอายุ" value={summary?.expired ?? null} />
        <StatCard label="ปิดไม้" value={summary?.closed ?? null} cls="text-violet-300" />
      </section>

      {/* ---------- Asset breakdown ---------- */}
      {summary && Object.keys(summary.by_asset).length > 0 && (
        <section className="panel">
          <p className="text-xs text-slate-500 mb-2">แยกตามสัญลักษณ์</p>
          <div className="flex flex-wrap gap-2 text-xs">
            {Object.entries(summary.by_asset).map(([asset, n]) => (
              <span key={asset} className="bg-slate-800/60 rounded px-3 py-1">
                <b>{asset}</b>: {n} เหตุการณ์
              </span>
            ))}
          </div>
        </section>
      )}

      {/* ---------- Filter + log table ---------- */}
      {/* หมายเหตุ: overflow-x-auto ต้องอยู่ div ลูก ไม่ใช่บน .panel — backdrop-filter
          บนตัว scroll container เองจะพังใน Chromium (blur หายเมื่อตารางโหลด/เลื่อน) */}
      <section className="panel">
        <div className="overflow-x-auto">
        <div className="flex flex-wrap gap-2 mb-3 text-xs">
          <button
            onClick={() => changeFilter("all")}
            className={`px-3 py-1 rounded ${filter === "all" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}
          >
            ทั้งหมด
          </button>
          {Object.entries(EVENT_META).map(([ev, meta]) => (
            <button
              key={ev}
              onClick={() => changeFilter(ev)}
              className={`px-3 py-1 rounded ${filter === ev ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}
            >
              {meta.label}
            </button>
          ))}
        </div>
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 text-left border-b border-slate-800">
              <th className="py-2 pr-3">เวลา</th>
              <th className="py-2 pr-3">สัญลักษณ์</th>
              <th className="py-2 pr-3">ทิศทาง</th>
              <th className="py-2 pr-3">เหตุการณ์</th>
              <th className="py-2 pr-3">Confidence</th>
              <th className="py-2 pr-3">Entry</th>
              <th className="py-2 pr-3">SL</th>
              <th className="py-2 pr-3">TP</th>
              <th className="py-2 pr-3">Exit</th>
              <th className="py-2 pr-3">Lots</th>
              <th className="py-2 pr-3">PnL</th>
              <th className="py-2 pr-3">Ticket</th>
              <th className="py-2 pr-3">ที่มา</th>
              <th className="py-2 max-w-[280px]">เหตุผล</th>
            </tr>
          </thead>
          <tbody>
            {loading && logs.length === 0 && (
              <tr><td colSpan={14} className="py-6">
                <LoadingGraphic message="กำลังโหลดข้อมูล... (API บน Render อาจใช้เวลาเริ่มต้นสักครู่)" compact />
              </td></tr>
            )}
            {!loading && pageRows.length === 0 && (
              <tr><td colSpan={14} className="py-6 text-center text-slate-500">
                {filter === "all"
                  ? "ยังไม่มีบันทึก — สัญญาณใหม่จะถูกบันทึกอัตโนมัติเมื่อ scanner เจอโอกาส"
                  : `ไม่มีรายการ "${EVENT_META[filter]?.label ?? filter}" ในช่วง 7 วันที่เก็บข้อมูล`}
              </td></tr>
            )}
            {pageRows.map((l) => {
              const meta = eventMeta(l.event);
              return (
                <tr key={l.id} className="border-b border-slate-800/50 hover:bg-white/[0.04]">
                  <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                    {l.created_at ? new Date(l.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                  </td>
                  <td className="py-2 pr-3 font-bold">{l.asset || "—"}</td>
                  <td className="py-2 pr-3">
                    {l.direction
                      ? <span className={`inline-flex items-center rounded-full px-2 py-0.5 font-bold ${l.direction === "buy" ? "bg-emerald-500/30 text-emerald-400" : "bg-red-500/30 text-red-400"}`}>
                          {l.direction.toUpperCase()}
                        </span>
                      : "—"}
                  </td>
                  <td className="py-2 pr-3">
                    <span className={`px-2 py-0.5 rounded whitespace-nowrap ${meta.cls}`}>{meta.label}</span>
                  </td>
                  <td className="py-2 pr-3 font-bold">{l.confidence != null ? `${l.confidence}%` : "—"}</td>
                  <td className="py-2 pr-3 font-mono font-bold">{l.entry != null ? fmtNum(l.entry, 4) : "—"}</td>
                  <td className="py-2 pr-3">{l.stop_loss != null ? <span className="inline-flex items-center rounded-full bg-loss/30 text-loss px-2 py-0.5 font-mono font-bold">{fmtNum(l.stop_loss, 4)}</span> : "—"}</td>
                  <td className="py-2 pr-3">{l.take_profit != null ? <span className="inline-flex items-center rounded-full bg-profit/30 text-profit px-2 py-0.5 font-mono font-bold">{fmtNum(l.take_profit, 4)}</span> : "—"}</td>
                  <td className="py-2 pr-3 font-mono font-bold">{l.exit_price != null ? fmtNum(l.exit_price, 4) : "—"}</td>
                  <td className="py-2 pr-3 font-bold">{l.volume != null ? l.volume : "—"}</td>
                  <td className="py-2 pr-3">
                    {l.pnl != null
                      ? <span className={`inline-flex items-center rounded-full px-2 py-0.5 font-mono font-bold ${l.pnl >= 0 ? "bg-profit/30 text-profit" : "bg-loss/30 text-loss"}`}>
                          {l.pnl.toFixed(2)}
                        </span>
                      : <span className="font-mono text-slate-500">—</span>}
                  </td>
                  <td className="py-2 pr-3 font-mono text-slate-500">{l.ticket || "—"}</td>
                  <td className="py-2 pr-3 text-slate-400">{l.source || "—"}</td>
                  <td className="py-2 max-w-[280px] text-slate-300" title={l.reason}>
                    <span className="block whitespace-nowrap overflow-hidden text-ellipsis">
                      {l.reason || "—"}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        </div>
        {(logs.length > 0 || sigTotal > 0) && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {sigTotal.toLocaleString()} รายการ
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
                    if (!sigHasMore && serverPage >= serverPages) { setPage((p) => Math.min(totalPages, p + 1)); return; }
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
    </div>
  );
}
