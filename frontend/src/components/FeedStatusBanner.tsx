"use client";

import { QuoteFeedStatus } from "@/lib/types";

/** Warning banner for live-price feed failures (timeout / HTTP error).
 *
 * Rendered ONLY when state === "error" — a healthy feed shows nothing so the
 * dashboard stays clean. Shows which assets failed and the backend's reason,
 * e.g. "GBPUSD: spot feed timeout".
 */
export default function FeedStatusBanner({
  feed,
}: {
  feed?: QuoteFeedStatus | null;
}) {
  if (!feed || feed.state !== "error") return null;
  const failed = feed.failed_assets?.length
    ? feed.failed_assets.join(", ")
    : "ทุกสินทรัพย์";
  return (
    <div
      role="alert"
      className="mb-4 rounded-lg border border-amber-400/50 bg-amber-500/10 px-4 py-3 text-sm text-amber-200"
    >
      <div className="font-semibold">⚠️ ดึงราคาจากตลาดไม่สำเร็จ — ราคาบางส่วนอาจไม่อัปเดต</div>
      <div className="mt-1 text-amber-200/80">
        ล้มเหลว: {failed}
        {feed.message ? ` (${feed.message})` : ""}
      </div>
      <div className="mt-1 text-xs text-amber-200/60">
        ระบบใช้ราคาสำรองแทนชั่วคราว กำไร/ขาดทุนที่แสดงอาจไม่ตรงเป้า — ลองรีเฟรชอีกครั้ง
      </div>
    </div>
  );
}
