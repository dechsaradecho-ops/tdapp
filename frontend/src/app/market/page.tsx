"use client";

import { useEffect } from "react";

/** /market ถูกรวมเข้าหน้าหลัก (/) ตามแผนจัดเมนูใหม่ Plan B —
 *  static export ไม่มี server redirect จึงใช้ client redirect แทน */
export default function MarketRedirect() {
  useEffect(() => {
    window.location.replace("/");
  }, []);
  return (
    <div className="panel p-6 text-center space-y-2">
      <p className="text-sm text-slate-400">📈 หน้า &quot;ตลาด&quot; ถูกรวมเข้าหน้าหลักแล้ว</p>
      <a href="/" className="text-accent underline text-sm">ไปที่หน้าหลัก</a>
    </div>
  );
}
