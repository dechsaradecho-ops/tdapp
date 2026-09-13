"use client";

import { useEffect, useRef, useState } from "react";

/** Mobile navigation — bottom dock (< md screens)
 *
 *  เขียนเองทั้งหมด (DOM + CSS + SVG filter) — ไม่มี WebGL, ไม่มี dependency
 *
 *  เดิมใช้ package `webgl-liquid-glass` แต่ pill ของมันถูกวาดใน fragment shader
 *  ด้วย `color = vec3(1.0)` (ขาวล้วน) บวกแถบขอบกว้าง ~2px → ได้ "กรอบขาว"
 *  รอบแท็บที่แก้ไม่ได้ (prop activeColor/inactiveColor ของ package มีผลกับตัวอักษร/
 *  ไอคอนใน DOM เท่านั้น ไม่ใช่สี pill) จึงยึดการวาด pill มาไว้เอง: มี pill เดียว
 *  หน้าตาคุมได้ 100% + สปริง/ยืดบี้/chromatic aberration/บิดด้วย SVG filter
 *  ตอนลาก (แบบ FluidGlass ของ React Bits แต่ไม่ใช้ three/.glb)
 *  ได้แถม: ไม่มี WebGL → ไม่เจอบั๊ก destroy()/loseContext กับ StrictMode
 *
 *  Desktop (md+) renders nothing; the inline nav in the header stays.
 *  หน้าตา/ขนาด/ตำแหน่งอยู่ใน .dock-glass* ที่ globals.css
 */
// Monotone stroke icons — สีจาก currentColor ของแท็บ (active = accent, ปกติ = slate)
const ICON = {
  home: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3 10.5 12 3l9 7.5" />
      <path d="M5.5 9.5V20a.5.5 0 0 0 .5.5h12a.5.5 0 0 0 .5-.5V9.5" />
      <path d="M9.5 20.5v-6h5v6" />
    </svg>
  ),
  signal: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8Z" />
    </svg>
  ),
  monitor: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M3.5 20.5h17" />
      <path d="M6.5 20.5V14" />
      <path d="M12 20.5V8.5" />
      <path d="M17.5 20.5V4.5" />
    </svg>
  ),
  logs: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2.5H6.5A1.5 1.5 0 0 0 5 4v16a1.5 1.5 0 0 0 1.5 1.5h11A1.5 1.5 0 0 0 19 20V7.5L14 2.5Z" />
      <path d="M14 2.5V8h5" />
      <path d="M9 13h6M9 17h6" />
    </svg>
  ),
  setting: (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1Z" />
    </svg>
  ),
};

const MENU: { href: string; label: string; icon: keyof typeof ICON }[] = [
  { href: "/", label: "Home", icon: "home" },
  { href: "/signals", label: "Signal", icon: "signal" },
  { href: "/monitor", label: "Monitor", icon: "monitor" },
  { href: "/logs", label: "Logs", icon: "logs" },
  { href: "/settings", label: "Setting", icon: "setting" },
];

/** static export เขียนไฟล์เป็น /signals.html → เทียบ path ตรง ๆ ไม่ได้
 *  (production host rewrite เป็น clean URL อยู่แล้ว จึงต้องรองรับทั้งสองแบบ) */
const normPath = (p: string) => {
  const s = p.replace(/\/index\.html$/, "/").replace(/\.html$/, "");
  return s === "" ? "/" : s;
};

const isActiveHref = (href: string, p: string) =>
  href === "/" ? p === "/" : p === href || p.startsWith(`${href}/`);

// เปลี่ยนแท็บ = นำทางจริง (static export ไม่มี Next router → ใช้ location)
const go = (href: string) => {
  if (normPath(window.location.pathname) === normPath(href)) return;
  window.location.assign(href);
};

/* ---- แผนที่ดิสเพลสเรเดียล (radial displacement map) ------------------------
   feDisplacementMap อ่าน "ทิศทาง + ขนาด" ของการดึงภาพจากค่า R (แกน x) และ G
   (แกน y) ของแผนที่: offset = scale × (ค่า/255 − 0.5) ⇒ 128 = ไม่ดึง
   อยากได้ "เลนส์หยดน้ำแบบ Fluid Glass" (ไม่ใช่แว่นขยาย): กลางวงซูม (~1.3x)
   แล้วบีบอัดแรงขึ้นแบบไม่เป็นเชิงเส้นยิ่งใกล้ขอบยิ่งพุ่ง + ขอบแยกสีรุ้งนุ่ม
   ⇒ สร้างสนามเวกเตอร์เรเดียลเอง (เครื่องหมายลบ = ดึงจุด sample เข้าหากลางวง
     ⇒ เลนส์นูน; เดิมเป็นบวก = ดึงออกนอก ⇒ ภาพหดแบบเลนส์เว้า):
        t = min(r / R, 1)        (r = ระยะจากกลางวง, R = รัศมีวง)
        f = S(t)^(P/2)·(1-D) + D(t)·D   (D = โดมนุ่ม t·(2-t), DOME = 0.5, P = 5.0
                                   → กลางซูม ~1.3x เร่งชันที่ขอบ = หยดน้ำ
                                   เร่งขอบ, D'(1)=0 จึงไม่มีรอยหักที่ขอบวง)
        R = 0.5 − 0.5·(u/r)·f
        G = 0.5 − 0.5·(v/r)·f
   ⇒ ดิสเพลสสโลป ~0.5·2 = 1.0 ที่จุดกลางวง (กลางซูม ~1.3x), โตแบบไม่เป็นเชิงเส้น
     (P/2 = 2.5 กดกลางราบ ยิ่งใกล้ขอบยิ่งชัน), ชันสุดแถบขอบวง แล้ว f อิ่มตัวเป็น 1 พอ r ≥ R
     (แรงสุดพอดีที่ขอบวง ไม่มีรอยกระโดด) — ต่างจากแว่นขยาย (P = 1.0) ที่ขยายเท่ากันทั้งวง
   ขอบวงถูกดึงเข้า ±(scale/2) px: scale G 16, R_px 42 ⇒ ขอบบีบ ~±8px (ลด shift ตำแหน่งลง)
   แต่แยกสี R/G/B 12/16/22 → ขอบแยก ~5px (เพิ่ม shift สี กลางแยกน้อยสุด ไล่พุ่งที่ขอบตาม f)
   (scale จริงถูกคูณด้วย eased alpha ทุกเฟรม — ดู transition ใน render)

   ⚠️ ทำไมต้องวาดเองด้วย canvas: feTurbulence ให้สนามที่ไม่เป็นเรเดียล (บิด
      ทั้งวงเป็นก้อน) และ feDiffuseLighting ก็ให้เรเดียลที่ยอดไม่ตรงขอบ
      (ยอดจริงอยู่ที่ ~0.8R แล้ว "ขึ้นอีกครั้ง" ที่ขอบ filter region) ⇒ ใช้เป็น
      แรมป์ขอบไม่ได้ ทดลองแล้วทั้งคู่ (ดูแผนที่เรเดียลนี้ทำงานจริง: pixel diff
      max 244 เทียบกับตอนไม่ใส่แผนที่)
   ⚠️ feImage + href เป็น data URI ทำงานใน backdrop-filter ของ Chromium ได้
      (แผนที่จากไฟล์เดียวกันก็ได้) ⇒ เลือกวาดใน JS เพื่อไม่ต้องมีไฟล์ภาพใน
      public/ ที่ต้องคอยซิงก์กับ --dock-orb และไม่ผูกกับ basePath ของ deployment
   ⚠️ SPAN ต้องเท่ากับ "ครึ่งหนึ่งของ filter region" ของ #dock-glass-lens
      (x=-20% width=140% ⇒ ครึ่งหนึ่ง = 70% ของกล่อง = 1.4 เท่าของรัศมี)
      ถ้าไม่ตรง แรมป์ f=1 จะไปอิ่มตัวผิดที่ (แรงสุดไม่พอดีที่ขอบวง)          */
