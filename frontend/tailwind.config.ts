import type { Config } from "tailwindcss";

const config: Config = {
  content: ["./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        surface: "#0b1220",
        panel: "#111a2c",
        accent: "#38bdf8",
        profit: "#22c55e",
        loss: "#ef4444",
      },
    },
  },
  plugins: [],
};
export default config;
