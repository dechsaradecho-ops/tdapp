"use client";

import { useEffect, useRef, useState } from "react";
import { HERO_DIM_DEFAULT, HERO_IMAGE_EVENT, HERO_ZOOM_DEFAULT, readStoredHero, readStoredHeroDim, readStoredHeroZoom } from "@/components/BackgroundPicker";

/**
 * ScrollZoomHero — แบ็กกราวด์ด้านบนสุดของหน้าแรก เลียนแบบ effect #26
 * "GSAP ScrollTrigger image zoom" (prismic.io/blog/css-scroll-effects)
 *
 * ทำหน้าที่เป็น "แบ็กกราวด์" ล้วน ๆ : แบนด์เต็มความกว้างจอ ติดขอบบนสุดของหน้า
 * (โดน mount จาก app/layout.tsx ผ่าน <AppHero /> จึงอยู่เหนือ <main> —
 * ถ้าอยู่ใน <main> แบนด์จะเริ่มต่ำลงมาอีก ~70-90px ตาม padding ของ main +
 * ความสูง sticky DesktopNav)
 *
 * ทำไมใช้ inset-x-0 ไม่ใช่ w-screen: 100vw นับความกว้าง scrollbar รวมเข้าไป
 * ด้วย → แบนด์กว้างกว่าพื้นที่จริง ~4px เกิดแถบเลื่อนแนวนอน (ก้นจอขยับได้)
 * แบนด์นี้อยู่ระดับเอกสาร (containing block = viewport) ดังนั้น inset-x-0
 * ให้ความกว้าง = viewport จริง → ชนขอบจอทั้งสองข้างพอดี
 *
 * ความสูงแบนด์: clamp(360px, 85vh, 780px) — 85vh ทำให้มือถือได้แบนด์สูงเกือบ
 * เต็มจอ (780px = เพดานกันจอ desktop สูงเกินไป) ตั้งใจให้แบนด์ "กินพื้นที่ลงมา"
 * ถึงการ์ดแถวที่ 2-3 ไม่ใช่แค่แถวแรก เพื่อให้เห็นการซูมของภาพชัดขึ้น
 * แบนด์เป็น absolute จึงไม่ดันเนื้อหา — เนื้อหาลอยทับอยู่ข้างบน (main pt-0 บนมือถือ)
 * และระยะ ScrollTrigger (start top top → end bottom 25%) ยึดความสูงนี้
 * → แบนด์สูงขึ้น = ระยะซูมยาวขึ้นตามไปด้วย ไม่ต้องแก้ timeline
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
 * ความสว่าง: Settings → slider "ความสว่าง (Image zoom)" (localStorage tdapp_hero_dim)
 * → scrim ดำทับ "เฉพาะภาพ + HUD" (อยู่ใต้ vignette) ยิ่งค่ามาก แบบด์ยิ่งมืด
 *   default = 0 (หน้าตาเดิมเป๊ะ) · ปรับแล้วเห็นผลทันทีทุกหน้าผ่าน event เดียวกัน
 *
 * ระดับการซูม: Settings → slider "ระดับการซูม (Zoom scale)" (localStorage tdapp_hero_zoom)
 * → ตัวคูณของ "ช่วงซูม" (end − start) ของทุกชั้น: 0 = ภาพนิ่งไม่ซูม · 1 = ค่าเดิมเป๊ะ
 *   · 2 = ช่วงซูมกว้างเป็น 2 เท่า — ดูรายละเอียดที่ zoomScale() ด้านล่าง
 *   ค่าใหม่มีผลทันทีผ่าน event เดียวกัน (effect ผูก zoom เป็น dependency → สร้าง
 *   timeline ใหม่ โดย ctx.revert() คืน inline style ให้ก่อน)
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
/** ค่า scale ของชั้นหนึ่ง ๆ ที่ "ระดับการซูม" k (Settings → tdapp_hero_zoom)
 *  start/end = scale ที่ k = 1 (ค่าที่ออกแบบไว้) →
 *    k = 0 → start          ทุกชั้นนิ่ง ไม่มีการขยายเลย (ยังมีการเลื่อน/เอียงตาม parallax)
 *    k = 1 → end            ค่าที่ออกแบบไว้เป๊ะ
 *    k = 2 → start + 2Δ     ช่วงซูมกว้างเป็น 2 เท่า (ซูมแรงขึ้น)
 *  ทำไมผูกเป็นตัวคูณ "ช่วง" ไม่ใช่ตัวคูณ scale ตรง ๆ: ทำให้ k = 1 ได้หน้าตาเดิม
 * เป๊ะเสมอ (hit-test ง่าย ไม่มี regression) และคง "สัดส่วน" ระหว่างชั้นไว้ทุกค่า k
 *  — ชั้นใกล้ยังขยายเร็วกว่าชั้นไกลเท่าเดิม จึงไม่เสียความลึก 3D */
function zoomScale(start: number, end: number, k: number): number {
  return start + (end - start) * k;
}

