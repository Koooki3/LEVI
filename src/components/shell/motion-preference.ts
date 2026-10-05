"use client";
/**
 * The in-app motion setting: follow the system (default), reduced, or normal.
 * Kept in localStorage under `levi-motion`, applied to <html> as
 * `data-motion="reduce"` or `data-motion="full"` (nothing for "system"), the
 * same attribute tokens.css already reads: "reduce" drops the motion tokens
 * like the system setting does, "full" keeps them even when the system asks
 * for less. A script in the root layout's <head> applies it before the first
 * paint (theme-boot.ts repeats the key; a test keeps them equal).
 */
import { useCallback, useEffect, useState } from "react";
import {
  readBrowserStorage,
  writeBrowserStorage,
} from "@/utils/browserStorage";

export type MotionPreference = "system" | "reduce" | "full";

export const MOTION_STORAGE_KEY = "levi-motion";
export const MOTION_DEFAULT_PREFERENCE: MotionPreference = "system";

export function normalizeMotionPreference(
  value: string | null | undefined,
): MotionPreference {
  return value === "reduce" || value === "full" || value === "system"
    ? value
    : MOTION_DEFAULT_PREFERENCE;
}

export function readMotionPreference(): MotionPreference {
  return normalizeMotionPreference(
    readBrowserStorage("local", MOTION_STORAGE_KEY),
  );
}

export function writeMotionPreference(preference: MotionPreference): boolean {
  return writeBrowserStorage("local", MOTION_STORAGE_KEY, preference);
}

/** "system" removes `data-motion` so the media query decides. */
export function applyMotion(
  element: HTMLElement | null | undefined,
  preference: MotionPreference,
): void {
  if (!element) return;
  if (preference === "system") element.removeAttribute("data-motion");
  else element.setAttribute("data-motion", preference);
}

export function useMotionPreference(): {
  preference: MotionPreference;
  setPreference: (preference: MotionPreference) => void;
  ready: boolean;
} {
  const [preference, setPreferenceState] = useState<MotionPreference>(
    MOTION_DEFAULT_PREFERENCE,
  );
  const [ready, setReady] = useState(false);
  useEffect(() => {
    setPreferenceState(readMotionPreference());
    setReady(true);
    const onStorage = (event: StorageEvent) => {
      if (event.key === null || event.key === MOTION_STORAGE_KEY)
        setPreferenceState(readMotionPreference());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);
  const setPreference = useCallback((next: MotionPreference) => {
    writeMotionPreference(next);
    setPreferenceState(next);
  }, []);
  return { preference, setPreference, ready };
}
