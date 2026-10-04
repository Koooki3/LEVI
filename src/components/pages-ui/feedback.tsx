"use client";
/**
 * Feedback pieces shared by the stage-4 pages (Live evaluation, Conversion &
 * review, Training pool, Explore) and the Agent Workbench content. They are
 * candidates for the design system (docs/DESIGN.md, "Pages (stage 4)"); until
 * then they live here so that the shared `src/components/ds/` stays with its
 * owner. Styles: `pages.css` (`pg-problem`, `pg-note`, `pg-jobcard`).
 */
import type { ReactNode } from "react";
import {
  CircleAlert,
  CircleCheck,
  Info,
  Inbox,
  TriangleAlert,
  type LucideIcon,
} from "lucide-react";
import { Icon } from "@/components/ds";
import { useLocale } from "@/components/levi-locale";

/**
 * An error in three parts: what happened (`title`), why (`why`: usually the
 * server's own words, translated when the catalogue knows them) and what to
 * do (`fix`: a sentence and/or buttons). The raw message can go into the
 * collapsed technical details (`details`). `role="alert"` by default so it is
 * announced when it appears; pass `live={false}` for a standing error that is
 * shown with the page.
 */
export function Problem({
  title,
  why,
  fix,
  details,
  tone = "danger",
  live = true,
  className,
}: {
  title: ReactNode;
  why?: ReactNode;
  fix?: ReactNode;
  details?: string;
  tone?: "danger" | "warning";
  live?: boolean;
  className?: string;
}) {
  const { t } = useLocale();
  return (
    <div
      className={[
        "pg-problem",
        tone === "warning" ? "pg-problem--warning" : "",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
      role={live ? "alert" : undefined}
    >
      <Icon icon={tone === "warning" ? TriangleAlert : CircleAlert} />
      <div className="pg-problem__body">
        <span className="pg-problem__title">{title}</span>
        {why && <p className="pg-problem__why">{why}</p>}
        {fix && <div className="pg-problem__fix">{fix}</div>}
        {details && (
          <details className="pg-problem__details">
            <summary>{t("Technical details")}</summary>
            <pre>{details}</pre>
          </details>
        )}
      </div>
    </div>
  );
}

/**
 * The common case: a request failed. `message` is the server's sentence (or
 * the exception text); it is shown as the reason, translated when the
 * catalogue has it. `action` names what failed ("The export did not start").
 */
export function RequestProblem({
  action,
  message,
  fix,
  className,
}: {
  action: string;
  message: string;
  fix?: ReactNode;
  className?: string;
}) {
  const { t } = useLocale();
  return (
    <Problem
      className={className}
      title={t(action)}
      why={t(message)}
      fix={
        fix ?? t("Check the reason above, change what it names and try again.")
      }
    />
  );
}

const NOTE_ICON: Record<"info" | "success" | "warning", LucideIcon> = {
  info: Info,
  success: CircleCheck,
  warning: TriangleAlert,
};

/** An inline note beside the thing it is about: icon, words, a status bar. */
export function Note({
  tone = "info",
  children,
  role,
  className,
}: {
  tone?: "info" | "success" | "warning";
  children: ReactNode;
  role?: "status" | "alert";
  className?: string;
}) {
  return (
    <div
      className={["pg-note", `pg-note--${tone}`, className ?? ""]
        .filter(Boolean)
        .join(" ")}
      role={role}
    >
      <Icon icon={NOTE_ICON[tone]} />
      <div>{children}</div>
    </div>
  );
}

/**
 * The job progress card used by every page that follows a job (pool export,
 * scan, push; conversion runs): a head row (status, title, meta on the right)
 * and whatever the page shows below (progress bar, banners, results).
 */
export function JobCard({
  status,
  title,
  meta,
  children,
  className,
  label,
}: {
  status?: ReactNode;
  title?: ReactNode;
  meta?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** Accessible name of the card region. */
  label?: string;
}) {
  return (
    <section
      className={["pg-jobcard", className ?? ""].filter(Boolean).join(" ")}
      aria-label={label}
    >
      {(status || title || meta) && (
        <div className="pg-jobcard__head">
          {status}
          {title && <span className="pg-jobcard__title">{title}</span>}
          {meta && <span className="pg-jobcard__meta">{meta}</span>}
        </div>
      )}
      {children}
    </section>
  );
}

/** A one-line empty state inside a card or table ("No jobs yet"). */
export function EmptyLine({
  icon = Inbox,
  children,
}: {
  icon?: LucideIcon;
  children: ReactNode;
}) {
  return (
    <p className="pg-empty-inline">
      <Icon icon={icon} />
      <span>{children}</span>
    </p>
  );
}
