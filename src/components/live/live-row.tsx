"use client";
import { useEffect, useId, useRef, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronUp,
  MoreHorizontal,
  type LucideIcon,
} from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** Actions are siblings of the disclosure button, never nested in it. */
export function LiveRow({
  detail = false,
  title,
  subtitle,
  icon,
  summary,
  actions,
  fault = false,
  open,
  onToggle,
  children,
}: {
  detail?: boolean;
  title: string;
  subtitle?: string;
  icon: LucideIcon;
  summary: ReactNode;
  actions?: ReactNode;
  fault?: boolean;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  const id = useId();
  return (
    <article
      className={`pg-live-card pg-live-list-row${fault ? " fault" : ""}`}
      aria-label={title}
    >
      <div className="pg-live-row-head">
        {detail ? (
          <div className="pg-live-session-head">
            <span className="pg-live-row-title">
              <strong>{title}</strong>
              <span>{subtitle}</span>
            </span>
            <span className="pg-live-row-summary">{summary}</span>
          </div>
        ) : (
          <button
            type="button"
            className="pg-live-row-toggle ds-focus"
            aria-expanded={open}
            aria-controls={open ? id : undefined}
            onClick={onToggle}
          >
            <span className="pg-live-row-thumb" aria-hidden="true">
              <Icon icon={icon} size="md" />
            </span>
            <span className="pg-live-row-title">
              <strong>
                <code>
                  <Breakable text={title} />
                </code>
              </strong>
              {subtitle && <span className="pg-pool-muted">{subtitle}</span>}
            </span>
            <span className="pg-live-row-summary">{summary}</span>
            <Icon icon={open ? ChevronUp : ChevronDown} />
          </button>
        )}
        {actions && <div className="pg-live-row-actions">{actions}</div>}
      </div>
      {open && (
        <div id={id} className="pg-live-row-detail">
          {children}
        </div>
      )}
    </article>
  );
}

/** A row's actions, the same on every live list: at most one primary action
 * in view, the rest (with the reason any of them is unavailable) behind one
 * "Actions" disclosure named after the row. */
export function RowActions({
  primary,
  name,
  open = false,
  children,
}: {
  primary?: ReactNode;
  /** The row's title, so each disclosure has its own accessible name. */
  name: string;
  /** Start open (the session's own page). */
  open?: boolean;
  children: ReactNode;
}) {
  const { t } = useLocale();
  const ref = useRef<HTMLDetailsElement>(null);
  // A menu (not the always-open one of a session's own page) closes on a
  // click elsewhere or on Escape, which returns focus to its button.
  useEffect(() => {
    if (open) return;
    const close = (event: Event) => {
      const menu = ref.current;
      if (!menu?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== "Escape" || !menu.contains(document.activeElement))
          return;
        menu.open = false;
        menu.querySelector<HTMLElement>(":scope > summary")?.focus();
        return;
      }
      // A dialog the menu opened lives in a portal: a click in it is not
      // "elsewhere".
      const target = event.target as Node | null;
      if (
        target &&
        !menu.contains(target) &&
        !(
          target instanceof Element &&
          target.closest("[role=dialog], [role=alertdialog], .ds-scrim")
        )
      )
        menu.open = false;
    };
    document.addEventListener("pointerdown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("pointerdown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);
  return (
    <>
      {primary}
      <details
        ref={ref}
        className={`pg-live-row-more${open ? " is-inline" : ""}`}
        open={open || undefined}
      >
        <summary
          className="ds-btn ds-btn--ghost ds-btn--sm ds-focus"
          aria-label={`${t("Actions")}: ${name}`}
        >
          <Icon icon={MoreHorizontal} />
          {t("Actions")}
        </summary>
        <div className="pg-live-row-menu">{children}</div>
      </details>
    </>
  );
}

/** Long identifiers (`model_step49999 / pick_the_carrot`) may break after `_`,
 * `/`, `-` and `.` instead of in the middle of a word. */
export function Breakable({ text }: { text: string }) {
  const parts = text.split(/(?<=[_/\-.])/);
  return (
    <>
      {parts.map((part, i) => (
        <span key={i}>
          {part}
          {i < parts.length - 1 && <wbr />}
        </span>
      ))}
    </>
  );
}
