"use client";
import { useId, useState, type ReactNode } from "react";
import { Info } from "lucide-react";
import { Badge, Card, IconButton, type Tone } from "@/components/ds";
import { T, useLocale } from "@/components/levi-locale";

/**
 * The building blocks of the analysis views (Action Insights, Filtering,
 * dataset statistics): a titled card with an optional description toggle, a
 * note, a callout, a state badge and a meter. They wrap the design-system
 * Card, Badge and IconButton, so the views keep layout only; the look is in
 * viewer.css (`.vw-a-*`) and the tokens. Candidates for promotion into ds.
 */

/**
 * A card with a title, an optional grey meta text after the title (scope,
 * sample size) and an optional description the reader opens with the info
 * button (`aria-expanded`, `aria-controls`).
 */
export function AnalysisCard({
  title,
  meta,
  info,
  children,
  actions,
  className,
}: {
  title: string;
  meta?: ReactNode;
  info?: ReactNode;
  children?: ReactNode;
  /** Extra header controls, shown before the info button. */
  actions?: ReactNode;
  className?: string;
}) {
  const { t } = useLocale();
  const [open, setOpen] = useState(false);
  const infoId = useId();
  return (
    <Card
      className={["vw-a-card", className].filter(Boolean).join(" ")}
      title={
        <>
          {t(title)}
          {meta && <span className="vw-a-meta">{meta}</span>}
        </>
      }
      actions={
        (info || actions) && (
          <>
            {actions}
            {info && (
              <IconButton
                icon={Info}
                size="sm"
                label={t("Toggle description")}
                pressed={open}
                aria-expanded={open}
                aria-controls={infoId}
                onClick={() => setOpen((value) => !value)}
              />
            )}
          </>
        )
      }
    >
      {info && open && (
        <div id={infoId} className="vw-a-info">
          <T>{info}</T>
        </div>
      )}
      {children}
    </Card>
  );
}

/** A quiet box inside a card: a verdict, a list, a table of extremes. */
export function AnalysisNote({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <Card
      variant="sunken"
      padding="compact"
      className={["vw-a-note", className].filter(Boolean).join(" ")}
    >
      {children}
    </Card>
  );
}

/** The one finding of a card: a large number and the sentence about it. */
export function Callout({
  value,
  title,
  children,
}: {
  value: ReactNode;
  title: ReactNode;
  children?: ReactNode;
}) {
  return (
    <div className="vw-a-callout">
      <span className="vw-a-callout__value">{value}</span>
      <div>
        <p className="vw-a-callout__title">{title}</p>
        {children && <p className="vw-a-callout__text">{children}</p>}
      </div>
    </div>
  );
}

/** A state in words and shape (never colour alone): Smooth, Jerky, ... */
export function StateBadge({
  tone,
  children,
}: {
  tone: Tone;
  children: ReactNode;
}) {
  return <Badge tone={tone}>{children}</Badge>;
}

/**
 * A thin horizontal meter (a ratio between 0 and 1). Decorative: the number
 * it stands for is always printed next to it.
 */
export function Meter({
  ratio,
  tone = "neutral",
  className,
}: {
  ratio: number;
  tone?: Tone;
  className?: string;
}) {
  const percent = Math.min(100, Math.max(0, ratio * 100));
  return (
    <div
      className={["vw-a-meter", className].filter(Boolean).join(" ")}
      aria-hidden="true"
    >
      <span data-tone={tone} style={{ width: `${percent}%` }} />
    </div>
  );
}

/** Label and value pairs, one per line (mean, median, ...). */
export function StatList({
  items,
}: {
  items: { label: string; value: ReactNode; tone?: Tone }[];
}) {
  const { t } = useLocale();
  return (
    <dl className="vw-a-stats">
      {items.map((item) => (
        <div key={item.label}>
          <dt>{t(item.label)}</dt>
          <dd data-tone={item.tone}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A colour key line under a chart: a swatch, a name. */
export function LegendItem({
  swatch,
  children,
}: {
  swatch: ReactNode;
  children: ReactNode;
}) {
  return (
    <span className="vw-a-legend__item">
      {swatch}
      <span>{children}</span>
    </span>
  );
}

/** Tone of a ratio: the lower, the worse (movement) or the higher, the worse. */
export function toneForRatio(
  ratio: number,
  [low, high]: [number, number],
  higherIsWorse = true,
): Tone {
  if (ratio < low) return higherIsWorse ? "success" : "danger";
  if (ratio < high) return "warning";
  return higherIsWorse ? "danger" : "success";
}

/** `t("... {n} ...")` with its placeholders filled in. */
export function fill(
  template: string,
  values: Record<string, string | number>,
): string {
  let text = template;
  for (const [key, value] of Object.entries(values))
    text = text.split(`{${key}}`).join(String(value));
  return text;
}
