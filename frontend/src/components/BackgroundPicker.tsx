"use client";

/**
 * BackgroundPicker — อัปโหลดรูปพื้นหลังจากเครื่องผู้ใช้ (ใช้ 2 จุดใน Settings)
 *
 *   variant="app"  (ค่าเริ่มต้น) พื้นหลังแอปทุกหน้า — BackgroundLayer อ่านไปวาดแบบ fixed
 *   variant="hero"               แบบด์ "image zoom" ทุกหน้า — ScrollZoomHero อ่านไปใช้
 *
 * เก็บรูปฝั่ง client เป็น data URL ใน localStorage (แยกคีย์ตาม variant)
 * เพราะ frontend เป็น static export และ Render FS ไม่ persistent — อัปโหลดไฟล์ขึ้น server ไม่ได้
 * รูปจะถูกย่อ/บีบ (max 1600px, ฮีโร่ 1920px, JPEG q0.72) ผ่าน <canvas> ก่อนเก็บ
 * เพื่อจำกัดขนาด localStorage (~5MB); ผู้ใช้ปลายทางอ่านค่ารูปและรับรู้การเปลี่ยนแปลง
 * ผ่าน event ประจำ variant (BackgroundLayer → "tdapp:bg-changed", ScrollZoomHero → "tdapp:hero-changed")
 * ความสว่าง (ความเข้ม scrim ดำ) เก็บแยกใน localStorage — ปรับได้จาก slider ทั้ง 2 variant
 * (key: tdapp_bg_dim สำหรับพื้นหลังแอป · tdapp_hero_dim สำหรับแบบด์ image zoom)
 * ระดับการซูม (เฉพาะ variant="hero") — เก็บใน tdapp_hero_zoom เป็น "ตัวคูณของช่วงซูม"
 * ของทุกชั้นใน ScrollZoomHero (0 = ภาพนิ่งไม่ซูม · 1 = ค่าที่ออกแบบไว้เป๊ะ · 2 = ซูมแรงสุด)
 */

import { useEffect, useRef, useState } from "react";
import Icon from "@/components/Icon";

const BG_KEY = "tdapp_bg_image";
const BG_EVENT = "tdapp:bg-changed";
const BG_DIM_KEY = "tdapp_bg_dim";
// ฮีโร่ image zoom ทุกหน้า — คีย์/อีเวนต์แยกจากพื้นหลังแอป (ตั้งค่าอิสระต่อกัน)
const HERO_KEY = "tdapp_hero_image";
const HERO_EVENT = "tdapp:hero-changed";
const HERO_DIM_KEY = "tdapp_hero_dim";
const HERO_ZOOM_KEY = "tdapp_hero_zoom";
const MAX_DIM = 1600; // px — ด้านยาวสุดของพื้นหลังแอป
const MAX_DIM_HERO = 1920; // ฮีโร่เป็นแบบด์กว้างเต็มจอ + ถูกซูมขยาย — เก็บรายละเอียดมากกว่า
const MAX_STORED_BYTES = 2_800_000; // ~2.8MB data URL — ปลอดภัยกับ quota localStorage ส่วนใหญ่ (5MB)

/** variant ของ picker — แยกคีย์ที่เก็บ/อีเวนต์/ข้อความให้ตรงกับที่ใช้งาน */
type PickerVariant = "app" | "hero";

/** ความเข้ม scrim ดำทับรูป (0 = สว่างสุด ... 0.85 = มืดสุด)
 *  พื้นหลังแอป default 0.55 (ใช้คู่กับ glow เยอะ จึงต้องหรี่พอให้ตัวหนังสืออ่านออก)
 *  แบบด์ฮีโร่ default 0 = รูปร่างเดิมเป๊ะ (มี vignette ช่วยอยู่แล้ว) แล้วให้ผู้ใช้หรี่เพิ่มเองได้ */
export const BG_DIM_DEFAULT = 0.55;
export const BG_DIM_MIN = 0;
export const BG_DIM_MAX = 0.85;
export const HERO_DIM_DEFAULT = 0;