const LENS_MAP_N = 256; // 256px + เบลอ 1px ฆ่าขั้นบันได 8-bit → ขอบเลนส์เรียบ ไม่หยัก (160px เดิมเห็นรอยหยักตอนซูม)
const LENS_MAP_P = 5.0; // เลขชี้กำลังหลัง smootherstep (ใช้ P/2 = 2.5): กดกลางให้ราบแล้วเร่งชันแบบไม่เป็นเชิงเส้นยิ่งใกล้ขอบยิ่งพุ่ง (หยดน้ำเร่งขอบ) + อนุพันธ์เป็น 0 ที่ขอบ (ไม่มีรอยหักแบบ min(r,1)^P)
const LENS_MAP_DOME = 0.5; // สัดส่วนโดมนุ่ม D(t)=t*(2-t) — ยกกลางซูมเยอะขึ้น (~1.3x) แต่ P สูงยังกดให้กลางราบแล้วพุ่งที่ขอบแบบไม่เป็นเชิงเส้น; D'(1)=0 จึงไม่มีรอยหักที่ขอบวง
const LENS_MAP_SPAN = 1.4; // ครึ่งหนึ่งของ filter region (หน่วย = รัศมีวง)
const LENS_MAP_WOB = 0.14; // วาร์ปทรงหยดน้ำ: ภาพบิดไม่สมมาตรตามมุม ±14% (ไม่ใช่แค่ shift — เส้นตรงในวงบิดเป็นคลื่นแบบน้ำ)
const buildLensMap = (): string | null => {
  if (typeof document === "undefined") return null; // กัน SSR ตอน build
  const cv = document.createElement("canvas");
  cv.width = LENS_MAP_N;
  cv.height = LENS_MAP_N;
  const ctx = cv.getContext("2d");
  if (!ctx) return null;
  const img = ctx.createImageData(LENS_MAP_N, LENS_MAP_N);
  const d = img.data;
  for (let y = 0; y < LENS_MAP_N; y++) {
    for (let x = 0; x < LENS_MAP_N; x++) {
      const u = (((x + 0.5) / LENS_MAP_N) * 2 - 1) * LENS_MAP_SPAN;
      const v = (((y + 0.5) / LENS_MAP_N) * 2 - 1) * LENS_MAP_SPAN;
      const r = Math.sqrt(u * u + v * v) || 1e-6; // กันหารศูนย์ที่กลางวง
      // smootherstep S(t) ยกกำลัง P/2 ดันความชันไปชิดขอบแบบหยดน้ำ + ผสมโดมนุ่ม D(t)=t*(2-t) เฉพาะพอให้กลางไม่แบน แต่หักเหน้อยสุด
      const t = Math.min(r, 1);
      const s = t * t * t * (t * (t * 6 - 15) + 10);
      const edge = Math.pow(s, LENS_MAP_P / 2);
      const fBase = edge * (1 - LENS_MAP_DOME) + t * (2 - t) * LENS_MAP_DOME;
      // วาร์ปทรงหยดน้ำ (image warp ไม่ใช่ shift): ผันแปร f ตามมุม (พู 3 + พู 5)
      // env = 0 ที่กลาง → พีคแถบกลาง-นอก → 0 ที่ขอบพอดี: กลางไม่เป็นหลุม ขอบต่อเนื่อง
      // (t อิ่มที่ 1 + wobble เท่ากับ 1 ทุกมุมที่ขอบ ⇒ ไม่มีรอยตัดทื่อที่ขอบวง)
      const th = Math.atan2(v, u);
      const env = Math.sin((Math.PI * t) / 2) * (1 - t * t * t * t);
      const wob =
        1 +
        LENS_MAP_WOB *
          env *
          (Math.sin(3 * th + 2.5 * r) * 0.65 +
            Math.sin(5 * th - 3.0 * r + 1.3) * 0.35);
      const f = fBase * wob;
      const i = (y * LENS_MAP_N + x) * 4;
      // ลบ = sample เข้าหากลางวง ⇒ ขยาย (เลนส์นูน); ห้ามกลับเป็นบวก (ภาพจะหด)
      d[i] = Math.round((0.5 - 0.5 * (u / r) * f) * 255);
      d[i + 1] = Math.round((0.5 - 0.5 * (v / r) * f) * 255);
      d[i + 2] = 128; // ไม่ใช้ช่อง B แต่ต้องมีค่า (128 = กลาง)
      d[i + 3] = 255;
    }
  }
  ctx.putImageData(img, 0, 0);
  // เบลอ 1px ฆ่าขั้นบันไดควอนไทซ์ 8-bit (255 ขั้น) — scale 12-22 ขยายขั้นพวกนี้เป็นรอยหยัก/แถบสีที่ขอบวง
  try {
    const soft = document.createElement("canvas");
    soft.width = LENS_MAP_N;
    soft.height = LENS_MAP_N;
    const sctx = soft.getContext("2d");
    if (sctx) {
      (sctx as unknown as { filter: string }).filter = "blur(1px)";
      sctx.drawImage(cv, 0, 0);
      ctx.clearRect(0, 0, LENS_MAP_N, LENS_MAP_N);
      ctx.drawImage(soft, 0, 0);
    }
  } catch (e) {
    void e; // browser ไม่มี canvas filter → ใช้แผนที่ดิบ (ยังดีกว่าไม่มี)
  }
  return cv.toDataURL("image/png");
};

