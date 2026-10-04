"use client";
/**
 * Reduced-motion helpers shared by the design-system components.
 *
 * CSS components need nothing from here: the motion tokens in tokens.css
 * drop to 0 under `prefers-reduced-motion: reduce` (or `data-motion="reduce"`
 * on an ancestor). Components that change what they *render* when motion is
 * reduced (indeterminate progress shows words instead of a moving bar, the
 * reorder list snaps) use `usePrefersReducedMotion`.
 */
import { useEffect, useState } from "react";

const REDUCE_QUERY = "(prefers-reduced-motion: reduce)";

export function prefersReducedMotion(element?: Element | null): boolean {
  if (element?.closest?.('[data-motion="reduce"]')) return true;
  try {
    return (
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia(REDUCE_QUERY).matches
    );
  } catch {
    return false;
  }
}

/** True when the person asked the system for less motion. */
export function usePrefersReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);
  useEffect(() => {
    setReduced(prefersReducedMotion());
    let query: MediaQueryList | null = null;
    try {
      query =
        typeof window.matchMedia === "function"
          ? window.matchMedia(REDUCE_QUERY)
          : null;
    } catch {
      query = null;
    }
    const onChange = (event: MediaQueryListEvent) => setReduced(event.matches);
    query?.addEventListener?.("change", onChange);
    return () => query?.removeEventListener?.("change", onChange);
  }, []);
  return reduced;
}

/** Motion durations in ms, mirroring tokens.css (§4.9 of the proposal). */
export const DURATION = {
  instant: 0,
  fast: 120,
  base: 200,
  baseExit: 140,
  slow: 320,
  slowExit: 220,
} as const;

/** Breakpoints in px (§4.8); CSS media queries cannot read variables. */
export const BREAKPOINT = { sm: 640, md: 900, lg: 1200, xl: 1440 } as const;