/** ระดับการซูมของแบบด์ฮีโร่ — ตัวคูณของ "ช่วงซูม" (end − start) ของทุกชั้นใน ScrollZoomHero
 *  0 = ไม่ซูมเลย (ทุกชั้นนิ่งอยู่ที่ scale เริ่มต้น) · 1 = ค่าที่ออกแบบไว้เป๊ะ
 *  2 = ช่วงซูมกว้างเป็น 2 เท่า (เห็นการซูมชัดขึ้น)
 *  เป็นตัวคูณ "ช่วง" ไม่ใช่ตัวคูณ scale ตรง ๆ → ค่า 1 ต้องได้หน้าตาเดิมเป๊ะเสมอ
 *  และสัดส่วนความลึก 3D ระหว่างชั้น (ใกล้ขยายเร็ว / ไกลขยายช้า) คงเดิมทุกค่า */
export const HERO_ZOOM_DEFAULT = 1;
export const HERO_ZOOM_MIN = 0;
export const HERO_ZOOM_MAX = 2;

const VARIANTS: Record<
  PickerVariant,
  {
    key: string;
    event: string;
    dimKey: string;
    /** ความเข้ม scrim เริ่มต้นของ variant นี้ (ใช้เป็นค่าเมื่อยังไม่เคยตั้ง + ค่าของปุ่ม "ค่าเริ่มต้น") */
    dimDefault: number;
    maxDim: number;
    showDim: boolean;
    dimLabel: string;
    dimHintOn: string;
    dimHintOff: string;
    /** สไลเดอร์ "ระดับการซูม" — มีเฉพาะ variant ที่มี scroll-zoom (ฮีโร่) */
    showZoom?: boolean;
    zoomLabel?: string;
    zoomHint?: string;
    /** พรีวิวตอนยังไม่ตั้งรูปเอง — ฮีโร่โชว์ภาพเริ่มต้นในตัว */
    defaultPreview?: string;
    emptyText: string;
    defaultText: string;
    currentText: string;
    doneText: string;
    clearedText: string;
    hint: string;
  }
> = {
  app: {
    key: BG_KEY,
    event: BG_EVENT,
    dimKey: BG_DIM_KEY,
    dimDefault: BG_DIM_DEFAULT,
    maxDim: MAX_DIM,
    showDim: true,
    dimLabel: "ความสว่างพื้นหลัง",
    dimHintOn: "เลื่อนไปขวา = รูปพื้นหลังมืดลง (ตัวหนังสืออ่านง่ายขึ้น) · ปรับแล้วใช้ได้ทันทีทุกหน้า",
    dimHintOff: "เพิ่มรูปพื้นหลังก่อนจึงจะปรับความสว่างได้",
    emptyText: "ยังไม่มีรูปพื้นหลัง — ใช้พื้นหลัง default (aurora)",
    defaultText: "",
    currentText: "รูปพื้นหลังปัจจุบัน",
    doneText: "ตั้งรูปพื้นหลังเรียบร้อย",
    clearedText: "ลบรูปพื้นหลังแล้ว — กลับไปใช้ default",
    hint: "รูปจะถูกย่อเหลือด้านยาวสุด 1600px และเก็บไว้ในเครื่องนี้ (localStorage) — แสดงหลัง login ทุกหน้า",
  },
  hero: {
    key: HERO_KEY,
    event: HERO_EVENT,
    dimKey: HERO_DIM_KEY,
    dimDefault: HERO_DIM_DEFAULT,
    maxDim: MAX_DIM_HERO,
    showDim: true,
    dimLabel: "ความสว่าง (Image zoom)",
    dimHintOn: "เลื่อนไปขวา = แบบด์ด้านบนมืดลง (การ์ด/ตัวหนังสืออ่านง่ายขึ้น) · ปรับแล้วใช้ได้ทันทีทุกหน้า",
    dimHintOff: "",
    showZoom: true,
    zoomLabel: "ระดับการซูม (Zoom scale)",
    zoomHint: "0% = ไม่ซูมเลย (ภาพนิ่ง) · 100% = ค่าเริ่มต้น · 200% = ซูมแรงสุด — มีผลกับทุกชั้นของแบบด์ (ภาพ/แสง/กริด/โบเก้) พร้อมกัน",
    defaultPreview: "/scroll-zoom.svg",
    emptyText: "ใช้ภาพเริ่มต้นในตัวอยู่",
    defaultText: "ภาพเริ่มต้น (Scroll Zoom)",
    currentText: "รูปที่ใช้เป็นแบบด์ด้านบน",
    doneText: "ตั้งรูปแบบด์ด้านบนเรียบร้อย",
    clearedText: "ลบรูปแล้ว — กลับไปใช้ภาพเริ่มต้น",
    hint: "รูปจะถูกย่อเหลือด้านยาวสุด 1920px และเก็บไว้ในเครื่องนี้ (localStorage) — แสดงเป็นแบบด์บนสุดของทุกหน้า",
  },
};

