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

    // Auto-update: when a new SW is waiting, activate it now and reload once
    // it takes control. Without this the phone keeps the old bundle until every
    // tab is closed (symptom: "มือถือยังไม่เปลี่ยน" after a deploy).
    let reloading = false;
    const onControllerChange = () => {
      if (reloading) return;
      reloading = true;
      window.location.reload();
    };
    navigator.serviceWorker.addEventListener("controllerchange", onControllerChange);

    const promote = (reg: ServiceWorkerRegistration) => {
      if (reg.waiting) reg.waiting.postMessage({ type: "SKIP_WAITING" });
      reg.addEventListener("updatefound", () => {
        const sw = reg.installing;
        if (!sw) return;
        sw.addEventListener("statechange", () => {
          if (sw.state === "installed" && navigator.serviceWorker.controller) {
            sw.postMessage({ type: "SKIP_WAITING" });
          }
        });
      });
    };

    const register = () => {
      navigator.serviceWorker
        .register("/sw.js")
        .then((reg) => {
          promote(reg);
          // Check for a newer SW on every load (the browser also does this
          // automatically, but an explicit call makes the update prompt).
          reg.update().catch(() => {});
        })
        .catch(() => { /* SW optional — app works fine without it */ });
    };

    if (document.readyState === "complete") register();
    else {
      window.addEventListener("load", register);
      return () => {
        window.removeEventListener("load", register);
        navigator.serviceWorker.removeEventListener("controllerchange", onControllerChange);
        stopSync();
      };
    }
    return () => {
      navigator.serviceWorker.removeEventListener("controllerchange", onControllerChange);
      stopSync();
    };
  }, []);

  return null;
}
