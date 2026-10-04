"use client";
import { T, useLocale } from "@/components/levi-locale";
import { Badge, StatusDot, type Tone } from "@/components/ds";
import { Problem } from "@/components/pages-ui/feedback";
import type { InputReport, RequirementStatus } from "./types";

const TONE: Record<RequirementStatus, Tone> = {
  pass: "success",
  warn: "warning",
  fail: "danger",
  info: "neutral",
};
const WORD: Record<RequirementStatus, string> = {
  pass: "Pass",
  warn: "Warning",
  fail: "Failed",
  info: "Info",
};

/** Detected format, summary and the requirement checklist of an inspection. */
export function InputReportView({ report }: { report: InputReport }) {
  const { t } = useLocale();
  const s = report.summary;
  if (!report.format) {
    return (
      <Problem
        title={t("Input format not recognized")}
        why={s.hint ? t(s.hint) : undefined}
        fix={t(
          "Choose a raw capture folder (task/demo_NNNN) or a LeRobot dataset with meta/info.json.",
        )}
      />
    );
  }
  const failed = report.requirements.filter((r) => r.status === "fail").length;
  const warned = report.requirements.filter((r) => r.status === "warn").length;
  const bad = report.episodes.filter((e) => e.errors.length);
  return (
    <div className="mt-5">
      <div className="pg-row">
        <Badge tone="info" icon={null}>
          {t(report.label)}
        </Badge>
        {report.variant && <Badge icon={null}>{t(report.variant)}</Badge>}
        <Badge tone={failed ? "danger" : warned ? "warning" : "success"}>
          {failed} <T>failed</T> · {warned} <T>warnings</T>
        </Badge>
      </div>
      <dl className="pg-summary">
        {s.demos !== undefined && (
          <div>
            <dt>
              <T>Episodes</T>
            </dt>
            <dd>{s.demos}</dd>
          </div>
        )}
        {s.frames !== undefined && (
          <div>
            <dt>
              <T>Frames</T>
            </dt>
            <dd>{s.frames}</dd>
          </div>
        )}
        {s.tasks && (
          <div>
            <dt>
              <T>Tasks</T>
            </dt>
            <dd>{Object.keys(s.tasks).length}</dd>
          </div>
        )}
        {s.outcomes && (
          <div>
            <dt>
              <T>Outcomes</T>
            </dt>
            <dd>
              {Object.entries(s.outcomes)
                .map(([k, v]) => `${t(k)} ${v}`)
                .join(" · ")}
            </dd>
          </div>
        )}
        {s.fps_min != null && (
          <div>
            <dt>
              <T>Measured FPS</T>
            </dt>
            <dd>
              {s.fps_min === s.fps_max
                ? s.fps_min.toFixed(2)
                : `${s.fps_min.toFixed(2)}–${(s.fps_max ?? s.fps_min).toFixed(2)}`}
            </dd>
          </div>
        )}
      </dl>
      <div className="ds-table-wrap">
        <table className="ds-table ds-table--compact pg-checklist">
          <tbody>
            {report.requirements.map((r) => (
              <tr key={r.id} className={r.status}>
                <td className="pg-checklist-status">
                  {r.verified === "during_scan" ? (
                    <StatusDot>{t("Pending")}</StatusDot>
                  ) : (
                    <StatusDot tone={TONE[r.status]}>
                      {t(WORD[r.status])}
                    </StatusDot>
                  )}
                </td>
                <td>
                  {t(r.label)}
                  {r.verified === "during_scan" && (
                    <span className="text-xs">
                      {" "}
                      (<T>checked while converting</T>)
                    </span>
                  )}
                  {r.detail && (
                    <p className="text-xs break-all">{t(r.detail)}</p>
                  )}
                  {r.fix && (r.status !== "pass" || r.verified !== "now") && (
                    <p className="text-xs pg-fix">{t(r.fix)}</p>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {bad.length > 0 && (
        <details className="mt-3">
          <summary className="cursor-pointer text-xs">
            {bad.length} <T>episodes with errors</T>
          </summary>
          <pre>
            {bad
              .map((e) => `${e.source_id}: ${e.errors.join("; ")}`)
              .join("\n")}
          </pre>
        </details>
      )}
    </div>
  );
}
