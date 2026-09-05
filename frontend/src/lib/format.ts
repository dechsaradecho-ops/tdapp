/** เงินทั้งระบบเป็น USD — ใช้ en-US ตรงๆ กัน hydration mismatch จาก ICU ต่างกันระหว่าง Node/เบราว์เซอร์ */
export const fmtMoney = (v: number) =>
  `${v < 0 ? "-" : ""}$${new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(Math.abs(v))}`;

export const fmtPct = (v: number, digits = 1) => `${v >= 0 ? "" : ""}${v.toFixed(digits)}%`;

export const fmtNum = (v: number, digits = 2) =>
  new Intl.NumberFormat("en-US", { maximumFractionDigits: digits }).format(v);

export function scoreColor(score: number): string {
  if (score >= 81) return "text-emerald-400";
  if (score >= 61) return "text-accent";
  if (score >= 31) return "text-amber-400";
  return "text-slate-500";
}

export function probabilityLabel(p: string): string {
  switch (p) {
    case "high_probability": return "High Probability";
    case "moderate_probability": return "Moderate Probability";
    case "low_probability": return "Low Probability";
    default: return p;
  }
}
