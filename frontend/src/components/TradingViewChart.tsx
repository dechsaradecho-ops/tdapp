"use client";

import { useEffect, useRef } from "react";

/** TradingView Advanced Chart widget (embeds tv.js once). */
export default function TradingViewChart({ symbol = "OANDA:XAUUSD" }: { symbol?: string }) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    ref.current.innerHTML = "";
    const widget = document.createElement("div");
    widget.className = "tradingview-widget-container__widget";
    widget.style.height = "400px";
    ref.current.appendChild(widget);

    const script = document.createElement("script");
    script.src = "https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js";
    script.async = true;
    script.text = JSON.stringify({
      autosize: true,
      symbol,
      interval: "60",
      timezone: "Asia/Bangkok",
      theme: "dark",
      style: "1",
      locale: "th",
      hide_side_toolbar: true,
    });
    ref.current.appendChild(script);
  }, [symbol]);

  return (
    <div className="tradingview-widget-container h-[400px] rounded-lg overflow-hidden" ref={ref} />
  );
}
