"use client";

import Icon from "@/components/Icon";

/**
 * แบนเนอร์ "ตลาดปิด" — ขึ้นบนหน้าหลักเมื่อตลาด FX/ทองคำปิดสุดสัปดาห์
 * (ศุกร์ 21:00 UTC → อาทิตย์ 21:00 UTC)
 *
 * กฎเหล็ก (เจ้าของ 2026-09-19): "เวลาตลาดปิด ห้ามมีการซื้อขาย หรือเปิด order"
 * — backend บังคับด้วย Gate 0b (`execution.market_closed_block`) ที่ไม่ขึ้นกับ
 * setting ใด ๆ แบนเนอร์นี้คือหน้าตาของกฎนั้น: บอกผู้ใช้ว่าตอนนี้ระบบงดเปิด
 * ออเดอร์ใหม่ทั้งหมด และจะเปิดอีกครั้งเมื่อไร (เวลาไทย)
 *
 * ปิด (return null) เมื่อตลาดเปิด — หน้าหลักสะอาดตามปกติ
 */
export default function MarketClosedBanner({
  marketClosed,
  nextOpenUtc,
}: {
  marketClosed?: boolean | null;
  nextOpenUtc?: string | null;
}) {
  if (!marketClosed) return null;

  const reopen = nextOpenUtc
    ? new Date(nextOpenUtc).toLocaleString("th-TH", {
        timeZone: "Asia/Bangkok",
        weekday: "short",
        day: "numeric",
        month: "short",
        hour: "2-digit",
        minute: "2-digit",
      })
    : null;

  return (
    <div
      role="alert"
      className="rounded-xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200"
    >
      <div className="font-semibold flex items-center gap-1.5">
        <Icon n="lock" size={15} /> ตลาดปิดอยู่ — งดเปิดออเดอร์ใหม่
      </div>
      <div className="mt-1 text-amber-200/80">
        ตลาด FX/ทองคำปิดสุดสัปดาห์ (ศุกร์ 21:00 UTC → อาทิตย์ 21:00 UTC)
        ระบบงดซื้อขายและเปิดออเดอร์ใหม่ทั้งหมดจนกว่าตลาดจะเปิด
        {reopen && ` — เปิดอีกครั้ง ${reopen} (เวลาไทย)`}
      </div>
      <div className="mt-1 text-xs text-amber-200/60">
        ยังปิดไม้ที่เปิดค้างได้ตามปกติ — เฉพาะการเปิดออเดอร์ใหม่ที่ถูกระงับ
      </div>
    </div>
  );
}
