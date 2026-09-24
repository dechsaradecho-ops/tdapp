"use client";

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import Icon from "@/components/Icon";
import { api } from "@/lib/api";
import { fmtNum } from "@/lib/format";
import { LimitExpandDecisionResult, LimitExpandState } from "@/lib/types";

/**
 * Popup ยืนยันขยายลิมิตความเสี่ยง — "เงื่อนไขเดียวกับ LINE"
 *
 * ขึ้นเฉพาะเมื่อ GET /api/trading/limit-expand คืนค่า ``pending = true``
 * (มีคำขอในตาราง kill_expand_requests รอเจ้าของบัญชีตอบ แถวเดียวกับที่
 * สร้างข้อความ LINE) — ตัวเลขที่โชว์คือตัวเลขของคำขอนั้น และการกด
 * อนุมัติ/ไม่อนุมัติเรียก endpoint เดียวกับที่ LINE ใช้ตัดสินใจ
 * (อนุมัติ → เขียนลิมิตใหม่ + เปิดเทรด + ประเมิน kill switch ใหม่ทันที)
 *
 * ถ้าไม่ตอบภายในเวลาที่ตั้งไว้ (Settings → kill_expand_ttl_min, default 180 นาที)
 * ระบบจะตัดสินแบบ fail-closed ก่อน: Settings → kill_expand_auto_widen
 *   - ปิด (default / ปลอดภัย) = “ลิมิตเดิมยังมีผล” ไม่ขยายให้เอง (auto:kept)
 *     แจ้งเตือนเจ้าของ แล้วปล่อยให้ kill switch ทำงานตามเดิม
 *   - เปิด = อนุญาตให้ระบบตัดสินให้เองตามนโยบาย Settings → kill_expand_auto_apply:
 *       · เปิด = “ขยายลิมิตให้อัตโนมัติ” ทุกครั้งที่หมดเวลา
 *       · ปิด = ขยายให้เองได้ “ครั้งเดียวใน 24 ชม.” ถ้าหมดโควตาแล้วยังไม่ตอบ
 *         ระบบจะไม่ขยายอีก และปล่อยให้ kill switch ทำงาน (ปิดไม้เพื่อความปลอดภัย)
 * ผลที่เกิดขึ้นจริงถูกส่งไปที่ LINE เสมอ — กล่องนี้ปิดเองเมื่อพ้นเวลา
 *
 * แต่ถ้าขยายอัตโนมัติ “ทำไม่ได้” (เขียนค่าลิมิตไม่สำเร็จ) ระบบจะไม่ปิดไม้และไม่
 * ตัดสินแทนเจ้าของ: คำขอยังเปิดอยู่ → กล่องนี้ยังขึ้นและยังกดตอบได้ โดยมีแถบ
 * amber บอกว่าเลยช่วงยืนยันแล้ว (ดู state.lapsed)
 *
 * กรณี setup_required = ยังไม่ได้รัน database/036_kill_expand_confirm.sql →
 * ขึ้นคำแนะนำเดิมกับที่ LINE แจ้ง (ลิมิตไม่ถูกแตะต้อง)
 */
const POLL_MS = 60_000;

