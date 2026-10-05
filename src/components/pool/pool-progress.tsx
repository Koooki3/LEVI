"use client";
import { useLocale } from "@/components/levi-locale";
import { Badge, Progress, type Tone } from "@/components/ds";
import { useServerText } from "@/components/pages-ui/messages";
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

/** A worker exists (or is being stopped): the job is not over. */
export const RUNNING = new Set(["running", "queued", "stalled", "cancelling"]);
/** States that stopped without finishing; a banner says why. */
export const STOPPED = new Set(["interrupted", "failed"]);

/** How long to wait before asking again: fast while work moves, slower when
 * it is stalled or being cancelled, never for a job that is over. */
export function pollDelay(status: string): number | null {
  if (status === "running" || status === "queued") return 1000;
  if (status === "stalled" || status === "cancelling") return 2000;
  return null;
}

/** "12 s ago", "3 min ago", "2 h ago" (t() translates the unit words). */
export function ago(
  seconds: number | null | undefined,
  t: (key: string) => string,
): string {
  if (seconds === null || seconds === undefined || !isFinite(seconds))
    return "—";
  const s = Math.max(0, Math.round(seconds));
  const [n, unit] =
    s < 90
      ? [s, t("s ago")]
      : s < 5400
        ? [Math.round(s / 60), t("min ago")]
        : s < 172800
          ? [Math.round(s / 3600), t("h ago")]
          : [Math.round(s / 86400), t("d ago")];
  // Chinese units follow the number directly, English ones after a space.
  return /[\u4e00-\u9fff]/.test(unit) ? `${n}${unit.trim()}` : `${n} ${unit}`;
}

export function statusTone(status: string): "pass" | "warn" | "fail" | "" {
  switch (status) {
    case "done":
      return "pass";
    case "failed":
    case "interrupted":
      return "fail";
    case "done_with_errors":
    case "stalled":
    case "cancelled":
    case "cancelling":
      return "warn";
    default:
      return "";
  }
}

const LEGACY_STATUS: Record<string, string> = { succeeded: "done" };

const BADGE_TONE: Record<"pass" | "warn" | "fail" | "", Tone> = {
  pass: "success",
  warn: "warning",
  fail: "danger",
  "": "neutral",
};

/** A job's status as a badge (icon shape + words); running states are info. */
export function StatusBadge({ status }: { status: string }) {
  const { t } = useLocale();
  const shown = LEGACY_STATUS[status] || status;
  const tone =
    RUNNING.has(shown) && shown !== "stalled"
      ? "info"
      : BADGE_TONE[statusTone(shown)];
  return <Badge tone={tone}>{t(shown)}</Badge>;
}

/** Stage chips, a bar, done / total and timing of a pool job's progress
 * file (levi/conversion/progress.py; a push adds bytes and rate). */
export function PoolJobProgress({ job }: { job: PoolJob }) {
  const { t } = useLocale();
  const serverText = useServerText();
  const p = job.progress;
  if (!p) return null;
  const running = RUNNING.has(job.status);
  const fraction = p.total ? Math.min(1, p.done / p.total) : 0;
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
        <Progress
          value={fraction * 100}
          showValue
          label={t(p.stages[p.stage_index] ?? p.stage ?? "In progress")}
        />
      )}
      <p className="pg-small tabular">
        {running && job.age_seconds !== undefined && (
          <>
            {t("Last update")} {ago(job.age_seconds, t)}
            {" · "}
          </>
        )}
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
        <ul className="pg-warnings">
          {p.warnings.slice(-5).map((w) => (
            <li key={w}>{serverText(w)}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
