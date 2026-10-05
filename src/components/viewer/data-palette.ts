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

/**
 * The 3D replay's robot materials and studio lights. Three.js reads plain
 * colours, not CSS variables, and these are the models' own paint (an
 * archetype per part), not interface colours: they do not follow the theme.
 */
export const URDF_MATERIAL = {
  /** Used when a mesh carries no colour of its own. */
  fallback: "#c0c4cc",
  /** Neutral off-white plastic. */
  neutral: "#9ba1ab",
  g1Light: "#9ca3af",
  g1Dark: "#1f2937",
  /** Servo housings (SO-arm). */
  servo: "#171a20",
  openArmBase: "#3a3a4a",
  openArmLight: "#f5f5f5",
} as const;
export const URDF_LIGHT = {
  key: "#fff2e3",
  fill: "#bfd9ff",
  rim: "#ffffff",
} as const;

/** Labels drawn on video: near-white words on a dark plate. */
export const MEDIA_LABEL_PLATE = "#000000d9";
export const MEDIA_LABEL_TEXT = "#f5f5f7";

/**
 * Line pattern for the series at `index`: solid for the first eight, then
 * dashed, then dotted, so a ninth series never looks like the first (the
 * colours repeat after eight; the legend shows the same pattern).
 */
export function seriesDash(index: number): string | undefined {
  const round = Math.floor(Math.max(0, index) / DATA_SERIES.length);
  return round === 0 ? undefined : round === 1 ? "6 3" : "1.5 3";
}