export default function MobileNav() {
  // path = null จนกว่า hydrate เสร็จ (static export prerender ทุกหน้าโดยไม่มี
  // window ⇒ HTML แรกต้องเป็นกลาง ไม่มีแท็บไหน active — ไม่งั้นทุกหน้า paint
  // "Home ฟ้า" ก่อน JS แก้เป็นแท็บจริง = วาบตอนกดเมนูตรง ๆ เช่น signal>monitor)
  // null → เรนเดอร์แรก client ตรงกับ HTML (ไม่มี mismatch) + pill ยังซ่อน
  // (opacity 0) จน sync หลังรู้ path จริงค่อยเผย ⇒ ไม่เคยเห็นแท็บผิด/pill ผิดที่
  const [path, setPath] = useState<string | null>(null);
  const navRef = useRef<HTMLElement>(null);
  // pill = ตัวแก้วตอนพัก / orb = วงกลมแก้วตอนลาก / wake = หางของเหลว
  // rim = ขอบ chromatic aberration (อยู่ใน orb — โผล่เฉพาะตอนลาก)
  const pillRef = useRef<HTMLDivElement>(null);
  const orbRef = useRef<HTMLSpanElement>(null);
  const wakeRef = useRef<HTMLDivElement>(null);
  const rimRRef = useRef<HTMLSpanElement>(null);
  const rimGRef = useRef<HTMLSpanElement>(null);
  const rimBRef = useRef<HTMLSpanElement>(null);
  // disperse = ชั้นแยกสี (แดง/เขียว/น้าเงิน คนละรัศมี) = การหักเหของสี
  const dispRef = useRef<HTMLSpanElement>(null);
  // feImage ในตัวกรองเลนส์ — effect หลักเป็นคนยัด href (data URI) ให้
  const mapRef = useRef<SVGFEImageElement>(null);
  // feDisplacementMap 3 ช่องสีของเลนส์ — render() ตั้ง scale ตาม alpha ทุกเฟรม
  // = transition ของเอฟเฟกต์ (เลนส์ค่อย ๆ นูนตอนเริ่มลาก / ยุบตอนปล่อย ไม่ป๊อป)
  const dispRRef = useRef<SVGFEDisplacementMapElement>(null);
  const dispGRef = useRef<SVGFEDisplacementMapElement>(null);
  const dispBRef = useRef<SVGFEDisplacementMapElement>(null);
  // warpRef = ดิสเพลสขั้นที่ 2 (turbulence wobble ใน #dock-glass-lens) — วาร์ปภาพจริง
  // ในหยดน้ำ (เส้นตรงบิดเป็นคลื่น) ไม่ใช่แค่ shift/แยกสีของขั้น radial
  const warpRef = useRef<SVGFEDisplacementMapElement>(null);
  // ให้ effect หลัก (deps []) วัดตำแหน่งใหม่ได้เมื่อแท็บ active เปลี่ยน
  const syncRef = useRef<(() => void) | null>(null);

  // Track current path so the active tab is highlighted.
  useEffect(() => {
    setPath(normPath(window.location.pathname));
    const onPop = () => setPath(normPath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // null (prerender/hydrate ครั้งแรก) หรือ path ที่ไม่มีแท็บตรง → -1 = เป็นกลาง
  // (ไม่ mark แท็บไหน + ไม่เผย pill) กันวาบแท็บผิดก่อนรู้ path จริง
  const activeIdx =
    path == null ? -1 : MENU.findIndex((l) => isActiveHref(l.href, path));

  // แท็บ active เปลี่ยน → ย้าย pill ไปแท็บใหม่ (position พักเปลี่ยน)
  useEffect(() => {
    syncRef.current?.();
  }, [activeIdx]);

  // ---- ฟิสิกส์ของเหลวของ pill/orb (สปริง + ยืดบี้ + chromatic aberration + warp) ----
  // หัวใจของ "แก้วเหลว" แบบ FluidGlass: pill ไม่ได้กระโดดตามนิ้ว แต่ "ไหล" ตาม
  // ด้วยสปริง underdamped (ตามช้าแล้วส่ายเข้าที่) + ยืดตามความเร็ว + ขอบสีเพี้ยน
  // คิดเป็น px/วินาที ทั้งหมด → เฟรมเรตเท่าไรก็ให้ความรู้สึกเดียวกัน
  // ตอนลาก pill แคปซูลจะจางหาย แล้วมี "วงกลมแก้ว" (orb) โผล่แทน: ขอบฟุ้ง
  // + backdrop-filter หักเหฉากหลัง + แถบแสงหักเห/ประกายบิดด้วย SVG filter 2 ความถี่
  //
  // ทำไมไม่ใช้ FluidGlass ตรง ๆ: มันไม่มี drag interaction เลย (mode = lens/cube/
  // bar) และต้องใช้ three + @react-three/fiber (peer React 19) + ไฟล์ .glb ที่
  // ไม่ได้แจกมา → ทำเฉพาะ "ความรู้สึก" ที่เป็นหัวใจด้วยสปริง + SVG filter แทน
  useEffect(() => {
    const nav = navRef.current;
    const pill = pillRef.current;
    const orb = orbRef.current;
    const wake = wakeRef.current;
    const rimR = rimRRef.current;
    const rimG = rimGRef.current;
    const rimB = rimBRef.current;
    const disp = dispRef.current;
    if (!nav || !pill || !orb || !wake || !rimR || !rimG || !rimB || !disp) return;

    // ผู้ใช้ที่ปิดอนิเมชัน → pill ยัง mark แท็บ active แต่นิ่ง ไม่มีสปริง/การลาก
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    // แผนที่ดิสเพลสของเลนส์: วาดเป็น bitmap แล้วยัดเป็น data URI ให้ <feImage>
    // (ต้องตั้ง href "ก่อน" ติดคลาสที่ทำให้ตัวกรองถูกใช้จริง ไม่งั้นเฟรมแรก
    //  ตัวกรองจะไม่เห็นแผนที่)
    const mapEl = mapRef.current;
    const mapHref = buildLensMap();
    if (mapEl && mapHref) {
      mapEl.setAttribute("href", mapHref);
      mapEl.setAttributeNS(
        "http://www.w3.org/1999/xlink",
        "xlink:href",
        mapHref,
      );
    }

    // เลนส์บิดเบี้ยวภาพหลังแก้ว (backdrop-filter: url()) เปิดตามความสามารถจริง
    // ของ browser เท่านั้น — เช็คแค่ CSS.supports ไม่กรอง UA
    //
    // (เดิมเคยกั้น Samsung Internet ไว้ เพราะบทเรียนเก่าที่ใส่ url() บน <nav>
    //  ทั้งแถบแล้ว Samsung render เป็นเฟรมซ้อน ตอนนี้ url() ย้ายมาอยู่บน
    //  .dock-glass__orb ซึ่ง opacity:0 ตอนพักและโผล่แค่ 84px ตอนลาก
    //  พื้นที่เสี่ยงจึงเหลือน้อยมาก — เลือกแลกให้ Samsung ได้เอฟเฟคนี้ไปด้วย)
    // ถอดคลาสออกเมื่อไร วงกลมกลับไปใช้ blur() ธรรมดาทันที ไม่มีอะไรเสียหาย
    const lensOk =
      !!mapHref &&
      typeof CSS !== "undefined" &&
      typeof CSS.supports === "function" &&
      (CSS.supports("backdrop-filter", 'url("#dock-glass-lens")') ||
        CSS.supports("-webkit-backdrop-filter", 'url("#dock-glass-lens")'));
    if (lensOk) nav.classList.add("dock-glass--lens");

    // pill ยังไม่เผยตรงนี้ — รอ sync หลังรู้ path จริง (ดู revealed ใน sync)
    // ไม่งั้นหน้า /signals, /monitor จะเห็น pill ที่ Home ก่อนหนึ่งเฟรม
    orb.style.opacity = "0";
    wake.style.opacity = "0";
    rimR.style.opacity = "0";
    rimG.style.opacity = "0";
    rimB.style.opacity = "0";
    disp.style.opacity = "0";

    const H = 44; // ต้องตรงกับ height ของ .dock-glass__pill ใน globals.css
    const INSET = 3; // ระยะห่างซ้าย/ขวาของ pill จากขอบแท็บ
    const ORB = 84; // ต้องตรงกับ --dock-orb ของ .dock-glass
    // ยอมให้วงกลมล้นขอบ dock ได้นิดหน่อย: แนวนอนเพื่อให้ไปถึงกลางแท็บสุดท้าย
    // (pill กว้าง ~65 → ครึ่งวง 42 จะเลยขอบ) แนวตั้งให้ "ไหล" ตามนิ้วออกนอกได้
    // ⚠️ เพดานกันวงล้นขอบจอ (เคยหลุดขอบขวาในภาพถ่าย): ขอบซ้าย/ขวาของ dock ห่าง
    // ขอบ viewport ข้างละ ~12px (width = 100vw − 1.5rem จัดกึ่งกลาง) ⇒ OVER_X
    // ต้อง ≤ 12 ไม่งั้นขอบวงเลยจอ (OVER_X = 18 เคยล้น ~6px) — ใช้ 8 เหลือร่น 4px
    // แนวตั้งไม่สมมาตร: ด้านบนที่ว่างเยอะให้ 34px ได้ แต่ด้านล่าง dock ห่างก้นจอ
    // แค่ ~12px ⇒ ลากลงเกิน 12px วงจะจมขอบจอ — แยกบน/ล่างแทนค่าเดียว
    const OVER_X = 8;
    const OVER_TOP = 34;
    const OVER_BOTTOM = 10;

    // สปริงตัวหลัก: underdamped (ζ = 0.88) → ตามนิ้วช้าแล้วส่ายเข้าที่นิดหนึ่ง
    // = ความ "เหลว/หนืด" แบบ FluidGlass แต่เฟรมเรตอิสระ
    const K = 289; // ω² = 17²
    const C = 29.92; // 2ζω = 2 × 0.88 × 17
    // สปริงตัวตาม (wake): over-damped (ζ = 1.05) → ไม่ส่าย ตามหลังเสมอ = หางของเหลว
    const KW = 81; // 9²
    const CW = 18.9; // 2 × 1.05 × 9

    const main = { p: 0, v: 0 };
    const mainY = { p: 0, v: 0 };
    const tail = { p: 0, v: 0 };
    const tailY = { p: 0, v: 0 };
    // โมเมนตัมนิ้วแบบ smoothed: กรอง velocity สปริงอีกชั้น (low-pass ~100ms)
    // ⇒ รูปทรงหยดน้ำแปรผันตาม "แรงลาก" ไม่ใช่ค่าดิบรายเฟรม ทุกการเปลี่ยน
    // ค่อย ๆ morph เห็น transform ชัด ไม่กระตุก/ป๊อป; ang ไล่แบบ shortest-arc
    const mom = { x: 0, y: 0, spd: 0, ang: 0 };

    let navH = 56;
    let pillW = 56;
    let restX = 0; // จุดกึ่งกลาง pill (พิกัดใน <nav>)
    let restY = 28; // จุดกึ่งกลางแนวตั้ง — pill ใช้ top:0 จึงต้องเป็น "กลาง" ไม่ใช่ offset
    let targetX = 0;
    let targetY = 28;
    let alpha = 0; // ความเข้มของ layer ของเหลว (wake/rim/warp) ตอนลาก
    let targetAlpha = 0;
    let dragging = false;
    let moved = false;
    // เผย pill แล้วหรือยัง — true ก็ต่อเมื่อ sync เห็น aria-current (รู้ path จริง)
    // กัน pill โผล่ที่แท็บผิดก่อน hydrate (ดู activeIdx = -1 ตอน path == null)
    let revealed = false;
    // ระยะลากสูงสุดจากจุดกด (px, client coords) — onUp ใช้แยก "tap/สะกิดโดน"
    // (นิ้วสั่น 10-24px) ออกจาก "ลากจริง" (ตั้งใจลากไปแท็บอื่น)
    let maxDrag = 0;
    // press = น้ำหนักกดตอนแตะค้าง (0→1 นุ่ม): pill ยุบเล็กน้อยให้เห็น transform
    // ตั้งแต่จังหวะกด ไม่ต้องรอลาก — tap ตรง ๆ ก็มี feedback ก่อน navigate
    let press = 0;
    let targetPress = 0;
    // หน่วง navigate หลังเริ่ม transform (tap-slide / drag-morph-back)
    // ให้เห็นการเปลี่ยนทรงก่อนย้ายหน้าจริง — ไม่ใช่ location.assign ทันที
    let navTimer = 0;
    let startX = 0;
    let startY = 0;
    // จุดกดนิ้ว (พิกัดใน <nav>) — อ้างอิงทิศลากจริง (นิ้วอยู่ไหนเทียบจุดกด)
    // ⚠️ เดิมใช้ target-rest (จุดพัก = กลางแท็บ active) ⇒ กดแท็บ Monitor บนหน้า
    // Home แล้วลากซ้าย/ขวา เวกเตอร์ชี้จาก Home→Monitor เหมือนกันทั้งคู่
    // (วัดจริง dang -11.1° vs -7.6° แทบไม่ต่าง) ⇒ รุ้งไม่ตามนิ้ว
    let startLocalX = 0;
    let startLocalY = 28;
    let raf = 0;
    let last = 0;
    let clickTimer = 0;

    const clamp = (v: number, lo: number, hi: number) =>
      v < lo ? lo : v > hi ? hi : v;

    const tabs = () =>
      Array.from(nav.querySelectorAll<HTMLButtonElement>(".dock-glass__tab"));

    const centreOf = (i: number) => {
      const b = tabs()[i];
      if (!b) return restX;
      const nr = nav.getBoundingClientRect();
      const r = b.getBoundingClientRect();
      return r.left - nr.left + r.width / 2;
    };

    const activeTabIdx = () =>
      tabs().findIndex((b) => b.getAttribute("aria-current") === "page");

    // วัดกล่อง <nav> + ความกว้างแท็บจริง → pill ตรงแท็บทุกขนาดจอโดยไม่ต้อง
    // hard-code ความกว้าง (nav จัดกึ่งกลางจอ ขนาดเปลี่ยนตาม viewport)
    const measure = () => {
      const nr = nav.getBoundingClientRect();
      navH = nr.height;
      restY = navH / 2;
      const btn =
        nav.querySelector<HTMLButtonElement>('[aria-current="page"]') ??
        tabs()[0];
      if (btn) {
        const r = btn.getBoundingClientRect();
        pillW = Math.max(36, r.width - INSET * 2);
        pill.style.width = `${pillW}px`;
        // wake/orb เป็นวงกลมขนาดคงที่ (var(--dock-orb)) — ไม่ตั้งขนาดจาก JS
        restX = r.left - nr.left + r.width / 2;
      }
      return nr;
    };

    // เขียนผลลง DOM — เรียกจาก rAF (ส่ง dt มาด้วย), ตอน snap และตอน resize
    const render = (dtRaw?: number) => {
      const dt = Math.min(Math.max(dtRaw ?? 1 / 60, 1 / 240), 0.05);
      // ---- โมเมนตัมนิ้วแบบ smoothed (หัวใจโจทย์รอบนี้) ----
      // velocity ดิบของสปริงกระโดดรายเฟรม (pointermove มาเป็นก้อน + spring
      // overshoot) ⇒ เอามายืดหยดน้ำตรง ๆ จะกระตุก/ป๊อป มองไม่เห็น transform
      // ⇒ กรอง low-pass อีกชั้น (TAU ~90ms magnitude / ~120ms angle):
      // ลากเร็ว = หยดน้ำค่อย ๆ ยืดตามทิศแรง, ผ่อน/หยุด = ค่อย ๆ หดกลับเป็นวงกลม
      // เห็น morph ชัดทุกการเปลี่ยน (flick แรง ๆ เห็นหดกลับ ~300ms)
      const rawVmag = Math.sqrt(main.v * main.v + mainY.v * mainY.v);
      const kMag = 1 - Math.exp(-dt / 0.09);
      mom.x += (main.v - mom.x) * kMag;
      mom.y += (mainY.v - mom.y) * kMag;
      const momMag = Math.sqrt(mom.x * mom.x + mom.y * mom.y);
      // squash & stretch ตามโมเมนตัม (จำกัดเพดานไม่ให้บิดเกิน) — หัวใจของ "ของเหลว"
      // ใช้ velocity 2 มิติ (x + y) — หยดน้ำกระเจิงแสงตาม "ทิศที่ลากจริง"
      // ไม่ใช่แค่แนวนอน (เดิมอ่านแค่ main.v ⇒ ลากขึ้นลงไม่มีผล)
      const spd = Math.min(momMag / 3200, 0.25);
      const spdN = Math.min(momMag / 3200, 1); // 0→1 normalized (spd = spdN*0.25)
      // ทิศเป้าหมาย: เคลื่อนเร็วใช้ทิศ velocity; ช้าใช้ทิศ "นิ้วอยู่ตรงไหนเทียบ
      // จุดกด" (ถือค้างรุ้งยังค้างตามนิ้ว); นิ่งสนิทคงทิศเดิม (ไม่ snap กลับ 0°)
      // ⚠️ ต้องเทียบจุดกด ไม่ใช่จุดพัก (rest = กลางแท็บ active — กดแท็บเดียว
      // แล้วลากซ้าย/ขวา เวกเตอร์ rest เหมือนกันทั้งคู่ ⇒ รุ้งไม่ตามนิ้ว)
      const tdx0 = targetX - startLocalX;
      const tdy0 = targetY - startLocalY;
      const tmag0 = Math.sqrt(tdx0 * tdx0 + tdy0 * tdy0);
      const targetAng =
        rawVmag > 40
          ? Math.atan2(mainY.v, main.v)
          : tmag0 > 8
            ? Math.atan2(tdy0, tdx0)
            : mom.ang;
      // ไล่มุมแบบ shortest-arc (ไม่หมุนอ้อม 350°→10°) + ช้ากว่า magnitude นิด
      // ⇒ เปลี่ยนทิศกระทันหัน (สะบัดนิ้วกลับ) หยดน้ำค่อย ๆ หมุนตาม ไม่วาร์ป
      // ปล่อยนิ้วแล้ว + ความเร็วต่ำ ⇒ ค่อย ๆ ดึงมุมกลับ 0° (แนวนอน) ให้เห็น
      // transform กลับตรง — ไม่งั้นกดลากเฉียงที่เมนูเดิมแล้วปล่อย mom.ang จะค้าง
      // ค่าสุดท้าย (เช่น ~90° จากการลากขึ้น) pill ค้างเอียงไม่กลับปกติ
      if (!dragging && rawVmag <= 40) {
        const toZero =
          ((0 - mom.ang + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
        mom.ang += toZero * (1 - Math.exp(-dt / 0.18));
      } else {
        const dAng =
          ((targetAng - mom.ang + Math.PI * 3) % (Math.PI * 2)) - Math.PI;
        mom.ang += dAng * (1 - Math.exp(-dt / 0.12));
      }
      mom.spd = spd;
      // dx/dy ไม่ต้องแยก — orb/pill/wake rotate(ang) ทั้งก้อนแล้ว ส่วนลูกใน orb
      // ใช้ local-x อย่างเดียว (ดู rim/disp ข้างล่าง) จึงเหลือแค่ angDeg
      const angDeg = ((mom.ang * 180) / Math.PI).toFixed(1);
      // ปริมาณการกระเจิง: ผสมความเร็วกับระยะลาก (อย่างใดอย่างหนึ่งแรงก็กระเจิง)
      // chromatic aberration: ขอบแดง/เขียว/น้าเงินเยื้องออกตามความเร็วการลาก
      // (การหักเหของสี — ยิ่งลากเร็ว สีแยกออกจากกันยิ่งชัด)
      const ca = 0.8 + spd * 16;

      // morph ทรง capsule<->circle (หัวใจโจทย์รอบนี้): เดิม pill (แคปซูล) กับ
      // orb (วงกลม) crossfade สวนกันคนละทรง ⇒ ตาเห็นเป็น "วาร์ป" ตอนเปลี่ยน
      // ⇒ ผูกทรงทั้งคู่เข้ากับ morphT (smootherstep ของ alpha ตัวเดียวกับเลนส์):
      //   morphT=0 ทั้งคู่เป็นแคปซูลเท่า pill / morphT=1 ทั้งคู่เป็นวงกลม 84px
      // กลางทางทรงตรงกัน crossfade จึง seamless — ไป/กลับเห็น transform เดียวกัน
      const morphT = alpha * alpha * alpha * (alpha * (alpha * 6 - 15) + 10);
      // press ยุบตอนแตะค้าง (smoothstep) — tap ตรง ๆ ก็เห็น transform ตั้งแต่กด
      const pressE = press * press * (3 - 2 * press);
      const ps = 1 - 0.07 * pressE;
      // pill ที่พักต้องตรงเสมอ (0°): เกทมุมเอียงด้วย morph + ความเร็ว — พักนิ่ง
      // (morphT=0/spd=0) ได้ 0° เป๊ะแม้ mom.ang ค้างจากการลากเฉียง, ตอนลากได้เต็ม
      // orb/wake ด้านล่างยังใช้ angDeg เต็ม (จางหายพร้อม alpha อยู่แล้ว)
      const pillTiltW = Math.min(1, morphT + spdN * 0.5);
      const pillAngDeg = ((mom.ang * pillTiltW * 180) / Math.PI).toFixed(1);
      // pill (แคปซูล) ยืด/บี้ "ตามทิศโมเมนตัม": rotate ไปตาม ang แล้ว
      // scale แกนยาวตามแรงลาก — flick แรงเห็นยืดชัด ผ่อนเห็นหดกลับนุ่ม
      const sx = 1 + spd * 0.9;
      const sy = 1 - spd * 0.45;
      const pillMorphW = pillW + (ORB - pillW) * morphT;
      const pillMorphH = H + (ORB - H) * morphT;
      const pillSX = ((pillMorphW / pillW) * sx * ps).toFixed(3);
      const pillSY = ((pillMorphH / H) * sy * ps).toFixed(3);
      pill.style.transform =
        `translate3d(${(main.p - pillW / 2).toFixed(2)}px,${(mainY.p - H / 2).toFixed(2)}px,0)` +
        ` rotate(${pillAngDeg}deg) scale(${pillSX},${pillSY})`;
      // ยังไม่รู้ path จริง (revealed=false) → ซ่อน pill ไว้ก่อน กันโผล่ผิดที่
      pill.style.opacity = revealed ? (1 - alpha).toFixed(3) : "0";

      // orb (หยดน้ำ) — ทรงแปรผันตามโมเมนตัม: ยืดตามทิศแรง + บีบขวาง
      // (teardrop morph: หน้าโป่ง-หลังเรียวผ่าน wake ที่ลากหางสวนทาง)
      // rotate+scale ใน transform เดียว ⇒ ทุกเฟรม morph ต่อเนื่อง เห็น transform
      const elong = spd * 1.5; // spd≤0.25 ⇒ ยืดสุด ~1.38x (ไม่ฉีกเป็นวงรี)
      const osx = 1 + elong;
      const osy = 1 - elong * 0.55;
      // orb เริ่มจากทรงแคปซูลเท่า pill (morphT=0) แล้วค่อยเป็นวงกลม+ยืดตามแรง
      // (morphT=1) — ครึ่งทางทรงตรงกับ pill พอดี crossfade จึงไม่วาร์ป
      const orbSX = (pillW / ORB + (osx - pillW / ORB) * morphT).toFixed(3);
      const orbSY = (H / ORB + (osy - H / ORB) * morphT).toFixed(3);
      orb.style.transform =
        `translate3d(${(main.p - ORB / 2).toFixed(2)}px,${(mainY.p - ORB / 2).toFixed(2)}px,0)` +
        ` rotate(${angDeg}deg) scale(${orbSX},${orbSY})`;
      orb.style.opacity = alpha.toFixed(3);

      // wake = หางหยดน้ำ: ทอดสวนทางโมเมนตัม (หางยาวตามแรง + จางตาม alpha)
      // ตำแหน่งตาม tail (over-damped ตามหลังเสมอ) + ยืดตามทิศ ang เดียวกัน
      const wakeLen = 0.5 + spd * 1.6;
      const wakeWid = 0.62 - spd * 0.5;
      wake.style.transform =
        `translate3d(${(tail.p - ORB / 2).toFixed(2)}px,${(tailY.p - ORB / 2).toFixed(2)}px,0)` +
        ` rotate(${angDeg}deg) scale(${wakeLen.toFixed(3)},${Math.max(wakeWid, 0.3).toFixed(3)})`;
      wake.style.opacity = (alpha * (0.3 + spdN * 0.35)).toFixed(3);

      const rimA = Math.min(alpha * 1.5, 1).toFixed(3);
      rimR.style.opacity = rimA;
      rimG.style.opacity = rimA;
      rimB.style.opacity = rimA;
      // แยกสีแบบปริซึม "ตามทิศลาก" (ไม่สมมาตร): แดงสวนทางนิ้ว / น้าเงินตามนิ้ว /
      // เขียวขยายวงตรงกลาง — หยดน้ำจริงกระเจิงแรงสุดตามแนวเคลื่อนที่ ไม่ใช่ซ้ายขวาเสมอ
      // ⚠️ shift บูสต์รอบแรงต่อเนื่อง 1.2–5.0 → 1.4–5.8px: แยกสีชัดขึ้น แต่ยัง
      // ต่ำกว่า ~6px ที่เคยหลุดเป็นขอบซ้อน
      // rim อยู่ใน orb ที่ rotate(ang) แล้ว ⇒ offset ต้องสั่งใน "local axis"
      // (แกน x หลังหมุน = ทิศโมเมนตัมบนจอ) ไม่งั้นจอได้มุม 2×ang
      // ⇒ แดงถอยหลัง/น้าเงินนำหน้าตามแนวลากพอดี ไม่หมุนเกิน
      const rimShift = 1.4 + spd * 17;
      rimR.style.transform = `translate3d(${(-rimShift).toFixed(2)}px,0px,0)`;
      rimG.style.transform = `scale(${(1 + ca * 0.0035).toFixed(4)})`;
      rimB.style.transform = `translate3d(${(rimShift).toFixed(2)}px,0px,0)`;

      // ชั้นหักเหของสี: จาง-เข้มตาม alpha และ "แยกสี" ตามความเร็ว (--sp 0→1)
      // --sdx/--sdy = เยื้องวงแหวนแดง/น้าเงินสวนกันตามทิศลาก (เขียวเป็นอ้างอิงกลาง)
      // ปริมาณ = ความเร็ว (spd) + ระยะลากจากจุดพัก ⇒ ถือค้างไว้เฉย ๆ ก็ยังกระเจิง
      const dragDist = Math.min(
        Math.sqrt(
          (targetX - startLocalX) * (targetX - startLocalX) +
            (targetY - startLocalY) * (targetY - startLocalY),
        ) / 120,
        1,
      );
      const sway = spdN * 0.6 + dragDist * 0.4;
      disp.style.opacity = Math.min(alpha * 1.15, 1).toFixed(3);
      disp.style.setProperty("--sp", spdN.toFixed(3));
      // disp อยู่ใน orb ที่ rotate แล้วเช่นกัน ⇒ เยื้องแค่แกน x local
      // (จอ = ทิศโมเมนตัม), --sdy = 0 ไม่ต้องขยับ 2 แกนซ้อน
      disp.style.setProperty("--sdx", (-sway * 5.6).toFixed(2) + "px");
      disp.style.setProperty("--sdy", "0px");
      // รุ้ง/แสงขอบ (conic `from`) ก็อยู่ใต้ orb ที่ rotate(ang) แล้ว
      // ⇒ ล็อก --dang = 0 ให้จอได้ from = ang พอดี (ไม่บวกซ้ำเป็น 2×ang)
      // ทิศเข้มสุดยังตามโมเมนตัมเหมือนเดิม แค่ไม่หมุนเกิน
      disp.style.setProperty("--dang", "0deg");

      // transition ของเลนส์: scale ดิสเพลสโตตาม alpha แบบ smootherstep
      // (0→เต็มใน ~1/9 วินาที เท่าความเร็ว alpha) ⇒ เริ่มลากเลนส์ค่อย ๆ นูน
      // ปล่อยนิ้วค่อย ๆ ยุบ ไม่ป๊อป — setAttribute ต่อเฟรมบน SVG attribute
      // ไม่ผ่าน CSS transition (SVG presentation attribute ไม่มี transition)
      // ลด shift ตำแหน่ง (17/20/24 → 12/16/22: ขอบบีบ ±6/±8/±11px) แต่เพิ่ม shift สี
      // (แยก R/G/B จาก 7 → 10px: กลางแยกน้อยสุด ไล่แรงขึ้นยิ่งใกล้ขอบยิ่งพุ่งแบบไม่เป็นเชิงเส้น)
      const ae = alpha * alpha * alpha * (alpha * (alpha * 6 - 15) + 10);
      if (dispRRef.current)
        dispRRef.current.setAttribute("scale", (12 * ae).toFixed(2));
      if (dispGRef.current)
        dispGRef.current.setAttribute("scale", (16 * ae).toFixed(2));
      if (dispBRef.current)
        dispBRef.current.setAttribute("scale", (22 * ae).toFixed(2));
      // วาร์ปภาพ (image warp ไม่ใช่ shift): ฐาน 9px + บูสต์ตามโมเมนตัมถึง ~18px × ae
      // ⇒ ถือค้างนิ่ง ๆ ภาพบิดนุ่ม ลากเร็วคลื่นแรงขึ้นแบบน้ำจริง
      // (spdN มาจาก mom ที่กรองแล้ว — คลื่นขึ้น/ลงนุ่ม ไม่กระชากตาม pointermove)
      if (warpRef.current)
        warpRef.current.setAttribute("scale", ((9 + 9 * spdN) * ae).toFixed(2));

      // บิดเฉพาะตอนลาก (toggle = no-op ถ้าสถานะเดิม → ไม่ repaint ซ้ำทุกเฟรม)
      pill.classList.toggle("is-fluid", alpha > 0.02);
      // is-warping = สถานะ "กำลังลาก" (marker ให้ debug/เทส + ให้ CSS ตรวจสอบได้)
      // ⚠️ ห้ามใช้คลาสนี้ไป "ซ่อน" หรือ "เจาะรู" .dock-glass__frost เด็ดขาด
      //    (ลองแล้วทั้ง display:none และ mask เป็นรู — ผู้ใช้ติทั้งคู่)
      //    แถบต้องเบลอ 20px ทุกพิกเซลตลอดเวลา แล้วให้เลนส์ขยาย "ผิวของแถบ" แทน
      nav.classList.toggle("is-warping", alpha > 0.02);
    };

    const tick = (t: number) => {
      const dt = last ? Math.min((t - last) / 1000, 0.05) : 1 / 60;
      last = t;

      main.v += (K * (targetX - main.p) - C * main.v) * dt;
      main.p += main.v * dt;
      mainY.v += (K * (targetY - mainY.p) - C * mainY.v) * dt;
      mainY.p += mainY.v * dt;

      tail.v += (KW * (targetX - tail.p) - CW * tail.v) * dt;
      tail.p += tail.v * dt;
      tailY.v += (KW * (targetY - tailY.p) - CW * tailY.v) * dt;
      tailY.p += tailY.v * dt;

      alpha += (targetAlpha - alpha) * (1 - Math.exp(-9 * dt));
      if (alpha < 0.004) alpha = 0;
      // press ไล่ตามเป้าแบบนุ่ม (~1/12 วิ) — กดเห็นยุบ ปล่อยเห็นคลาย
      press += (targetPress - press) * (1 - Math.exp(-12 * dt));
      if (Math.abs(targetPress - press) < 0.004) press = targetPress;

      render(dt);

      // หยุด rAF เมื่อนิ่งแล้ว (ไม่กินแบตเตอรี่ตอนไม่ได้ลาก)
      const settled =
        !dragging &&
        alpha === 0 &&
        press === targetPress &&
        Math.abs(main.v) < 4 &&
        Math.abs(tail.v) < 5 &&
        Math.abs(mainY.v) < 4 &&
        Math.abs(tailY.v) < 4;
      if (settled) {
        main.v = 0;
        tail.v = 0;
        mainY.v = 0;
        tailY.v = 0;
        raf = 0;
        return;
      }
      raf = requestAnimationFrame(tick);
    };

    const startLoop = () => {
      if (!raf) {
        last = 0;
        raf = requestAnimationFrame(tick);
      }
    };

    // วาง pill นิ่ง ๆ ที่ตำแหน่งพัก (ตอน mount / resize / แท็บ active เปลี่ยน)
    const snap = () => {
      main.p = restX;
      main.v = 0;
      mainY.p = restY;
      mainY.v = 0;
      tail.p = restX;
      tail.v = 0;
      tailY.p = restY;
      tailY.v = 0;
      mom.x = 0;
      mom.y = 0;
      mom.ang = 0;
      mom.spd = 0;
      press = 0;
      targetPress = 0;
      alpha = 0;
      targetAlpha = 0;
      targetX = restX;
      targetY = restY;
      render();
    };

    const sync = () => {
      measure();
      // รู้แท็บจริงแล้ว (DOM มี aria-current) → ค่อยเผย pill (ครั้งแรกครั้งเดียว)
      // หน้า prerender ที่ path ยัง null จะไม่มี aria-current ⇒ pill ยังซ่อน
      if (!revealed && nav.querySelector('[aria-current="page"]')) {
        revealed = true;
        pill.style.opacity = "1";
      }
      if (!dragging) snap();
    };
    syncRef.current = sync;
    sync();

    // แท็บที่จุดกึ่งกลางใกล้ x ที่สุด (พิกัดใน <nav>)
    const idxAtX = (localX: number) => {
      const nr = nav.getBoundingClientRect();
      let best = 0;
      let bestD = Infinity;
      tabs().forEach((b, i) => {
        const r = b.getBoundingClientRect();
        const d = Math.abs(r.left - nr.left + r.width / 2 - localX);
        if (d < bestD) {
          bestD = d;
          best = i;
        }
      });
      return best;
    };

    // กัน click ที่ browser ยิงหลังลาก (pointerdown/pointerup คนละแท็บ) ไม่ให้
    // นำทางซ้ำ/นำทางผิดแท็บ — ถอน listener ใน 400ms กันค้าง
    const onSwallowClick = (ev: Event) => {
      ev.preventDefault();
      ev.stopPropagation();
    };
    const suppressClick = () => {
      window.addEventListener("click", onSwallowClick, true);
      window.clearTimeout(clickTimer);
      clickTimer = window.setTimeout(() => {
        window.removeEventListener("click", onSwallowClick, true);
        clickTimer = 0;
      }, 400);
    };

    // ย้าย pill ให้เห็น transform ก่อน navigate จริง (tap-slide / drag-morph-back)
    // delay สั้น (~180ms / ~220ms ตามลำดับ) — ไม่วาร์ป เพราะสปริงวิ่งให้เห็นก่อน
    // location.assign ทีหลัง; ถ้า path เดิม (tap แท็บเดิม) ไม่ต้องหน่วง/ไม่ navigate
    const morphNav = (href: string, peakAlpha: number, delayMs: number) => {
      if (normPath(window.location.pathname) === normPath(href)) return;
      suppressClick();
      targetAlpha = peakAlpha;
      targetPress = 0;
      startLoop();
      window.clearTimeout(navTimer);
      navTimer = window.setTimeout(() => {
        navTimer = 0;
        go(href);
      }, delayMs);
    };

    const onDown = (e: PointerEvent) => {
      if (reduce) return;
      if (e.pointerType === "mouse" && e.button !== 0) return;
      const nr = measure();
      dragging = true;
      moved = false;
      maxDrag = 0;
      startX = e.clientX;
      startY = e.clientY;
      startLocalX = e.clientX - nr.left;
      startLocalY = e.clientY - nr.top;
      // กด = เริ่ม press-morph ทันที (pill ยุบ ~7% นุ่ม ๆ): tap ตรง ๆ ก็เห็น
      // transform ตั้งแต่จังหวะกด โดยยังไม่เปิดของเหลว (targetAlpha ค้าง 0)
      // ⇒ tap (signal>monitor) ไม่วาบ แต่มี feedback ก่อน navigate ใน onUp
      targetPress = 1;
      window.clearTimeout(navTimer);
      navTimer = 0;
      startLoop();
    };

    const onMove = (e: PointerEvent) => {
      if (!dragging) return;
      if (!moved) {
        // touch slop ~10px: นิ้วสั่น/สะกิดโดนไม่ติดของเหลว
        // (tap ตรง ๆ signal>monitor มาไม่ถึงจุดนี้ ⇒ ไม่มี orb วาบ)
        if (
          Math.abs(e.clientX - startX) > 10 ||
          Math.abs(e.clientY - startY) > 10
        ) {
          // เริ่มลากจริงครั้งแรก: ค่อยเปิดของเหลว + วิ่ง loop
          moved = true;
          targetAlpha = 1;
          startLoop();
        } else {
          return;
        }
      }
      const nr = nav.getBoundingClientRect();
      targetX = clamp(e.clientX - nr.left, ORB / 2 - OVER_X, nr.width - ORB / 2 + OVER_X);
      targetY = clamp(e.clientY - nr.top, ORB / 2 - OVER_TOP, nr.height - ORB / 2 + OVER_BOTTOM);
      const dd = Math.hypot(e.clientX - startX, e.clientY - startY);
      if (dd > maxDrag) maxDrag = dd;
    };

    const onUp = (e: PointerEvent, cancelled = false) => {
      if (!dragging) return;
      dragging = false;
      targetPress = 0;
      if (cancelled) {
        // gesture ถูกระบบยกเลิก (สายเข้า/overscroll/เบราว์เซอร์ขโมย pointer)
        // ⇒ ห้าม navigate เด็ดขาด — แค่คลายของเหลวแล้วไหลกลับที่พักให้เห็น
        window.clearTimeout(navTimer);
        navTimer = 0;
        targetAlpha = 0;
        targetX = restX;
        targetY = restY;
        startLoop();
        return;
      }
      if (!moved) {
        // tap ตรง ๆ: สไลด์สปริงไปแท็บที่แตะให้เห็น transform (~180ms ตามด้วย
        // morph วงกลมนิด ๆ peak 0.55) แล้วค่อย navigate — ไม่วาร์ปข้ามหน้า
        // ส่วน click ของปุ่มถูกกลืนโดย suppressClick ใน morphNav
        const nrTap = nav.getBoundingClientRect();
        const tapIdx = idxAtX(e.clientX - nrTap.left);
        if (tapIdx !== activeTabIdx() && MENU[tapIdx]) {
          restX = centreOf(tapIdx);
          targetX = restX;
          targetY = restY;
          morphNav(MENU[tapIdx].href, 0.55, 180);
        } else {
          // tap แท็บเดิม: แค่คลาย press ให้เห็นยุบกลับ (ไม่ navigate)
          targetAlpha = 0;
          targetX = restX;
          targetY = restY;
          startLoop();
        }
        return;
      }
      targetY = restY;

      const nr = nav.getBoundingClientRect();
      const idx = idxAtX(e.clientX - nr.left);

      if (idx !== activeTabIdx() && MENU[idx] && maxDrag > 24) {
        // ลากจริง (>24px) แล้วปล่อยเหนือแท็บอื่น → morph กลับเป็น capsule
        // ที่แท็บใหม่ให้เห็น transform (~220ms alpha ค่อย ๆ ลง) แล้วค่อย
        // navigate (browser ไม่ยิง click เองเพราะ down/up คนละ element)
        restX = centreOf(idx);
        targetX = restX;
        morphNav(MENU[idx].href, 0, 220);
      } else if (maxDrag <= 24) {
        // สะกิดโดน/นิ้วสั่น (ขยับแค่ 10-24px): ไม่ใช่การลากจริง → ดับเลเยอร์
        // ของเหลว + วาง pill กลับที่พักทันที (hard settle ไม่สปริงส่าย)
        // กัน orb วาบ + pill กระตุกตอน tap ตรง ๆ (เช่น signal>monitor)
        if (raf) {
          cancelAnimationFrame(raf);
          raf = 0;
        }
        snap();
        return;
      } else {
        // ลากจริงแล้ววกกลับแท็บเดิม: ดับเลเยอร์ของเหลว + สปริงไหลกลับที่พัก
        // (fluid release — morph วงกลมกลับเป็น capsule ให้เห็น transform)
        targetAlpha = 0;
        targetX = restX;
      }
      startLoop();
    };

    const onUpEv = (ev: Event) => onUp(ev as PointerEvent);
    const onCancelEv = (ev: Event) => onUp(ev as PointerEvent, true);

    nav.addEventListener("pointerdown", onDown);
    window.addEventListener("pointermove", onMove, { passive: true });
    window.addEventListener("pointerup", onUpEv);
    window.addEventListener("pointercancel", onCancelEv);
    window.addEventListener("resize", sync);
    window.addEventListener("orientationchange", sync);
    // ต้อง observe <html> ด้วย ไม่ใช่แค่ nav: ตำแหน่งของ nav เปลี่ยนได้โดยขนาดคงเดิม
    // (เช่น scrollbar ของ layout โผล่ → viewport แคบลง → nav ที่จัดกึ่งกลางเลื่อนตาม)
    const ro =
      typeof ResizeObserver !== "undefined" ? new ResizeObserver(sync) : null;
    ro?.observe(nav);
    ro?.observe(document.documentElement);
    // เลย์เอาต์นิ่งช้า (font/รูปโหลดเสร็จ) → sync อีกครั้งหลังโหลดครบ
    window.addEventListener("load", sync);

    return () => {
      if (raf) cancelAnimationFrame(raf);
      window.clearTimeout(clickTimer);
      window.clearTimeout(navTimer);
      window.removeEventListener("click", onSwallowClick, true);
      nav.removeEventListener("pointerdown", onDown);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUpEv);
      window.removeEventListener("pointercancel", onCancelEv);
      window.removeEventListener("resize", sync);
      window.removeEventListener("orientationchange", sync);
      window.removeEventListener("load", sync);
      nav.classList.remove("dock-glass--lens");
      nav.classList.remove("is-warping");
      ro?.disconnect();
      if (syncRef.current === sync) syncRef.current = null;
    };
  }, []);

  return (
    <div className="md:hidden">
      <nav ref={navRef} aria-label="เมนูหลัก" className="dock-glass">
        {/* frost = เบลอฉากหลังของแถบ dock (ย้ายมาจาก backdrop-filter ของ <nav>)
            ⚠️ ห้ามย้ายกลับไปที่ <nav> และห้ามเป็น pseudo-element:
               • backdrop-filter บน nav = backdrop root ⇒ เลนส์ของ orb เห็นภาพเรียบ
                 ⇒ displacement ไม่เกิด (วัดจาก pixel diff แล้ว)
               • pseudo-element + backdrop-filter = ใช้ไม่ได้บน Samsung Internet
            เป็นลูกตัวแรกสุดเพื่อให้วาดใต้ wake/pill/แท็บ/orb — แถบนี้ต้องเบลอ
            20px ทุกพิกเซลตลอดเวลา (ห้ามซ่อน/ห้ามเจาะรู ดูเหตุผลใน globals.css) */}
        <span className="dock-glass__frost" aria-hidden="true" />

        {/* filter defs ต้องอยู่ใน DOM จริง (ห้าม display:none) เหมือน #lg-refract
            ใน layout.tsx ไม่งั้นบาง browser จะไม่ resolve filter ให้ */}
        <svg
          aria-hidden="true"
          focusable="false"
          width="0"
          height="0"
          style={{ position: "absolute" }}
        >
          <defs>
            {/* คลื่นยาวความถี่เดียว (octave 1) + แอมพลิจูดน้อย → บิดนุ่ม
                octave 2 เดิมมีความถี่สูงซ้อน ทำให้ hairline 1px เป็นคลื่นถี่
                อ่านเป็น "รอยหยัก" ตอนซูม ⇒ ตัดออก + เบลอแรงขึ้น + scale 6→4 */}
            <filter
              id="dock-glass-warp"
              x="-40%"
              y="-40%"
              width="180%"
              height="180%"
              colorInterpolationFilters="sRGB"
            >
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.008 0.01"
                numOctaves="1"
                seed="71"
                result="noise"
              />
              <feGaussianBlur in="noise" stdDeviation="8" result="soft" />
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="4"
                xChannelSelector="R"
                yChannelSelector="G"
              />
            </filter>

            {/* คลื่นประกายในเนื้อแก้ว + "การหักเหของสี": แยกช่องสี R/G/B
                แล้วดิสเพลสคนละสเกล (แดงน้อยสุด → น้าเงินมากสุด เหมือนแก้วจริง)
                แล้ว screen กลับ — ได้ประกายที่มีขอบสีรุ้งในเนื้อแก้ว
                ใช้กับ "เนื้อใน" เท่านั้น จึงไม่ทําให้เส้นรอบวงหยัก */}
            <filter
              id="dock-glass-caustic"
              x="-30%"
              y="-30%"
              width="160%"
              height="160%"
              colorInterpolationFilters="sRGB"
            >
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.032 0.038"
                numOctaves="2"
                seed="53"
                result="noise"
              />
              <feGaussianBlur in="noise" stdDeviation="3.5" result="soft" />
              {/* red */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="2"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispR"
              />
              <feColorMatrix
                in="dispR"
                type="matrix"
                values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chR"
              />
              {/* green */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="4"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispG"
              />
              <feColorMatrix
                in="dispG"
                type="matrix"
                values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chG"
              />
              {/* blue */}
              <feDisplacementMap
                in="SourceGraphic"
                in2="soft"
                scale="6"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dispB"
              />
              <feColorMatrix
                in="dispB"
                type="matrix"
                values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0"
                result="chB"
              />
              <feBlend
                in="chR"
                in2="chG"
                mode="screen"
                result="chRG"
              />
              <feBlend in="chRG" in2="chB" mode="screen" />
            </filter>

            {/* เลนส์บิดเบี้ยว "ภาพหลังแก้ว": คนละหน้าที่กับ 2 ตัวบน
                - dock-glass-warp/caustic = บิด "แถบแสงที่เราวาดเอง" (filter:)
                - dock-glass-lens        = บิด "ภาพจริงที่อยู่ข้างหลังวงกลม"
                                           (ใช้ผ่าน backdrop-filter: ของ orb)

                ⚙️ กลไก: feImage (แผนที่เรเดียลที่วาดด้วย canvas ใน MobileNav)
                   → feDisplacementMap ⇒ ดึงภาพฉากหลังเป็นแนวเรเดียล
                   อ่อนที่กลางวง → แรงสุดที่ขอบวง (ดูสูตร/เหตุผลใน buildLensMap)
                   วัดผลในหน้านี้จริง: scale 26 ให้ pixel diff สูงสุด 244
                   (ต่างกันเป็นวงชัดเจน) เทียบกับตอนไม่ใส่แผนที่ (mean 0.11/max 7)

                ⚠️⚠️ backdrop-filter ต้องมี url() "ตัวเดียว" เท่านั้น
                   วัดแล้ว (harness + diff พิกเซล): ถ้ามี blur()/saturate() ปนอยู่
                   Chromium จะทิ้ง url() ทั้งตัว → ได้แค่ blur เฉย ๆ ไม่มีการบิด
                   (cfg sweep 5 ค่า baseFrequency + สลับลำดับ blur/lens → เหมือนเดิม)
                   ⇒ ห้ามใส่ blur() ลงใน backdrop-filter ของ orb เด็ดขาด
                   (ผู้ใช้ระบุชัด: วงกลมต้องไม่เบลอ ใส่แค่ distortion)

                ⚠️⚠️ ตัวที่จะทำให้ทุกอย่างข้างบนไม่เกิดผลเลย: backdrop-filter บน <nav>
                   (หรือบรรพบุรุษใด ๆ ของ orb) — จะสร้าง backdrop root ทำให้ที่นี่
                   รับภาพที่กรองแล้วของ nav (= เรียบ) ⇒ ตัวกรองทำงานแต่
                   feDisplacementMap เห็นภาพเรียบจึงไม่ปรากฏ (วัดแล้ว: เปลี่ยน
                   scale 34→0 ภาพต่างกัน mean 0.11/max 7 = สัญญาณรบกวน)
                   ย้ายความฟุ้งของแถบ dock ไปที่ .dock-glass__frost แล้ว ⇒ ปัจจุบัน
                   วัดซ้ำได้ mean 0.91/max 36/3.8% ของพิกเซล

                ⚠️ ลูกของ orb ที่มี backdrop-filter เอง (เช่น orb-edge ที่เพิ่งถอดออก)
                   จะได้ backdrop = "ผลที่ตัวกรองนี้บิดแล้ว" ⇒ ถ้าไป blur ทับ ก็ลบ
                   รอยบิดทิ้งในโซนขอบซึ่งเป็นโซนที่แรงที่สุด · อย่าเพิ่มกลับ
                ⚠️ เดิมใช้ feTurbulence เป็นสนาม (คาบ 43–53px + blur 7 + scale 34)
                   แต่สนามแบบนั้น "ไม่" เรเดียล — ได้การบิดทั้งวงเป็นก้อน ๆ
                   และ feDiffuseLighting ก็ให้ยอดที่ ~0.8R ไม่ใช่ที่ขอบวง
                   ⇒ เปลี่ยนมาใช้แผนที่เรเดียลที่เขียนเอง (buildLensMap)
                   ถ้าอนาคตจะมี feTurbulence กลับมา → bump seed ทุกครั้งที่แก้ */}
            <filter
              id="dock-glass-lens"
              x="-20%"
              y="-20%"
              width="140%"
              height="140%"
              colorInterpolationFilters="sRGB"
            >
              {/* 1) แผนที่ดิสเพลส (href ถูกยัดเป็น data URI ตอน mount)
                    x/y/width/height="100%" = ยืดแผนที่ให้เต็ม filter region
                    ⇒ ครึ่งหนึ่งของ region (70% ของกล่อง = 1.4 เท่าของรัศมี)
                      ต้องตรงกับ LENS_MAP_SPAN พอดี (ดู buildLensMap) */}
              <feImage
                ref={mapRef}
                x="0"
                y="0"
                width="100%"
                height="100%"
                preserveAspectRatio="none"
                result="map"
              />
              {/* 2) แยก 3 ช่องสีออกมา แล้วดิสเพลสคนละ scale
                    = chromatic aberration จริง (แดงดึงน้อยสุด → น้าเงินดึงมากสุด)
                    วัดจาก scale เต็ม: ขอบวงถูกดึงเข้า ±(scale/2) px
                      R 12 = ±6px · G 16 = ±8px · B 22 = ±11px
                    ⇒ ลด shift ตำแหน่ง (ขอบบีบ ~±8px แทน ~±10px) แต่เพิ่ม shift สี:
                       ขอบแยก B−R ~5px (เดิม ~3.5px) = ขอบสีรุ้งชัด ไม่เป็นวงขาวหนา
                       กลางแยกน้อยสุด ไล่แรงขึ้นยิ่งใกล้ขอบยิ่งพุ่งแบบไม่เป็นเชิงเส้น
                       (ทุกช่องใช้ f ตัวเดียวกัน P = 5.0/DOME 0.22 — f โตแบบเร่งที่ขอบ)
                    ⚠️ scale เริ่มต้น = 0 (เลนส์แบน) — render() ดันเป็น
                    12/16/22 ตาม alpha แบบ smootherstep ทุกเฟรม = transition
                    นูนตอนเริ่มลาก / ยุบตอนปล่อย (ดู transition ใน render)
                    (กลางวงหักเหน้อยสุด ~1.1x — ภาพกลางนิ่ง ขอบบีบแรง
                     บีบแรงเฉพาะแถบขอบแบบหยดน้ำ/เลนส์นูน Fluid Glass ไม่ใช่แว่นขยาย)
                    feColorMatrix ทำหน้าที่ "เปิดช่องเดียว" (ช่องอื่น = 0)
                    แล้ว feBlend mode=screen รวมกลับ (ช่องไม่ทับกัน → ได้ค่าเดิม)
                    ⚠️ ห้ามเร่งสเกลเกิน ~24: displacement เป็นสัดส่วนกับระยะจาก
                       กลางวง ⇒ โซนขอบจะถูกดึงเป็นวงซ้อน ๆ (onion ring) แตก */}
              <feColorMatrix
                in="SourceGraphic"
                type="matrix"
                values="1 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chR"
              />
              <feColorMatrix
                in="SourceGraphic"
                type="matrix"
                values="0 0 0 0 0  0 1 0 0 0  0 0 0 0 0  0 0 0 1 0"
                result="chG"
              />
              <feColorMatrix
                in="SourceGraphic"
                type="matrix"
                values="0 0 0 0 0  0 0 0 0 0  0 0 1 0 0  0 0 0 1 0"
                result="chB"
              />
              <feDisplacementMap
                ref={dispRRef}
                in="chR"
                in2="map"
                scale="0"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dR"
              />
              <feDisplacementMap
                ref={dispGRef}
                in="chG"
                in2="map"
                scale="0"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dG"
              />
              <feDisplacementMap
                ref={dispBRef}
                in="chB"
                in2="map"
                scale="0"
                xChannelSelector="R"
                yChannelSelector="G"
                result="dB"
              />
              <feBlend in="dR" in2="dG" mode="screen" result="dRG" />
              <feBlend in="dRG" in2="dB" mode="screen" result="dRGB" />
              {/* 3) วาร์ปภาพในหยดน้ำ (image warp — ไม่ใช่แค่ shift/แยกสี):
                    turbulence ความถี่ต่ำ + เบลอนุ่ม แล้วดิสเพลสซ้อนอีกที
                    scale เริ่ม 0 — render() ดันเป็น ~9 (นิ่ง) → ~18 (ลากเร็ว) × ae
                    radial อย่างเดียวให้แค่ขยาย/บีบสมมาตร + ขอบแยกสี ขั้นนี้ทำให้
                    เส้นตรงในภาพบิดเป็นคลื่นแบบน้ำจริง (orb แค่ 84px ตอนลากจึงไหว) */}
              <feTurbulence
                type="fractalNoise"
                baseFrequency="0.015 0.02"
                numOctaves="2"
                seed="7"
                result="warpNoise"
              />
              <feGaussianBlur in="warpNoise" stdDeviation="2.5" result="warpSoft" />
              <feDisplacementMap
                ref={warpRef}
                in="dRGB"
                in2="warpSoft"
                scale="0"
                xChannelSelector="R"
                yChannelSelector="G"
              />
            </filter>
          </defs>
        </svg>

        {/* wake มาก่อน orb/pill → วาดใต้ทั้งคู่ (หางของเหลวที่ตามหลัง) */}
        <div ref={wakeRef} className="dock-glass__wake" aria-hidden="true" />

        {/* pill = ตัวแก้วตอนพัก (แคปซูล) — ตอนลากจะจางหายไปให้ orb แทนที่ */}
        <div ref={pillRef} className="dock-glass__pill" aria-hidden="true" />

        {/* แท็บ — ต้องวาด "ก่อน" orb (และห้ามมี z-index เด็ดขาด ดูเหตุผลใน globals.css)
            เพราะ backdrop ของ orb = ทุกอย่างที่วาดก่อนหน้า ⇒ ตัวหนังสือ/ไอคอน
            ของแท็บถูกนับเป็นฉากหลังของเลนส์ ⇒ หยดน้ำ "บีบตัวหนังสือบนแถบ"
            เฉพาะแถบขอบ (กลางนิ่ง) เหมือนของจริง แทนที่จะทะลุไปเห็นหน้าเว็บด้านหลังแถบ
            (ก่อนหน้านี้แท็บอยู่ "หลัง" orb + z-index: 1 ⇒ ตัวหนังสือลอยทับวง
             คม ๆ ไม่อยู่ในฉากหลังของเลนส์) */}
        {MENU.map((l, i) => (
          <button
            key={l.href}
            type="button"
            className="dock-glass__tab"
            aria-current={activeIdx >= 0 && i === activeIdx ? "page" : undefined}
            style={{
              color:
                activeIdx >= 0 && i === activeIdx
                  ? "#7cc4ff"
                  : "rgba(148, 163, 184, 0.92)",
            }}
            onClick={() => go(l.href)}
          >
            <span className="dock-glass__icon">{ICON[l.icon]}</span>
            <span className="dock-glass__label">{l.label}</span>
          </button>
        ))}

        {/* orb = วงกลมแก้วตอนลาก: หักเหฉากหลังจริง (distortion) + แถบแสง/ประกาย
            + 3 rim สีเพี้ยน (rim อยู่ข้างในวง → ใช้ border-radius: inherit = 50%)
            ⚠️ เอา .dock-glass__orb-edge (ชั้นที่เบลอฉากหลังซ้อนเฉพาะโซนขอบ) ออก:
               มันเป็นลูกของ orb ⇒ backdrop ของมันคือ "ผลลัพธ์ที่ orb บิดแล้ว"
               ⇒ ไปเบลอทับบริเวณที่การบิดแรงที่สุด (โซนขอบ) พอดี = ตาเห็นเป็น
               รอยฟุ้ง ไม่เห็นการบิด · ตอนนี้การบิดมาจากตัวกรองเลนส์ล้วน ๆ
               (ผู้ใช้ระบุ: "วงกลมต้องไม่เบลอ ใส่แค่ distortion") */}
        <span ref={orbRef} className="dock-glass__orb" aria-hidden="true">
          <span className="dock-glass__orb-sheen" />
          <span className="dock-glass__orb-caustic" />
          {/* scatter = แสงกระเจิงไม่สมมาตรในเนื้อหยดน้ำ (วาดทับเฉย ๆ ไม่แตะ backdrop) */}
          <span className="dock-glass__scatter" />
          {/* แยกสีที่ขอบวง: แดงในสุด → เขียวกึ่งกลาง → น้าเงินนอกสุด
              + รุ้ง/แสงขอบแบบไม่สมมาตร (อยู่ใน disperse จึงจาง-เข้มพร้อมกัน) */}
          <span ref={dispRef} className="dock-glass__disperse">
            <span className="dock-glass__disperse--r" />
            <span className="dock-glass__disperse--g" />
            <span className="dock-glass__disperse--b" />
            <span className="dock-glass__irid" />
            <span className="dock-glass__edge-light" />
          </span>
          <span ref={rimRRef} className="dock-glass__rim dock-glass__rim--r" />
          <span ref={rimGRef} className="dock-glass__rim dock-glass__rim--g" />
          <span ref={rimBRef} className="dock-glass__rim dock-glass__rim--b" />
        </span>
      </nav>
    </div>
  );
}
