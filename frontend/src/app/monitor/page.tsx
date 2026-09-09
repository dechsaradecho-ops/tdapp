"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import CloseGroupModal, { CloseGroupMode } from "@/components/CloseGroupModal";
import ClosePositionModal from "@/components/ClosePositionModal";
import CopyNum from "@/components/CopyNum";
import CalcNotes from "@/components/CalcNotes";
import FeedStatusBanner from "@/components/FeedStatusBanner";
import GlassSelect from "@/components/GlassSelect";
import Icon from "@/components/Icon";
import PerformancePanel from "@/components/PerformancePanel";
import RiskPanel from "@/components/RiskPanel";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { ClosePositionResult, MonitorSnapshot, SignalLog } from "@/lib/types";

// ความถี่รีเฟรชเลือกได้จาก UI — จำค่าใน DB (trading_settings.monitor_refresh_sec)
// ตามทุกเครื่อง ไม่ใช่แค่เบราว์เซอร์นี้ (backend cache spot quotes 30s ดังนั้น
// ยิงถี่กว่า 10s ก็ไม่เพิ่มโหลด feed)
const REFRESH_OPTIONS = [
  { label: "ปิด", value: 0 },
  { label: "10 วิ", value: 10 },
  { label: "30 วิ", value: 30 },
  { label: "1 นาที", value: 60 },
  { label: "5 นาที", value: 300 },
];

/** Badge "ระดับถูกขยับ" ข้างค่า SL/TP ในตารางไม้เปิด — hover หรือแตะเพื่อดู
 *  รายละเอียด: ค่าเริ่มต้น → ค่าปัจจุบัน, เวลาที่ขยับ และเหตุผล
 *  (breakeven = ทุนคืน, trailing = trailing stop, manual = ปรับด้วยมือ).
 *  PC: hover แสดง native title + คลิกเปิด popover ได้ / มือถือ: แตะเปิด popover —
 *  popover แบบ glass ใช้ position: fixed ตามตำแหน่งป้าย ใช้งานเหมือนกันทุกอุปกรณ์. */
function LevelMovedBadge({ moved, initial, current, movedAt, reason, level }: {
  moved: boolean;
  initial: number | null;
  current: number | null;
  movedAt: string | null;
  reason: string;
  level: "SL" | "TP";
}) {
  const [pop, setPop] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const btn = btnRef.current, popEl = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 260;
    const ph = popEl?.offsetHeight ?? 100;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.top - ph - 8;                 // เหนือป้ายเป็นค่าเริ่มต้น
    if (top < 8) top = r.bottom + 8;          // พื้นที่บนไม่พอ → แสดงใต้ป้ายแทน
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    // touchstart จับก่อน click เพื่อไม่ให้การแตะนอกลูกบิดปิด-เปิดซ้ำ
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onScrollOrResize = () => setPop(false);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [pop, place]);

  if (!moved) return null;
  const reasonLabel =
    reason === "breakeven" ? "ทุนคืน (Breakeven)"
    : reason === "trailing" ? "Trailing Stop"
    : reason.startsWith("manual") ? "ปรับด้วยมือ"
    : reason || "-";
  const when = movedAt
    ? new Date(movedAt).toLocaleString("th-TH",
        { dateStyle: "short", timeStyle: "short" })
    : "-";
  const title = `${level} ถูกขยับ: ${initial != null ? fmtNum(initial, 5) : "-"} → ${current != null ? fmtNum(current, 5) : "-"}\nเมื่อ: ${when}\nเหตุผล: ${reasonLabel}`;
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        title={title}
        aria-label={title}
        aria-expanded={pop}
        className="ml-1 inline-flex cursor-help align-middle p-1 -m-1 touch-manipulation"
      >
        <Icon n="arrowsH" size={12} className="text-accent" />
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="tooltip"
          style={{ position: "fixed", top: pos.top, left: pos.left, maxWidth: "min(280px, calc(100vw - 16px))" }}
          className="z-50 rounded-lg border border-slate-700 bg-slate-900/95 backdrop-blur px-3 py-2 shadow-xl text-xs leading-relaxed whitespace-pre-line text-slate-200"
        >
          {title}
        </div>,
        document.body
      )}
    </>
  );
}

/** Badge "Smart Exit" — คะแนนคุณภาพการถือไม้ (0-100) + คำแนะนำ
 *  แตะ/คลิกเพื่อดู 9 ปัจจัย + เหตุผลภาษาไทย (portal to body เหมือน LevelMovedBadge). */
