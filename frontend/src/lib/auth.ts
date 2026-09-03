"use client";

/** Session-token helpers for the 6-digit PIN gate.
 *
 * Token lives in localStorage (single-user dashboard; XSS surface is our own
 * static bundle). On any 401 the api layer fires AUTH_EXPIRED_EVENT so the
 * AuthGate overlay can flip back to the PIN screen.
 */
const TOKEN_KEY = "tdapp_session_token";

export const AUTH_EXPIRED_EVENT = "tdapp:auth-expired";

export function getToken(): string | null {
  if (typeof window === "undefined") return null;
  return window.localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  window.localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  window.localStorage.removeItem(TOKEN_KEY);
}

export function notifyAuthExpired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
  }
}
