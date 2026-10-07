/**
 * Running-job counts for the header's Jobs entry, from the two job lists the
 * service already has: training pool jobs (`/api/levi/pool/jobs`) and
 * conversions (`/api/levi/jobs`). No new API.
 */

/** Pool job states that mean "still working" (levi/pool/jobs.py). */
const POOL_ACTIVE = new Set(["running", "stalled", "cancelling"]);
/** Conversion job states that mean "still working" (levi/jobs.py). */
const CONVERSION_ACTIVE = new Set(["queued", "running"]);

export type JobCounts = { pool: number; conversion: number };

type WithStatus = { status?: unknown };

function count(rows: unknown, active: Set<string>): number {
  if (!Array.isArray(rows)) return 0;
  return rows.filter(
    (row: WithStatus) =>
      row && typeof row.status === "string" && active.has(row.status),
  ).length;
}

/** Running pool jobs in a `/api/levi/pool/jobs` answer (`{ jobs: [...] }`). */
export function countPoolJobs(body: unknown): number {
  const jobs =
    body && typeof body === "object" ? (body as { jobs?: unknown }).jobs : null;
  return count(jobs, POOL_ACTIVE);
}

/** Running conversions in a `/api/levi/jobs` answer (a list). */
export function countConversionJobs(body: unknown): number {
  return count(body, CONVERSION_ACTIVE);
}

export function totalJobs(counts: JobCounts | null): number {
  return counts ? counts.pool + counts.conversion : 0;
}

export type JobKind = "pool" | "conversion";

/** One job as the Jobs entry follows it. */
export type JobEntry = {
  /** `kind:id`, unique across both lists. */
  key: string;
  kind: JobKind;
  /** The English catalogue key of what it is ("Training pool export"). */
  what: string;
  /** What it works on (an export's name, a conversion's dataset), or "". */
  name: string;
  status: string;
  /** 0–1 when the job reports its progress. */
  fraction: number | null;
};

function fractionOf(progress: unknown): number | null {
  if (!progress || typeof progress !== "object") return null;
  const { done, total } = progress as { done?: unknown; total?: unknown };
  if (typeof done !== "number" || typeof total !== "number" || total <= 0)
    return null;
  return Math.max(0, Math.min(1, done / total));
}

const POOL_WHAT: Record<string, string> = {
  scan: "Training pool scan",
  export: "Training pool export",
  push: "Training pool push",
};

/** Every job of a `/api/levi/pool/jobs` answer that has an id and a status. */
export function poolEntries(body: unknown): JobEntry[] {
  const jobs =
    body && typeof body === "object" ? (body as { jobs?: unknown }).jobs : null;
  if (!Array.isArray(jobs)) return [];
  const entries: JobEntry[] = [];
  for (const job of jobs) {
    if (!job || typeof job.id !== "string" || typeof job.status !== "string")
      continue;
    entries.push({
      key: `pool:${job.id}`,
      kind: "pool",
      what: POOL_WHAT[job.kind] ?? "Training pool job",
      name: typeof job.options?.name === "string" ? job.options.name : "",
      status: job.status,
      fraction: fractionOf(job.progress),
    });
  }
  return entries;
}

/** Every job of a `/api/levi/jobs` answer that has an id and a status. */
export function conversionEntries(body: unknown): JobEntry[] {
  if (!Array.isArray(body)) return [];
  const entries: JobEntry[] = [];
  for (const job of body) {
    if (!job || typeof job.id !== "string" || typeof job.status !== "string")
      continue;
    entries.push({
      key: `conversion:${job.id}`,
      kind: "conversion",
      what: "Conversion",
      name:
        typeof job.dataset === "string" && job.dataset
          ? job.dataset
          : typeof job.source === "string"
            ? (job.source.split("/").filter(Boolean).pop() ?? "")
            : "",
      status: job.status,
      fraction: fractionOf(job.progress),
    });
  }
  return entries;
}

const ACTIVE: Record<JobKind, Set<string>> = {
  pool: POOL_ACTIVE,
  conversion: CONVERSION_ACTIVE,
};
export const isActive = (entry: JobEntry) =>
  ACTIVE[entry.kind].has(entry.status);

/** The mean progress of the running jobs of one kind that report it. */
export function runningFraction(
  entries: JobEntry[],
  kind: JobKind,
): number | null {
  const values = entries
    .filter((e) => e.kind === kind && isActive(e) && e.fraction !== null)
    .map((e) => e.fraction as number);
  if (!values.length) return null;
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

export type JobOutcome = "success" | "warning" | "failure";

/** How a finished status ends, or null when it is no result worth a toast
 * (cancelled, interrupted: the person or the machine did that). */
export function jobOutcome(entry: JobEntry): JobOutcome | null {
  if (entry.kind === "pool")
    return entry.status === "done"
      ? "success"
      : entry.status === "done_with_errors" ||
          entry.status === "done_with_warnings"
        ? "warning"
        : entry.status === "failed"
          ? "failure"
          : null;
  return entry.status === "succeeded"
    ? "success"
    : entry.status === "failed"
      ? "failure"
      : null;
}

/**
 * The jobs that were running in `previous` and have a result now. `previous`
 * is null before the first answer: nothing that finished earlier is
 * announced. A job missing from the new answer is not announced either (it
 * may have been cleared, its result unknown).
 */
export function finishedSince(
  previous: ReadonlyMap<string, JobEntry> | null,
  next: JobEntry[],
): Array<{ entry: JobEntry; outcome: JobOutcome }> {
  if (!previous) return [];
  const finished: Array<{ entry: JobEntry; outcome: JobOutcome }> = [];
  for (const entry of next) {
    const before = previous.get(entry.key);
    if (!before || !isActive(before) || isActive(entry)) continue;
    const outcome = jobOutcome(entry);
    if (outcome) finished.push({ entry, outcome });
  }
  return finished;
}

/** While a job runs the entry asks this often, so a result is announced soon. */
export const JOBS_ACTIVE_POLL_MS = 5_000;

/**
 * While the tab is visible the counts refresh this often; they are also
 * asked on first load and whenever the Jobs menu opens. A request still
 * waiting for its answer is never sent again.
 */
export const JOBS_POLL_MS = 60_000;
