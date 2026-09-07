"use client";

/**
 * BackgroundPicker — อัปโหลดรูปพื้นหลังของแอปจากเครื่องผู้ใช้
 *
 * เก็บรูปฝั่ง client เป็น data URL ใน localStorage (key: tdapp_bg_image)
 * เพราะ frontend เป็น static export และ Render FS ไม่ persistent — อัปโหลดไฟล์ขึ้น server ไม่ได้
 * รูปจะถูกย่อ/บีบ (max 1600px, JPEG q0.72) ผ่าน <canvas> ก่อนเก็บ เพื่อจำกัดขนาด localStorage (~5MB)
 * layout.tsx อ่านค่านี้ตอน mount และรับรู้การเปลี่ยนแปลงผ่าน event "tdapp:bg-changed"
 * ความสว่าง (ความเข้ม scrim ดำ) เก็บแยกใน localStorage (key: tdapp_bg_dim) — ปรับได้จาก slider
 */

import { useEffect, useRef, useState } from "react";

const BG_KEY = "tdapp_bg_image";
const BG_EVENT = "tdapp:bg-changed";
const BG_DIM_KEY = "tdapp_bg_dim";
const MAX_DIM = 1600; // px — ด้านยาวสุด
const MAX_STORED_BYTES = 2_800_000; // ~2.8MB data URL — ปลอดภัยกับ quota localStorage ส่วนใหญ่ (5MB)

/** ความเข้ม scrim ดำทับรูปพื้นหลัง (0 = สว่างสุด ... 0.85 = มืดสุด) — default 0.55 */
export const BG_DIM_DEFAULT = 0.55;
export const BG_DIM_MIN = 0;
export const BG_DIM_MAX = 0.85;

export function readStoredBgDim(): number {
  if (typeof window === "undefined") return BG_DIM_DEFAULT;
  try {
    const raw = window.localStorage.getItem(BG_DIM_KEY);
    if (raw === null) return BG_DIM_DEFAULT;
    const n = Number(raw);
    if (!Number.isFinite(n)) return BG_DIM_DEFAULT;
    return Math.min(BG_DIM_MAX, Math.max(BG_DIM_MIN, n));
  } catch {
    return BG_DIM_DEFAULT; // private mode / storage disabled
  }
}

export function readStoredBg(): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(BG_KEY);
  } catch {
    return null; // private mode / storage disabled
  }
}

export default function BackgroundPicker() {
  const [preview, setPreview] = useState<string | null>(null);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [dim, setDim] = useState(BG_DIM_DEFAULT);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setPreview(readStoredBg());
    setDim(readStoredBgDim());
  }, []);

  const apply = (dataUrl: string | null) => {
    try {
      if (dataUrl) window.localStorage.setItem(BG_KEY, dataUrl);
      else window.localStorage.removeItem(BG_KEY);
    } catch {
      setMsg("พื้นที่จัดเก็บไม่พอ — ลองใช้รูปที่เล็กกว่านี้");
      return;
    }
    setPreview(dataUrl);
    window.dispatchEvent(new Event(BG_EVENT));
  };

  /** ย่อรูปผ่าน canvas → JPEG data URL (รักษาสัดส่วน, crop ไม่ต้อง — ให้ CSS cover จัดการ) */
  const processFile = (file: File): Promise<string> =>
    new Promise((resolve, reject) => {
      const img = new Image();
      const url = URL.createObjectURL(file);
      img.onload = () => {
        const scale = Math.min(1, MAX_DIM / Math.max(img.width, img.height));
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
      setMsg("ตั้งรูปพื้นหลังเรียบร้อย");
    } catch (err) {
      setMsg(`${err instanceof Error ? err.message : String(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const setBgDim = (value: number) => {
    const clamped = Math.min(BG_DIM_MAX, Math.max(BG_DIM_MIN, value));
    try {
      window.localStorage.setItem(BG_DIM_KEY, String(clamped));
    } catch {
      // private mode — ยังอัปเดต UI ให้เห็นผลทันทีแม้เก็บไม่ได้
    }
    setDim(clamped);
    window.dispatchEvent(new Event(BG_EVENT));
  };

  return (
    <div className="space-y-3">
      {/* พรีวิว: ความสูง 96px — โชว์รูปปัจจุบัน หรือพื้นหลัง default */}
      <div
        className="rounded-lg border border-slate-700 h-24 bg-cover bg-center relative overflow-hidden"
        style={
          preview
            ? { backgroundImage: `url(${preview})` }
            : undefined
        }
      >
        {!preview && (
          <div className="absolute inset-0 bg-surface flex items-center justify-center text-xs text-slate-500">
            ยังไม่มีรูปพื้นหลัง — ใช้พื้นหลัง default (aurora)
          </div>
        )}
        {preview && (
          <div className="absolute inset-0 bg-black/40 flex items-center justify-center">
            <span className="text-xs text-white/90 font-medium">รูปพื้นหลังปัจจุบัน</span>
          </div>
        )}
      </div>

      <div className="flex flex-wrap gap-2">
        <button
          onClick={() => fileRef.current?.click()}
          disabled={busy}
          className="bg-accent text-white font-semibold rounded px-4 py-2 text-sm disabled:opacity-50"
        >
          {busy ? "กำลังประมวลผล..." : "เลือกรูป"}
        </button>
        {preview && (
          <button
            onClick={() => { apply(null); setMsg("ลบรูปพื้นหลังแล้ว — กลับไปใช้ default"); }}
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

      {/* ปรับความสว่างของพื้นหลัง — ยิ่งเลื่อนขวา ยิ่ง scrim ดำเข้ม รูปยิ่งมืด */}
      <div className="rounded-lg border border-slate-700 p-3 space-y-2">
        <div className="flex items-center justify-between text-xs">
          <span className="text-slate-300 font-medium">ความสว่างพื้นหลัง</span>
          <div className="flex items-center gap-2">
            <span className="text-slate-400 tabular-nums">
              {preview ? `${Math.round((1 - dim) * 100)}%` : "—"}
            </span>
            {preview && dim !== BG_DIM_DEFAULT && (
              <button
                onClick={() => setBgDim(BG_DIM_DEFAULT)}
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
          disabled={!preview}
          onChange={(e) => setBgDim(Number(e.target.value))}
          className="bg-dim-slider w-full"
          aria-label="ปรับความสว่างพื้นหลัง"
        />
        <p className="text-[11px] text-slate-500">
          {preview
            ? "เลื่อนไปขวา = รูปพื้นหลังมืดลง (ตัวหนังสืออ่านง่ายขึ้น) · ปรับแล้วใช้ได้ทันทีทุกหน้า"
            : "เพิ่มรูปพื้นหลังก่อนจึงจะปรับความสว่างได้"}
        </p>
      </div>

      <p className="text-xs text-slate-500">
        {msg || "รูปจะถูกย่อเหลือด้านยาวสุด 1600px และเก็บไว้ในเครื่องนี้ (localStorage) — แสดงหลัง login ทุกหน้า"}
      </p>
    </div>
  );
}
