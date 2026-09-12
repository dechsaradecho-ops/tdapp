"use client";

import { useEffect, useRef, useState } from "react";
import { HERO_IMAGE_EVENT, readStoredHero } from "@/components/BackgroundPicker";

/**
 * ScrollZoomHero — แบ็กกราวด์ด้านบนสุดของหน้าแรก เลียนแบบ effect #26
 * "GSAP ScrollTrigger image zoom" (prismic.io/blog/css-scroll-effects)
 *
 * ทำหน้าที่เป็น "แบ็กกราวด์" ล้วน ๆ : แบนด์เต็มความกว้างจอ ติดขอบบนสุดของหน้า
 * (โดน mount จาก app/layout.tsx ผ่าน <HomeHero /> จึงอยู่เหนือ <main> —
 * ถ้าอยู่ใน <main> แบนด์จะเริ่มต่ำลงมาอีก ~70-90px ตาม padding ของ main +
 * ความสูง sticky DesktopNav)
 *
 * ทำไมใช้ inset-x-0 ไม่ใช่ w-screen: 100vw นับความกว้าง scrollbar รวมเข้าไป
 * ด้วย → แบนด์กว้างกว่าพื้นที่จริง ~4px เกิดแถบเลื่อนแนวนอน (ก้นจอขยับได้)
 * แบนด์นี้อยู่ระดับเอกสาร (containing block = viewport) ดังนั้น inset-x-0
 * ให้ความกว้าง = viewport จริง → ชนขอบจอทั้งสองข้างพอดี
 *
 * ไม่กินพื้นที่ใน layout (absolute) และไม่รับ pointer event
 * → เนื้อหาทุกอย่างในหน้า (การ์ด/ฟอร์ม/กราฟ) วางทับอยู่ข้างบนตามปกติ
 * เนื่องจากแบนด์เริ่มที่ y=0 พร้อมหน้า จึงอยู่ "หลัง" sticky DesktopNav (z-30)
 * และหลัง AuthGate overlay (z-50) — ทั้งคู่ทึบกว่า จึงยังอ่าน/กดได้ปกติ
 *
 * แนวคิด: การเลื่อนหน้า = การเคลื่อนกล้อง (camera dolly-in) ไม่ใช่แค่ fade-in
 *   • ภาพพื้นหลัง   ขยาย 1.18 → 1.62  + เลื่อนขึ้นเล็กน้อย + เร่ง saturate
 *   • เลเยอร์แสง (ไกล)  ขยายช้า  →  เหมือนอยู่ไกลหลังภาพ
 *   • กริด HUD  (กลาง)  ขยายกลาง
 *   • โบเก้      (ใกล้)  ขยายเร็วสุด → ดูเหมือนลอยอยู่หน้ากล้อง
 *   • rotateX ของแต่ละชั้นไล่จาก 5° → 0° = รู้สึกว่ากล้องกำลัง "จ่อเข้า" ฉาก 3D
 *   • ขอบมืดด้านบน (vignette) ให้การ์ดแถวแรกอ่านออกบนภาพที่สว่าง
 *
 * การวางชั้น (สำคัญ): แบ็กกราวด์เดิมของแอป — body aurora + BackgroundLayer
 * ที่ผู้ใช้อัปโหลด (fixed, z ลบ) — ยังคงเป็น "ชั้นล่างสุด" เสมอ แบนด์นี้เป็น
 * ชั้นที่วางทับอยู่ข้างบน แล้วจางหายเป็น alpha ที่ปลายล่าง (mask ใน CSS)
 * จึงไม่ทับ/กลบแบ็กกราวด์เดิมด้วยสีดำ
 *
 * รูป: ใช้ /scroll-zoom.svg (ภาพเริ่มต้นในตัว) หรือรูปที่ผู้ใช้ตั้งใน
 * Settings → "แบบด์ image zoom" (localStorage tdapp_hero_image, device-local)
 *   • ภาพเริ่มต้น = ภาพวาดประกอบ → โชว์เลเยอร์ HUD (glow/grid/bokeh) ให้เป็นฉาก 3D
 *   • รูปผู้ใช้    = ภาพถ่าย → ตัด grid/bokeh ออก เปิดแค่ glow จาง ๆ + vignette
 *     เพื่อไม่ให้ลายกริดทับรูป และคงความอ่านง่ายของการ์ดด้านบน
 *   • เปลี่ยนรูปแล้วอัปเดตทันทีผ่าน event tdapp:hero-changed
 *   • ผู้ใช้ที่ปิดอนิเมชัน (prefers-reduced-motion) ก็ยังเห็นรูปที่ตั้งไว้ (ภาพนิ่ง)
 *
 * ทำไมไม่ import gsap ตรง ๆ ด้านบน: หน้าแรกเป็น dashboard ที่ผู้ใช้เปิดบ่อย
 * GSAP + ScrollTrigger ~90KB ก่อน gzip — dynamic import ทำให้มันไปอยู่ใน chunk
 * แยกที่โหลดหลัง hydrate เสร็จ แล้ว first paint ของการ์ดข้อมูลไม่ถูกถ่วง
 *
 * ความปลอดภัย:
 *   • prefers-reduced-motion → ไม่แตะ DOM เลย ปล่อยภาพนิ่งตามค่าใน CSS
 *   • gsap.context(...).revert() ตอน unmount = คืน inline style + kill trigger
 *     (จำเป็นเพราะ React StrictMode ใน dev เรียก effect สองรอบ)
 *   • ScrollTrigger.refresh() เมื่อรูปโหลดเสร็จ ไม่งั้นวัดความสูงผิด
 */
