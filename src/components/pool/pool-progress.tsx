"use client";
import { useLocale } from "@/components/levi-locale";
import type { PoolJob } from "./types";

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !isFinite(seconds))
    return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  return m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s}s`;
}

export function bytes(value: number | null | undefined): string {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = value;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i ? 1 : 0)} ${units[i]}`;
}

export const RUNNING = new Set(["running", "queued", "cancelling"]);

export function StatusBadge({ status }: { status: string }) {
  const { t } = useLocale();
  const tone =
    status === "succeeded"
      ? "pass"
      : status === "failed" || status === "interrupted"
        ? "fail"
        : status === "cancelled" || status === "cancelling"
          ? "warn"
          : "";
  return <span className={`levi-status ${tone}`}>{t(status)}</span>;
}

/** Stage chips, a bar, done / total and timing of a pool job's progress
 * file (levi/conversion/progress.py; a push adds bytes and rate). */
export function PoolJobProgress({ job }: { job: PoolJob }) {
  const { t } = useLocale();
  const p = job.progress;
  if (!p) return null;
  const running = RUNNING.has(job.status);
  const fraction = p.total ? Math.min(1, p.done / p.total) : 0;
  return (
    <div className="levi-progress" aria-live="polite">
      <ol className="levi-steps">
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
        <div
          className="levi-bar"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(fraction * 100)}
        >
          <span style={{ width: `${(fraction * 100).toFixed(1)}%` }} />
        </div>
      )}
      <p className="text-xs tabular">
        {running && p.total > 0 && (
          <>
            {job.kind === "push"
              ? `${p.done}%`
              : `${p.done.toLocaleString()} / ${p.total.toLocaleString()}`}
            {p.bytes !== undefined && ` · ${bytes(p.bytes)}`}
            {p.rate && ` · ${p.rate}`}
            {!p.bytes && p.current ? ` · ${p.current}` : ""}
            {" · "}
          </>
        )}
        {t("Elapsed")} {duration(p.elapsed_seconds)}
        {running && (
          <>
            {" · "}
            {t("Remaining")}{" "}
            {duration(p.rsync_eta_seconds ?? p.eta_seconds ?? null)}
          </>
        )}
      </p>
      {p.warnings.length > 0 && (
        <ul className="levi-warnings">
          {p.warnings.slice(-5).map((w) => (
            <li key={w}>{t(w)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
