"use client";
import { useEffect, useRef, type RefObject } from "react";

/** Join class names, skipping empty values. */
export function cx(...names: Array<string | false | null | undefined>): string {
  return names.filter(Boolean).join(" ");
}

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
  "[contenteditable='true']",
].join(",");

export function focusableIn(root: HTMLElement): HTMLElement[] {
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE)).filter(
    (element) => !element.hasAttribute("hidden") && !element.closest("[inert]"),
  );
}

/**
 * Modal focus handling: on open, remember the focused element and move focus
 * inside (`initialFocus`, else the first focusable, else the container);
 * Tab and Shift+Tab wrap inside; Escape calls `onEscape`; on close, focus goes
 * back to the element that had it.
 */
export function useModalFocus(
  containerRef: RefObject<HTMLElement | null>,
  open: boolean,
  options: {
    initialFocus?: RefObject<HTMLElement | null>;
    onEscape?: () => void;
  } = {},
): void {
  const onEscapeRef = useRef(options.onEscape);
  onEscapeRef.current = options.onEscape;
  const initialFocus = options.initialFocus;

  useEffect(() => {
    if (!open) return;
    const container = containerRef.current;
    if (!container) return;
    const previous =
      typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null;
    const target =
      initialFocus?.current ?? focusableIn(container)[0] ?? container;
    target.focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        event.preventDefault();
        onEscapeRef.current?.();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusableIn(container);
      if (items.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === container)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    container.addEventListener("keydown", onKeyDown);
    return () => {
      container.removeEventListener("keydown", onKeyDown);
      if (
        previous &&
        typeof previous.focus === "function" &&
        previous.isConnected
      )
        previous.focus();
    };
  }, [open, containerRef, initialFocus]);
}

/** Roving focus over a list: returns the index the key moves to, or null. */
export function rovingIndex(
  key: string,
  current: number,
  count: number,
  orientation: "horizontal" | "vertical" = "horizontal",
  isDisabled: (index: number) => boolean = () => false,
): number | null {
  if (count === 0) return null;
  const next = orientation === "horizontal" ? "ArrowRight" : "ArrowDown";
  const prev = orientation === "horizontal" ? "ArrowLeft" : "ArrowUp";
  let step: number;
  let start: number;
  if (key === next) {
    step = 1;
    start = current;
  } else if (key === prev) {
    step = -1;
    start = current;
  } else if (key === "Home") {
    step = 1;
    start = -1;
  } else if (key === "End") {
    step = -1;
    start = count;
  } else return null;
  for (let i = 1; i <= count; i += 1) {
    const index =
      key === "Home" || key === "End"
        ? start + step * i
        : (((start + step * i) % count) + count) % count;
    if (index < 0 || index >= count) break;
    if (!isDisabled(index)) return index;
  }
  return null;
}
