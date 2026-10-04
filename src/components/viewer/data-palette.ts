/**
 * The viewer's data palette (time segments, chart series, masks), as CSS
 * variables defined in viewer.css (`--dv-1` … `--dv-8`, light and dark
 * steps). Interface colours are never taken from here, and data colours are
 * never used for text: a coloured mark always sits beside words in a text
 * colour (legend, lane name, label).
 */

/** The categorical slots in their fixed order (never re-ordered by rank). */
export const DATA_SERIES = [
  "var(--dv-1)",
  "var(--dv-2)",
  "var(--dv-3)",
  "var(--dv-4)",
  "var(--dv-5)",
  "var(--dv-6)",
  "var(--dv-7)",
  "var(--dv-8)",
] as const;

/**
 * The colour of the series at `index`. More than eight series repeat the
 * slots, so a chart with many series must still name each one (legend).
 */
export function seriesColor(index: number): string {
  const n = DATA_SERIES.length;
  return DATA_SERIES[((index % n) + n) % n];
}

/** Positive / negative / neutral data (advantage, value, pass/flag bars). */
export const DATA_POSITIVE = "var(--dv-positive)";
export const DATA_NEGATIVE = "var(--dv-negative)";
export const DATA_NEUTRAL = "var(--dv-neutral)";

/**
 * The same eight slots as plain colours for drawing on video and canvas:
 * the dark steps of the design tokens --ds-data-1 … --ds-data-8 (same
 * values; media is black in both themes). A test keeps them equal to the
 * dark block of viewer.css. Canvas and the 3D scene cannot read CSS
 * variables, which is why they are spelled out here.
 */
export const DATA_ON_MEDIA = [
  "#3987e5",
  "#d95926",
  "#199e70",
  "#c98500",
  "#d55181",
  "#008300",
  "#9085e9",
  "#e66767",
] as const;

/** The on-media colour at `index` (repeats after eight). */
export function mediaColor(index: number): string {
  const n = DATA_ON_MEDIA.length;
  return DATA_ON_MEDIA[((index % n) + n) % n];
}

/**
 * The 3D replay's scene: black like video (proposal §4.6), with the dark
 * theme's separator greys for the floor grid.
 */
export const MEDIA_BACKGROUND = "#000000";
export const MEDIA_GRID = "#3a3a3c";
export const MEDIA_GRID_SECTION = "#48484a";

/** Labels drawn on video: near-white words on a dark plate. */
export const MEDIA_LABEL_PLATE = "#000000d9";
export const MEDIA_LABEL_TEXT = "#f5f5f7";
