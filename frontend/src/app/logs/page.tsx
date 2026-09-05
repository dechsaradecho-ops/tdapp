"use client";

import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import {
  QuoteApiLog,
  QuoteLogSummary,
  QuoteTestResult,
} from "@/lib/types";

/** สีของ badge ตามสถานะ call */
function statusBadge(status: string) {
  return status === "success"
    ? "bg-emerald-500/15 text-emerald-400"
    : "bg-red-500/15 text-red-400";
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
  const [logs, setLogs] = useState<QuoteApiLog[]>([]);
  const [summary, setSummary] = useState<QuoteLogSummary | null>(null);
  const [ttlDays, setTtlDays] = useState(7);
  const [err, setErr] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [loading, setLoading] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<QuoteTestResult | null>(null);
  const [filter, setFilter] = useState<"all" | "forex" | "gold">("all");
  const [page, setPage] = useState(1);
  const PAGE_SIZE = 50;

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // API caps limit at 500 — มากสุดที่ดึงได้ต่อครั้ง
      const res = await api.quoteLogs(500);
      setLogs(res.logs ?? []);
      setSummary(res.summary ?? null);
      setTtlDays(res.ttl_days ?? 7);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      setPage(1);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const r = await api.quoteTest();
      setTestResult(r);
      await load(); // ดึง log ล่าสุดที่เพิ่งเกิดจากการทดสอบ
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

  const shown = filter === "all" ? logs : logs.filter((l) => l.category === filter);
  const totalPages = Math.max(1, Math.ceil(shown.length / PAGE_SIZE));
  const safePage = Math.min(page, totalPages);
  const pageRows = shown.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  return (
    <div className="space-y-4">
      {/* ---------- Header + test button ---------- */}
      <section className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 className="text-xl font-bold">📜 Quote API Logs</h2>
          <p className="text-xs text-slate-500">
            บันทึกการดึงราคาทุกครั้งจาก API ภายนอก — เก็บ {ttlDays} วัน ลบเกินอายุอัตโนมัติ
            {updatedAt && ` · อัปเดต ${updatedAt}`}
          </p>
        </div>
        <div className="flex gap-2">
          <button onClick={load} disabled={loading}
            className="btn-secondary disabled:opacity-50">
            {loading ? "กำลังโหลด..." : "รีเฟรช"}
          </button>
          <button onClick={runTest} disabled={testing} className="btn-primary">
            {testing ? "กำลังทดสอบ..." : "ทดสอบดึงราคา"}
          </button>
        </div>
      </section>

      {err && <p className="text-loss text-sm">⚠️ {err}</p>}

      {/* ---------- Test result ---------- */}
      {testResult && (
        <section className={`panel ${testResult.verdict === "ok" ? "border-profit" : "border-loss"}`}>
          <p className="text-sm font-bold mb-2">
            {testResult.verdict === "ok" ? "✅ ทดสอบสำเร็จ" : "❌ ทดสอบล้มเหลว"}
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

      {/* ---------- Provider breakdown ---------- */}
      {summary && Object.keys(summary.by_provider).length > 0 && (
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
      <section className="panel overflow-x-auto">
        <div className="flex gap-2 mb-3 text-xs">
          {(["all", "forex", "gold"] as const).map((f) => (
            <button
              key={f}
              onClick={() => { setFilter(f); setPage(1); }}
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
            {shown.length === 0 && (
              <tr><td colSpan={11} className="py-6 text-center text-slate-500">
                ยังไม่มี log — กด &quot;⚡ ทดสอบดึงราคา&quot; เพื่อสร้างรายการแรก
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
                <td className="py-2 max-w-[200px] truncate text-loss" title={l.error ?? ""}>{l.error || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {logs.length > 0 && (
          <div className="flex flex-wrap items-center justify-between gap-2 mt-3">
            <p className="text-xs text-slate-500">
              หน้า {safePage}/{totalPages} · แสดง {pageRows.length} จาก {shown.length} รายการ
              {filter !== "all" && ` (กรองจาก ${logs.length})`} · เก็บสูงสุด {ttlDays} วัน
            </p>
            {totalPages > 1 && (
              <div className="flex items-center gap-2">
                <button onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={safePage <= 1}
                  className="border border-slate-700 rounded px-3 py-1 text-xs text-slate-300 disabled:opacity-40">
                  ก่อนหน้า
                </button>
                <span className="text-xs text-slate-400">{safePage} / {totalPages}</span>
                <button onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={safePage >= totalPages}
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
