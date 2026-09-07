"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

export type GlassOption = { value: string; label: string };

/** จริง ๆ แล้ว <640px — ตรงกับ breakpoint sm ของ Tailwind */
function useIsMobile() {
  const [mobile, setMobile] = useState(false);
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 639px)");
    const update = () => setMobile(mq.matches);
    update();
    mq.addEventListener("change", update);
    return () => mq.removeEventListener("change", update);
  }, []);
  return mobile;
}

/**
 * Liquid-glass dropdown — replaces native <select> ทั้งแอป (2026-09-07).
 *
 * ทำไมต้อง custom: ตัวกล่อง native select ทำเป็นแก้วฝ้าได้ผ่าน CSS แล้ว
 * แต่ "รายการที่กางออกมา" (option popup) เป็น native ของ OS — Windows/Android
 * วาดสีดำทึบและจะโปร่งใสไม่ได้ไม่ว่าด้วยวิธีใด (ดู UI-DESIGN-SYSTEM §9)
 * จึงต้องวาด popup เองด้วย glass panel เดียวกับทั้งแอป
 *
 * Keyboard: Enter/Space เปิด · ↑↓ เลื่อน · Enter เลือก · Esc ปิด
 * ปิดเองเมื่อคลิกนอกกล่อง
 */
export default function GlassSelect({
  value,
  onChange,
  options,
  className = "",
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: GlassOption[];
  /** sizing/placement ของกล่อง trigger เช่น "mt-1 w-full" หรือ "text-xs" */
  className?: string;
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const rootRef = useRef<HTMLDivElement>(null);
  const isMobile = useIsMobile();

  const current = options.find((o) => o.value === value) ?? options[0];

  // คลิกนอกกล่อง → ปิด (ปิดเฉพาะ target ที่ “ไม่ใช่” popup/scrim — มือถือ
  // popup ถูก portal ไป body จึงต้องเช็ค closest ด้วย ไม่งั้นคลิก item = ปิดก่อนเลือก)
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node | null;
      if (t instanceof Element && t.closest(".glass-select-popup, .glass-select-scrim")) return;
      if (rootRef.current && !rootRef.current.contains(t)) {
        setOpen(false);
      }
    };
    document.addEventListener("pointerdown", onDown);
    return () => document.removeEventListener("pointerdown", onDown);
  }, [open]);

  // เปิดครั้งใหม่ → ไฮไลต์เริ่มที่ตัวที่เลือกอยู่
  useEffect(() => {
    if (open) {
      setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    }
  }, [open, options, value]);

  const pick = (v: string) => {
    onChange(v);
    setOpen(false);
  };

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        setOpen(true);
        e.preventDefault();
      }
      return;
    }
    if (e.key === "Escape") {
      setOpen(false);
      e.preventDefault();
    } else if (e.key === "ArrowDown") {
      setActive((a) => Math.min(options.length - 1, a + 1));
      e.preventDefault();
    } else if (e.key === "ArrowUp") {
      setActive((a) => Math.max(0, a - 1));
      e.preventDefault();
    } else if (e.key === "Enter") {
      if (options[active]) pick(options[active].value);
      e.preventDefault();
    }
  };

  const items = options.map((o, i) => (
    <button
      type="button"
      key={o.value}
      role="option"
      aria-selected={o.value === value}
      onMouseEnter={() => setActive(i)}
      onClick={() => pick(o.value)}
      className={`glass-select-item ${i === active ? "is-active" : ""} ${
        o.value === value ? "is-selected" : ""
      }`}
    >
      {o.label}
    </button>
  ));

  return (
    <div ref={rootRef} className={`relative ${className}`} onKeyDown={onKeyDown}>
      <button
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        data-open={open}
        onClick={() => setOpen((v) => !v)}
        className="glass-select-trigger w-full flex items-center justify-between gap-2 text-left"
      >
        <span className="truncate">{current?.label ?? ""}</span>
        <svg
          className={`shrink-0 transition-transform duration-150 ${open ? "rotate-180" : ""}`}
          width="12" height="8" viewBox="0 0 12 8" fill="none" aria-hidden="true"
        >
          <path d="M1 1.5L6 6.5L11 1.5" stroke="#94a3b8" strokeWidth="2"
            strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>

      {/* มือถือ: portal ไป body — backdrop-filter ของ .panel สร้าง containing block
          ทำ position:fixed ในการ์ดวัดกับการ์ด ไม่ใช่จอ → sheet ต้องอยู่นอกการ์ด
          (desktop คง absolute ติดกับ trigger เหมือนเดิม) */}
      {open && isMobile
        ? createPortal(
            <>
              <div
                className="glass-select-scrim"
                aria-hidden="true"
                onClick={() => setOpen(false)}
              />
              <div role="listbox" aria-label={ariaLabel} className="glass-select-popup">
                {items}
              </div>
            </>,
            document.body,
          )
        : open && (
            <div role="listbox" aria-label={ariaLabel} className="glass-select-popup">
              {items}
            </div>
          )}
    </div>
  );
}
