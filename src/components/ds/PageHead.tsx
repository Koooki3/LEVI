import type { HTMLAttributes, ReactNode } from "react";
import { cx } from "./internal";

/**
 * The head of a page (layout audit 2026-10-21, P2): the title, and on the
 * same row the page's tools (`meta`, e.g. how fresh the data is, then
 * `actions`), vertically centred on the title; an optional lead paragraph
 * below. The h1 has no margin of its own: every space is a gap, so the
 * buttons never sit lower than the title text.
 */
export function PageHead({
  title,
  lead,
  meta,
  actions,
  className,
  ...rest
}: {
  title: ReactNode;
  lead?: ReactNode;
  meta?: ReactNode;
  actions?: ReactNode;
} & Omit<HTMLAttributes<HTMLElement>, "title">) {
  return (
    <header className={cx("ds-page-head", className)} {...rest}>
      <div className="ds-page-head__row">
        <h1 className="ds-page-head__title">{title}</h1>
        {(meta || actions) && (
          <div className="ds-page-head__tools">
            {meta}
            {actions}
          </div>
        )}
      </div>
      {lead && <p className="ds-page-head__lead">{lead}</p>}
    </header>
  );
}
