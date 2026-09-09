"use client";

import { useState } from "react";

/**
 * บล็อก "วิธีคำนวณ" แบบพับได้ — แสดงขั้นตอนคิดทีละขั้น (ภาษาไทย) ที่ backend
 * ส่งมาใน calc_notes (SL จาก ATR, TP จาก RR, ขนาดไม้, สเปรด, R, risk$ ฯลฯ)
 * ปิดเป็นค่าเริ่มต้นเหมือน LimitLevels — ไม่รกการ์ด แต่กดดูได้ทุกตัวเลข
 */
export default function CalcNotes({ notes, defaultOpen = false }: { notes?: string[] | null; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  if (!notes || notes.length === 0) return null;
  return (
    <div className="mt-2 rounded-xl border border-white/10 bg-white/[0.04]">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-2 py-1.5 text-xs hover:bg-white/10 rounded-xl"
        aria-expanded={open}
      >
        <span className="text-slate-400">
          วิธีคำนวณ ({notes.length} ขั้นตอน)
        </span>
        <span className="text-slate-500 shrink-0 ml-2">{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ol className="list-decimal list-inside px-3 pb-2 space-y-1 text-slate-300 text-xs">
          {notes.map((n, i) => (
            <li key={i}>{n}</li>
          ))}
        </ol>
      )}
    </div>
  );
}
