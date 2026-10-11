"use client";
import type { HTMLAttributes, ReactNode, TableHTMLAttributes } from "react";
import { useLocale } from "@/components/levi-locale";
import { cx } from "./internal";

/**
 * Base table styling: sunken header, 1 px row separators, hover row, a
 * selected row (`data-selected` on <tr>: grey fill, weight 500 and a 2 px
 * bar on the left), numbers in tabular figures (`ds-num` on a cell).
 * The wrapper scrolls sideways so the page never does.
 */
export function Table({
  caption,
  density = "regular",
  className,
  children,
  ...rest
}: TableHTMLAttributes<HTMLTableElement> & {
  caption?: ReactNode;
  density?: "compact" | "regular";
  children: ReactNode;
}) {
  const { t } = useLocale();
  return (
    <div
      className="ds-table-wrap ds-focus"
      tabIndex={0}
      role="region"
      aria-label={typeof caption === "string" ? caption : t("Scrollable table")}
    >
      <table
        className={cx("ds-table", `ds-table--${density}`, className)}
        {...rest}
      >
        {caption && <caption className="ds-sr-only">{caption}</caption>}
        {children}
      </table>
    </div>
  );
}

export function TableRow({
  selected = false,
  className,
  children,
  ...rest
}: HTMLAttributes<HTMLTableRowElement> & { selected?: boolean }) {
  return (
    <tr
      data-selected={selected || undefined}
      aria-current={selected || undefined}
      className={className}
      {...rest}
    >
      {children}
    </tr>
  );
}
