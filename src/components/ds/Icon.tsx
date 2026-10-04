"use client";
import type { LucideIcon } from "lucide-react";
import { cx } from "./internal";

export type IconSize = "sm" | "md" | "lg";

const PX: Record<IconSize, number> = { sm: 16, md: 20, lg: 24 };
const STROKE: Record<IconSize, number> = { sm: 1.75, md: 1.5, lg: 1.5 };

/**
 * A Lucide icon at one of the three sizes (16 / 20 / 24 px) with the matching
 * stroke (1.75 / 1.5 / 1.5). Decorative by default (`aria-hidden`); give
 * `label` when the icon alone carries meaning.
 */
export function Icon({
  icon: Glyph,
  size = "sm",
  label,
  className,
}: {
  icon: LucideIcon;
  size?: IconSize;
  label?: string;
  className?: string;
}) {
  return (
    <Glyph
      width={PX[size]}
      height={PX[size]}
      strokeWidth={STROKE[size]}
      className={cx("ds-icon", className)}
      focusable="false"
      {...(label
        ? { role: "img", "aria-label": label }
        : { "aria-hidden": true })}
    />
  );
}
