"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
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

export default function SignalLogsPage() {
  const [logs, setLogs] = useState<SignalLog[]>([]);
  const [summary, setSummary] = useState<SignalLogSummary | null>(null);
  const [ttlDays, setTtlDays] = useState(7);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [filter, setFilter] = useState<string>("all");
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const res = await api.signalLogs(200);
      setLogs(res.logs ?? []);
      setSummary(res.summary ?? null);
      setTtlDays(res.ttl_days ?? 7);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const shown = filter === "all" ? logs : logs.filter((l) => l.event === filter);

  return (
    <div className="space-y-4">
      {/* ---------- Header ---------- */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold">🗂️ Signal Logs — บันทึกชีวิตสัญญาณ</h2>
          <p className="text-xs text-slate-500">
            ตั้งแต่เกิดสัญญาณ → เปิด/ไม่เปิดออเดอร์ → ปิดไม้ พร้อมเหตุผล — เก็บ {ttlDays} วัน ลบเกินอายุอัตโนมัติ
            {updatedAt && ` · อัปเดต ${updatedAt}`}
          </p>
        </div>
        <button onClick={load} disabled={loading}
          className="btn-secondary disabled:opacity-50">
          {loading ? "กำลังโหลด..." : "รีเฟรช"}
        </button>
      </section>

      {err && <p className="text-loss text-sm">⚠️ {err}</p>}

      {/* ---------- Summary cards ---------- */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <StatCard label="เหตุการณ์ทั้งหมด (7 วัน)" value={summary?.total ?? null} />
        <StatCard label="✅ เปิดออเดอร์" value={summary?.opened ?? null} cls="text-emerald-400" />
        <StatCard label="⛔ ไม่เปิดออเดอร์" value={summary?.blocked ?? null} cls="text-amber-400" />
        <StatCard label="⌛ หมดอายุ" value={summary?.expired ?? null} />
        <StatCard label="🔒 ปิดไม้" value={summary?.closed ?? null} cls="text-violet-300" />
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
      <section className="panel overflow-x-auto">
        <div className="flex flex-wrap gap-2 mb-3 text-xs">
          <button
            onClick={() => setFilter("all")}
            className={`px-3 py-1 rounded ${filter === "all" ? "bg-accent text-white font-bold" : "bg-slate-800 text-slate-400"}`}
          >
            ทั้งหมด
          </button>
          {Object.entries(EVENT_META).map(([ev, meta]) => (
            <button
              key={ev}
              onClick={() => setFilter(ev)}
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
              <th className="py-2">เหตุผล</th>
            </tr>
          </thead>
          <tbody>
            {loading && logs.length === 0 && (
              <tr><td colSpan={14} className="py-6 text-center text-slate-500 animate-pulse">
                ⏳ กำลังโหลดข้อมูล... (API บน Render อาจใช้เวลาเริ่มต้นสักครู่)
              </td></tr>
            )}
            {!loading && shown.length === 0 && (
              <tr><td colSpan={14} className="py-6 text-center text-slate-500">
                {filter === "all"
                  ? "ยังไม่มีบันทึก — สัญญาณใหม่จะถูกบันทึกอัตโนมัติเมื่อ scanner เจอโอกาส"
                  : `ไม่มีรายการ "${EVENT_META[filter]?.label ?? filter}" ในช่วง 7 วันที่เก็บข้อมูล`}
              </td></tr>
            )}
            {shown.map((l) => {
              const meta = eventMeta(l.event);
              return (
                <tr key={l.id} className="border-b border-slate-800/50 hover:bg-white/[0.04]">
                  <td className="py-2 pr-3 whitespace-nowrap text-slate-400">
                    {l.created_at ? new Date(l.created_at).toLocaleString("th-TH", { hour12: false }) : "—"}
                  </td>
                  <td className="py-2 pr-3 font-bold">{l.asset || "—"}</td>
                  <td className="py-2 pr-3">
                    {l.direction
                      ? <span className={`font-bold ${l.direction === "buy" ? "text-emerald-400" : "text-red-400"}`}>
                          {l.direction.toUpperCase()}
                        </span>
                      : "—"}
                  </td>
                  <td className="py-2 pr-3">
                    <span className={`px-2 py-0.5 rounded whitespace-nowrap ${meta.cls}`}>{meta.label}</span>
                  </td>
                  <td className="py-2 pr-3">{l.confidence != null ? `${l.confidence}%` : "—"}</td>
                  <td className="py-2 pr-3 font-mono">{l.entry != null ? fmtNum(l.entry, 4) : "—"}</td>
                  <td className="py-2 pr-3 font-mono text-loss">{l.stop_loss != null ? fmtNum(l.stop_loss, 4) : "—"}</td>
                  <td className="py-2 pr-3 font-mono text-emerald-400">{l.take_profit != null ? fmtNum(l.take_profit, 4) : "—"}</td>
                  <td className="py-2 pr-3 font-mono">{l.exit_price != null ? fmtNum(l.exit_price, 4) : "—"}</td>
                  <td className="py-2 pr-3">{l.volume != null ? l.volume : "—"}</td>
                  <td className={`py-2 pr-3 font-mono ${(l.pnl ?? 0) >= 0 ? "text-emerald-400" : "text-red-400"}`}>
                    {l.pnl != null ? l.pnl.toFixed(2) : "—"}
                  </td>
                  <td className="py-2 pr-3 font-mono text-slate-500">{l.ticket || "—"}</td>
                  <td className="py-2 pr-3 text-slate-400">{l.source || "—"}</td>
                  <td className="py-2 max-w-[280px] text-slate-300" title={l.reason}>{l.reason || "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {logs.length > 0 && (
          <p className="text-xs text-slate-500 mt-2">
            แสดง {shown.length} รายการล่าสุด (จาก {logs.length}) · เก็บสูงสุด {ttlDays} วัน
          </p>
        )}
      </section>
    </div>
  );
}
