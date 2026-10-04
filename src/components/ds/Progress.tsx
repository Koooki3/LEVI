"use client";
import { LoaderCircle } from "lucide-react";
import { useLocale } from "@/components/levi-locale";
import { usePrefersReducedMotion } from "@/lib/design/motion";
import { Icon } from "./Icon";
import { cx } from "./internal";

/**
 * A progress bar. With a `value` it is determinate (`aria-valuenow`); with
 * `value={null}` it is indeterminate: a short bar travels back and forth, or,
 * under reduced motion, a still track with the words "In progress".
 * Never a dialog: it does not trap focus or cover the page.
 */
export function Progress({
  value,
  max = 100,
  label,
  showValue = false,
  className,
}: {
  value: number | null;
  max?: number;
  /** Accessible name, shown above the bar. */
  label: string;
  showValue?: boolean;
  className?: string;
}) {
  const { t } = useLocale();
  const reduced = usePrefersReducedMotion();
  const indeterminate = value === null || !Number.isFinite(value);
  const clamped = indeterminate
    ? 0
    : Math.min(Math.max(value as number, 0), max);
  const percent = max > 0 ? Math.round((clamped / max) * 100) : 0;
  return (
    <div className={cx("ds-progress", className)}>
      <div className="ds-progress__meta">
        <span className="ds-progress__label">{label}</span>
        {showValue && !indeterminate && (
          <span className="ds-progress__value">{percent}%</span>
        )}
        {indeterminate && reduced && (
          <span className="ds-progress__value">{t("In progress")}</span>
        )}
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={max}
        aria-valuenow={indeterminate ? undefined : clamped}
        aria-valuetext={indeterminate ? t("In progress") : `${percent}%`}
        className={cx(
          "ds-progress__track",
          indeterminate && "ds-progress__track--indeterminate",
          indeterminate && reduced && "ds-progress__track--still",
        )}
      >
        <span
          className="ds-progress__bar"
          style={indeterminate ? undefined : { width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

/**
 * A small spinner for "working" next to the thing that works. It is a
 * polite status message (`role="status"`), never a dialog; under reduced
 * motion the ring stands still.
 */
export function Spinner({
  label,
  showLabel = false,
  className,
}: {
  label?: string;
  showLabel?: boolean;
  className?: string;
}) {
  const { t } = useLocale();
  const text = label ?? t("Loading");
  return (
    <span role="status" className={cx("ds-spinner", className)}>
      <Icon icon={LoaderCircle} className="ds-spin" />
      <span className={showLabel ? "ds-spinner__label" : "ds-sr-only"}>
        {text}
      </span>
    </span>
  );
}
