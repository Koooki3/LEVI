"use client";
import {
  useEffect,
  useId,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import type { LucideIcon } from "lucide-react";
import { ChevronDown } from "lucide-react";
import { Icon } from "./Icon";
import { cx, rovingIndex } from "./internal";

export type MenuItem = {
  id: string;
  label: ReactNode;
  icon?: LucideIcon;
  shortcut?: ReactNode;
  tone?: "default" | "danger";
  disabled?: boolean;
  onSelect: () => void;
};

/**
 * A menu button (dropdown) with the ARIA menu pattern: Enter, Space or ↓
 * open it on the first item, ↑ on the last; ↑/↓/Home/End move; Enter or
 * Space choose; Escape closes and returns focus to the button; Tab or a
 * click outside closes it.
 */
export function Menu({
  label,
  items,
  icon,
  align = "start",
  triggerClassName,
  ariaLabel,
}: {
  label: ReactNode;
  items: MenuItem[];
  icon?: LucideIcon;
  align?: "start" | "end";
  triggerClassName?: string;
  /** Accessible name when `label` is not text. */
  ariaLabel?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const itemRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const disabled = (i: number) => Boolean(items[i]?.disabled);

  const openAt = (where: "first" | "last") => {
    const index = rovingIndex(
      where === "first" ? "Home" : "End",
      0,
      items.length,
      "vertical",
      disabled,
    );
    setActive(index ?? 0);
    setOpen(true);
  };
  const close = (returnFocus: boolean) => {
    setOpen(false);
    if (returnFocus) trigger.current?.focus();
  };

  useEffect(() => {
    if (open) itemRefs.current[active]?.focus();
  }, [open, active]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  const choose = (index: number) => {
    const item = items[index];
    if (!item || item.disabled) return;
    close(true);
    item.onSelect();
  };

  const onTriggerKey = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (["ArrowDown", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      openAt("first");
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      openAt("last");
    }
  };

  const onMenuKey = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close(true);
      return;
    }
    if (event.key === "Tab") {
      setOpen(false);
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      choose(active);
      return;
    }
    const index = rovingIndex(
      event.key,
      active,
      items.length,
      "vertical",
      disabled,
    );
    if (index !== null) {
      event.preventDefault();
      setActive(index);
    }
  };

  return (
    <div className="ds-menu-anchor" ref={root}>
      <button
        ref={trigger}
        type="button"
        id={`${id}-button`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? `${id}-menu` : undefined}
        aria-label={ariaLabel}
        className={cx(
          "ds-btn ds-btn--secondary ds-btn--md ds-focus",
          triggerClassName,
        )}
        onClick={() => (open ? close(false) : openAt("first"))}
        onKeyDown={onTriggerKey}
      >
        {icon && <Icon icon={icon} />}
        <span className="ds-btn__label">{label}</span>
        <Icon icon={ChevronDown} />
      </button>
      {open && (
        <div
          role="menu"
          id={`${id}-menu`}
          aria-labelledby={`${id}-button`}
          className={cx("ds-menu ds-on-raised", `ds-menu--${align}`)}
          onKeyDown={onMenuKey}
        >
          {items.map((item, index) => (
            <button
              key={item.id}
              ref={(element) => {
                itemRefs.current[index] = element;
              }}
              type="button"
              role="menuitem"
              tabIndex={index === active ? 0 : -1}
              aria-disabled={item.disabled || undefined}
              className={cx(
                "ds-menu__item ds-focus",
                item.tone === "danger" && "ds-menu__item--danger",
              )}
              onClick={() => choose(index)}
              onPointerEnter={() => !item.disabled && setActive(index)}
            >
              {item.icon ? (
                <Icon icon={item.icon} />
              ) : (
                <span className="ds-menu__spacer" />
              )}
              <span className="ds-menu__label">{item.label}</span>
              {item.shortcut && (
                <span className="ds-menu__shortcut">{item.shortcut}</span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
