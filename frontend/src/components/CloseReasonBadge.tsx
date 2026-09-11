"use client";

/**
 * เหตุผลที่ไม้ถูกปิด — ป้ายในตาราง "ประวัติการยิง order ล่าสุด"
 *
 * เดิมคอลัมน์นี้แสดง "-" สำหรับทุกไม้ที่ Smart Exit ปิด (`smart_exit:*`)
 * ทั้งที่ไม้พวกนั้นคือไม้ที่ต้องอธิบายมากที่สุด ทำไมบอทถึงทิ้งมัน
 *
 * ตัวป้าย = <button> ที่กดแล้วเปิด popup อธิบาย (portal ไป document.body
 * เพราะ .panel มี backdrop-filter → position:fixed จะถูกกักบริเวณ)
 * ดู /memories/repo/tap-popover-pattern.md
 *
 * ตัวเลขใน popup มาจาก snapshot.exit_rules (ค่าที่ worker ใช้จริงรอบล่าสุด)
 * ไม่ได้เก็บข้อความ prose ต่อไม้ใน DB — จึงไม่มี "การกู้ประวัติ" ของไม้เก่า
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { fmtNum } from "@/lib/format";
import { MonitorExitRules, MonitorTrade } from "@/lib/types";

/** รหัส → ป้ายไทยสั้น ๆ (ใช้ทั้งในตารางและหัว popup). */
export function closeReasonLabel(reason: string | null | undefined): string {
  const r = String(reason || "");
  switch (r) {
    case "sl": return "ตัดขาดทุน (SL)";
    case "tp": return "ถึงเป้า (TP)";
    case "manual": return "ปิดเอง";
    case "time": return "หมดเวลา (Time Stop)";
    case "emergency": return "ปิดฉุกเฉิน";
    case "close_all": return "ปิดทั้งหมด";
    case "close_group": return "ปิดยกกลุ่ม";
    case "": return "—";
  }
  if (r.startsWith("smart_exit")) {
    const trig = r.includes(":") ? r.split(":")[1] : "";
    switch (trig) {
      case "left_behind": return "ปล่อยไม้ค้างทุน";
      case "exit_score": return "คะแนนต่ำ";
      case "reversal": return "สัญญาณกลับตัว";
      case "news": return "เลี่ยงข่าว";
      case "volatility": return "ผันผวนผิดปกติ";
      case "profit_protect": return "กันกำไร";
      case "time_stop": return "หมดเวลา";
      default: return "Smart Exit";
    }
  }
  return r;
}

/** สีป้าย — กำไร/ขาดทุน/กลาง. */
function toneOf(reason: string, pnl: number | null): string {
  const r = String(reason || "");
  if (r === "tp" || r === "smart_exit:profit_protect") {
    return "text-profit border-profit/40 bg-profit/10";
  }
  if (r === "sl" || r === "smart_exit:left_behind" || r === "time") {
    return "text-amber-400 border-amber-400/40 bg-amber-400/10";
  }
  if (r === "emergency") {
    return "text-loss border-loss/40 bg-loss/10";
  }
  if (pnl !== null && pnl < 0) return "text-loss border-loss/40 bg-loss/10";
  return "text-slate-300 border-slate-600 bg-slate-700/40";
}

