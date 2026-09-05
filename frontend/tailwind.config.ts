import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // โทน iOS Liquid Glass dark (user request 2026-09-05):
        // พื้นหลังดำสนิท + aurora glow, การ์ด = แก้วฝ้าโปร่งแสง blur เบื้องหลัง
        surface: "#000000",
        panel: "#0a0a0c",
        glass: "rgba(255,255,255,0.10)",
        accent: "#0a84ff",
        profit: "#30d158",
        loss: "#ff453a",
      },
    },
  },
  plugins: [],
};
export default config;
