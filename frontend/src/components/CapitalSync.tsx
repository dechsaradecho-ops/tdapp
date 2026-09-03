"use client";

import { useEffect } from "react";
import { api } from "@/lib/api";
import { setPortfolioCapital } from "@/lib/portfolio";

/**
 * Seed the global capital store from saved settings (DB = single source of truth).
 * Runs once on app load; every page using usePortfolio() sees the settings capital.
 */
export default function CapitalSync() {
  useEffect(() => {
    api.getSettings()
      .then((s) => setPortfolioCapital(s.capital))
      .catch(() => { /* backend unreachable — keep local value */ });
  }, []);
  return null;
}