/** ประโยคอธิบายว่าทำไมกฎถึงยิง — คืน [] เมื่อไม่มีอะไรจะอธิบาย. */
function explain(t: MonitorTrade, rules?: MonitorExitRules | null): string[] {
  const reason = String(t.close_reason || "");
  const held = t.holding_days ?? null;
  const heldTxt = held === null ? null : `${fmtNum(held, 1)} วัน`;

  // R ตอนปิด — วัดกับ SL เริ่มต้น (SL ที่ถูกเลื่อนจะทำให้ R เพี้ยน)
  const sl = t.initial_stop_loss ?? t.stop_loss ?? null;
  let rTxt: string | null = null;
  if (sl !== null && t.exit_price !== null && t.entry_price) {
    const risk = Math.abs(t.entry_price - sl);
    if (risk > 0) {
      const sign = t.direction === "BUY" ? 1 : -1;
      rTxt = `${fmtNum(((t.exit_price - t.entry_price) * sign) / risk, 2)}R`;
    }
  }

  const out: string[] = [];

  if (reason === "smart_exit:left_behind") {
    const minR = rules?.no_behind_min_r ?? 0;
    const mult = rules?.no_behind_hold_mult ?? 0;
    const avg = rules?.avg_hold_days ?? 0;
    const thr = rules?.left_behind_days ?? 0;
    out.push(
      "กฎ \"ไม่ทิ้งไม้ค้างทุน\": ไม้ที่กำไรน้อยและถือมานานเกินเกณฑ์ จะถูกปล่อย" +
      "เพื่อคืนวงเงินไปหาโอกาสใหม่ (Capital Efficiency)"
    );
    if (rTxt) out.push(`กำไรตอนปิด ${rTxt} — ต่ำกว่าเกณฑ์ ${fmtNum(minR, 1)}R`);
    if (heldTxt && thr > 0) {
      // เกณฑ์ที่แสดงคือค่าปัจจุบัน ไม่ใช่ค่าที่ใช้ตอนปิด (ไม่ได้เก็บไว้ต่อไม้)
      // ถ้าอายุไม้ "ไม่ถึง" เกณฑ์แสดงว่า เทสต์ผ่านเพราะเกณฑ์ถูกเปลี่ยนทีหลัง
      const over = held !== null && held >= thr;
      out.push(
        over
          ? `ถือมา ${heldTxt} — เกินเกณฑ์ ${fmtNum(thr, 1)} วัน`
          : `ถือมา ${heldTxt} · เกณฑ์ปัจจุบัน ${fmtNum(thr, 1)} วัน` +
            ` (ไม้เก่านี้ปิดตอนที่เกณฑ์ยังเป็นค่าอื่น — ไม่ได้เก็บเกณฑ์ของรอบนั้นไว้)`
      );
      if (avg > 0 && mult > 0) {
        out.push(
          `เกณฑ์ = ${fmtNum(mult, 2)} × ค่าเฉลี่ยเวลาถือ (${fmtNum(avg, 1)} วัน)` +
          (rules?.no_behind_min_days
            ? ` โดยมีพื้นขั้นต่ำ ${fmtNum(rules.no_behind_min_days, 1)} วัน`
            : "")
        );
      }
    }
    const maxHold = rules?.max_hold_days ?? 0;
    if (maxHold > 0) {
      out.push(`เกณฑ์นี้ถูกบีบไม่ให้เกิน max hold ${maxHold} วัน — จึงยิงก่อน time stop เสมอ`);
    }
    return out;
  }

  if (reason === "smart_exit:exit_score") {
    out.push(
      `คะแนนคุณภาพการถือต่ำกว่า ${fmtNum(rules?.exit_score_close ?? 0, 0)}` +
      `${rTxt ? ` และกำไร ${rTxt} < 1R` : ""} → ปิดทั้งไม้ (ถ้ากำไร ≥ 1R จะแบ่งปิด 50% แทน)`
    );
    if (heldTxt) out.push(`ถือมา ${heldTxt}`);
    return out;
  }

  if (reason === "smart_exit:reversal") {
    out.push("อินดิเคเตอร์กลับขั้วตั้งแต่ 2 สัญญาณขึ้นไป — ปิดก่อนขาดทุนเพิ่ม");
    if (heldTxt) out.push(`ถือมา ${heldTxt}`);
    return out;
  }

  if (reason === "smart_exit:news") {
    out.push("มีข่าวผลกระทบสูงใกล้ประกาศ และไม้กำไรอยู่ — ปิดก่อนความผันผวนกระโดด");
    if (heldTxt) out.push(`ถือมา ${heldTxt}`);
    return out;
  }

  if (reason === "smart_exit:volatility") {
    out.push("ATR กระโดดสูงผิดปกติ — แบ่งปิดลดความเสี่ยง");
    if (heldTxt) out.push(`ถือมา ${heldTxt}`);
    return out;
  }

  if (reason === "smart_exit:profit_protect") {
    out.push("กำไรถึงเป้าที่ตั้งไว้แต่คุณภาพแนวโน้มไม่ดี — แบ่งปิดล็อกกำไร");
    if (heldTxt) out.push(`ถือมา ${heldTxt}`);
    return out;
  }

  if (reason === "time") {
    const maxHold = rules?.max_hold_days ?? 0;
    const minR = rules?.time_stop_min_r ?? 0;
    if (heldTxt) {
      const over = maxHold > 0 && held !== null && held >= maxHold;
      out.push(
        over
          ? `ถือมา ${heldTxt} เกินเพดาน ${maxHold} วัน`
          : `ถือมา ${heldTxt}` +
            (maxHold > 0
              ? ` · เพดานปัจจุบัน ${maxHold} วัน (ไม้เก่านี้ปิดตอนที่ค่าตั้งยังต่างจากนี้)`
              : "")
      );
    }
    if (rTxt && minR > 0) {
      out.push(
        `กำไรตอนปิด ${rTxt} — ต่ำกว่าเกณฑ์ยกเว้น ${fmtNum(minR, 1)}R ` +
        `(ถ้ากำไร ≥ ${fmtNum(minR, 1)}R จะไม่ถูกปิดเพราะอายุ)`
      );
    }
    return out;
  }

  if (reason === "sl") {
    out.push("ราคาชน stop loss — ขาดทุนตามแผน");
    return out;
  }
  if (reason === "tp") {
    out.push("ราคาถึง take profit — ปิดตามแผน");
    return out;
  }
  if (reason === "emergency") {
    out.push("kill switch ทำงาน (ขาดทุนถึงเพดาน) — ปิดทุกไม้พร้อมกัน");
    return out;
  }
  if (reason === "manual" || reason === "close_all" || reason === "close_group") {
    out.push("ปิดด้วยมือจากหน้า Monitor");
    return out;
  }
  if (!reason) {
    out.push("ไม้ยังไม่ถูกปิด — ยังไม่มีเหตุผลปิด");
    return out;
  }
  return out;
}

