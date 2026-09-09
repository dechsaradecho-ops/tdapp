"use client";

/**
 * กราฟิกวิ่งตอนกำลังโหลด — ใช้แทนข้อความ "กำลังโหลด..." นิ่ง ๆ ทั้งเว็บ
 * ประกอบด้วย: เส้นกราฟวิ่ง (stroke-dash ไหล) + แท่งเทียนกระดิก + จุดเรืองแสง
 * Pure CSS/SVG — ไม่เพิ่ม dependency, เคารพ prefers-reduced-motion ผ่าน globals.css
 * ใช้: <LoadingGraphic message="กำลังโหลดข้อมูล..." /> หรือ <LoadingGraphic compact />
 */
export default function LoadingGraphic({
  message,
  compact = false,
}: {
  message?: string;
  compact?: boolean;
}) {
  return (
    <div
      role="status"
      aria-live="polite"
      className={`load-wrap${compact ? " load-compact" : ""}`}
    >
      <svg viewBox="0 0 120 44" className="load-chart" aria-hidden="true">
        {/* เส้นกราฟวิ่ง — dash ไหลไปข้างหน้าตลอด */}
        <path
          d="M2 34 L18 28 L32 30 L48 20 L62 23 L78 12 L92 16 L106 8 L118 10"
          className="load-line"
          fill="none"
        />
        {/* แท่งเทียนกระดิกด้านล่าง */}
        <g className="load-candles" aria-hidden="true">
          <rect x="14" y="30" width="6" height="10" rx="1.5" className="load-candle c1" />
          <rect x="44" y="24" width="6" height="14" rx="1.5" className="load-candle c2" />
          <rect x="74" y="18" width="6" height="18" rx="1.5" className="load-candle c3" />
          <rect x="102" y="14" width="6" height="20" rx="1.5" className="load-candle c4" />
        </g>
        {/* จุดปลายเส้น — กระพริบเรืองแสงเหมือนราคาสด */}
        <circle cx="118" cy="10" r="3.5" className="load-dot" />
      </svg>
      <div className="load-bars" aria-hidden="true">
        <span />
        <span />
        <span />
        <span />
        <span />
      </div>
      {message && <p className="load-msg">{message}</p>}
      <span className="sr-only">กำลังโหลด</span>
    </div>
  );
}
