"use client";
import { T, useLocale } from "@/components/levi-locale";
import type { InputReport, RequirementStatus } from "./types";

const MARK: Record<RequirementStatus, string> = {
  pass: "✓",
  warn: "!",
  fail: "✕",
  info: "·",
};

/** Detected format, summary and the requirement checklist of an inspection. */
export function InputReportView({ report }: { report: InputReport }) {
  const { t } = useLocale();
  const s = report.summary;
  if (!report.format) {
    return (
      <p className="levi-error" role="alert">
        <T>Input format not recognized</T>. {s.hint ? t(s.hint) : ""}
      </p>
    );
  }
  const failed = report.requirements.filter((r) => r.status === "fail").length;
  const warned = report.requirements.filter((r) => r.status === "warn").length;
  const bad = report.episodes.filter((e) => e.errors.length);
  return (
    <div className="mt-5">
      <div className="levi-row">
        <span className="levi-status pass">{t(report.label)}</span>
        {report.variant && (
          <span className="levi-status">{t(report.variant)}</span>
        )}
        <span className={`levi-status ${failed ? "fail" : "pass"}`}>
          {failed} <T>failed</T> · {warned} <T>warnings</T>
        </span>
      </div>
      <dl className="levi-summary">
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
      <table className="levi-table levi-checklist">
        <tbody>
          {report.requirements.map((r) => (
            <tr key={r.id} className={r.status}>
              <td className="w-6">
                <span className={`levi-status ${r.status}`}>
                  {r.verified === "during_scan" ? "…" : MARK[r.status]}
                </span>
              </td>
              <td>
                {t(r.label)}
                {r.verified === "during_scan" && (
                  <span className="text-xs">
                    {" "}
                    (<T>checked while converting</T>)
                  </span>
                )}
                {r.detail && <p className="text-xs break-all">{t(r.detail)}</p>}
                {r.fix && (r.status !== "pass" || r.verified !== "now") && (
                  <p className="text-xs levi-fix">{t(r.fix)}</p>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
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
