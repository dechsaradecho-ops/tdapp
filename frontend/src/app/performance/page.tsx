"use client";

import { useEffect } from "react";

/** /performance ถูกรวมเป็นแท็บ "Performance" ในหน้ามอนิเตอร์ (/monitor?tab=performance)
 *  ตามแผนจัดเมนูใหม่รอบ 2: monitor + performance เมนูเดียว แยกแท็บภายใน —
 *  static export ไม่มี server redirect จึงใช้ client redirect แทน
 *  (ชี้ .html เพื่อรองรับ static hosting ทั้ง Render และ preview server) */
export default function PerformanceRedirect() {
  useEffect(() => {
    window.location.replace("/monitor.html?tab=performance");
  }, []);
  return (
    <div className="panel p-6 text-center space-y-2">
      <p className="text-sm text-slate-400">🎯 หน้า &quot;Performance&quot; ถูกรวมเป็นแท็บในหน้ามอนิเตอร์แล้ว</p>
      <a href="/monitor.html?tab=performance" className="text-accent underline text-sm">ไปที่หน้ามอนิเตอร์ (แท็บ Performance)</a>
    </div>
  );
}
