/**
 * ชุดไอคอน SVG "monotone" กลางของแอป — แทน emoji ทั้งหมด (ตามคำขอ 2026-09-06)
 * เส้น stroke สีเดียวตาม currentColor → รับสีจาก text-*, ขนาดคุมด้วย size
 * ใช้: <Icon n="bot" size={15} /> — เสริม className ได้ เช่น className="text-profit"
 */

export type IconName =
  | "bot" | "user" | "users" | "home" | "hand"
  | "check" | "checkCircle" | "x" | "xCircle" | "warning" | "ban" | "help"
  | "target" | "trendUp" | "trendDown" | "bolt" | "chart" | "waves"
  | "archive" | "scroll" | "news" | "inbox" | "lock"
  | "bulb" | "flask" | "coins" | "clock" | "hourglass"
  | "octagon" | "pause" | "play" | "arrowsH"
  | "message" | "arrowDown" | "undo" | "shield" | "refresh" | "spinner";

const PATHS: Record<IconName, React.ReactNode> = {
  bot: (
    <>
      <rect x="5" y="9" width="14" height="11" rx="2.5" />
      <path d="M12 9V5" />
      <circle cx="12" cy="3.5" r="1" />
      <path d="M9.5 13.5h.01" />
      <path d="M14.5 13.5h.01" />
      <path d="M9.5 16.5h5" />
    </>
  ),
  user: (
    <>
      <circle cx="12" cy="8" r="3.5" />
      <path d="M5 20c.8-3.2 3.6-5 7-5s6.2 1.8 7 5" />
    </>
  ),
  users: (
    <>
      <circle cx="9" cy="8" r="3.5" />
      <path d="M3 20c.7-3 3.2-4.8 6-4.8s5.3 1.8 6 4.8" />
      <path d="M16 5a3.5 3.5 0 0 1 0 6.5" />
      <path d="M18 15.5c1.8.8 2.9 2.4 3.3 4.5" />
    </>
  ),
  home: (
    <>
      <path d="M4 10.5 12 4l8 6.5" />
      <path d="M6 9.5V20h12V9.5" />
      <path d="M10 20v-5.5h4V20" />
    </>
  ),
  hand: (
    <>
      <path d="M18 11V7.5a1.5 1.5 0 0 0-3 0V12" />
      <path d="M15 11V5.5a1.5 1.5 0 0 0-3 0V12" />
      <path d="M12 11.5V6.5a1.5 1.5 0 0 0-3 0V13" />
      <path d="M18 11a1.5 1.5 0 0 1 3 0v4a7 7 0 0 1-7 7h-2c-2 0-3.4-.7-4.6-2L4 16.5a1.6 1.6 0 0 1 2.3-2.2L9 16.7" />
    </>
  ),
  check: <path d="m4.5 12.5 5 5L19.5 7" />,
  checkCircle: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m8.5 12.2 2.4 2.4 4.6-4.9" />
    </>
  ),
  x: <path d="M6 6l12 12M18 6 6 18" />,
  xCircle: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9 9l6 6M15 9l-6 6" />
    </>
  ),
  warning: (
    <>
      <path d="M10.3 4 2.9 17a2 2 0 0 0 1.7 3h14.8a2 2 0 0 0 1.7-3L13.7 4a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9.5v4" />
      <path d="M12 16.5h.01" />
    </>
  ),
  ban: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="m6 6 12 12" />
    </>
  ),
  help: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M9.6 9.2a2.5 2.5 0 0 1 4.9.6c0 1.7-2.5 2.2-2.5 3.7" />
      <path d="M12 17h.01" />
    </>
  ),
  target: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="4.5" />
      <circle cx="12" cy="12" r="1" />
    </>
  ),
  trendUp: (
    <>
      <path d="m3 17 6-6 4 4 8-8" />
      <path d="M16 7h5v5" />
    </>
  ),
  trendDown: (
    <>
      <path d="m3 7 6 6 4-4 8 8" />
      <path d="M16 17h5v-5" />
    </>
  ),
  bolt: <path d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z" />,
  chart: (
    <>
      <path d="M3.5 3.5v17h17" />
      <path d="M8 16.5v-5" />
      <path d="M12.5 16.5v-8" />
      <path d="M17 16.5v-3" />
    </>
  ),
  waves: (
    <>
      <path d="M2 6.5c1.7 0 1.7 2 3.4 2s1.7-2 3.3-2 1.7 2 3.3 2 1.7-2 3.4-2 1.6 2 3.3 2 1.6-2 3.3-2" />
      <path d="M2 12c1.7 0 1.7 2 3.4 2s1.7-2 3.3-2 1.7 2 3.3 2 1.7-2 3.4-2 1.6 2 3.3 2 1.6-2 3.3-2" />
      <path d="M2 17.5c1.7 0 1.7 2 3.4 2s1.7-2 3.3-2 1.7 2 3.3 2 1.7-2 3.4-2 1.6 2 3.3 2 1.6-2 3.3-2" />
    </>
  ),
  archive: (
    <>
      <rect x="3" y="4" width="18" height="4.5" rx="1" />
      <path d="M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5" />
      <path d="M10 12.5h4" />
    </>
  ),
  scroll: (
    <>
      <path d="M14.5 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7.5L14.5 3Z" />
      <path d="M14 3v5h5" />
      <path d="M9 13h6M9 16.5h6" />
    </>
  ),
  news: (
    <>
      <path d="M4.5 21.5h14a2 2 0 0 0 2-2v-15a2 2 0 0 0-2-2h-10a2 2 0 0 0-2 2v15a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-8" />
      <path d="M10 7h6.5" />
      <path d="M10 10.5h6.5" />
      <path d="M10 14h4" />
    </>
  ),
  inbox: (
    <>
      <path d="M21.5 12.5h-5.3l-1.6 2.6h-5.2l-1.6-2.6H2.5" />
      <path d="M5.4 5.6 2.5 12.5v5a2 2 0 0 0 2 2h15a2 2 0 0 0 2-2v-5l-2.9-6.9a2 2 0 0 0-1.8-1.1H7.2a2 2 0 0 0-1.8 1.1Z" />
    </>
  ),
  lock: (
    <>
      <rect x="5" y="11" width="14" height="9.5" rx="2" />
      <path d="M8.5 11V7.5a3.5 3.5 0 0 1 7 0V11" />
      <path d="M12 15v1.5" />
    </>
  ),
  bulb: (
    <>
      <path d="M9.5 18h5" />
      <path d="M10.5 21h3" />
      <path d="M12 3a6.5 6.5 0 0 0-3.6 11.9c.7.5 1.1 1.3 1.1 2.1v1h5v-1c0-.8.4-1.6 1.1-2.1A6.5 6.5 0 0 0 12 3Z" />
    </>
  ),
  flask: (
    <>
      <path d="M10 3v6.2L4.7 18.6A2 2 0 0 0 6.4 21.5h11.2a2 2 0 0 0 1.7-2.9L14 9.2V3" />
      <path d="M8.5 3h7" />
      <path d="M7.5 14.5h9" />
    </>
  ),
  coins: (
    <>
      <circle cx="8.5" cy="8.5" r="5.5" />
      <path d="M17.8 9.9a5.5 5.5 0 1 1-7.9 7.9" />
      <path d="M7 6.5h1.5v4" />
      <path d="m15.5 14 .7.7-2.4 2.4" />
    </>
  ),
  clock: (
    <>
      <circle cx="12" cy="12" r="8.5" />
      <path d="M12 7.5V12l3 2" />
    </>
  ),
  hourglass: (
    <>
      <path d="M6.5 3h11" />
      <path d="M6.5 21h11" />
      <path d="M8 3v3.5a4 4 0 0 0 4 4 4 4 0 0 1 4 4V21" />
      <path d="M16 3v3.5a4 4 0 0 1-4 4 4 4 0 0 0-4 4V21" />
    </>
  ),
  octagon: <path d="M8 2.5h8L21.5 8v8L16 21.5H8L2.5 16V8L8 2.5Z" />,
  pause: (
    <>
      <rect x="7" y="5" width="3.2" height="14" rx="1" />
      <rect x="13.8" y="5" width="3.2" height="14" rx="1" />
    </>
  ),
  play: <path d="M7.5 4.5 19 12 7.5 19.5v-15Z" />,
  arrowsH: (
    <>
      <path d="m7.5 3.5-4 4 4 4" />
      <path d="M3.5 7.5h17" />
      <path d="m16.5 12.5 4 4-4 4" />
      <path d="M20.5 16.5h-17" />
    </>
  ),
  message: <path d="M21 14.5a2 2 0 0 1-2 2H7.5L3 21V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v9.5Z" />,
  arrowDown: <path d="M12 4.5v15M5.5 13l6.5 6.5L18.5 13" />,
  refresh: (
    <>
      <path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1" />
      <path d="M20.5 3.5V8H16" />
    </>
  ),
  spinner: <path d="M12 3.5a8.5 8.5 0 1 0 8.5 8.5" />,
  undo: (
    <>
      <path d="M3.5 7v6h6" />
      <path d="M20.5 17a8.5 8.5 0 0 0-14.3-6.2L3.5 13" />
    </>
  ),
  shield: <path d="M12 21.5s7.5-3.2 7.5-9.5V5.2L12 2.5 4.5 5.2V12c0 6.3 7.5 9.5 7.5 9.5Z" />,
};

export default function Icon({
  n,
  size = 16,
  className = "",
}: {
  n: IconName;
  size?: number;
  className?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      className={`inline-block shrink-0 align-[-0.15em] ${className}`}
    >
      {PATHS[n]}
    </svg>
  );
}
