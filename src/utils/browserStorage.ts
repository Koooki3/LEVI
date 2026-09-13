// Modified for LEVI (2026); see NOTICE and docs/UPSTREAM.md.

/**
 * Storage access for browser-only preferences and drafts.
 *
 * Storage can be unavailable in private browsing, blocked third-party
 * contexts, sandboxed iframes, or after a user has exhausted the quota. The
 * workbench should keep its in-memory behavior in all of those cases, so
 * every operation is deliberately best-effort.
 */
export type BrowserStorageArea = "local" | "session";

function getStorage(area: BrowserStorageArea): Storage | null {
  if (typeof window === "undefined") return null;
  try {
    return area === "local" ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

export function readBrowserStorage(
  area: BrowserStorageArea,
  key: string,
): string | null {
  try {
    return getStorage(area)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeBrowserStorage(
  area: BrowserStorageArea,
  key: string,
  value: string,
): boolean {
  try {
    const storage = getStorage(area);
    if (!storage) return false;
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

export function removeBrowserStorage(
  area: BrowserStorageArea,
  key: string,
): boolean {
  try {
    const storage = getStorage(area);
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}

export function listBrowserStorageKeys(
  area: BrowserStorageArea,
  prefix?: string,
): string[] {
  try {
    const storage = getStorage(area);
    if (!storage) return [];
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key && (prefix === undefined || key.startsWith(prefix)))
        keys.push(key);
    }
    return keys;
  } catch {
    return [];
  }
}

export function browserStorageAvailable(area: BrowserStorageArea): boolean {
  try {
    const storage = getStorage(area);
    if (!storage) return false;
    const probe = `__levi_storage_probe_${area}__`;
    storage.setItem(probe, "1");
    storage.removeItem(probe);
    return true;
  } catch {
    return false;
  }
}
