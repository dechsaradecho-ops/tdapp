import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

/**
 * Ultra-light keepalive endpoint (mirrors backend GET /ping).
 * Point external cron/uptime pingers (cron-job.org, UptimeRobot, ...) here
 * every ~10 min so the Render free-tier instance never spins down.
 * Touches no data / no API call — stays fast even on a cold instance.
 */
export async function GET() {
  return NextResponse.json({ pong: true });
}