export default function ScrollZoomHero() {
  const rootRef = useRef<HTMLElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const nearRef = useRef<HTMLDivElement>(null);
  // รูปที่ผู้ใช้ตั้งใน Settings (data URL ใน localStorage) — null = ใช้ภาพเริ่มต้นในตัว
  const [src, setSrc] = useState<string | null>(null);
  const custom = src !== null;

  // อ่านรูปที่ตั้งไว้ + ฟัง event ให้เปลี่ยนได้ทันทีโดยไม่ต้องรีโหลดหน้า
  // (แยก effect จากตัว GSAP เพราะต้องทำงานแม้ผู้ใช้ปิดอนิเมชัน)
  useEffect(() => {
    const read = () => setSrc(readStoredHero());
    read();
    window.addEventListener(HERO_IMAGE_EVENT, read);
    return () => window.removeEventListener(HERO_IMAGE_EVENT, read);
  }, []);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    // ผู้ใช้ขอปิดอนิเมชัน (หรือ OS ตั้งไว้) → คงภาพนิ่ง ไม่ต้องโหลด GSAP เลย
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;

    let cancelled = false;
    let ctx: { revert: () => void } | null = null;
    let st: { refresh: () => void } | null = null;

    (async () => {
      // import แยก chunk — bundler แยก gsap ออกจาก main bundle ของหน้าแรก
      const gsapMod = await import("gsap");
      const stMod = await import("gsap/ScrollTrigger");
      if (cancelled) return;
      const gsap = gsapMod.gsap;
      const ScrollTrigger = stMod.ScrollTrigger;
      gsap.registerPlugin(ScrollTrigger);
      st = ScrollTrigger;

      ctx = gsap.context(() => {
        const tl = gsap.timeline({
          defaults: { ease: "none" },
          scrollTrigger: {
            trigger: root,
            // ระยะซูม = “ช่วงที่แบนด์เลื่อนพ้นขอบจอบน”
            //   start: ขอบบนแบนด์ชนขอบจอบน (ก่อนหน้านั้น progress ถูก clamp 0
            //          → เปิดหน้ามาเห็นภาพมุมกว้างเต็มใบเสมอ)
            //   end:   ขอบล่างแบนด์ไต่มาถึง 25% ของความสูงจอ (จบก่อนแบนด์
            //          ลับขอบจอ → เห็นสภาวะซูมสุดค้างอยู่ชั่วขณะ)
            start: "top top",
            end: "bottom 25%",
            scrub: 0.6,
            invalidateOnRefresh: true,
          },
        });

        // "กล้อง" — ทุกชั้นเริ่มพร้อมกันและจบพร้อมกัน (position 0) ต่างกันที่อัตรา
        if (imgRef.current) {
          tl.fromTo(
            imgRef.current,
            { scale: 1.18, yPercent: 4, rotateX: 5, transformPerspective: 1100, filter: "brightness(1.06) saturate(1.02)" },
            { scale: 1.62, yPercent: -5, rotateX: 0, filter: "brightness(0.74) saturate(1.3)" },
            0,
          );
        }
        if (glowRef.current) {
          tl.fromTo(
            glowRef.current,
            { scale: 1.06, yPercent: 3, rotateX: 3, transformPerspective: 1100, opacity: 0.75 },
            { scale: 1.24, yPercent: -7, rotateX: 0, opacity: 1 },
            0,
          );
        }
        if (!custom && gridRef.current) {
          tl.fromTo(
            gridRef.current,
            { scale: 1, yPercent: 6, rotateX: 4, transformPerspective: 1100, opacity: 0.55 },
            { scale: 1.46, yPercent: -12, rotateX: 0, opacity: 0.9 },
            0,
          );
        }
        if (!custom && nearRef.current) {
          tl.fromTo(
            nearRef.current,
            { scale: 1.1, yPercent: 10, rotateX: 2, transformPerspective: 1100, opacity: 0.85 },
            { scale: 2.3, yPercent: -16, rotateX: 0, opacity: 1 },
            0,
          );
        }
      }, root);
    })();

    // รูปโหลดช้า / ถูกเปลี่ยนsrc หลัง mount → ตำแหน่ง trigger ถูกวัดตอนรูปยังไม่สูงเต็ม
    // ต้อง refresh อีกครั้งเมื่อรูปโหลดเสร็จ
    const img = imgRef.current;
    const onLoad = () => st?.refresh();
    img?.addEventListener("load", onLoad);

    return () => {
      cancelled = true;
      if (img) img.removeEventListener("load", onLoad);
      ctx?.revert();
    };
    // custom = เปลี่ยนชุดเลเยอร์ (มี/ไม่มี grid+bokeh) → ต้องสร้าง timeline ใหม่
    // ให้ตรงกับ DOM ปัจจุบัน; ctx.revert() ใน cleanup คืน inline style ให้ก่อน
  }, [custom]);

  return (
    <section
      ref={rootRef}
      aria-hidden="true"
      className={`zoom-hero pointer-events-none absolute inset-x-0 top-0 h-[clamp(320px,70vh,660px)] overflow-hidden${
        custom ? " zoom-hero--custom" : ""
      }`}
    >
      <div className="relative h-full w-full">
        {/* ชั้นไกลสุด: ภาพ (เริ่มต้นในตัว หรือรูปที่ผู้ใช้ตั้งใน Settings) */}
        <img
          ref={imgRef}
          src={src ?? "/scroll-zoom.svg"}
          alt=""
          aria-hidden="true"
          draggable={false}
          className="zoom-hero-img select-none"
          style={{ transform: "scale(1.18) translate3d(0, 4%, 0)" }}
        />

        {/* ชั้นกลางไกล: แสงบรรยากาศ */}
        <div
          ref={glowRef}
          className="zoom-hero-layer zoom-hero-glow"
          style={{ transform: "scale(1.06)" }}
        />

        {/* ชั้นกริด HUD + โบเก้ = ลายวาดประกอบของภาพเริ่มต้น → ซ่อนเมื่อใช้รูปผู้ใช้ */}
        {!custom && (
          <>
            {/* ชั้นกลาง: กริด HUD แบบ perspective */}
            <div
              ref={gridRef}
              className="zoom-hero-layer zoom-hero-grid"
              style={{ transform: "scale(1)" }}
            />

            {/* ชั้นใกล้สุด: โบเก้ (ลอยอยู่หน้ากล้อง) */}
            <div
              ref={nearRef}
              className="zoom-hero-layer"
              style={{ transform: "scale(1.1) translate3d(0, 10%, 0)" }}
            >
              <div className="zoom-hero-layer zoom-hero-bokeh" />
            </div>
          </>
        )}

        {/* ชั้นนิ่ง: ขอบมืดด้านบน (การ์ดอ่านออก) — ปลายล่างปล่อยให้ mask ของ
            .zoom-hero จางหายเป็น alpha เอง เพื่อคงแบ็กกราวด์เดิมของแอปไว้ชั้นล่างสุด */}
        <div className="zoom-hero-vignette" />
      </div>
    </section>
  );
}
