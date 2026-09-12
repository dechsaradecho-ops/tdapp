"use client";

import { usePathname } from "next/navigation";
import ScrollZoomHero from "@/components/ScrollZoomHero";

/* Mount แบ็กกราวด์ฮีโร่ (GSAP ScrollTrigger image zoom) เฉพาะหน้าหลัก
   — วางไว้ใน app/layout.tsx (นอก <main>) เพื่อให้แถบเริ่มที่ขอบบนสุดของหน้า
   "ตรงกับ header" ไม่ใช่เริ่มใต้ nav; ถ้าอยู่ใน <main> จะติด padding
   py-4/sm:py-5 ของ main และความสูงของ sticky DesktopNav ลงมาอีก ~90px

   ใช้ usePathname() แทนการย้ายเข้า page.tsx เพราะ layout ไม่มีข้อมูล route
   (ตอน static export/prerender ค่าที่ได้คือ "/" ของหน้านั้น ๆ จึงได้แถบ
   อยู่ใน HTML แรกที่ส่งไป ไม่ต้องรอ JS หลัง hydrate แล้วค่อยโผล่) */
export default function HomeHero() {
  const pathname = usePathname();
  if (pathname !== "/") return null;
  return <ScrollZoomHero />;
}
