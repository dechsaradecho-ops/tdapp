"use client";

/**
 * BackgroundLayer — client component แสดงรูปพื้นหลังที่ผู้ใช้อัปโหลด (ตั้งใน Settings)
 * อ่าน data URL จาก localStorage (key: tdapp_bg_image) แล้ว render fixed layer ทั้งจอ
 * อยู่หลังเนื้อหาทั้งหมด (z-index ลบ) + scrim ดำโปร่งเพื่อคงความอ่านง่าย
 * รับรู้การเปลี่ยนแปลงทันทีผ่าน event "tdapp:bg-changed" ที่ BackgroundPicker dispatch
 */

import { useEffect, useState } from "react";
import { readStoredBg } from "@/components/BackgroundPicker";

export default function BackgroundLayer() {
  const [bg, setBg] = useState<string | null>(null);

  useEffect(() => {
    setBg(readStoredBg());
    const onChange = () => setBg(readStoredBg());
    window.addEventListener("tdapp:bg-changed", onChange);
    return () => window.removeEventListener("tdapp:bg-changed", onChange);
  }, []);

  if (!bg) return null;

  return (
    <>
      <div
        aria-hidden="true"
        className="fixed inset-0 z-[-2] bg-cover bg-center"
        style={{
          backgroundImage: `url(${bg})`,
          /* เบลอเบา ๆ เฉพาะพอให้ขอบรูปนุ่ม — ไม่เบลอทั้งรูป (user ต้องการเห็นรูปชัด)
             ส่วนความฝ้าของแก้วมาจาก backdrop-filter ของ .panel เอง (ซึ่ง Samsung รองรับ
             บน element จริง) + scrim ดำด้านล่าง ช่วยคุม contrast */
          filter: "blur(2px)",
          /* ขยายเกินจอเล็กน้อยกันขอบรูปขาวเพราะเบลอ */
          transform: "scale(1.03)",
        }}
      />
      <div
        aria-hidden="true"
        className="fixed inset-0 z-[-1]"
        style={{ background: "rgba(0,0,0,0.55)" }}
      />
    </>
  );
}