export default function CloseReasonBadge({
  trade, rules,
}: {
  trade: MonitorTrade;
  rules?: MonitorExitRules | null;
}) {
  const [pop, setPop] = useState(false);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const popRef = useRef<HTMLDivElement | null>(null);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const place = useCallback(() => {
    const btn = btnRef.current, popEl = popRef.current;
    if (!btn) return;
    const r = btn.getBoundingClientRect();
    const pw = popEl?.offsetWidth ?? 320;
    const ph = popEl?.offsetHeight ?? 220;
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

  const closed = trade.status === "closed";
  const reason = String(trade.close_reason || "");
  const label = closed ? closeReasonLabel(trade.close_reason) : "ยังเปิดอยู่";
  const lines = closed ? explain(trade, rules) : [];
  // ไม้ที่ปิดก่อนเริ่มเก็บข้อมูลปิด — ไม่กู้ประวัติย้อนหลัง (จะกลายเป็นแต่งข้อมูล)
  // แสดงเฉพาะกฎที่คำอธิบายต้องพึ่งข้อมูลเวลา (smart_exit:* / time)
  const needsTiming = reason.startsWith("smart_exit") || reason === "time";
  const noDetail = closed && needsTiming
    && (trade.holding_days === null || trade.holding_days === undefined);
  const title = closed
    ? `${label}${reason ? ` (${reason})` : ""}\n${lines.join("\n")}`
    : label;

  if (!closed) {
    return <span className="text-slate-500">—</span>;
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={() => setPop((v) => !v)}
        title={title}
        aria-label={title}
        aria-expanded={pop}
        data-close-reason={reason}
        className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-bold cursor-help touch-manipulation ${toneOf(reason, trade.pnl)}`}
      >
        {label}
      </button>
      {pop && createPortal(
        <div
          ref={popRef}
          role="tooltip"
          data-testid="close-reason-popup"
          style={{
            position: "fixed", top: pos.top, left: pos.left,
            maxWidth: "min(360px, calc(100vw - 16px))",
          }}
          className="z-50 rounded-lg border border-slate-700 bg-slate-900/95 backdrop-blur px-3 py-2 shadow-xl text-xs leading-relaxed text-slate-200"
        >
          <div className="font-bold mb-1">{trade.asset} {trade.direction} · {label}</div>
          <div className="text-slate-400 mb-1">
            {reason ? <span className="font-mono">{reason}</span> : "ไม่มีรหัสเหตุผล"}
            {trade.holding_days !== null && trade.holding_days !== undefined
              ? ` · ถือ ${fmtNum(trade.holding_days, 1)} วัน` : ""}
            {trade.closed_at ? ` · ปิด ${new Date(trade.closed_at).toLocaleString("th-TH")}` : ""}
          </div>
          {trade.pnl !== null && (
            <div className="mb-1">
              PnL{" "}
              <span className={trade.pnl >= 0 ? "text-profit" : "text-loss"}>
                {trade.pnl >= 0 ? "+" : ""}{fmtNum(trade.pnl, 2)}
              </span>
            </div>
          )}
          {lines.map((l, i) => (
            <div key={i} className="text-slate-300">• {l}</div>
          ))}
          {noDetail && (
            <div className="mt-1 text-slate-500">
              ไม้เก่า — จัดเก็บรหัสเหตุผลไว้ แต่ไม่มีข้อมูลอายุ/เกณฑ์ของรอบนั้น
              (ไม่กู้ย้อนหลังเพื่อไม่ให้ข้อมูลที่แสดงไม่ตรงกับที่เกิดขึ้นจริง)
            </div>
          )}
        </div>,
        document.body
      )}
    </>
  );
}
