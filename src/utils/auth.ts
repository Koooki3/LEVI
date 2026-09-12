// Client-side helpers for the HF OAuth flow. The token is owned by
// AuthProvider (src/context/auth-context.tsx); these read-only helpers exist
// so non-React fetch utilities can attach Authorization: Bearer <token>
// without going through React.

const STORAGE_KEY = "levi-hf-auth-v1";
export const LEGACY_AUTH_STORAGE_KEYS = ["lerobot-viz-oauth"] as const;

export function getAuthToken(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (!stored) return null;
    const parsed = JSON.parse(stored) as { accessToken?: string };
    return parsed.accessToken?.trim() || null;
  } catch {
    return null;
  }
}

export function authHeaders(): Record<string, string> {
  const accessToken = getAuthToken();
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

export const AUTH_STORAGE_KEY = STORAGE_KEY;

export function clearLegacyAuthStorage(): void {
  if (typeof window === "undefined") return;
  for (const key of LEGACY_AUTH_STORAGE_KEYS)
    window.localStorage.removeItem(key);
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
