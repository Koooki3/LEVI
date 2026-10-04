"use client";
import type { HTMLAttributes, ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import {
  Circle,
  CircleCheck,
  CircleDot,
  CircleX,
  Info,
  TriangleAlert,
  X,
} from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { Icon } from "./Icon";
import { cx } from "./internal";

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

/** Status shapes differ as well as colours, so status never rests on colour. */
export const TONE_ICON: Record<Tone, LucideIcon> = {
  neutral: Circle,
  success: CircleCheck,
  warning: TriangleAlert,
  danger: CircleX,
  info: Info,
};

/** A status badge: icon + words in a status colour. */
export function Badge({
  tone = "neutral",
  icon,
  children,
  className,
}: {
  tone?: Tone;
  icon?: LucideIcon | null;
  children: ReactNode;
  className?: string;
}) {
  const glyph = icon === null ? null : (icon ?? TONE_ICON[tone]);
  return (
    <span className={cx("ds-badge", `ds-badge--${tone}`, className)}>
      {glyph && <Icon icon={glyph} />}
      <span>{children}</span>
    </span>
  );
}

export type TagProps = { className?: string } & (
  | { onRemove?: undefined; removeLabel?: undefined; children: ReactNode }
  | {
      onRemove: () => void;
      /** Accessible name of the remove button; default "Remove <text>". */
      removeLabel?: string;
      children: string;
    }
  | {
      onRemove: () => void;
      /** Required when the tag is not plain text. */
      removeLabel: string;
      children: ReactNode;
    }
);

/**
 * A neutral label (category, filter value). With `onRemove` it gets a remove
 * button named "Remove <label>" (pass `removeLabel` when the label is not
 * plain text); the button's target is 24 × 24 px, drawn at 16 px.
 */
export function Tag({ children, onRemove, removeLabel, className }: TagProps) {
  const { t } = useLocale();
  const text = typeof children === "string" ? children : "";
  return (
    <span className={cx("ds-tag", className)}>
      <span>{children}</span>
      {onRemove && (
        <button
          type="button"
          className="ds-tag__remove ds-focus"
          aria-label={removeLabel ?? `${t("Remove")} ${text}`.trim()}
          onClick={onRemove}
        >
          <span className="ds-tag__remove-mark">
            <Icon icon={X} />
          </span>
        </button>
      )}
    </span>
  );
}

/**
 * Shape + colour + text for a row's state. `live` marks a running state:
 * a slow opacity breath every 2 s, static under reduced motion.
 */
export function StatusDot({
  tone = "neutral",
  live = false,
  children,
  className,
}: {
  tone?: Tone;
  live?: boolean;
  children: ReactNode;
  className?: string;
}) {
  return (
    <span className={cx("ds-status", `ds-status--${tone}`, className)}>
      <Icon
        icon={live ? CircleDot : TONE_ICON[tone]}
        className={live ? "ds-breathe" : undefined}
      />
      <span>{children}</span>
    </span>
  );
}

export function Card({
  variant = "default",
  padding = "regular",
  title,
  description,
  actions,
  children,
  className,
  ...rest
}: {
  variant?: "default" | "sunken" | "raised";
  padding?: "compact" | "regular";
  title?: ReactNode;
  description?: ReactNode;
  actions?: ReactNode;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  return (
    <section
      className={cx(
        "ds-card",
        `ds-card--${variant}`,
        `ds-card--${padding}`,
        variant === "sunken" && "ds-on-sunken",
        variant === "raised" && "ds-on-raised",
        className,
      )}
      {...rest}
    >
      {(title || actions) && (
        <header className="ds-card__header">
          <div>
            {title && <h3 className="ds-card__title">{title}</h3>}
            {description && (
              <p className="ds-card__description">{description}</p>
            )}
          </div>
          {actions && <div className="ds-card__actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Divider({
  orientation = "horizontal",
  className,
}: {
  orientation?: "horizontal" | "vertical";
  className?: string;
}) {
  return (
    <div
      role="separator"
      aria-orientation={orientation}
      className={cx("ds-divider", `ds-divider--${orientation}`, className)}
    />
  );
}

/** A key or shortcut, e.g. <Kbd>⌘</Kbd><Kbd>K</Kbd>. */
export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="ds-kbd">{children}</kbd>;
}

/**
 * A static placeholder block in the final layout's shape (no shimmer). Mark
 * the region that is loading with `aria-busy="true"`; skeletons are hidden
 * from assistive technology.
 */
export function Skeleton({
  width,
  height = 16,
  radius = "sm",
  className,
}: {
  width?: number | string;
  height?: number | string;
  radius?: "xs" | "sm" | "md" | "full";
  className?: string;
}) {
  return (
    <span
      aria-hidden="true"
      className={cx("ds-skeleton", `ds-skeleton--${radius}`, className)}
      style={{ width, height }}
    />
  );
}

export function SkeletonText({
  lines = 3,
  className,
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <span aria-hidden="true" className={cx("ds-skeleton-text", className)}>
      {Array.from({ length: lines }, (_, index) => (
        <Skeleton
          key={index}
          height={12}
          width={index === lines - 1 && lines > 1 ? "60%" : "100%"}
        />
      ))}
    </span>
  );
}

/**
 * Empty state: an icon, one sentence, and the next step as a button. For a
 * filter that matched nothing, pass a "Clear filters" action.
 */
export function EmptyState({
  icon,
  title,
  description,
  action,
  secondaryAction,
  className,
}: {
  icon?: LucideIcon;
  title: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  secondaryAction?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cx("ds-empty", className)}>
      {icon && (
        <span className="ds-empty__icon">
          <Icon icon={icon} size="lg" />
        </span>
      )}
      <p className="ds-empty__title">{title}</p>
      {description && <p className="ds-empty__description">{description}</p>}
      {(action || secondaryAction) && (
        <div className="ds-empty__actions">
          {action}
          {secondaryAction}
        </div>
      )}
    </div>
  );
}
