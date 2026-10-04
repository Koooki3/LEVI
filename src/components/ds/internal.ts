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

/** Open modal layers, innermost last: only the top one handles keys. */
const modalStack: HTMLElement[] = [];

/**
 * Modal focus handling: on open, remember the focused element and move focus
 * inside (`initialFocus`, else the first focusable, else the container).
 * The listeners sit on `document`, so they work wherever focus is: Tab and
 * Shift+Tab wrap inside, focus that lands outside (a click on the scrim, a
 * stray focus()) is pulled back, Escape calls `onEscape`. With nested
 * modals only the innermost one reacts. On close, focus goes back to the
 * element that had it.
 *
 * `trap: false` is for a non-modal layer (a side panel next to the page):
 * focus still moves in on open and back on close, but Tab may leave, focus
 * elsewhere is not pulled back, and Escape closes it only while focus is
 * inside.
 */
export function useModalFocus(
  containerRef: RefObject<HTMLElement | null>,
  open: boolean,
  options: {
    initialFocus?: RefObject<HTMLElement | null>;
    onEscape?: () => void;
    trap?: boolean;
  } = {},
): void {
  const onEscapeRef = useRef(options.onEscape);
  onEscapeRef.current = options.onEscape;
  const initialFocus = options.initialFocus;
  const trap = options.trap ?? true;

  useEffect(() => {
    if (!open) return;
    const container = containerRef.current;
    if (!container) return;
    const previous =
      typeof document !== "undefined"
        ? (document.activeElement as HTMLElement | null)
        : null;
    // A modal opened in the same render as one nested inside it (effects
    // run inner first) goes below the inner one and leaves focus there.
    const nested = modalStack.findIndex(
      (other) => other !== container && container.contains(other),
    );
    if (nested >= 0) modalStack.splice(nested, 0, container);
    else modalStack.push(container);
    const isTop = () => modalStack[modalStack.length - 1] === container;
    const focusEdge = (end: "first" | "last") => {
      const items = focusableIn(container);
      const target =
        (end === "first" ? items[0] : items[items.length - 1]) ?? container;
      target.focus();
    };
    if (nested < 0)
      (initialFocus?.current ?? focusableIn(container)[0] ?? container).focus();

    // Capture phase: runs before anything inside can move focus out.
    const onTab = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !trap || !isTop()) return;
      const items = focusableIn(container);
      const active = document.activeElement;
      if (!active || !container.contains(active)) {
        event.preventDefault();
        focusEdge(event.shiftKey ? "last" : "first");
        return;
      }
      if (items.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && (active === first || active === container)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    // Bubble phase: a menu or tooltip inside that handles Escape itself
    // (stopPropagation / preventDefault) closes first, not the modal.
    const onEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !isTop() || event.defaultPrevented) return;
      if (!trap && !container.contains(document.activeElement)) return;
      event.preventDefault();
      // Handled: listeners on window (page shortcuts) do not see it, as
      // with a native confirm().
      event.stopPropagation();
      onEscapeRef.current?.();
    };
    const onFocusIn = (event: FocusEvent) => {
      if (!trap || !isTop()) return;
      const target = event.target as Node | null;
      if (target && target !== document && !container.contains(target))
        focusEdge("first");
    };
    document.addEventListener("keydown", onTab, true);
    document.addEventListener("keydown", onEscape);
    document.addEventListener("focusin", onFocusIn, true);
    return () => {
      document.removeEventListener("keydown", onTab, true);
      document.removeEventListener("keydown", onEscape);
      document.removeEventListener("focusin", onFocusIn, true);
      const index = modalStack.lastIndexOf(container);
      if (index >= 0) modalStack.splice(index, 1);
      if (
        previous &&
        typeof previous.focus === "function" &&
        previous.isConnected
      )
        previous.focus();
    };
  }, [open, containerRef, initialFocus, trap]);
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

/**
 * For a modal layer's panel (`onKeyDown`): keys typed inside a modal stay
 * inside, as with a native dialog, so page shortcuts bound on window (Space,
 * arrows, J/K, Ctrl/⌘+S/Z/Y) do not act behind it. Tab and Escape go on: the
 * layer's own document listeners handle them.
 */
export function keepKeysInside<
  E extends { key: string; stopPropagation: () => void },
>(event: E, passes?: (event: E) => boolean): void {
  if (event.key === "Tab" || event.key === "Escape") return;
  if (passes?.(event)) return;
  event.stopPropagation();
}
