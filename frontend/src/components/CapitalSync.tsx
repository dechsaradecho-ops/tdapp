"use client";

import { useEffect } from "react";
import { refreshPortfolio } from "@/lib/portfolio";

/**
 * Seed the global portfolio store from the backend (DB = single source of truth).
 * Runs once on app load; every page using usePortfolio() sees the same numbers.
 */
export default function CapitalSync() {
  useEffect(() => {
    refreshPortfolio();
  }, []);
  return null;
}
