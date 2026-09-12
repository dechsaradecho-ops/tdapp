"use client";

import ScrollZoomHero from "@/components/ScrollZoomHero";

/* Mount แบ็กกราวด์ฮีโร่ (GSAP ScrollTrigger image zoom) ให้ "ทุกหน้า"
   — เดิมชื่อ HomeHero และ gate ด้วย usePathname() === "/" (โชว์แค่หน้าหลัก)
     ตอนนี้แสดงทุกหน้าเพื่อให้ทุกหน้ามีแบ็กกราวด์แบรนด์เดียวกัน
     (ผู้ใช้: "โอเค นำไปใช้กับทุกหน้าเลย")
   — วางไว้ใน app/layout.tsx (นอก <main>) เพื่อให้แถบเริ่มที่ขอบบนสุดของหน้า
     "ตรงกับ header" ไม่ใช่เริ่มใต้ nav; ถ้าอยู่ใน <main> จะติด padding
     py-4/sm:py-5 ของ main และความสูงของ sticky DesktopNav ลงมาอีก ~90px
   — เป็น client component (ScrollZoomHero ใช้ useEffect + dynamic import gsap)
     แต่ยังถูก SSR เป็น HTML แรกของทุกหน้า จึงไม่ต้องรอ JS หลัง hydrate แล้วโผล่
   — แบนด์มี z-index: -1 (ใน .zoom-hero) จึงอยู่ใต้เนื้อหาทุกหน้าโดยอัตโนมัติ
     ไม่ต้องใส่ relative z-10 ที่หน้าไหนอีก (และห้ามใส่ที่ <main> — ดู globals.css) */
export default function AppHero() {
  return <ScrollZoomHero />;
}
