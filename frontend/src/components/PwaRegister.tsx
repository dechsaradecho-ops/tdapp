"use client";

import { useEffect } from "react";

/** Registers the service worker (PWA offline shell) in production builds.
 * Dev server is skipped — Next dev has no stable SW story and HMR breaks. */
export default function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (process.env.NODE_ENV !== "production") return;
    if (!("serviceWorker" in navigator)) return;

    const register = () => {
      navigator.serviceWorker
        .register("/sw.js")
        .catch(() => { /* SW optional — app works fine without it */ });
    };

    if (document.readyState === "complete") register();
    else {
      window.addEventListener("load", register);
      return () => window.removeEventListener("load", register);
    }
  }, []);

  return null;
}
