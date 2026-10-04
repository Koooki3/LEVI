"use client";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Icon } from "./Icon";
import { Tooltip } from "./Tooltip";
import { cx } from "./internal";

export interface IconButtonProps extends Omit<
  ButtonHTMLAttributes<HTMLButtonElement>,
  "aria-label"
> {
  icon: LucideIcon;
  /** Required: the accessible name, also shown as the visible tooltip. */
  label: string;
  size?: "sm" | "md" | "lg";
  variant?: "ghost" | "secondary";
  /** Optional shortcut shown in the tooltip, e.g. <Kbd>⌘K</Kbd>. */
  shortcut?: ReactNode;
  tooltipPlacement?: "top" | "bottom";
  /** Toggle buttons: sets aria-pressed and the selected look. */
  pressed?: boolean;
}

/**
 * A button that shows only an icon. It always has an `aria-label` and a
 * tooltip with the same words (hover and keyboard focus), never `title=`.
 */
export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(
  function IconButton(
    {
      icon,
      label,
      size = "md",
      variant = "ghost",
      shortcut,
      tooltipPlacement = "top",
      pressed,
      className,
      type = "button",
      ...rest
    },
    ref,
  ) {
    return (
      <Tooltip
        content={label}
        describe={false}
        shortcut={shortcut}
        placement={tooltipPlacement}
      >
        <button
          ref={ref}
          type={type}
          aria-label={label}
          aria-pressed={pressed}
          className={cx(
            "ds-icon-btn ds-focus",
            `ds-icon-btn--${size}`,
            `ds-icon-btn--${variant}`,
            className,
          )}
          {...rest}
        >
          <Icon icon={icon} size={size === "lg" ? "md" : "sm"} />
        </button>
      </Tooltip>
    );
  },
);
