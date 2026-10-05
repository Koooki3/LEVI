"use client";
import { useEffect, useRef } from "react";

/**
 * While `active`, Escape calls `onEscape` (e.g. to leave an enlarged video).
 * Ignored during IME composition, when another handler already took the key
 * (`defaultPrevented`), and while a modal layer is open (it handles Escape).
 */
export function useEscape(active: boolean, onEscape: () => void): void {
  const handler = useRef(onEscape);
  handler.current = onEscape;
  useEffect(() => {
    if (!active) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || event.isComposing || event.defaultPrevented)
        return;
      if (document.querySelector('[aria-modal="true"]')) return;
      event.preventDefault();
      handler.current();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active]);
}
