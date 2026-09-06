"use client";

import { useEffect } from "react";

/** /risk ถูกรวมเข้าหน้ามอนิเตอร์ (/monitor) ตามแผนจัดเมนูใหม่ Plan B —
 *  static export ไม่มี server redirect จึงใช้ client redirect แทน */
export default function RiskRedirect() {
  useEffect(() => {
    window.location.replace("/monitor.html");
  }, []);
  return (
    <div className="panel p-6 text-center space-y-2">
      <p className="text-sm text-slate-400">หน้า &quot;Risk&quot; ถูกรวมเข้าหน้ามอนิเตอร์แล้ว</p>
      <a href="/monitor.html" className="text-accent underline text-sm">ไปที่หน้ามอนิเตอร์</a>
    </div>
  );
}
