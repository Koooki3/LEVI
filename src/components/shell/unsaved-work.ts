import type { MouseEvent } from "react";
import { requestGo } from "./shell-events";

/**
 * Whether something the person typed or drew is not saved yet (the annotation
 * editor's draft). The pages that hold such work say so with
 * `setUnsavedWork(true)`; the frame asks before a keyboard jump (G then a
 * letter) would leave it behind. A module-level flag: the frame sits above the
 * pages and cannot read their state.
 */
let unsaved = false;
export function setUnsavedWork(value: boolean): void {
  unsaved = value;
}
export function hasUnsavedWork(): boolean {
  return unsaved;
}

/**
 * A navigation that is not a keyboard jump (the palette, the Jobs menu, a
 * toast action) goes through the frame's confirmation when work is unsaved,
 * and straight to `push` otherwise.
 */
export function goGuarded(push: (href: string) => void, href: string): void {
  if (hasUnsavedWork()) requestGo(href);
  else push(href);
}

/**
 * Capture-phase click handler for the header (brand and every link in it): a
 * plain left click on an internal link, while work is unsaved, becomes a
 * guarded `requestGo` instead of the link's own navigation. Modified clicks
 * (new tab, download, another target) leave the page alone and pass through.
 */
export function guardLinkClick(event: MouseEvent): void {
  if (!hasUnsavedWork() || event.defaultPrevented) return;
  if (event.button !== 0) return;
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const target = event.target;
  if (!(target instanceof Element)) return;
  const anchor = target.closest("a[href]");
  if (!anchor || anchor.hasAttribute("download")) return;
  const where = anchor.getAttribute("target");
  if (where && where !== "_self") return;
  const href = anchor.getAttribute("href") ?? "";
  if (!href.startsWith("/") || href.startsWith("//")) return;
  event.preventDefault();
  event.stopPropagation();
  requestGo(href);
}
