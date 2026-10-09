"use client";
import { useId, type ReactNode } from "react";
import { ChevronDown, ChevronUp, type LucideIcon } from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/** Actions are siblings of the disclosure button, never nested in it. */
export function LiveRow({
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
  const { t } = useLocale();
  const id = useId();
  return (
    <article
      className={`pg-live-card pg-live-list-row${fault ? " fault" : ""}`}
      aria-label={title}
    >
      <div className="pg-live-row-head">
        <button
          type="button"
          className="pg-live-row-toggle ds-focus"
          aria-label={`${t(open ? "Collapse details" : "Expand details")}: ${title}${subtitle ? ` · ${subtitle}` : ""}`}
          aria-expanded={open}
          aria-controls={id}
          onClick={onToggle}
        >
          <span className="pg-live-row-thumb" aria-hidden="true">
            <Icon icon={icon} size="md" />
          </span>
          <span className="pg-live-row-title">
            <strong>
              <code>{title}</code>
            </strong>
            {subtitle && <span className="pg-pool-muted">{subtitle}</span>}
          </span>
          <span className="pg-live-row-summary">{summary}</span>
          <Icon icon={open ? ChevronUp : ChevronDown} />
        </button>
        {actions && <div className="pg-live-row-actions">{actions}</div>}
      </div>
      {open && (
        <div
          id={id}
          className="pg-live-row-detail"
          role="region"
          aria-label={title}
        >
          {children}
        </div>
      )}
    </article>
  );
}