function SmartExitBadge({ info }: { info: NonNullable<MonitorSnapshot["open_positions"][number]["exit_info"]> }) {
  const [pop, setPop] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const btn = btnRef.current, popEl = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 300;
    const ph = popEl?.offsetHeight ?? 200;
    let left = r.left + r.width / 2 - pw / 2;
    left = Math.max(8, Math.min(left, window.innerWidth - pw - 8));
    let top = r.top - ph - 8;
    if (top < 8) top = r.bottom + 8;
    setPos({ top, left });
  }, []);

  useEffect(() => {
    if (!pop) return;
    place();
    const close = (e: MouseEvent | TouchEvent) => {
      const t = e.target as Node | null;
      if (t && (btnRef.current?.contains(t) || popRef.current?.contains(t))) return;
      setPop(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("touchstart", close, { passive: true });
    const onScrollOrResize = () => setPop(false);
    window.addEventListener("scroll", onScrollOrResize, true);
    window.addEventListener("resize", onScrollOrResize);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("touchstart", close);
      window.removeEventListener("scroll", onScrollOrResize, true);
      window.removeEventListener("resize", onScrollOrResize);
    };
  }, [pop, place]);

  const qColor =
    info.quality === "High" ? "text-profit border-profit/40 bg-profit/10"
    : info.quality === "Medium" ? "text-amber-400 border-amber-400/40 bg-amber-400/10"
    : "text-loss border-loss/40 bg-loss/10";
  const recLabel =
    info.final === "CONTINUE" ? "ถือต่อ"
    : info.final === "PROTECT" ? "กันกำไร"
    : info.final === "SCALE_OUT" ? "แบ่งปิด"
    : info.final === "CLOSE" ? "ปิด"
    : "ปิดด่วน";
  const f = info.factors;
  const factorRows: [string, number][] = [
    ["เทรนด์", f.trend_strength], ["โมเมนตัม", f.momentum],
    ["วอลุ่ม", f.volume_proxy], ["Regime", f.market_regime],
    ["ข่าว", f.news_risk], ["เวลาถือ", f.holding_time],
    ["ความผันผวน", f.volatility], ["โอกาสใหม่", f.opportunity_score],
    ["ความเสี่ยงรวม", f.risk_exposure],
  ];
  const title = `Smart Exit ${fmtNum(info.exit_score, 0)}/100 (${info.quality}) — ${recLabel}\nR: ${fmtNum(info.r_multiple, 2)} | อายุ ${fmtNum(info.position_age_days, 1)} วัน\n${info.reasoning.join("\n")}`;
  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        title={title}
        aria-label={title}
        aria-expanded={pop}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-bold cursor-help touch-manipulation ${qColor}`}
      >
        {fmtNum(info.exit_score, 0)} · {recLabel}
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="tooltip"
          style={{ position: "fixed", top: pos.top, left: pos.left, maxWidth: "min(320px, calc(100vw - 16px))" }}
          className="z-50 rounded-lg border border-slate-700 bg-slate-900/95 backdrop-blur px-3 py-2 shadow-xl text-xs leading-relaxed text-slate-200"
        >
          <div className="font-bold mb-1">Smart Exit {fmtNum(info.exit_score, 0)}/100 ({info.quality}) — {recLabel}</div>
          <div className="text-slate-400 mb-1">R {fmtNum(info.r_multiple, 2)} · อายุ {fmtNum(info.position_age_days, 1)} วัน · {info.trigger}</div>
          {factorRows.map(([label, v]) => (
            <div key={label} className="flex items-center gap-2">
              <span className="w-20 shrink-0 text-slate-400">{label}</span>
              <div className="flex-1 h-1.5 rounded bg-slate-700 overflow-hidden">
                <div className="h-full rounded bg-accent" style={{ width: `${Math.max(0, Math.min(100, v))}%` }} />
              </div>
              <span className="w-8 text-right tabular-nums">{fmtNum(v, 0)}</span>
            </div>
          ))}
          <div className="mt-1 whitespace-pre-line text-slate-300">{info.reasoning.join("\n")}</div>
        </div>,
        document.body
      )}
    </>
  );
}

export default function MonitorPage() {
  const [snap, setSnap] = useState<MonitorSnapshot | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [updatedAt, setUpdatedAt] = useState<string>("");
  // ไทม์ไลน์ SL/TP ต่อไม้: ticket → events จาก signal-logs (order_opened
  // "SL ย้ายไป..." + closed) — โหลดพร้อม snapshot ครั้งเดียว
  const [moveLogs, setMoveLogs] = useState<Record<string, SignalLog[]>>({});
  const [openTimeline, setOpenTimeline] = useState<Record<string, boolean>>({});
  const [openCalc, setOpenCalc] = useState<Record<string, boolean>>({});
  // ค่าเริ่มต้น 10 วิ — เดี๋ยว sync จาก settings (DB) หลังโหลดครั้งแรก
  const [intervalSec, setIntervalSec] = useState<number>(10);
  const [closeResult, setCloseResult] = useState<ClosePositionResult | null>(null);
  const [closeError, setCloseError] = useState("");
  const [closingTicket, setClosingTicket] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetMsg, setResetMsg] = useState("");
  const [closeAllMsg, setCloseAllMsg] = useState("");
  // Risk Engine Status — ค่าจริงจาก backend (snap.risk คำนวณพร้อม monitor
  // ด้วย inputs เดียวกับ worker — แทนการยิง /risk/check ด้วยค่าปลอมเดิม
  // ที่ทำให้การ์ดโชว์ low ทั้งที่ระบบ pause อยู่)
  const risk = snap?.risk ?? null;
  const riskErr = !snap ? "" : (!snap.risk ? "ยังไม่มีข้อมูลความเสี่ยง" : "");
  // แท็บย่อย: มอนิเตอร์ | Performance (รวมหน้า /performance เดิม — รอบ 2)
  // static export ไม่มี server — อ่าน ?tab=performance จาก window.location.search ใน effect
  const [tab, setTab] = useState<"monitor" | "performance">("monitor");
  useEffect(() => {
    if (new URLSearchParams(window.location.search).get("tab") === "performance") setTab("performance");
  }, []);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const s = await api.monitor();
      setSnap(s);
      setErr("");
      setUpdatedAt(new Date().toLocaleTimeString("th-TH"));
      // ไทม์ไลน์ SL/TP: ดึง signal-logs แล้วจัดกลุ่มตาม ticket — เหตุการณ์
      // order_opened ที่มี reason "SL ย้ายไป..." คือทุกครั้งที่ guard ขยับ
      // SL/TP (backend log ไว้ทุก move) — ไม่ต้องเพิ่ม endpoint ใหม่
      try {
        const logs = await api.signalLogs(200);
        const byTicket: Record<string, SignalLog[]> = {};
        for (const l of logs.logs ?? []) {
          if (!l.ticket) continue;
          const moveLike =
            l.event === "closed" ||
            (l.event === "order_opened" &&
              (/SL ย้าย|TP ย้าย|breakeven|trailing/i.test(l.reason || "") ||
                (l.stop_loss != null && l.stop_loss > 0)));
          if (!moveLike) continue;
          (byTicket[l.ticket] ||= []).push(l);
        }
        for (const k of Object.keys(byTicket)) {
          byTicket[k].sort((a, b) =>
            String(a.created_at || "") < String(b.created_at || "") ? -1 : 1);
        }
        setMoveLogs(byTicket);
      } catch {
        /* ไทม์ไลน์ล้มเหลว — badge เดิมยังทำงานจาก journal row */
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    if (intervalSec <= 0) return; // ปิดรีเฟรชอัตโนมัติ
    const t = setInterval(load, intervalSec * 1000);
    return () => clearInterval(t);
  }, [load, intervalSec]);

  // sync ค่า interval จาก settings (DB) ครั้งแรกที่โหลดหน้า
  useEffect(() => {
    api.getSettings()
      .then((s) => {
        const saved = Number(s.monitor_refresh_sec);
        if (REFRESH_OPTIONS.some((o) => o.value === saved)) setIntervalSec(saved);
      })
      .catch(() => { /* settings ล้มเหลว — ใช้ค่าเริ่มต้น */ });
  }, []);

  const changeInterval = async (v: number) => {
    setIntervalSec(v);
    try {
      await api.saveSettings({ monitor_refresh_sec: v }); // จำลง DB — ตามทุกเครื่อง
    } catch {
      /* save ล้มเหลว — ค่ายังใช้ได้ในหน้านี้จนกว่าจะปิด */
    }
  };

  const togglePause = async () => {
    setBusy(true);
    try {
      await api.setTradingPause(!(snap?.pause.paused ?? false), "");
      await load();
    } finally {
      setBusy(false);
    }
  };

  // ปิดไม้ด้วยมือ → เด้ง popup สรุปกำไร/ขาดทุน
  const handleClosePosition = async (ticket: string) => {
    if (!ticket) return;
    setClosingTicket(ticket);
    setCloseError("");
    try {
      const res = await api.closePosition(ticket);
      if (res.ok) {
        setCloseResult(res);
        await load();
      } else {
        setCloseError(res.message);
      }
    } catch (e) {
      setCloseError(e instanceof Error ? e.message : String(e));
    } finally {
      setClosingTicket(null);
    }
  };

  // รีเซ็ตสถิติ — ลบไม้ที่ปิดแล้วทั้งหมด (ไม้เปิดค้างไม่ถูกแตะ) → PnL/Win Rate กลับเป็น 0
  const handleResetStats = async () => {
    if (resetting) return;
    const ok = window.confirm(
      "รีเซ็ตสถิติการเทรด?\n\n" +
      "• ลบไม้ที่ปิดแล้วทั้งหมด (PnL วันนี้ / 7 วัน / รวม, Win Rate กลับเป็น 0)\n" +
      "• ไม้ที่เปิดค้างจะไม่ถูกลบ — SL/TP ยังทำงานตามปกติ\n" +
      "• ทำแล้วย้อนกลับไม่ได้");
    if (!ok) return;
    setResetting(true);
    setResetMsg("");
    try {
      const res = await api.resetStats();
      setResetMsg(res.ok ? res.message : `รีเซ็ตไม่สำเร็จ: ${res.message}`);
      await load();
    } catch (e) {
      setResetMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  };

  // ปิดไม้เป็นกลุ่ม (ทั้งหมด/กำไร/ขาดทุน) — เปิด popup สรุปก่อนเสมอ
  // (ผู้ใช้ขอ 2026-09-07: ปุ่ม 3 ปุ่ม + popup สรุปก่อนยิง แทน window.confirm)
  const [groupMode, setGroupMode] = useState<CloseGroupMode | null>(null);
  const [groupBusy, setGroupBusy] = useState(false);
  const [groupError, setGroupError] = useState("");

  const openGroupModal = (mode: CloseGroupMode) => {
    const openCount = snap?.stats.open_positions ?? 0;
    if (openCount === 0) {
      setCloseAllMsg("ไม่มีไม้ที่เปิดค้างอยู่");
      return;
    }
    setGroupError("");
    setGroupMode(mode);
  };

  const confirmCloseGroup = async () => {
    if (!groupMode || groupBusy) return;
    setGroupBusy(true);
    setGroupError("");
    try {
      const res = groupMode === "all"
        ? await api.closeAll()
        : await api.closeGroup(groupMode);
      setGroupMode(null);
      setCloseAllMsg(res.message ||
        (res.ok ? `ปิดแล้ว ${res.closed} ไม้` : "ปิดไม่สำเร็จ"));
      await load();
    } catch (e) {
      setGroupError(e instanceof Error ? e.message : String(e));
    } finally {
      setGroupBusy(false);
    }
  };

  const st = snap?.stats;
  // ยอดรวม PnL ทั้งหมด = realized (ไม้ที่ปิดแล้ว) + unrealized (ไม้ที่เปิดค้าง)
  const unrealizedTotal = snap
    ? snap.open_positions.reduce((sum, p) => sum + p.unrealized_pnl, 0)
    : 0;
  const totalPnl = (st?.pnl_total ?? 0) + unrealizedTotal;

  return (
    <div className="space-y-4">
      {/* ---------- แท็บ: มอนิเตอร์ | Performance ---------- */}
      <div className="flex gap-2">
        <button
          onClick={() => setTab("monitor")}
          className={`px-4 py-2 min-h-[40px] rounded-xl text-sm border font-semibold ${tab === "monitor" ? "border-accent text-accent bg-accent/10" : "border-white/15 bg-white/[0.04] text-slate-400 active:bg-white/10"}`}
        >
          <span className="inline-flex items-center gap-1.5"><Icon n="chart" size={15} /> มอนิเตอร์</span>
        </button>
        <button
          onClick={() => setTab("performance")}
          className={`px-4 py-2 min-h-[40px] rounded-xl text-sm border font-semibold ${tab === "performance" ? "border-accent text-accent bg-accent/10" : "border-white/15 bg-white/[0.04] text-slate-400 active:bg-white/10"}`}
        >
          <span className="inline-flex items-center gap-1.5"><Icon n="target" size={15} /> Performance</span>
        </button>
      </div>

      {tab === "performance" && <PerformancePanel />}

      {tab === "monitor" && (
      <>
      <FeedStatusBanner feed={snap?.feed_status} />

      {/* ---------- Status strip ---------- */}
      <section className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <div className={`panel ${snap?.pause.paused ? "border-loss" : ""}`}>
          <p className="text-xs text-slate-500">Execution Switch</p>
          {snap?.pause.paused ? (
            <>
              <p className="text-lg font-bold text-loss">PAUSED</p>
              <p className="text-xs text-slate-400 truncate">{snap.pause.reason || "manual"}</p>
            </>
          ) : (
            <p className="text-lg font-bold text-profit">Active</p>
          )}
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">โหมดเทรด</p>
          <p className="text-lg font-bold">
            {snap?.order_mode === "auto" ? "Auto"
              : snap?.order_mode === "semi_auto" ? "Semi-Auto" : "Manual"}
          </p>
        </div>
        <div className={`panel ${snap?.kill.engaged ? "border-loss" : ""}`}>
          <p className="text-xs text-slate-500">Kill Switch</p>
          {snap?.kill.engaged ? (
            <>
              <p className="text-lg font-bold text-loss">ENGAGED</p>
              <p className="text-xs text-slate-400 truncate">{snap.kill.message}</p>
            </>
          ) : (
            <p className="text-lg font-bold text-profit">Clear</p>
          )}
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">ไม้เปิดค้าง</p>
          <p className="text-lg font-bold">{st?.open_positions ?? "-"}</p>
        </div>
        <div className="panel">
          <p className="text-xs text-slate-500">Capital</p>
          <p className="text-lg font-bold">${fmtNum(snap?.capital ?? 0, 0)}</p>
        </div>
      </section>

      {/* ---------- Stats ---------- */}
      <section className="space-y-2">
        <h2 className="panel-title">สถิติการเทรด</h2>
        <section className="grid grid-cols-2 md:grid-cols-6 gap-3">
          <div className="panel">
            <p className="text-xs text-slate-500">เทรดวันนี้</p>
            <p className="text-xl font-bold">{st?.trades_today ?? "-"}</p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL วันนี้</p>
            <PnlText v={st?.pnl_today} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL 7 วัน</p>
            <PnlText v={st?.pnl_week} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL รวม (ปิดแล้ว)</p>
            <PnlText v={st?.pnl_total} />
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">PnL ไม้ค้าง (ยังไม่ปิด)</p>
            <PnlText v={snap ? unrealizedTotal : undefined} />
          </div>
          <div className={`panel ${snap ? (totalPnl >= 0 ? "border-profit/50" : "border-loss/50") : ""}`}>
            <p className="text-xs text-slate-500">ยอดรวม PnL สุทธิ</p>
            <PnlText v={snap ? totalPnl : undefined} />
            <p className="text-xs text-slate-500">
              ปิดแล้ว {st ? `${st.pnl_total >= 0 ? "+" : ""}$${fmtNum(st.pnl_total, 2)}` : "-"} + ค้าง {snap ? `${unrealizedTotal >= 0 ? "+" : ""}$${fmtNum(unrealizedTotal, 2)}` : "-"}
            </p>
          </div>
          <div className="panel">
            <p className="text-xs text-slate-500">Win Rate</p>
            <p className={`text-xl font-bold ${(st?.win_rate ?? 0) >= 50 ? "text-profit" : "text-loss"}`}>
              {st ? `${fmtNum(st.win_rate, 1)}%` : "-"}
            </p>
            <p className="text-xs text-slate-500">{st?.closed_count ?? 0} ไม้ที่ปิดแล้ว</p>
          </div>
        </section>
      </section>

      {/* ---------- Open positions ---------- */}
      <div className="panel">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <h2 className="panel-title">ไม้ที่เปิดค้าง (Paper)</h2>
          <div className="flex items-center gap-3">
            {updatedAt && <span className="text-xs text-slate-500">อัปเดต {updatedAt}</span>}
            <GlassSelect
              value={String(intervalSec)}
              onChange={(v) => changeInterval(Number(v))}
              className="text-xs"
              ariaLabel="ตั้งเวลารีเฟรชอัตโนมัติ"
              options={REFRESH_OPTIONS.map((o) => ({
                value: String(o.value), label: `รีเฟรช: ${o.label}`,
              }))}
            />
            <button onClick={togglePause} disabled={busy || !snap}
              className={snap?.pause.paused
                ? "bg-profit text-white font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50 active:brightness-90"
                : "bg-loss text-white font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50 active:brightness-90"}>
              {busy ? "..." : snap?.pause.paused ? "Resume" : "Pause"}
            </button>
            <button onClick={load} disabled={loading}
              className="border border-slate-700 rounded px-3 py-2 text-sm min-h-[40px] text-slate-300 active:bg-slate-800 disabled:opacity-50">
              {loading ? "กำลังโหลด..." : "รีเฟรช"}
            </button>
          </div>
        </div>
        {err && <p className="text-loss text-sm mt-2">โหลดไม่สำเร็จ: {err}</p>}
        {!snap && !err && (
          <p className="text-slate-400 text-sm mt-3 animate-pulse">
            กำลังโหลดข้อมูล — API บน Render free tier อาจใช้เวลาเริ่มต้น 30 วิ หาก service หลับ
          </p>
        )}
        {closeError && (
          <p className="text-loss text-sm mt-2 bg-loss/10 border border-loss/40 rounded px-3 py-2">
            ปิดไม้ไม่สำเร็จ: {closeError}
          </p>
        )}

        {snap && snap.open_positions.length === 0 && (
          <p className="text-slate-500 text-sm mt-3">ไม่มีไม้เปิดค้าง — auto trader จะยิงเมื่อเจอ signal ที่ผ่าน gate</p>
        )}
        {snap && snap.open_positions.length > 0 && (
          <div className="overflow-x-auto mt-3">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 uppercase tracking-wide">
                  <th className="py-2 pr-4">Asset</th>
                  <th className="py-2 pr-4">ฝั่ง</th>
                  <th className="py-2 pr-4">Lots</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">ราคาปัจจุบัน</th>
                  <th className="py-2 pr-4">SL</th>
                  <th className="py-2 pr-4">TP</th>
                  <th className="py-2 pr-4">PnL (ยังไม่ปิด)</th>
                  <th className="py-2 pr-4">R / เสี่ยง</th>
                  <th className="py-2 pr-4">Smart Exit</th>
                  <th className="py-2 pr-4">ที่มา</th>
                  <th className="py-2 pr-4">Ticket</th>
                  <th className="py-2">จัดการ</th>
                </tr>
              </thead>
              <tbody>
                {snap.open_positions.map((p) => {
                  const rMult = p.r_multiple ?? 0;
                  const riskUsd = p.risk_amount ?? 0;
                  const srcLabel =
                    p.price_source === "spot" ? "spot สด"
                    : p.price_source === "daily" ? "daily close"
                    : p.price_source === "broker" ? "broker"
                    : p.price_source === "entry" ? "entry (ไม่มี feed)"
                    : "—";
                  const timeline = p.ticket ? (moveLogs[p.ticket] ?? []) : [];
                  const tlOpen = openTimeline[p.id] ?? false;
                  const calcOpen = openCalc[p.id] ?? false;
                  const notes = p.calc_notes ?? [];
                  return (
                  <>
                  <tr key={p.id} className="border-t border-slate-800">
                    <td className="py-2 pr-4 font-semibold">{p.asset}</td>
                    <td className="py-2 pr-4 font-bold">
                      <span className={p.direction === "BUY" ? "text-profit" : "text-loss"}>
                        {p.direction === "BUY" ? "▲ BUY" : "▼ SELL"}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-bold">{fmtNum(p.volume, 2)}</td>
                    <td className="py-2 pr-4 font-bold"><CopyNum value={p.entry_price} /></td>
                    <td className="py-2 pr-4 font-bold">
                      {fmtNum(p.current_price, 5)}
                      <span className={`ml-1 inline-flex rounded-full px-1.5 py-0.5 text-[10px] font-semibold ${
                        p.price_source === "spot" ? "bg-profit/20 text-profit"
                        : p.price_source === "daily" ? "bg-amber-500/20 text-amber-400"
                        : p.price_source === "entry" ? "bg-loss/20 text-loss"
                        : "bg-slate-500/20 text-slate-400"}`}
                        title={p.price_source === "spot" ? "ราคาสดจาก spot feed (Yahoo intraday)"
                          : p.price_source === "daily" ? "ราคาปิดรายวัน (สำรองตอน spot ล่ม)"
                          : p.price_source === "broker" ? "ราคาจาก broker book (สำรอง)"
                          : p.price_source === "entry" ? "ไม่มี feed — ใช้ entry, PnL นิ่ง"
                          : "ที่มาราคาไม่ทราบ"}>
                        {srcLabel}
                      </span>
                    </td>
                    <td className="py-2 pr-4"><CopyNum value={p.stop_loss} className="font-bold text-loss" /><LevelMovedBadge moved={p.sl_moved_at != null || (p.initial_stop_loss != null && p.stop_loss != null && Math.abs(p.stop_loss - p.initial_stop_loss) > 1e-9)} initial={p.initial_stop_loss} current={p.stop_loss} movedAt={p.sl_moved_at} reason={p.sl_move_reason} level="SL" /></td>
                    <td className="py-2 pr-4"><CopyNum value={p.take_profit} className="font-bold text-profit" /><LevelMovedBadge moved={p.tp_moved_at != null || (p.initial_take_profit != null && p.take_profit != null && Math.abs(p.take_profit - p.initial_take_profit) > 1e-9)} initial={p.initial_take_profit} current={p.take_profit} movedAt={p.tp_moved_at} reason={p.tp_move_reason} level="TP" /></td>
                    <td className="py-2 pr-4 font-bold">
                      <span className={p.unrealized_pnl >= 0 ? "text-profit" : "text-loss"}>
                        {p.unrealized_pnl >= 0 ? "+" : ""}${fmtNum(p.unrealized_pnl, 2)}
                      </span>
                    </td>
                    <td className="py-2 pr-4">
                      <span className={`font-bold ${rMult >= 0 ? "text-profit" : "text-loss"}`}>
                        {rMult >= 0 ? "+" : ""}{fmtNum(rMult, 2)}R
                      </span>
                      <span className="block text-xs text-slate-500">
                        เสี่ยง ${fmtNum(riskUsd, 2)}
                      </span>
                    </td>
                    <td className="py-2 pr-4">{p.exit_info ? <SmartExitBadge info={p.exit_info} /> : <span className="text-slate-600 text-xs">-</span>}</td>
                    <td className="py-2 pr-4 text-xs">{p.source === "auto" ? "Auto" : "Approve"}</td>
                    <td className="py-2 text-xs text-slate-500">{p.ticket || "-"}</td>
                    <td className="py-2">
                      <button
                        onClick={() => handleClosePosition(p.ticket)}
                        disabled={!p.ticket || closingTicket === p.ticket}
                        className="bg-loss text-white font-semibold rounded px-2.5 py-1.5 text-xs min-h-[32px] disabled:opacity-50 active:brightness-90"
                      >
                        {closingTicket === p.ticket ? "..." : "ปิด"}
                      </button>
                    </td>
                  </tr>
                  <tr key={`${p.id}-detail`} className="border-t border-slate-800/50">
                    <td colSpan={13} className="py-1 pr-4">
                      <div className="flex flex-wrap items-center gap-2 text-xs">
                        {notes.length > 0 && (
                          <button
                            onClick={() => setOpenCalc((v) => ({ ...v, [p.id]: !v[p.id] }))}
                            className="rounded-full border border-accent/40 bg-accent/10 px-2 py-0.5 font-semibold text-accent"
                            aria-expanded={calcOpen}
                          >
                            วิธีคำนวณ ({notes.length}) {calcOpen ? "▾" : "▸"}
                          </button>
                        )}
                        {timeline.length > 0 && (
                          <button
                            onClick={() => setOpenTimeline((v) => ({ ...v, [p.id]: !v[p.id] }))}
                            className="rounded-full border border-white/15 bg-white/[0.04] px-2 py-0.5 font-semibold text-slate-300"
                            aria-expanded={tlOpen}
                          >
                            ไทม์ไลน์ SL/TP ({timeline.length}) {tlOpen ? "▾" : "▸"}
                          </button>
                        )}
                        {timeline.length === 0 && (p.sl_moved_at != null || p.tp_moved_at != null) && (
                          <span className="text-slate-500">
                            SL/TP ถูกขยับ — ดูรายละเอียดที่ป้าย ↔ ข้างค่า SL/TP
                          </span>
                        )}
                      </div>
                      {calcOpen && notes.length > 0 && (
                        <div className="mt-1 max-w-2xl">
                          <CalcNotes notes={notes} defaultOpen />
                        </div>
                      )}
                      {tlOpen && timeline.length > 0 && (
                        <ol className="mt-1 max-w-2xl space-y-1 border-l-2 border-accent/40 pl-3">
                          {timeline.map((l) => (
                            <li key={l.id} className="text-xs text-slate-300">
                              <span className="text-slate-500">
                                {l.created_at ? new Date(l.created_at).toLocaleString("th-TH", { dateStyle: "short", timeStyle: "short" }) : "-"}
                              </span>
                              {" · "}
                              <span className="font-semibold">
                                {l.event === "closed" ? "ปิดไม้" : "SL/TP ขยับ"}
                              </span>
                              {l.stop_loss != null && l.stop_loss > 0 && (
                                <> — SL {fmtNum(l.stop_loss, 5)}</>
                              )}
                              {l.reason && <span className="text-slate-400"> — {l.reason}</span>}
                            </li>
                          ))}
                        </ol>
                      )}
                    </td>
                  </tr>
                  </>
                  );
                })}
                <tr className="border-t-2 border-slate-700 font-bold">
                  <td className="py-2 pr-4" colSpan={7}>รวม uPnL ({snap.open_positions.length} ไม้)</td>
                  <td className="py-2 pr-4">
                    <span className={unrealizedTotal >= 0 ? "text-profit" : "text-loss"}>
                      {unrealizedTotal >= 0 ? "+" : ""}${fmtNum(unrealizedTotal, 2)}
                    </span>
                  </td>
                  <td colSpan={5} />
                </tr>
              </tbody>
            </table>
          </div>
        )}

        {/* ---------- ปุ่มจัดการกลุ่ม — ย้ายมาไว้ด้านล่างตาราง (ใช้งานสะดวกบนมือถือ) ---------- */}
        <div className="flex items-center flex-wrap gap-2 mt-4">
          <button onClick={() => openGroupModal("all")} disabled={groupBusy}
            className="bg-loss text-white font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50"
            title="ปิดไม้ที่เปิดค้างทุกไม้ที่ราคาปัจจุบัน">
            ปิดทั้งหมด
          </button>
          <button onClick={() => openGroupModal("profit")} disabled={groupBusy}
            className="bg-profit text-white font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50"
            title="ปิดเฉพาะไม้ที่กำไร (ที่ราคาปัจจุบัน)">
            ปิดกำไร
          </button>
          <button onClick={() => openGroupModal("loss")} disabled={groupBusy}
            className="border border-loss text-loss font-semibold rounded px-3 py-2 text-sm min-h-[40px] disabled:opacity-50 active:bg-loss/10"
            title="ปิดเฉพาะไม้ที่ขาดทุน (cut loss ทั้งกลุ่ม)">
            ปิดขาดทุน
          </button>
          <button onClick={handleResetStats} disabled={resetting}
            className="border border-slate-700 rounded px-3 py-2 text-sm min-h-[40px] text-slate-300 active:bg-slate-800 disabled:opacity-50"
            title="ลบไม้ที่ปิดแล้วทั้งหมด — ไม้ที่เปิดค้างไม่ถูกลบ">
            {resetting ? "กำลังรีเซ็ต..." : "รีเซ็ตสถิติ"}
          </button>
        </div>
        {closeAllMsg && (
          <p className="text-xs text-slate-400 bg-white/[0.05] border border-white/10 rounded-xl px-3 py-2 mt-2">
            {closeAllMsg}
          </p>
        )}
        {resetMsg && (
          <p className="text-xs text-slate-400 bg-white/[0.05] border border-white/10 rounded-xl px-3 py-2 mt-2">
            {resetMsg}
          </p>
        )}
      </div>

      {/* ---------- Recent executions ---------- */}
      <div className="panel">
        <h2 className="panel-title">ประวัติการยิง order ล่าสุด</h2>
        {snap && snap.recent.length === 0 && (
          <p className="text-slate-500 text-sm mt-3">ยังไม่มีประวัติ — รอ signal แรกผ่าน gate</p>
        )}
        {snap && snap.recent.length > 0 && (
          <div className="overflow-x-auto mt-3">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-slate-500 uppercase tracking-wide">
                  <th className="py-2 pr-4">เวลา</th>
                  <th className="py-2 pr-4">Asset</th>
                  <th className="py-2 pr-4">ฝั่ง</th>
                  <th className="py-2 pr-4">Lots</th>
                  <th className="py-2 pr-4">Entry</th>
                  <th className="py-2 pr-4">Exit</th>
                  <th className="py-2 pr-4">PnL</th>
                  <th className="py-2 pr-4">สถานะ</th>
                  <th className="py-2 pr-4">เหตุผลปิด</th>
                  <th className="py-2">ที่มา</th>
                </tr>
              </thead>
              <tbody>
                {snap.recent.map((t) => (
                  <tr key={t.id} className="border-t border-slate-800">
                    <td className="py-2 pr-4 text-xs text-slate-400">
                      {t.created_at ? new Date(t.created_at).toLocaleString("th-TH") : "-"}
                    </td>
                    <td className="py-2 pr-4 font-semibold">{t.asset}</td>
                    <td className="py-2 pr-4 font-bold">
                      <span className={t.direction === "BUY" ? "text-profit" : "text-loss"}>
                        {t.direction === "BUY" ? "▲" : "▼"} {t.direction}
                      </span>
                    </td>
                    <td className="py-2 pr-4 font-bold">{fmtNum(t.volume, 2)}</td>
                    <td className="py-2 pr-4 font-bold">{fmtNum(t.entry_price, 5)}</td>
                    <td className="py-2 pr-4 font-bold">{t.exit_price != null ? fmtNum(t.exit_price, 5) : "-"}</td>
                    <td className="py-2 pr-4 font-bold">
                      {t.pnl != null ? (
                        <span className={`font-bold ${t.pnl >= 0 ? "text-profit" : "text-loss"}`}>
                          {t.pnl >= 0 ? "+" : ""}${fmtNum(t.pnl, 2)}
                        </span>
                      ) : "-"}
                    </td>
                    <td className="py-2 pr-4">
                      <StatusBadge status={t.status} />
                    </td>
                    <td className="py-2 pr-4 text-xs">
                      {t.close_reason === "sl" ? "ตัดขาดทุน (SL)"
                        : t.close_reason === "tp" ? "ถึงเป้า (TP)"
                        : t.close_reason === "manual" ? "ปิดเอง"
                        : t.close_reason === "time" ? "หมดเวลา (Time Stop)"
                        : "-"}
                    </td>
                    <td className="py-2 text-xs">{t.source === "auto" ? "Auto" : "Approve"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ---------- Risk Engine Status (จากหน้า /risk เดิม — ย้ายมาด้านล่างตาม request) ---------- */}
      <div className="panel">
        <h2 className="panel-title">Risk Engine Status</h2>
        {riskErr && <p className="text-loss text-sm">{riskErr}</p>}
        <RiskPanel risk={risk} />
        <p className="text-xs text-slate-500 mt-3">
          ลิมิต: ขาทุนรายวัน/สัปดาห์/เดือน + Max Drawdown — ตั้งค่าได้ที่หน้าตั้งค่า (Kill Switch &amp; Risk)
        </p>
      </div>

      {/* ---------- Popup สรุปผลการปิดไม้ ---------- */}
      <ClosePositionModal result={closeResult} onClose={() => setCloseResult(null)} />
      {/* ---------- Popup สรุปก่อนปิดเป็นกลุ่ม (ทั้งหมด/กำไร/ขาดทุน) ---------- */}
      <CloseGroupModal
        mode={groupMode}
        positions={snap?.open_positions ?? []}
        busy={groupBusy}
        errorMsg={groupError}
        onConfirm={confirmCloseGroup}
        onClose={() => { if (!groupBusy) setGroupMode(null); }}
      />
      </>
      )}
    </div>
  );
}

function PnlText({ v }: { v?: number }) {
  if (v == null) return <p className="text-xl font-bold">-</p>;
  return (
    <p className={`text-xl font-bold ${v >= 0 ? "text-profit" : "text-loss"}`}>
      {v >= 0 ? "+" : ""}${fmtNum(v, 2)}
    </p>
  );
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    open: { label: "เปิดอยู่", cls: "text-accent" },
    closed: { label: "ปิดแล้ว", cls: "text-slate-400" },
    rejected: { label: "ถูกบล็อก", cls: "text-loss" },
  };
  const it = map[status] ?? { label: status, cls: "text-slate-400" };
  return <span className={`text-xs font-semibold ${it.cls}`}>{it.label}</span>;
}
