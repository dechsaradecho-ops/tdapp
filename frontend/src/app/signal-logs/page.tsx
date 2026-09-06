"use client";

import { useEffect } from "react";

/** /signal-logs ถูกรวมเป็นแท็บ "บันทึกสัญญาณ" ในหน้าสัญญาณ (/signals?tab=logs)
 *  ตามแผนจัดเมนูใหม่ Plan B — static export ไม่มี server redirect จึงใช้ client redirect แทน */
export default function SignalLogsRedirect() {
  useEffect(() => {
    window.location.replace("/signals.html?tab=logs");
  }, []);
  return (
    <div className="panel p-6 text-center space-y-2">
      <p className="text-sm text-slate-400">🗂️ หน้า &quot;Signal Logs&quot; ถูกรวมเป็นแท็บในหน้าสัญญาณแล้ว</p>
      <a href="/signals.html?tab=logs" className="text-accent underline text-sm">ไปที่หน้าสัญญาณ (แท็บบันทึกสัญญาณ)</a>
    </div>
  );
}
