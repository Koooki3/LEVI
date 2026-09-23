"use client";
import { useLocale } from "@/components/levi-locale";
import type { DatasetFormat } from "@/types/dataset-format.types";

const INPUT_LABELS: Record<string, string> = {
  robot_capture: "Robot capture (CSV + video)",
  image_sequence: "Robot capture (CSV + image folders)",
  droid_raw: "DROID raw (HDF5 + three cameras)",
  lerobot: "LeRobot dataset",
};

const VARIANT_LABELS: Record<string, string> = {
  policy_rollout: "Policy rollout",
  teleop: "Teleoperation",
};

function fps(value: number | null | undefined): string {
  return value == null ? "" : `${Number(value.toFixed(3))} fps`;
}

/** Short badge text + detail lines describing a dataset's format, version
 * and origin — so raw captures, LEVI conversions, RECAP exports, annotated
 * exports and external datasets are told apart at a glance. */
export function useFormatDescription(format: DatasetFormat | undefined) {
  const { t } = useLocale();
  if (!format) return { badge: "—", tone: "", lines: [] as string[] };
  const version = format.version ? `LeRobot ${format.version}` : "LeRobot";
  const lines: string[] = [];
  const source = format.input_format
    ? t(INPUT_LABELS[format.input_format] ?? format.input_format)
    : null;
  const variant = format.variant
    ? t(VARIANT_LABELS[format.variant] ?? format.variant)
    : null;
  if (format.origin === "raw_capture") {
    lines.push([source, variant].filter(Boolean).join(" · "));
    if (format.view_status === "ready") {
      lines.push(
        `${t("Browsing view")} · ${t("every frame kept")} · ${fps(
          format.view_fps ?? format.fps,
        )}`,
      );
      if (format.input_format === "droid_raw") {
        lines.push(
          t(
            "Nominal video clock; original control timestamps are preserved separately",
          ),
        );
        if (format.source_time_error_max_seconds != null)
          lines.push(
            `${t("Maximum source/view clock difference")} ${format.source_time_error_max_seconds.toFixed(2)} s`,
          );
      }
      if (format.excluded)
        lines.push(
          `${format.excluded} ${t("episodes left out (failed inspection)")}`,
        );
    } else if (format.view_status === "failed") {
      lines.push(t("Browsing view could not be built"));
    } else {
      lines.push(t("Building the browsing view…"));
    }
    return {
      badge: t("Raw capture"),
      tone: format.view_status === "failed" ? "fail" : "warn",
      lines,
    };
  }
  if (format.origin === "levi_recap") {
    const r = format.recap ?? {};
    const outcomes = r.outcomes
      ? `${t("success")} ${r.outcomes.success ?? 0} / ${t("failure")} ${
          r.outcomes.failure ?? 0
        }`
      : "";
    lines.push(
      [
        t("RECAP value dataset"),
        r.dataset_type ? t(r.dataset_type) : "",
        outcomes,
      ]
        .filter(Boolean)
        .join(" · "),
    );
    if (r.failure_reward != null)
      lines.push(
        `${t("Failure reward")} ${r.failure_reward} · gamma ${r.gamma ?? 1}`,
      );
  } else if (format.origin === "levi_conversion") {
    lines.push(
      [t("Converted by LEVI"), source ? `${t("from")} ${source}` : "", variant]
        .filter(Boolean)
        .join(" · "),
    );
  } else if (format.origin === "levi_export") {
    lines.push(t("Annotated export (language/event columns)"));
  } else {
    lines.push(t("Registered as-is (produced outside LEVI)"));
  }
  if (format.origin === "levi_conversion" || format.origin === "levi_recap") {
    const timing =
      format.timing === "retime"
        ? t("every frame kept (retime)")
        : t("resampled");
    const filtered = format.filter_static
      ? ` · ${t("static frames dropped")}`
      : "";
    lines.push(`${timing} · ${fps(format.fps)}${filtered}`);
  } else if (format.fps != null) {
    lines.push(fps(format.fps));
  }
  return {
    badge: format.origin === "levi_recap" ? `${version} · RECAP` : version,
    tone: "pass",
    lines,
  };
}

export function DatasetFormatBadge({
  format,
  compact = false,
}: {
  format: DatasetFormat | undefined;
  compact?: boolean;
}) {
  const { badge, tone, lines } = useFormatDescription(format);
  return (
    <div className="levi-format">
      <span className={`levi-status ${tone}`}>{badge}</span>
      {!compact &&
        lines.map((line) => (
          <p key={line} className="text-xs">
            {line}
          </p>
        ))}
    </div>
  );
}