export default function ScrollZoomHero() {
  const rootRef = useRef<HTMLElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const glowRef = useRef<HTMLDivElement>(null);
  const gridRef = useRef<HTMLDivElement>(null);
  const nearRef = useRef<HTMLDivElement>(null);
  // รูปที่ผู้ใช้ตั้งใน Settings (data URL ใน localStorage) — null = ใช้ภาพเริ่มต้นในตัว
  const [src, setSrc] = useState<string | null>(null);
  // ความสว่างของแบบด์ (ความเข้ม scrim ดำ 0–0.85) — ตั้งใน Settings, default 0 = หน้าตาเดิม
  const [dim, setDim] = useState(HERO_DIM_DEFAULT);
  // ระดับการซูม (ตัวคูณของช่วงซูม 0–2) — ตั้งใน Settings, default 1 = ค่าที่ออกแบบไว้
  const [zoom, setZoom] = useState(HERO_ZOOM_DEFAULT);
  const custom = src !== null;

  // อ่านรูป/ความสว่าง/ระดับการซูมที่ตั้งไว้ + ฟัง event ให้เปลี่ยนได้ทันทีโดยไม่ต้องรีโหลดหน้า
  // (แยก effect จากตัว GSAP เพราะต้องทำงานแม้ผู้ใช้ปิดอนิเมชัน)
  useEffect(() => {
    const read = () => {
      setSrc(readStoredHero());
      setDim(readStoredHeroDim());
      setZoom(readStoredHeroZoom());
    };
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
        // ระดับการซูมจาก Settings — ตัวคูณของ "ช่วงซูม" ของทุกชั้น (ดู zoomScale)
        //   k = 0 → ทุกชั้นนิ่ง (scale คงที่ = ค่า start) · k = 1 → ค่าเดิมเป๊ะ
        //   k = 2 → ช่วงซูมกว้างเป็น 2 เท่า · สัดส่วนระหว่างชั้นคงเดิมทุกค่า k
        const k = zoom;
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
        // ⚠️ yPercent ต้องเป็นค่าติดลบเท่านั้น (เลื่อนขึ้น) ห้ามเป็นบวกเด็ดขาด
        //    เดิม img เริ่มที่ yPercent: 4 = ขยับ "ลง" 4% ของความสูงแบนด์
        //    scale 1.18 ให้ส่วนเกินข้างละ 9% → พอเลื่อนลง 4% ขอบบนของรูปก็ไป
        //    อยู่ต่ำกว่าขอบจอ ~4% (≈30px ที่แบนด์ 760px) เผยให้เห็นพื้นหลังแอป
        //    (ดำเกือบสนิท) เป็น "ช่องว่างด้านบน" ของแบ็กกราวด์ — ไม่เกี่ยวกับ
        //    การ์ด/ระยะ padding เลย จึงแก้ padding เท่าไรก็ไม่หาย
        //    ค่าใหม่ -2% = รูปเลยขอบบนขึ้นไปเล็กน้อยตลอดเวลา (กันการ์ด/กรอบ
        //    rotateX 5° ที่ทำให้ขอบบนหดเข้าไปด้วย) ส่วนล่างถูก mask จางอยู่แล้ว
        //    จึงไม่เห็นส่วนที่ถูกตัดทิ้ง
        if (imgRef.current) {
          tl.fromTo(
            imgRef.current,
            { scale: 1.18, yPercent: -2, rotateX: 5, transformPerspective: 1100, filter: "brightness(1.06) saturate(1.02)" },
            { scale: zoomScale(1.18, 1.62, k), yPercent: -7, rotateX: 0, filter: "brightness(0.74) saturate(1.3)" },
            0,
          );
        }
        if (glowRef.current) {
          tl.fromTo(
            glowRef.current,
            { scale: 1.06, yPercent: 0, rotateX: 3, transformPerspective: 1100, opacity: 0.75 },
            { scale: zoomScale(1.06, 1.24, k), yPercent: -7, rotateX: 0, opacity: 1 },
            0,
          );
        }
        if (!custom && gridRef.current) {
          tl.fromTo(
            gridRef.current,
            { scale: 1, yPercent: 6, rotateX: 4, transformPerspective: 1100, opacity: 0.55 },
            { scale: zoomScale(1, 1.46, k), yPercent: -12, rotateX: 0, opacity: 0.9 },
            0,
          );
        }
        if (!custom && nearRef.current) {
          tl.fromTo(
            nearRef.current,
            { scale: 1.1, yPercent: 10, rotateX: 2, transformPerspective: 1100, opacity: 0.85 },
            { scale: zoomScale(1.1, 2.3, k), yPercent: -16, rotateX: 0, opacity: 1 },
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
    // zoom  = เปลี่ยน "ช่วงซูม" ของทุกชั้น → timeline ต้องสร้างใหม่ (ค่าถูก bake ตอน build)
    //         ctx.revert() คืน inline style ก่อนเช่นกัน → ไม่มี transform ค้างทับกัน
  }, [custom, zoom]);

  return (
    <section
      ref={rootRef}
      aria-hidden="true"
      className={`zoom-hero pointer-events-none absolute inset-x-0 top-0 h-[clamp(360px,85vh,780px)] overflow-hidden${
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
          // ค่าเริ่มต้นก่อน hydrate / ตอน prefers-reduced-motion: ต้อง "เลยขอบบนขึ้นไป"
          // เท่ากับ timeline (translate ติดลบ) ไม่ใช่ 4% ที่เลื่อนลงจนเห็นเป็นช่องว่าง
          style={{ transform: "scale(1.18) translate3d(0, -2%, 0)" }}
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

        {/* ชั้นนิ่ง: ความสว่างของแบบด์ (slider ใน Settings) — scrim ดำทับภาพ+HUD
            วาง "ใต้" vignette ตั้งใจ: หรี่รูปได้ตามใจ แต่ความเข้มของ vignette
            ที่ช่วยให้การ์ดแถวบนสุดอ่านออกจะไม่ถูกตัดทอนไปด้วย */}
        <div className="zoom-hero-scrim" style={{ background: `rgba(0,0,0,${dim})` }} />

        {/* ชั้นนิ่ง: ขอบมืดด้านบน (การ์ดอ่านออก) — ปลายล่างปล่อยให้ mask ของ
            .zoom-hero จางหายเป็น alpha เอง เพื่อคงแบ็กกราวด์เดิมของแอปไว้ชั้นล่างสุด */}
        <div className="zoom-hero-vignette" />
      </div>
    </section>
  );
}
