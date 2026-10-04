"use client";
/**
 * Token values for code that cannot use CSS variables: canvas and WebGL
 * (three.js) drawing do not resolve `var(--ds-…)`. (Recharts' `stroke` and
 * `fill` accept `var(--ds-…)` in current browsers; the report reads the
 * values anyway, which also works where they do not.) `useCssTokens(names)` reads the current values from
 * <html> and reads them again when the theme changes (a `data-theme`
 * change or the system switching light and dark). Before mount it returns
 * the `fallback` (or empty strings).
 */
import { useEffect, useState } from "react";

export function readCssTokens<T extends string>(
  names: readonly T[],
  read: (name: string) => string,
): Record<T, string> {
  const out = {} as Record<T, string>;
  for (const name of names) out[name] = read(name).trim();
  return out;
}

export function useCssTokens<T extends string>(
  names: readonly T[],
  fallback?: Partial<Record<T, string>>,
): Record<T, string> {
  const key = names.join(",");
  const [values, setValues] = useState<Record<T, string>>(() =>
    readCssTokens(names, (name) => fallback?.[name as T] ?? ""),
  );
  useEffect(() => {
    const list = key.split(",") as T[];
    const update = () => {
      const style = getComputedStyle(document.documentElement);
      setValues(readCssTokens(list, (name) => style.getPropertyValue(name)));
    };
    update();
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["data-theme", "class", "style"],
    });
    const media = window.matchMedia?.("(prefers-color-scheme: dark)");
    media?.addEventListener?.("change", update);
    return () => {
      observer.disconnect();
      media?.removeEventListener?.("change", update);
    };
  }, [key]);
  return values;
}

/** The eight data colours, in their fixed order. */
export const DATA_TOKENS = [
  "--ds-data-1",
  "--ds-data-2",
  "--ds-data-3",
  "--ds-data-4",
  "--ds-data-5",
  "--ds-data-6",
  "--ds-data-7",
  "--ds-data-8",
] as const;
