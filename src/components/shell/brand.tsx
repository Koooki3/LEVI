/**
 * The LEVI mark: one graphite tile with a geometric "L" and a square point,
 * on a 32-unit grid. This file is the only definition of its shape. The
 * browser tab icon (`src/app/icon.svg`), the touch icon and `favicon.ico`
 * are drawn from the same numbers (`scripts/brand_icons.py`; a test checks
 * that `icon.svg` matches `LEVI_MARK`).
 *
 * In the page the tile is `--ds-accent` and the glyph `--ds-on-accent`, so
 * the mark is black on light and white on dark, like the primary button.
 */
import type { CSSProperties } from "react";

export const LEVI_MARK = {
  /** The grid. */
  size: 32,
  /** Corner radius of the tile. */
  radius: 7,
  /** The glyph as rectangles [x, y, width, height]: the L's stem, its foot,
   * and the point at the top right. */
  rects: [
    [9, 8, 4, 16],
    [9, 20, 14, 4],
    [19, 8, 4, 4],
  ] as const,
};

/** The glyph as one SVG path (what `icon.svg` carries). */
export const LEVI_MARK_PATH = LEVI_MARK.rects
  .map(([x, y, w, h]) => `M${x} ${y}h${w}v${h}h-${w}z`)
  .join("");

export function LeviMark({
  size = 20,
  className,
  style,
}: {
  size?: number;
  className?: string;
  style?: CSSProperties;
}) {
  const grid = LEVI_MARK.size;
  return (
    <svg
      className={["levi-mark", className].filter(Boolean).join(" ")}
      style={style}
      width={size}
      height={size}
      viewBox={`0 0 ${grid} ${grid}`}
      aria-hidden="true"
      focusable="false"
    >
      <rect
        className="levi-mark__tile"
        width={grid}
        height={grid}
        rx={LEVI_MARK.radius}
      />
      <path className="levi-mark__glyph" d={LEVI_MARK_PATH} />
    </svg>
  );
}

/** Mark and name together, as in the top bar. The name is real text. */
export function LeviWordmark({
  size = 20,
  className,
}: {
  size?: number;
  className?: string;
}) {
  return (
    <span className={["levi-wordmark", className].filter(Boolean).join(" ")}>
      <LeviMark size={size} />
      <span className="levi-wordmark__name">LEVI</span>
    </span>
  );
}