export default function LimitExpandPopup() {
  const [state, setState] = useState<LimitExpandState | null>(null);
  const [mounted, setMounted] = useState(false);
  const [busy, setBusy] = useState<"" | "approve" | "reject">("");
  const [result, setResult] = useState<LimitExpandDecisionResult | null>(null);
  const [error, setError] = useState("");
  const [hiddenReq, setHiddenReq] = useState("");      // ปิดกล่องชั่วคราว (คำขอนี้)
  const [setupHidden, setSetupHidden] = useState(false);
  const [decidedReq, setDecidedReq] = useState("");    // id คำขอที่เพิ่งตัดสินใจ

  const load = useCallback(async () => {
    try {
      const st = await api.getLimitExpand();
      setState(st);
      if (!st.setup_required) setSetupHidden(false);
    } catch {
      // 401 จัดการที่ AuthGate / เน็ตสะดุด → คงสถานะเดิมไว้ (ไม่ต้องเด้ง error)
    }
  }, []);

  useEffect(() => {
    setMounted(true);
    load();
    const timer = setInterval(load, POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  const decide = async (decision: "approve" | "reject") => {
    setBusy(decision);
    setError("");
    // id คำขอที่กำลังจะตัดสิน — ถ้าระหว่างนี้มีคำขอใหม่ (race สร้างซ้อน)
    // จะได้ล้างจอผลเก่าแล้วโชว์คำขอใหม่แทน ไม่ค้างจอเดิม
    const decidingId = state?.request?.id ?? "";
    setDecidedReq(decidingId);
    try {
      const res = await api.decideLimitExpand(decision);
      setResult(res);
      setState(res.state);
      if (!res.applied_decision) {
        // ไม่มีคำขอค้าง — กดอนุมัติลอย ๆ ไม่ขยายลิมิตใด ๆ
        setError("ไม่มีคำขอที่รอการยืนยันอยู่ — ไม่มีการเปลี่ยนแปลงลิมิต");
      } else if (res.applied_decision === "auto") {
        // คำขอหมดเวลาไปแล้ว: reply อธิบายผลที่ระบบทำเอง (ไม่ใช่ผลจากการกดปุ่ม)
        setError("");
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      load();
    } finally {
      setBusy("");
    }
  };

  if (!mounted || !state) return null;

  const req = state.request;
  // คำขอใหม่โผล่มาหลังเราตัดสินใจ (id ไม่ใช่คำขอที่เพิ่งกด) → ทิ้งจอผลเก่า
  // โชว์คำขอใหม่ทันที ไม่ต้องรอให้ user ปิดจอเอง
  const resultStale = !!result && !!req && state.pending && req.id !== decidedReq;
  const showConfirm = !!req && state.pending && req.id !== hiddenReq && (resultStale || !result);
  const showSetup = !showConfirm && !result && state.setup_required && !setupHidden;
  if (!showConfirm && !showSetup && !result) return null;

  const ageTxt = req?.age_min != null ? `ผ่านมาแล้ว ${fmtNum(req.age_min, 1)} นาที` : "";
  const ttl = fmtNum(state.ttl_min, 0);

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4 animate-fade"
      style={{ background: "rgba(0,0,0,0.75)", WebkitBackdropFilter: "blur(18px) saturate(140%)", backdropFilter: "blur(18px) saturate(140%)" }}
    >
      <div className="panel w-full max-w-md space-y-4 animate-pop" role="dialog" aria-modal="true"
        aria-label="ยืนยันขยายลิมิตความเสี่ยง">

        {result && !resultStale ? (
          <>
            <div className="flex items-center justify-between">
              <h3 className="panel-title flex items-center gap-1.5">
                <Icon n={result.applied_decision === "reject" ? "xCircle" : "checkCircle"}
                  size={16} className={result.applied_decision === "reject" ? "text-loss" : "text-profit"} />
                ผลการตัดสินใจ
              </h3>
              <button onClick={() => setResult(null)} className="text-slate-400 hover:text-accent text-lg leading-none" aria-label="ปิดหน้าต่าง">✕</button>
            </div>

            <pre className="whitespace-pre-wrap break-words text-xs text-slate-200 bg-white/[0.04] border border-white/10 rounded-lg p-3 font-sans leading-relaxed">
              {result.reply}
            </pre>

            <p className="text-xs text-slate-500">
              ข้อความเดียวกันถูกส่งไปที่ LINE แล้ว · สถานะเทรดตอนนี้:{" "}
              <span className={result.state.paused ? "text-loss font-semibold" : "text-profit font-semibold"}>
                {result.state.paused ? "หยุดเปิดออเดอร์ใหม่" : "เทรดอยู่"}
              </span>
            </p>
            {result.state.pause_reason && (
              <p className="text-xs text-slate-500">เหตุผล: {result.state.pause_reason}</p>
            )}

            <button onClick={() => setResult(null)} className="btn-primary w-full">รับทราบ</button>
          </>
        ) : showSetup ? (
          <>
            <div className="flex items-center justify-between">
              <h3 className="panel-title flex items-center gap-1.5">
                <Icon n="warning" size={16} className="text-amber-400" /> เกินลิมิต — ยังบันทึกคำขอยืนยันไม่ได้
              </h3>
              <button onClick={() => setSetupHidden(true)} className="text-slate-400 hover:text-accent text-lg leading-none" aria-label="ปิดหน้าต่าง">✕</button>
            </div>

            <TriggerList state={state} />

            <div className="rounded-lg p-3 text-xs text-amber-300 bg-amber-400/10 border border-amber-400/25 space-y-1">
              <p className="font-semibold">ต้องรัน migration ก่อน</p>
              <p className="text-amber-200/90">
                รัน <code className="text-slate-100">database/036_kill_expand_confirm.sql</code>{" "}
                (ตารางคำขอยืนยัน) และ{" "}
                <code className="text-slate-100">database/037_kill_expand_ttl.sql</code>{" "}
                (คอลัมน์เวลาในการรอ) ใน Supabase SQL Editor แล้วระบบจะส่งคำขอให้ยืนยันอีกครั้ง
              </p>
            </div>

            <p className="text-xs text-slate-500">ลิมิตยังไม่ถูกแตะต้อง — ยังขยายอัตโนมัติไม่ได้เพราะบันทึกคำขอไม่สำเร็จ</p>

            <button onClick={() => setSetupHidden(true)} className="btn-secondary w-full">ปิดไปก่อน</button>
          </>
        ) : (
          <>
            <div className="flex items-center justify-between">
              <h3 className="panel-title flex items-center gap-1.5">
                <Icon n="warning" size={16} className="text-amber-400" /> เกินลิมิตความเสี่ยง — ขอยืนยันก่อนขยาย
              </h3>
              <button onClick={() => req && setHiddenReq(req.id)} className="text-slate-400 hover:text-accent text-lg leading-none" aria-label="ปิดหน้าต่าง">✕</button>
            </div>

            <TriggerList state={state} />

            {state.lapsed && (
              <div className="rounded-lg border border-amber-400/25 bg-amber-400/10 p-2.5 text-xs text-amber-300 flex items-start gap-1.5">
                <Icon n="clock" size={14} className="mt-[1px] shrink-0" />
                <span>
                  {state.lapsed_closes ? (
                    <>
                      เลยช่วงยืนยัน ({ttl} นาที) แล้ว — นโยบายขยายอัตโนมัติปิดอยู่และ
                      โควตา “ขยายเอง 1 ครั้ง” ถูกใช้ไปแล้ว จึง<b>ไม่ขยายลิมิตให้อีก</b> ·
                      ระบบจะให้ kill switch ปิดไม้เพื่อความปลอดภัย (ยังตอบได้ แต่คำตอบ
                      จะไม่หยุดการปิดไม้)
                    </>
                  ) : (
                    <>
                      เลยช่วงยืนยัน ({ttl} นาที) แล้ว แต่ระบบยังตัดสินให้ไม่ได้ —
                      ลิมิตยังไม่ถูกแตะต้อง และ<b>ระบบยังไม่ปิดไม้</b>ระหว่างรอ ·
                      คำตอบของคุณยังมีผลอยู่
                    </>
                  )}
                </span>
              </div>
            )}

            <p className="text-xs text-slate-400">
              สถานะ: <span className={state.paused ? "text-loss font-semibold" : "text-profit font-semibold"}>
                {state.paused ? "หยุดเปิดออเดอร์ใหม่ (pause)" : "เทรดอยู่"}
              </span>{" "}
              — ลิมิตยังไม่ถูกแตะต้อง
            </p>
            {!state.lapsed && (
              <p className="text-xs text-amber-300 flex items-start gap-1.5">
                <Icon n="warning" size={14} className="mt-[1px] shrink-0" />
                <span>
                  {state.auto_apply
                    ? <>ถ้าไม่ยืนยันภายใน {ttl} นาที ระบบจะขยายลิมิตให้อัตโนมัติ (+{fmtNum(state.step_pct, 0)}%) แล้วเปิดเทรดต่อ</>
                    : <>ถ้าไม่ยืนยันภายใน {ttl} นาที ระบบจะขยายให้เอง “ครั้งเดียวใน 24 ชม.” (+{fmtNum(state.step_pct, 0)}%) — ถ้ายังเกินลิมิตใหม่อีกครั้งหรือโควตาหมด ระบบจะไม่ขยายอีกและปล่อยให้ kill switch ปิดไม้เพื่อความปลอดภัย</>}
                </span>
              </p>
            )}

            <p className="text-xs text-slate-500 flex items-start gap-1.5">
              <Icon n="clock" size={14} className="mt-[1px] shrink-0" />
              <span>
                คำขอมีอายุ {ttl} นาที{ageTxt && ` · ${ageTxt}`} · อนุมัติแล้วระบบจะขยายลิมิต +
                เปิดเทรดต่อ + ประเมิน kill switch ให้ใหม่ทันที
              </span>
            </p>
            <p className="text-xs text-slate-500">
              คำขอเดียวกันถูกส่งไปที่ LINE ด้วย — ตอบผ่าน <code className="text-slate-300">{state.approve_command}</code>{" "}
              หรือ <code className="text-slate-300">{state.reject_command}</code> ได้เช่นกัน
            </p>

            {error && <p className="text-xs text-loss">{error}</p>}

            <div className="grid grid-cols-2 gap-2">
              <button onClick={() => decide("reject")} disabled={!!busy} aria-busy={busy === "reject"}
                className="btn-secondary">
                {busy === "reject"
                  ? <><Icon n="spinner" size={15} className="animate-spin" /> กำลังบันทึก...</>
                  : "ไม่อนุมัติ"}
              </button>
              <button onClick={() => decide("approve")} disabled={!!busy} aria-busy={busy === "approve"}
                className="btn-primary">
                {busy === "approve"
                  ? <><Icon n="spinner" size={15} className="animate-spin" /> กำลังขยาย...</>
                  : `อนุมัติ +${fmtNum(state.step_pct, 0)}%`}
              </button>
            </div>

            <button onClick={() => req && setHiddenReq(req.id)} disabled={!!busy}
              className="w-full text-xs text-slate-500 hover:text-slate-300 disabled:opacity-50">
              {state.lapsed_closes
                ? "ปิดไปก่อน (คำขอนี้จะไม่ถูกขยาย — kill switch จะปิดไม้ตามลิมิตเดิม)"
                : state.lapsed
                  ? "ปิดไปก่อน (คำขอยังค้างอยู่ — ระบบยังไม่ปิดไม้และยังไม่แตะลิมิตระหว่างรอคำตอบ)"
                  : state.auto_apply
                    ? "ปิดไปก่อน (คำขอยังรออยู่ใน LINE — พ้นเวลาแล้วระบบขยายให้อัตโนมัติ)"
                    : "ปิดไปก่อน (คำขอยังรออยู่ใน LINE — พ้นเวลาแล้วระบบขยายให้เองครั้งเดียวใน 24 ชม.)"}
            </button>
          </>
        )}
      </div>
    </div>,
    document.body,
  );
}

/** รายการลิมิตที่เกิน — ข้อความชุดเดียวกับ prompt ของ LINE
 *  (ทศนิยม 2 ตำแหน่งเหมือน build_limit_expand_prompt: 10.06% / 10.00% → 15.00%) */
function TriggerList({ state }: { state: LimitExpandState }) {
  if (!state.triggers.length) return null;
  return (
    <ul className="space-y-1.5">
      {state.triggers.map((t) => (
        <li key={t.trigger} className="text-sm text-slate-200 flex flex-wrap items-baseline gap-x-2">
          <span className="font-semibold">{t.label}</span>
          <span className="text-slate-400">
            {t.value.toFixed(2)}% เกิน {t.limit.toFixed(2)}%
          </span>
          <span className="text-accent font-semibold">
            → เสนอ {t.new_limit.toFixed(2)}% (+{fmtNum(state.step_pct, 0)}%)
          </span>
        </li>
      ))}
    </ul>
  );
}
