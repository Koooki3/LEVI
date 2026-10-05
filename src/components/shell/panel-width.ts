/**
 * The Agent Workbench drawer's width, in px, for its resize handle (a
 * `role="separator"` that people drag or move with the arrow keys). The
 * same numbers set `aria-valuemin` / `aria-valuenow` / `aria-valuemax`, so a
 * screen reader hears the width the drawer really has.
 */

/** The narrowest drawer (shell.css `.levi-agent-sheet` min-width). */
export const PANEL_MIN = 360;
/** The width before anyone resizes it (shell.css `.levi-agent-sheet`). */
export const PANEL_DEFAULT = 490;
/** Space kept free beside the drawer. */
export const PANEL_GUTTER = 32;
/** Arrow-key steps (Shift for the larger one). */
export const PANEL_STEP = 24;
export const PANEL_STEP_LARGE = 80;

export function panelMax(viewport: number): number {
  return Math.max(PANEL_MIN, Math.round(viewport) - PANEL_GUTTER);
}

export function clampPanelWidth(width: number, viewport: number): number {
  return Math.min(Math.max(PANEL_MIN, Math.round(width)), panelMax(viewport));
}

/** The width after a key on the handle, or null for another key. The handle
 * is the drawer's left edge: ← widens it, → narrows it. */
export function panelWidthForKey(
  key: string,
  shift: boolean,
  current: number | null,
  viewport: number,
): number | null {
  const base = current ?? PANEL_DEFAULT;
  const step = shift ? PANEL_STEP_LARGE : PANEL_STEP;
  if (key === "ArrowLeft") return clampPanelWidth(base + step, viewport);
  if (key === "ArrowRight") return clampPanelWidth(base - step, viewport);
  if (key === "Home") return PANEL_MIN;
  if (key === "End") return panelMax(viewport);
  return null;
}

/** The separator's ARIA values for the drawer's current width. */
export function panelAria(current: number | null, viewport: number) {
  const max = panelMax(viewport);
  return {
    "aria-valuemin": PANEL_MIN,
    "aria-valuemax": max,
    "aria-valuenow": clampPanelWidth(current ?? PANEL_DEFAULT, viewport),
  };
}