/** อ่านค่าตัวเลขที่เก็บใน localStorage แบบ clamp ช่วง (ยังไม่เคยตั้ง/อ่านไม่ได้ → fallback)
 *  ใช้ร่วมกันทั้งความสว่าง (0–0.85) และระดับการซูม (0–2) */
function readStoredNumber(key: string, fallback: number, min: number, max: number): number {
  if (typeof window === "undefined") return fallback;
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const n = Number(raw);
    if (!Number.isFinite(n)) return fallback;
    return Math.min(max, Math.max(min, n));
  } catch {
    return fallback; // private mode / storage disabled
  }
}

/** อ่านความเข้ม scrim — ค่าที่เก็บไว้ หรือ fallback ตาม variant (private mode → fallback) */
export function readStoredDim(key: string = BG_DIM_KEY, fallback: number = BG_DIM_DEFAULT): number {
  return readStoredNumber(key, fallback, BG_DIM_MIN, BG_DIM_MAX);
}

export function readStoredBgDim(): number {
  return readStoredDim(BG_DIM_KEY, BG_DIM_DEFAULT);
}

/** ความสว่างของแบบด์ image zoom (ตั้งใน Settings) — ใช้โดย ScrollZoomHero */
export function readStoredHeroDim(): number {
  return readStoredDim(HERO_DIM_KEY, HERO_DIM_DEFAULT);
}

/** ระดับการซูมของแบบด์ฮีโร่ (ตั้งใน Settings) — ใช้โดย ScrollZoomHero */
export function readStoredHeroZoom(): number {
  return readStoredNumber(HERO_ZOOM_KEY, HERO_ZOOM_DEFAULT, HERO_ZOOM_MIN, HERO_ZOOM_MAX);
}

/** อ่าน data URL ของรูปที่ผู้ใช้ตั้งไว้ (คีย์ไหนก็ได้) — null = ยังไม่ตั้ง/อ่านไม่ได้ */
export function readStoredImage(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // private mode / storage disabled
  }
}

export function readStoredBg(): string | null {
  return readStoredImage(BG_KEY);
}

/** รูปแบบด์ image zoom หน้าแรก (ตั้งใน Settings) — null = ใช้ภาพเริ่มต้น /scroll-zoom.svg */
export function readStoredHero(): string | null {
  return readStoredImage(HERO_KEY);
}

/** ชื่อ event ที่ picker ยิงเมื่อรูป/ความสว่างเปลี่ยน — ใช้ให้ component ปลายทาง listen */
export const HERO_IMAGE_EVENT = HERO_EVENT;

