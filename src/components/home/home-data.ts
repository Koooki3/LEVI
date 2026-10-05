/**
 * What the home page shows, from answers the service already gives:
 * agent tasks (`/api/levi/agent/v1/activity/tasks`), conversions
 * (`/api/levi/jobs`), training pool jobs (`/api/levi/pool/jobs`) and the
 * catalog (`/api/levi/catalog`). No new API; pure functions, tested.
 */
import type { RecentVisit } from "@/components/shell/recent";

/** An agent task whose next step is a person's (levi/agent/tracking.py). */
export type PendingTask = {
  runId: string;
  dataset: string;
  workflow: string;
  instruction: string;
  /** "human_approval" | "human_review" | "human_commit" */
  step: string;
  episodes: number;
};

/** The words for whose turn it is, as an English catalog key. */
export const STEP_LABEL: Record<string, string> = {
  human_approval: "Approve the plan",
  human_review: "Review the proposals",
  human_commit: "Commit the reviewed changes",
};

type TaskRow = {
  run_id?: unknown;
  dataset?: unknown;
  workflow?: unknown;
  instruction?: unknown;
  waiting_for?: unknown;
  episodes?: unknown;
  status?: unknown;
};

/** Run states that will never need a person again. */
const CLOSED = new Set(["failed", "cancelled"]);

export function pendingTasks(body: unknown): PendingTask[] {
  const tasks =
    body && typeof body === "object"
      ? (body as { tasks?: unknown }).tasks
      : null;
  if (!Array.isArray(tasks)) return [];
  return tasks
    .filter(
      (task: TaskRow) =>
        task &&
        typeof task.run_id === "string" &&
        typeof task.waiting_for === "string" &&
        task.waiting_for in STEP_LABEL &&
        // `finished` is also true for a run whose proposals wait for review
        // (levi/agent/tracking.py), so only a closed status rules it out.
        !(typeof task.status === "string" && CLOSED.has(task.status)),
    )
    .map((task: TaskRow) => ({
      runId: task.run_id as string,
      dataset: typeof task.dataset === "string" ? task.dataset : "",
      workflow: typeof task.workflow === "string" ? task.workflow : "",
      instruction: typeof task.instruction === "string" ? task.instruction : "",
      step: task.waiting_for as string,
      episodes: Array.isArray(task.episodes) ? task.episodes.length : 0,
    }));
}

/** One running job, whichever list it came from. */
export type RunningJob = {
  id: string;
  kind: "pool" | "conversion";
  /** English catalog key for what it does. */
  title: string;
  /** Dataset, export name or source it works on (shown as data). */
  subject: string;
  stage: string;
  /** 0–1 when the job knows how far it is, else null. */
  fraction: number | null;
  href: string;
};

const POOL_ACTIVE = new Set(["running", "stalled", "cancelling"]);
const CONVERSION_ACTIVE = new Set(["queued", "running"]);
const POOL_TITLE: Record<string, string> = {
  scan: "Training pool scan",
  export: "Training pool export",
  push: "Send to remote",
};

type ProgressLike = { done?: unknown; total?: unknown; stage?: unknown };

function fraction(progress: ProgressLike | null | undefined): number | null {
  if (!progress) return null;
  const done = Number(progress.done);
  const total = Number(progress.total);
  if (!Number.isFinite(done) || !Number.isFinite(total) || total <= 0)
    return null;
  return Math.min(1, Math.max(0, done / total));
}

function lastPart(path: unknown): string {
  if (typeof path !== "string") return "";
  return path.replace(/\/+$/, "").split("/").pop() ?? "";
}

