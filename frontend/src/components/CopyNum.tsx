"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { fmtNum } from "@/lib/format";

/** ตัวเลขราคาที่กดแล้วคัดลอกลงคลิปบอร์ด (ผู้ใช้ขอ 2026-09-08: หน้า monitor/signals
 *  กดที่ราคา Entry/SL/TP เพื่อ copy — ไม่มีไอคอน copy)
 *  - คัดลอก "ค่าดิบ" เต็มความแม่นยำ (ไม่มี comma) เพื่อเอาไปวางในแอปเทรดได้เลย
 *  - feedback: ป้าย "คัดลอกแล้ว" ลอยเหนือตัวเลข ~1.2 วิ (ไม่มี icon)
 *  - ป้ายใช้ portal ไป document.body เหมือน popover อื่น เพราะ .panel มี
 *    backdrop-filter ที่ทำให้ position:fixed ถูกตรึงกับ panel แทน viewport
 *  - fallback execCommand สำหรับ context ที่ไม่มี navigator.clipboard (http ธรรมดา) */
export default function CopyNum({ value, digits = 5, className = "" }: {
  value: number | null | undefined;
  digits?: number;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const copy = useCallback(() => {
    if (typeof value !== "number" || !Number.isFinite(value)) return;
    const text = String(value); // ค่าดิบ ไม่มี comma
    const done = () => {
      const r = btnRef.current?.getBoundingClientRect();
      if (r) setPos({ top: r.top - 30, left: r.left + r.width / 2 });
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 1200);
    };
    if (navigator.clipboard?.writeText) {
      navigator.clipboard.writeText(text).then(done).catch(() => { /* ignore */ });
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* ignore */ }
      document.body.removeChild(ta);
      done();
    }
  }, [value]);

  // ไม่มีค่า → แสดง "-" แบบเดิม (กดไม่ได้)
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <span className={className}>-</span>;
  }

  return (
    <>
      <button
        ref={btnRef}
        type="button"
        onClick={copy}
        title={`คัดลอก ${value}`}
        aria-label={`คัดลอก ${value}`}
        className={`cursor-pointer touch-manipulation active:opacity-60 ${className}`}
      >
        {fmtNum(value, digits)}
      </button>
      {copied && createPortal(
        <div
          role="status"
          style={{ position: "fixed", top: pos.top, left: pos.left, transform: "translateX(-50%)" }}
          className="z-50 rounded-md border border-accent/50 bg-slate-900/95 backdrop-blur px-2 py-0.5 text-[11px] font-semibold text-accent shadow-lg pointer-events-none"
        >
          คัดลอกแล้ว
        </div>,
        document.body
      )}
    </>
  );
}
