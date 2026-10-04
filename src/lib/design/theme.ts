"use client";
/**
 * Light/dark theme preference for the design system.
 *
 * The preference is "system" (follow `prefers-color-scheme`), "light" or
 * "dark", kept in localStorage under `levi-theme`. Storage may be missing or
 * throw (private windows, blocked site data); every read and write goes
 * through `browserStorage`, which never throws. Without a stored value the
 * preference is THEME_DEFAULT_PREFERENCE.
 *
 * Nothing here touches the page by itself: the caller decides where
 * `data-theme` goes (`applyTheme`); the global frame applies it to <html>.
 */
import { useCallback, useEffect, useState } from "react";
import {
  readBrowserStorage,
  removeBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "levi-theme";

/**
 * The preference when none is stored. Transitional: "dark" until the pages
 * use the tokens (design stage 5), because a light frame over the still-dark
 * pages looks broken; stage 5 sets it back to "system". An explicit choice,
 * "system" included, is stored and always wins. The pre-paint script
 * (components/shell/theme-boot.ts) repeats this value; a test keeps them equal.
 */
export const THEME_DEFAULT_PREFERENCE: ThemePreference = "dark";
const DARK_QUERY = "(prefers-color-scheme: dark)";

export function normalizeThemePreference(
  value: string | null | undefined,
): ThemePreference {
  return value === "light" || value === "dark" || value === "system"
    ? value
    : THEME_DEFAULT_PREFERENCE;
}

export function readThemePreference(): ThemePreference {
  return normalizeThemePreference(
    readBrowserStorage("local", THEME_STORAGE_KEY),
  );
}

/**
 * Store the preference; the default (THEME_DEFAULT_PREFERENCE) removes the
 * key, so no stored value always means "the default". False when storage
 * fails.
 */
export function writeThemePreference(preference: ThemePreference): boolean {
  return preference === THEME_DEFAULT_PREFERENCE
    ? removeBrowserStorage("local", THEME_STORAGE_KEY)
    : writeBrowserStorage("local", THEME_STORAGE_KEY, preference);
}

export function resolveTheme(
  preference: ThemePreference,
  systemDark: boolean,
): ResolvedTheme {
  if (preference === "system") return systemDark ? "dark" : "light";
  return preference;
}

export function systemPrefersDark(): boolean {
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(DARK_QUERY).matches
    );
  } catch {
    return false;
  }
}

/**
 * Put the preference on an element: "system" removes `data-theme` so the
 * tokens follow the media query; "light"/"dark" set it.
 */
export function applyTheme(
  element: HTMLElement | null | undefined,
  preference: ThemePreference,
): void {
  if (!element) return;
  if (preference === "system") element.removeAttribute("data-theme");
  else element.setAttribute("data-theme", preference);
}

/**
 * The stored preference, a setter that persists it, and the theme it resolves
 * to now. Follows system changes and other tabs (the `storage` event).
 * Before mount it reports the default preference (and a light system) so
 * server and client render alike; `ready` turns true once the stored value
 * has been read, so a caller applying the theme never applies that
 * placeholder over what is stored.
 */
export function useThemePreference(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  setPreference: (preference: ThemePreference) => void;
  ready: boolean;
} {
  const [preference, setPreferenceState] = useState<ThemePreference>(
    THEME_DEFAULT_PREFERENCE,
  );
  const [systemDark, setSystemDark] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setPreferenceState(readThemePreference());
    setReady(true);
    setSystemDark(systemPrefersDark());
    let query: MediaQueryList | null = null;
    try {
      query =
        typeof window.matchMedia === "function"
          ? window.matchMedia(DARK_QUERY)
          : null;
    } catch {
      query = null;
    }
    const onSystem = (event: MediaQueryListEvent) =>
      setSystemDark(event.matches);
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === THEME_STORAGE_KEY)
        setPreferenceState(readThemePreference());
    };
    query?.addEventListener?.("change", onSystem);
    window.addEventListener("storage", onStorage);
    return () => {
      query?.removeEventListener?.("change", onSystem);
      window.removeEventListener("storage", onStorage);
    };
  }, []);

  const setPreference = useCallback((next: ThemePreference) => {
    writeThemePreference(next);
    setPreferenceState(next);
  }, []);

  return {
    preference,
    resolved: resolveTheme(preference, systemDark),
    setPreference,
    ready,
  };
}