export function runningJobs(
  poolBody: unknown,
  conversionBody: unknown,
): RunningJob[] {
  const out: RunningJob[] = [];
  const pool =
    poolBody && typeof poolBody === "object"
      ? (poolBody as { jobs?: unknown }).jobs
      : null;
  if (Array.isArray(pool))
    for (const job of pool) {
      if (!job || typeof job.id !== "string") continue;
      if (typeof job.status !== "string" || !POOL_ACTIVE.has(job.status))
        continue;
      out.push({
        id: job.id,
        kind: "pool",
        title: POOL_TITLE[job.kind] ?? "Training pool job",
        subject:
          (job.options && typeof job.options.name === "string"
            ? job.options.name
            : "") || lastPart(job.destination ?? job.target),
        stage:
          job.status === "running" && typeof job.progress?.stage === "string"
            ? job.progress.stage
            : job.status,
        fraction: fraction(job.progress),
        href: "/pool",
      });
    }
  if (Array.isArray(conversionBody))
    for (const job of conversionBody) {
      if (!job || typeof job.id !== "string") continue;
      if (typeof job.status !== "string" || !CONVERSION_ACTIVE.has(job.status))
        continue;
      out.push({
        id: job.id,
        kind: "conversion",
        title: "Conversion",
        subject:
          (typeof job.dataset === "string" && job.dataset) ||
          lastPart(job.output) ||
          lastPart(job.source),
        stage:
          typeof job.progress?.stage === "string"
            ? job.progress.stage
            : typeof job.stage === "string"
              ? job.stage
              : job.status,
        fraction: fraction(job.progress),
        href: "/workbench",
      });
    }
  return out;
}

/** A registered local dataset, as much as the home page needs. */
export type DatasetSummary = {
  repo: string;
  name: string;
  episodes: number | null;
  kind: "raw" | "lerobot" | null;
};

export function catalogDatasets(body: unknown): DatasetSummary[] {
  const local =
    body && typeof body === "object"
      ? (body as { local?: unknown }).local
      : null;
  if (!Array.isArray(local)) return [];
  return local
    .filter((entry) => entry && typeof entry.id === "string")
    .map((entry) => ({
      repo: entry.id as string,
      name: typeof entry.name === "string" ? entry.name : entry.id,
      episodes:
        typeof entry.info?.total_episodes === "number"
          ? entry.info.total_episodes
          : typeof entry.format?.episodes === "number"
            ? entry.format.episodes
            : null,
      kind:
        entry.kind === "raw" || entry.kind === "lerobot" ? entry.kind : null,
    }));
}

/** Recently opened datasets first (newest first), then the other local ones;
 * at most `limit`. A recent dataset that is not local (a Hub dataset) is
 * kept too. */
export function recentDatasets(
  recent: RecentVisit[],
  local: DatasetSummary[],
  limit = 6,
): (DatasetSummary & { visitedAt: number | null; episode: number | null })[] {
  const byRepo = new Map(local.map((item) => [item.repo, item]));
  const seen = new Set<string>();
  const out: (DatasetSummary & {
    visitedAt: number | null;
    episode: number | null;
  })[] = [];
  for (const visit of recent) {
    if (seen.has(visit.repo)) continue;
    seen.add(visit.repo);
    const known = byRepo.get(visit.repo);
    out.push({
      repo: visit.repo,
      name: known?.name ?? visit.repo,
      episodes: known?.episodes ?? null,
      kind: known?.kind ?? null,
      visitedAt: visit.at,
      episode: visit.episode,
    });
  }
  for (const item of local) {
    if (seen.has(item.repo)) continue;
    seen.add(item.repo);
    out.push({ ...item, visitedAt: null, episode: null });
  }
  return out.slice(0, limit);
}

/** The greeting for the hour (English catalog key). */
export function greeting(hour: number): string {
  if (hour >= 5 && hour < 12) return "Good morning.";
  if (hour >= 12 && hour < 18) return "Good afternoon.";
  return "Good evening.";
}

/** "3 min ago" style text, from English catalog keys with {n}. */
export function relativeTime(
  then: number,
  now: number,
): { key: string; n: number } {
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return { key: "just now", n: 0 };
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return { key: "{n} min ago", n: minutes };
  const hours = Math.round(minutes / 60);
  if (hours < 24) return { key: "{n} h ago", n: hours };
  return { key: "{n} d ago", n: Math.round(hours / 24) };
}
