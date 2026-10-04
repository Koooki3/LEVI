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

/** Poll interval: often enough to notice a job, rare enough to cost nothing. */
export const JOBS_POLL_MS = 30_000;
