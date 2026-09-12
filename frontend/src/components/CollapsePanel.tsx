"use client";

/**
 * CollapsePanel — การ์ดแก้วแบบ "พับได้" (หัวการ์ดทั้งแถบเป็นปุ่มกด)
 *
 *   ใช้ที่ Settings → "ตกแต่ง (Appearance)" เพื่อรวบ setting ที่เป็นงานตกแต่งล้วน
 *   (ไม่กระทบการเทรด) ให้พับเก็บเป็นค่าเริ่มต้น — การ์ดที่ใช้งานจริงจึงอยู่ใกล้มือกว่า
 *
 * ลักษณะเฉพาะ:
 *   - defaultOpen = false → เริ่มต้นแบบพับเสมอ (ไม่จำสถานะลง localStorage:
 *     ผู้ใช้ที่กดเปิดแล้วกลับมาหน้าใหม่จะเห็นพับอยู่ ซึ่งเป็นเจตนาของ "default = collapse")
 *   - หัวการ์ดทั้งก้อนเป็นปุ่มเดียว (รวมบรรทัดคำอธิบาย) → touch target ใหญ่ตาม guideline 44px
 *   - มี aria-expanded / aria-controls ครบ (screen reader รู้ว่าพับ/กาง)
 *   - เนื้อหาถูก render เฉพาะตอนกาง (ไม่ใช่ซ่อนด้วย CSS) → ฟอร์มข้างใน mount ใหม่ทุกครั้ง
 *     ซึ่งปลอดภัยเพราะ BackgroundPicker เขียนค่าลง localStorage ตั้งแต่ตอนขยับสไลเดอร์
 *   - เข้าหา/ออกจากเนื้อหาใช้ .collapse-body (fade + เลื่อนลง 6px ใน globals.css)
 *
 * ⚠️ ข้างใน CollapsePanel ต้องเป็น "บล็อกมีขอบ" (rounded border ... bg-surface/40)
 *    ห้ามซ้อน .panel — backdrop-filter ซ้อน backdrop-filter ทำให้แก้วทึบ/เบลอเพี้ยน
 *    (บทเรียนเดิม: Samsung Internet ไม่ render blur ที่ pseudo-element/ชั้นซ้อน)
 */

import { useId, useState } from "react";
import Icon from "@/components/Icon";
import type { IconName } from "@/components/Icon";

export default function CollapsePanel({
  title,
  icon,
  hint,
  defaultOpen = false,
  className = "",
  children,
}: {
  /** หัวข้อหมวด (แสดงตัวใหญ่ uppercase) */
  title: string;
  icon?: IconName;
  /** บรรทัดอธิบายใต้หัวข้อ — มองเห็นได้แม้ตอนพับอยู่ (ผู้ใช้รู้ว่าข้างในมีอะไร) */
  hint?: string;
  /** เปิดไว้ตั้งแต่แรกหรือไม่ (ค่าเริ่มต้น = พับ) */
  defaultOpen?: boolean;
  /** คลาสเสริมของตัวการ์ด เช่น "md:col-span-2" */
  className?: string;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = useId();

  return (
    <div className={`panel ${className}`}>
      <h2>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-controls={bodyId}
          className="w-full min-h-[44px] flex items-start justify-between gap-3 text-left"
        >
          <span className="min-w-0">
            <span className="flex items-center gap-2 text-sm font-semibold text-slate-400 uppercase tracking-wide">
              {icon && <Icon n={icon} size={14} />}
              {title}
            </span>
            {hint && (
              <span className="block text-xs font-normal normal-case tracking-normal text-slate-500 mt-1">
                {hint}
              </span>
            )}
          </span>
          <span className="shrink-0 flex items-center gap-2 pt-0.5">
            <span className="text-[11px] text-slate-500">{open ? "ย่อ" : "ขยาย"}</span>
            <span
              aria-hidden
              className={`text-slate-400 leading-none transition-transform duration-200 ${
                open ? "rotate-90" : ""
              }`}
            >
              ›
            </span>
          </span>
        </button>
      </h2>

      {open && (
        <div id={bodyId} className="collapse-body mt-3 space-y-4">
          {children}
        </div>
      )}
    </div>
  );
}
