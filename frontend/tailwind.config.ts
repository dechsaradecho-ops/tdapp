import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // โทนดำ (user request 2026-09-04): พื้นหลังหน้า = ดำสนิท,
        // การ์ด/แผง = ดำอมเทาเข้ม ให้ยังแยกชั้นกับพื้นหลังได้
        surface: "#000000",
        panel: "#0a0a0c",
        accent: "#38bdf8",
        profit: "#22c55e",
        loss: "#ef4444",
      },
    },
  },
  plugins: [],
};
export default config;
