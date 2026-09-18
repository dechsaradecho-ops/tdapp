"use client";

import { useEffect } from "react";
import { api } from "@/lib/api";
import { installPushSync } from "@/lib/push";

/** Registers the service worker (PWA offline shell) in production builds.
 * Dev server is skipped — Next dev has no stable SW story and HMR breaks.
 *
 * It ALSO installs the Web Push subscription self-heal (see lib/push.ts): the
 * browser can rotate a push endpoint at any time, and the notification-click /
 * push handlers live in the service worker which cannot call our PIN-guarded
 * API by itself. Doing it here keeps it off the Settings page's critical path.
 */
export default function PwaRegister() {
  useEffect(() => {
    if (typeof window === "undefined") return;
    if (process.env.NODE_ENV !== "production") return;
    if (!("serviceWorker" in navigator)) return;

    const stopSync = installPushSync(api.pushSubscribe);

    const register = () => {
      navigator.serviceWorker
        .register("/sw.js")
        .catch(() => { /* SW optional — app works fine without it */ });
    };

    if (document.readyState === "complete") register();
    else {
      window.addEventListener("load", register);
      return () => {
        window.removeEventListener("load", register);
        stopSync();
      };
    }
    return stopSync;
  }, []);

  return null;
}
