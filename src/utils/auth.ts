// Client-side helpers for the HF OAuth flow. The token is owned by
// AuthProvider (src/context/auth-context.tsx); these read-only helpers exist
// so non-React fetch utilities can attach Authorization: Bearer <token>
// without going through React.

import { readBrowserStorage, removeBrowserStorage } from "./browserStorage";

const STORAGE_KEY = "levi-hf-auth-v1";
export const LEGACY_AUTH_STORAGE_KEYS = ["lerobot-viz-oauth"] as const;

export function getAuthToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = readBrowserStorage("local", STORAGE_KEY);
    if (!stored) return null;
    const parsed = JSON.parse(stored) as { accessToken?: string };
    return parsed.accessToken?.trim() || null;
  } catch {
    return null;
  }
}

/** The token LEVI's own file service requires, and only for that service.
 *
 * In the browser these requests go through the same-origin /api/levi proxy,
 * which attaches the token server-side. During server rendering there is no
 * proxy in front of them: the fetch goes straight to the loopback API, which
 * rejects it without this header. The URL is checked because the same helper
 * builds headers for Hugging Face requests, and LEVI's internal token must
 * never travel to the Hub.
 */
function leviServiceToken(url?: string): string | null {
  if (typeof window !== "undefined" || !url) return null;
  const backend = process.env.LEVI_BACKEND_URL;
  if (!backend || !url.startsWith(backend)) return null;
  return process.env.LEVI_UI_TOKEN || null;
}

/** Whether this URL is served by LEVI itself rather than by the Hub.
 *
 * Local datasets are served from the same origin (through /api/levi) in the
 * browser and from the loopback API during server rendering.
 */
function isLeviService(url?: string): boolean {
  if (!url) return false;
  if (url.startsWith("/api/levi") || url.startsWith("/annotations/api"))
    return true;
  const backend = process.env.LEVI_BACKEND_URL;
  return Boolean(backend && url.startsWith(backend));
}

export function authHeaders(url?: string): Record<string, string> {
  const headers: Record<string, string> = {};
  // A Hugging Face credential belongs to the Hub. Sending it to LEVI's own
  // file service leaks it to a service that has no use for it, and LEVI reads
  // an unrecognised Bearer token as a failed Agent credential -- which is how
  // signing in to the Hub used to make every local dataset answer 401.
  const accessToken = isLeviService(url) ? null : getAuthToken();
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const internal = leviServiceToken(url);
  if (internal) headers["x-levi-ui-token"] = internal;
  return headers;
}

export const AUTH_STORAGE_KEY = STORAGE_KEY;

export function clearLegacyAuthStorage(): void {
  if (typeof window === "undefined") return;
  for (const key of LEGACY_AUTH_STORAGE_KEYS)
    removeBrowserStorage("local", key);
}

// Native video elements cannot carry an Authorization header. To play videos
// from private datasets, we route them through our same-origin /api/proxy
// endpoint, which reads the access token from an HttpOnly cookie set during
// sign-in and forwards the request to huggingface.co. Returns the original
// URL when running server-side or when the user is not signed in.
export function proxyHfUrl(url: string): string {
  if (typeof window === "undefined") return url;
  if (!getAuthToken()) return url;
  try {
    const parsed = new URL(url);
    if (parsed.hostname !== "huggingface.co") return url;
    return `/api/proxy${parsed.pathname}${parsed.search}`;
  } catch {
    return url;
  }
}
