"use client";
import { T, useLocale } from "@/components/levi-locale";
import { Progress } from "@/components/ds";
import { useServerText } from "@/components/pages-ui/messages";
import type { Job } from "./types";

function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !isFinite(seconds))
    return "—";
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

/** Stage stepper, bar, current item, elapsed / ETA — from the job's
 * structured progress file (written by the conversion worker). */
export function JobProgress({ job }: { job: Job }) {
  const { t } = useLocale();
  const serverText = useServerText();
  const p = job.progress;
  if (!p) return null;
  const running = job.status === "running" || job.status === "queued";
  const fraction = p.total ? Math.min(1, p.done / p.total) : 0;
  const rate =
    p.done && p.updated_at > p.started_at
      ? p.done / Math.max(1e-3, p.elapsed_seconds)
      : null;
  return (
    <div className="pg-progress" aria-live="polite">
      <ol className="pg-steps">
        {p.stages.map((name, index) => (
          <li
            key={name}
            className={
              index < p.stage_index || p.stage === "done"
                ? "done"
                : index === p.stage_index && running
                  ? "active"
                  : ""
            }
          >
            {t(name)}
          </li>
        ))}
      </ol>
      {running && p.total > 0 && (
        <>
          <Progress
            value={fraction * 100}
            showValue
            label={`${p.done} / ${p.total}${p.current ? ` · ${p.current}` : ""}`}
          />
        </>
      )}
      <p className="pg-small tabular">
        <T>Elapsed</T> {duration(p.elapsed_seconds)}
        {running && (
          <>
            {" · "}
            <T>Remaining</T> {duration(p.eta_seconds)}
          </>
        )}
        {rate !== null && running && ` · ${rate.toFixed(2)}/s`}
      </p>
      {p.warnings.length > 0 && (
        <ul className="pg-warnings" aria-label={t("Warnings")}>
          {p.warnings.map((w) => (
            <li key={w}>{serverText(w)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
