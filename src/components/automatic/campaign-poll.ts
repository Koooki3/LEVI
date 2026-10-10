"use client";
import { useCallback, useEffect, useRef, useState } from "react";

/** Reads `load` every `intervalMs` while the page is visible; after a failure
 * it waits longer (up to 4x) and keeps the last good value. `refresh` reads at
 * once (after a button press). */
export function usePolled<T>(
  load: () => Promise<T>,
  intervalMs = 2000,
): {
  value: T | null;
  error: string | null;
  loading: boolean;
  refresh: () => void;
} {
  const [value, setValue] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;
  const kick = useRef<() => void>(() => {});
  const refresh = useCallback(() => kick.current(), []);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    let busy = false;
    const tick = async () => {
      if (!alive || busy) return;
      busy = true;
      clearTimeout(timer);
      try {
        const next = await loadRef.current();
        if (!alive) return;
        failures = 0;
        setValue(next);
        setError(null);
      } catch (e) {
        if (!alive) return;
        failures += 1;
        setError(e instanceof Error ? e.message : String(e));
      } finally {
        busy = false;
        if (alive) {
          setLoading(false);
          schedule(intervalMs * Math.min(4, 1 + failures));
        }
      }
    };
    // Look again after `delay`; while the page is hidden keep waiting, so
    // nothing is asked of the server that nobody sees.
    const schedule = (delay: number) => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!alive) return;
        if (document.visibilityState === "hidden") schedule(intervalMs);
        else void tick();
      }, delay);
    };
    kick.current = () => void tick();
    void tick();
    return () => {
      alive = false;
      clearTimeout(timer);
    };
  }, [intervalMs]);

  return { value, error, loading, refresh };
}
