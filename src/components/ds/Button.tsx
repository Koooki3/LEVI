"use client";
import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { LoaderCircle } from "lucide-react";
import { Icon } from "./Icon";
import { cx } from "./internal";

export type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
export type ButtonSize = "sm" | "md" | "lg";

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Shows a spinner, sets `aria-busy` and blocks clicks; the label stays. */
  loading?: boolean;
  icon?: LucideIcon;
  iconEnd?: LucideIcon;
  children?: ReactNode;
}

/**
 * Buttons. One `primary` per screen (graphite fill); `secondary` is outlined,
 * `ghost` has no frame until hovered, `danger` is only for an irreversible
 * action. Sizes: sm 28 px, md 32 px (default), lg 40 px.
 */
export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      size = "md",
      loading = false,
      icon,
      iconEnd,
      disabled,
      className,
      children,
      type = "button",
      onClick,
      ...rest
    },
    ref,
  ) {
    return (
      <button
        ref={ref}
        type={type}
        className={cx(
          "ds-btn ds-focus",
          `ds-btn--${variant}`,
          `ds-btn--${size}`,
          className,
        )}
        disabled={disabled}
        aria-busy={loading || undefined}
        aria-disabled={loading || undefined}
        onClick={(event) => {
          if (loading) {
            event.preventDefault();
            return;
          }
          onClick?.(event);
        }}
        {...rest}
      >
        {loading ? (
          <Icon icon={LoaderCircle} className="ds-spin" />
        ) : icon ? (
          <Icon icon={icon} />
        ) : null}
        {children !== undefined && (
          <span className="ds-btn__label">{children}</span>
        )}
        {iconEnd && !loading && <Icon icon={iconEnd} />}
      </button>
    );
  },
);