export default function BackgroundPicker({ variant = "app" }: { variant?: PickerVariant }) {
  const cfg = VARIANTS[variant];
  const [preview, setPreview] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [dim, setDim] = useState(BG_DIM_DEFAULT);
  // ระดับการซูมของแบบด์ฮีโร่ — ใช้เฉพาะ variant="hero" (ตั้งใจไม่ set ตอน variant="app")
  const [zoom, setZoom] = useState(HERO_ZOOM_DEFAULT);
  // เครื่อง/OS ที่ปิดอนิเมชัน → ScrollZoomHero ไม่สร้าง timeline → สไลเดอร์ซูมไม่มีผล
  const [reduceMotion, setReduceMotion] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setPreview(readStoredImage(cfg.key));
    setDim(readStoredDim(cfg.dimKey, cfg.dimDefault));
    setZoom(readStoredHeroZoom());
  }, [cfg.key, cfg.dimKey, cfg.dimDefault]);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const sync = () => setReduceMotion(mq.matches);
    sync();
    mq.addEventListener("change", sync);
    return () => mq.removeEventListener("change", sync);
  }, []);

  const apply = (dataUrl: string | null) => {
    try {
      if (dataUrl) window.localStorage.setItem(cfg.key, dataUrl);
      else window.localStorage.removeItem(cfg.key);
    } catch {
      setMsg("พื้นที่จัดเก็บไม่พอ — ลองใช้รูปที่เล็กกว่านี้");
      return;
    }
    setPreview(dataUrl);
    window.dispatchEvent(new Event(cfg.event));
  };

  /** ย่อรูปผ่าน canvas → JPEG data URL (รักษาสัดส่วน, crop ไม่ต้อง — ให้ CSS cover จัดการ) */
  const processFile = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        const scale = Math.min(1, cfg.maxDim / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          URL.revokeObjectURL(url);
          reject(new Error("canvas unsupported"));
          return;
        }
        ctx.drawImage(img, 0, 0, w, h);
        URL.revokeObjectURL(url);
        resolve(canvas.toDataURL("image/jpeg", 0.72));
      };
      img.onerror = () => {
        URL.revokeObjectURL(url);
        reject(new Error("อ่านรูปไม่สำเร็จ"));
      };
      img.src = url;
    });

  const onPick = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // อนุญาตเลือกไฟล์เดิมซ้ำ
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setMsg("กรุณาเลือกไฟล์รูปภาพ (JPG/PNG/WebP)");
      return;
    }
    setBusy(true);
    setMsg("");
    try {
      const dataUrl = await processFile(file);
      if (dataUrl.length > MAX_STORED_BYTES) {
        setMsg("รูปใหญ่เกินไปแม้ย่อแล้ว — ลองใช้รูปขนาดเล็กกว่านี้");
        return;
      }
      apply(dataUrl);
      setMsg(cfg.doneText);
    } catch (err) {
      setMsg(`${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const setBgDim = (value: number) => {
    const clamped = Math.min(BG_DIM_MAX, Math.max(BG_DIM_MIN, value));
    try {
      window.localStorage.setItem(cfg.dimKey, String(clamped));
    } catch {
      // private mode — ยังอัปเดต UI ให้เห็นผลทันทีแม้เก็บไม่ได้
    }
    setDim(clamped);
    window.dispatchEvent(new Event(cfg.event));
  };

  /** ระดับการซูม — คีย์เดียว (HERO_ZOOM_KEY) เพราะมีผลกับฮีโร่เท่านั้น
   *  ยิง event เดียวกับรูป/ความสว่าง → ScrollZoomHero อ่านค่าใหม่แล้วสร้าง timeline ใหม่ */
  const setZoomScale = (value: number) => {
    const clamped = Math.min(HERO_ZOOM_MAX, Math.max(HERO_ZOOM_MIN, value));
    try {
      window.localStorage.setItem(HERO_ZOOM_KEY, String(clamped));
    } catch {
      // private mode — ยังอัปเดต UI ให้เห็นผลทันทีแม้เก็บไม่ได้
    }
    setZoom(clamped);
    window.dispatchEvent(new Event(cfg.event));
  };

  // พรีวิว = รูปที่ผู้ใช้ตั้งไว้ หรือภาพเริ่มต้นในตัว (เฉพาะฮีโร่)
  const previewSrc = preview ?? cfg.defaultPreview ?? null;
  // พื้นหลังแอป: ต้องมีรูปก่อนจึงจะปรับความสว่างได้
  // แบบด์ฮีโร่: มีภาพเริ่มต้นในตัวเสมอ → ปรับได้ทันทีโดยไม่ต้องอัปโหลดรูป
  const dimEnabled = cfg.showDim && previewSrc !== null;

  return (
    <div className="space-y-3">
      {/* พรีวิว: ความสูง 96px — โชว์รูปปัจจุบัน หรือพื้นหลัง default */}
      <div
        className="rounded-lg border border-slate-700 h-24 bg-cover bg-center relative overflow-hidden"
        style={
          previewSrc
            ? { backgroundImage: `url(${previewSrc})` }
            : undefined
        }
      >
        {!previewSrc && (
          <div className="absolute inset-0 bg-surface flex items-center justify-center text-xs text-slate-500">
            {cfg.emptyText}
          </div>
        )}
        {preview && (
          <div className="absolute inset-0 bg-black/40 flex items-center justify-center">
            <span className="text-xs text-white/90 font-medium">{cfg.currentText}</span>
          </div>
        )}
        {!preview && cfg.defaultPreview && (
          <div className="absolute inset-0 bg-black/45 flex items-center justify-center">
            <span className="text-xs text-white/90 font-medium">{cfg.defaultText}</span>
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => fileRef.current?.click()}
          disabled={busy}
          aria-busy={busy}
          className="bg-accent text-white font-semibold rounded px-4 py-2 text-sm disabled:opacity-50"
        >
          <span className="inline-flex items-center gap-1.5">
            {busy && <Icon n="spinner" size={14} className="animate-spin" />}
            {busy ? "กำลังประมวลผล..." : "เลือกรูป"}
          </span>
        </button>
        {preview && (
          <button
            onClick={() => { apply(null); setMsg(cfg.clearedText); }}
            className="border border-slate-700 text-slate-300 rounded px-4 py-2 text-sm active:bg-slate-800"
          >
            ลบรูปพื้นหลัง
          </button>
        )}
      </div>

      <input
        ref={fileRef}
        type="file"
        accept="image/*"
        className="hidden"
        onChange={onPick}
      />

      {/* ปรับความสว่างของแบ็กกราวด์ — ยิ่งเลื่อนขวา ยิ่ง scrim ดำเข้ม ภาพยิ่งมืด
          ใช้ทั้งพื้นหลังแอป (tdapp_bg_dim) และแบบด์ image zoom (tdapp_hero_dim) */}
      {cfg.showDim && (
      <div className="rounded-lg border border-slate-700 p-3 space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-300 font-medium">{cfg.dimLabel}</span>
          <div className="flex items-center gap-2">
            <span className="text-slate-400 tabular-nums">
              {dimEnabled ? `${Math.round((1 - dim) * 100)}%` : "—"}
            </span>
            {dimEnabled && dim !== cfg.dimDefault && (
              <button
                onClick={() => setBgDim(cfg.dimDefault)}
                className="text-[11px] text-slate-400 border border-slate-700 rounded px-2 min-h-[28px] active:bg-slate-800"
              >
                ค่าเริ่มต้น
              </button>
            )}
          </div>
        </div>
        <input
          type="range"
          min={BG_DIM_MIN}
          max={BG_DIM_MAX}
          step={0.05}
          value={dim}
          disabled={!dimEnabled}
          onChange={(e) => setBgDim(Number(e.target.value))}
          className="bg-dim-slider w-full"
          aria-label={cfg.dimLabel}
        />
        <p className="text-[11px] text-slate-500">
          {dimEnabled ? cfg.dimHintOn : cfg.dimHintOff}
        </p>
      </div>
      )}

      {/* ปรับ "ระดับการซูม" (เฉพาะ variant ที่มี scroll-zoom = ฮีโร่)
          ค่าเป็นตัวคูณของ "ช่วงซูม" (end − start) ของทุกชั้นใน ScrollZoomHero
          → 100% = ค่าที่ออกแบบไว้เป๊ะ · 0% = ทุกชั้นนิ่ง (ไม่ซูม) · 200% = ช่วงซูมกว้าง 2 เท่า
          ปรับแล้ว ScrollZoomHero สร้าง timeline ใหม่ทันทีผ่าน event เดียวกับรูป/ความสว่าง
          (ไม่ต้องอัปโหลดรูปใหม่ — สไลเดอร์นี้ใช้ได้กับภาพเริ่มต้นในตัวด้วย) */}
      {cfg.showZoom && (
      <div className="rounded-lg border border-slate-700 p-3 space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-300 font-medium">{cfg.zoomLabel}</span>
          <div className="flex items-center gap-2">
            <span className="text-slate-400 tabular-nums">{Math.round(zoom * 100)}%</span>
            {zoom !== HERO_ZOOM_DEFAULT && (
              <button
                onClick={() => setZoomScale(HERO_ZOOM_DEFAULT)}
                className="text-[11px] text-slate-400 border border-slate-700 rounded px-2 min-h-[28px] active:bg-slate-800"
              >
                ค่าเริ่มต้น
              </button>
            )}
          </div>
        </div>
        <input
          type="range"
          min={HERO_ZOOM_MIN}
          max={HERO_ZOOM_MAX}
          step={0.05}
          value={zoom}
          onChange={(e) => setZoomScale(Number(e.target.value))}
          className="bg-dim-slider w-full"
          aria-label={cfg.zoomLabel}
        />
        <p className="text-[11px] text-slate-500">
          {reduceMotion
            ? "เครื่องนี้ตั้งปิดอนิเมชัน (prefers-reduced-motion) — แบบด์แสดงเป็นภาพนิ่ง ระดับการซูมจึงยังไม่เห็นผล"
            : cfg.zoomHint}
        </p>
      </div>
      )}

      <p className="text-xs text-slate-500">
        {msg || cfg.hint}
      </p>
    </div>
  );
}
