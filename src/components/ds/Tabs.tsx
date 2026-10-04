"use client";
import { useId, useRef, type KeyboardEvent, type ReactNode } from "react";
import type { LucideIcon } from "lucide-react";
import { Icon } from "./Icon";
import { cx, rovingIndex } from "./internal";

export type TabItem = {
  id: string;
  label: ReactNode;
  icon?: LucideIcon;
  disabled?: boolean;
  content?: ReactNode;
};

/**
 * Tabs with the ARIA tabs pattern: one tab stop, ←/→ move and select,
 * Home/End jump. Switching is instant (no animation, proposal §5).
 * Controlled: pass `value` and `onChange`.
 */
export function Tabs({
  items,
  value,
  onChange,
  label,
  className,
}: {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  /** Accessible name of the tab list. */
  label: string;
  className?: string;
}) {
  const base = useId();
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const current = Math.max(
    0,
    items.findIndex((item) => item.id === value),
  );
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const index = rovingIndex(
      event.key,
      current,
      items.length,
      "horizontal",
      (i) => Boolean(items[i]?.disabled),
    );
    if (index === null) return;
    event.preventDefault();
    onChange(items[index].id);
    refs.current[index]?.focus();
  };
  const selected = items[current];
  return (
    <div className={cx("ds-tabs", className)}>
      <div
        role="tablist"
        aria-label={label}
        className="ds-tabs__list"
        onKeyDown={onKeyDown}
      >
        {items.map((item, index) => {
          const isSelected = index === current;
          return (
            <button
              key={item.id}
              ref={(element) => {
                refs.current[index] = element;
              }}
              type="button"
              role="tab"
              id={`${base}-tab-${item.id}`}
              aria-selected={isSelected}
              aria-controls={`${base}-panel-${item.id}`}
              tabIndex={isSelected ? 0 : -1}
              disabled={item.disabled}
              className="ds-tab ds-focus"
              onClick={() => onChange(item.id)}
            >
              {item.icon && <Icon icon={item.icon} />}
              <span>{item.label}</span>
            </button>
          );
        })}
      </div>
      {selected?.content !== undefined && (
        <div
          role="tabpanel"
          id={`${base}-panel-${selected.id}`}
          aria-labelledby={`${base}-tab-${selected.id}`}
          tabIndex={0}
          className="ds-tabs__panel ds-focus"
        >
          {selected.content}
        </div>
      )}
    </div>
  );
}

export type SegmentOption = {
  value: string;
  label: ReactNode;
  icon?: LucideIcon;
  /** Accessible name when the label is an icon only. */
  ariaLabel?: string;
  disabled?: boolean;
};

/**
 * A segmented control (a radio group drawn as joined buttons): one tab stop,
 * arrow keys move and select. For 2–5 short, exclusive choices.
 */
export function SegmentedControl({
  options,
  value,
  onChange,
  label,
  size = "md",
  className,
}: {
  options: SegmentOption[];
  value: string;
  onChange: (value: string) => void;
  label: string;
  size?: "sm" | "md";
  className?: string;
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);
  const current = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );
  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const key =
      event.key === "ArrowDown"
        ? "ArrowRight"
        : event.key === "ArrowUp"
          ? "ArrowLeft"
          : event.key;
    const index = rovingIndex(key, current, options.length, "horizontal", (i) =>
      Boolean(options[i]?.disabled),
    );
    if (index === null) return;
    event.preventDefault();
    onChange(options[index].value);
    refs.current[index]?.focus();
  };
  return (
    <div
      role="radiogroup"
      aria-label={label}
      className={cx("ds-segmented", `ds-segmented--${size}`, className)}
      onKeyDown={onKeyDown}
    >
      {options.map((option, index) => {
        const checked = index === current;
        return (
          <button
            key={option.value}
            ref={(element) => {
              refs.current[index] = element;
            }}
            type="button"
            role="radio"
            aria-checked={checked}
            aria-label={option.ariaLabel}
            tabIndex={checked ? 0 : -1}
            disabled={option.disabled}
            className="ds-segment ds-focus"
            onClick={() => onChange(option.value)}
          >
            {option.icon && <Icon icon={option.icon} />}
            {option.label && <span>{option.label}</span>}
          </button>
        );
      })}
    </div>
  );
}
