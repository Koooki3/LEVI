// Pure helpers for the live page: what counts as a fault, how fast to poll,
// how to put the GPU gate in plain words. No React, no fetching, so they can
// be tested on their own.
import type {
  DatasetDetail,
  DatasetRow,
  DemoRow,
  Fr3Health,
  LiveSession,
  ServiceStatus,
} from "./types";

/** Session states in which the evaluation is going on. */
export const LIVE_STATES = ["running", "homing", "waiting_reset", "standby"];
/** Session states that ended (on purpose, by a fault or by a crash). */
export const ENDED_STATES = ["fault", "stopped", "finished", "crashed"];
/** The client writes every ≤ 2 s; beyond this the page says it lost contact. */
export const LOST_AFTER_S = 10;

export const POLL_ACTIVE_MS = 2000;
export const POLL_IDLE_MS = 10000;
export const POLL_MAX_MS = 30000;
/** A dataset's detail is fetched again at most this often while it changes. */
export const DETAIL_REFRESH_MS = 10000;

/** Not finished, not crashed, yet silent for too long. */
export function isLost(session: LiveSession): boolean {
  if (ENDED_STATES.includes(session.state)) return false;
  return (session.age_s ?? 0) > LOST_AFTER_S;
}

/** An evaluation is going on (fast polling) or the service is working. */
export function isEvaluating(
  sessions: LiveSession[],
  service: ServiceStatus | null | undefined,
): boolean {
  if (sessions.some((s) => LIVE_STATES.includes(s.state) && !s.crashed))
    return true;
  return service?.state === "active" || service?.state === "annotating";
}

/** Milliseconds until the next request: 2 s while evaluating, 10 s idle,
 * doubling after each failure up to 30 s. */
export function nextDelay(evaluating: boolean, failures: number): number {
  const base = evaluating ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  if (failures <= 0) return base;
  return Math.min(POLL_MAX_MS, base * 2 ** Math.min(failures, 6));
}

export interface FaultInfo {
  /** Show the red banner. */
  active: boolean;
  /** The health monitor reports the red light. */
  redLight: boolean;
  /** Sessions whose state is `fault`. */
  sessions: LiveSession[];
  /** Plain reasons, as the robot side wrote them. */
  reasons: string[];
}

/** The red banner: a red light, or a session in state `fault`. Offline or
 * missing monitor is NOT a red light. */
export function detectFault(
  fr3: Fr3Health | null | undefined,
  sessions: LiveSession[],
): FaultInfo {
  const redLight = fr3?.state === "red";
  const faulted = sessions.filter((s) => s.state === "fault" || s.fault);
  const reasons: string[] = [];
  if (redLight) {
    for (const r of fr3?.reasons ?? []) reasons.push(r);
    // The monitor's reasons already name the errors; list them alone only
    // when it gave no reason.
    if (reasons.length === 0)
      for (const e of fr3?.current_errors ?? []) reasons.push(String(e));
  }
  for (const s of faulted) if (s.reason) reasons.push(s.reason);
  return {
    active: redLight || faulted.length > 0,
    redLight,
    sessions: faulted,
    reasons: [...new Set(reasons)].slice(0, 8),
  };
}

export function datasetOfSession(s: LiveSession): string {
  return s.dataset || `${s.group}__${s.task_folder}`;
}

/** `current`: a session of this dataset is in fault now (or the robot shows
 * the red light while it is moving); `earlier`: rollouts aborted by an FR3
 * fault before; null: none. */
export function datasetFaultKind(
  name: string,
  row: DatasetRow | undefined,
  sessions: LiveSession[],
  redLight: boolean,
): "current" | "earlier" | null {
  const mine = sessions.filter((s) => datasetOfSession(s) === name);
  if (mine.some((s) => s.state === "fault" || s.fault)) return "current";
  if (redLight && mine.some((s) => LIVE_STATES.includes(s.state)))
    return "current";
  if (row && (row.fr3_fault || row.fault)) return "earlier";
  return null;
}

/** Datasets worth loading details for first: faulted, being annotated, then
 * the most recently processed. */
export function rankDatasets(
  rows: Record<string, DatasetRow>,
  sessions: LiveSession[],
  redLight: boolean,
): string[] {
  const score = (name: string) => {
    const row = rows[name];
    const fault = datasetFaultKind(name, row, sessions, redLight);
    return (
      (fault === "current" ? 3e12 : 0) +
      (row.state === "annotating" ? 2e12 : 0) +
      (row.pending ? 1e12 : 0) +
      (row.last_processed_at ?? 0)
    );
  };
  return Object.keys(rows).sort(
    (a, b) => score(b) - score(a) || (a < b ? -1 : 1),
  );
}

/** Changes whenever a dataset row says something new, so its detail is
 * worth fetching again. */
export function rowSignature(row: DatasetRow | undefined): string {
  if (!row) return "";
  return [
    row.episodes,
    row.pending,
    row.annotating,
    row.done,
    row.failed,
    row.last_processed_at ?? 0,
    row.state ?? "",
    row.last_error ?? "",
  ].join("|");
}

// --- the automatic results -------------------------------------------------

export interface Tally {
  success: number;
  failure: number;
  undecided: number;
  /** Episodes with no verdict (set aside, no gripper opening, not reviewed yet). */
  none: number;
}

/** Automatic success/failure among the demos the service has judged. */
export function verdictTally(demos: DemoRow[] | undefined): Tally {
  const out: Tally = { success: 0, failure: 0, undecided: 0, none: 0 };
  for (const d of demos ?? []) {
    const v = d.verdict;
    if (!v) {
      if (d.state === "done") out.none += 1;
      continue;
    }
    if (v.undecided || !v.outcome) out.undecided += 1;
    else if (v.outcome === "success") out.success += 1;
    else if (v.outcome === "failure") out.failure += 1;
    else out.undecided += 1;
  }
  return out;
}

/** Demos with committed time segments, and how many segments that is. */
export function segmentSummary(demos: DemoRow[] | undefined) {
  let committed = 0;
  let segments = 0;
  for (const d of demos ?? []) {
    if (d.committed_at || (d.segments ?? 0) > 0) {
      committed += 1;
      segments += d.segments ?? 0;
    }
  }
  return { committed, segments };
}

export interface ReviewRun {
  runId: string;
  demos: number;
  success: number;
  failure: number;
  undecided: number;
  /** Epoch seconds of the newest verdict in the run. */
  at: number;
}

/** The release-review runs a dataset's verdicts come from. The service
 * leaves one per batch open for a person; the API does not say which a person
 * already settled, so this lists every run that holds a verdict. */
export function reviewRuns(detail: DatasetDetail | undefined): ReviewRun[] {
  const runs = new Map<string, ReviewRun>();
  for (const d of detail?.demos ?? []) {
    const v = d.verdict;
    const id = v?.run_id;
    if (!v || !id) continue;
    const run = runs.get(id) ?? {
      runId: id,
      demos: 0,
      success: 0,
      failure: 0,
      undecided: 0,
      at: 0,
    };
    run.demos += 1;
    if (v.undecided || !v.outcome) run.undecided += 1;
    else if (v.outcome === "success") run.success += 1;
    else if (v.outcome === "failure") run.failure += 1;
    else run.undecided += 1;
    run.at = Math.max(run.at, v.at ?? 0);
    runs.set(id, run);
  }
  return [...runs.values()].sort((a, b) => b.at - a.at);
}

export type ReviewFilter = "latest" | "day" | "all";
export const REVIEW_LATEST = 3;

/** A read-only view filter: the newest few, the last 24 h, or all. */
export function filterReviewRuns(
  runs: ReviewRun[],
  mode: ReviewFilter,
  nowSeconds: number,
): { shown: ReviewRun[]; hidden: number } {
  let shown = runs;
  if (mode === "latest") shown = runs.slice(0, REVIEW_LATEST);
  else if (mode === "day")
    shown = runs.filter((r) => nowSeconds - r.at <= 86400);
  return { shown, hidden: runs.length - shown.length };
}

// --- plain-language explanations (English; the page translates them) ---------

/** What the service's own state means. */
export function serviceStateNote(state: string | undefined): string {
  switch (state) {
    case "starting":
      return "The service is starting and does not accept sessions yet.";
    case "idle":
      return "Nothing to do: no evaluation is running and nothing waits to be labelled.";
    case "active":
      return "An evaluation is running or rollouts are being written; the service is watching.";
    case "annotating":
      return "A worker is labelling rollouts now.";
    case "gpu_wait":
      return "Rollouts wait for the local model; it may not work yet (see the GPU section).";
    case "error":
      return "The service reports an error (see the last error).";
    case "stopped":
      return "The service has stopped.";
    default:
      return "";
  }
}

export interface GateExplanation {
  /** Short headline. */
  title: string;
  /** One or two sentences in plain words. */
  detail: string;
  tone: "pass" | "warn" | "";
}

/** Why the model is or is not labelling right now, in words a person who is
 * running the robot understands. */
export function explainGate(
  service: ServiceStatus | null | undefined,
): GateExplanation | null {
  const gpu = service?.gpu;
  if (!service || !gpu) return null;
  const gate = gpu.gate;
  if (gate && gate.open === false) {
    if (gate.code === "policy_inferring")
      return {
        title: "Labelling is paused while the robot policy runs",
        detail:
          "The model and the policy share one GPU. A model request during inference would slow the policy and make the recorded time steps jitter, so labelling waits. It continues between episodes (returning home, waiting for the reset) and after the session ends.",
        tone: "warn",
      };
    if (gate.code === "unknown_client")
      return {
        title: "Labelling waits for a policy server it cannot see",
        detail:
          "A policy server is listening but no evaluation session reports that it is idle, so the service assumes it may be in use and keeps the GPU free.",
        tone: "warn",
      };
    return {
      title: "Labelling is paused",
      detail: "The GPU gate is closed.",
      tone: "warn",
    };
  }
  const d = gpu.decision;
  if (d && d.allowed === false && service.state === "gpu_wait") {
    const why: Record<string, string> = {
      settling:
        "A policy server just appeared or went away. The service waits a few seconds because a starting policy server grabs GPU memory.",
      vram: "There is not enough free GPU memory to start the local model yet.",
      lock: "Another job holds the GPU lock.",
      manual:
        "The GPU mode is manual and no model server answers on the configured port.",
      external:
        "Another model server is using the port; this service leaves it alone.",
      external_busy:
        "Another model server is busy; this service leaves it alone.",
      error: "The service could not read the GPU state.",
    };
    return {
      title: "Waiting for the local model to start",
      detail:
        why[d.code ?? ""] ||
        "The model is not ready yet; the service starts it when the GPU allows.",
      tone: "warn",
    };
  }
  if (service.state === "annotating")
    return {
      title: "The model is labelling",
      detail: "The GPU gate is open and a worker is labelling rollouts.",
      tone: "pass",
    };
  return {
    title: "Labelling may run whenever work arrives",
    detail:
      "The GPU gate is open. The model starts when a finished rollout is waiting.",
    tone: "pass",
  };
}

export function gpuModeNote(mode: string | undefined): string {
  switch (mode) {
    case "timeshare":
      return "Time-share: the policy and the model both stay in GPU memory; the model works only between episodes.";
    case "coexist":
      return "Co-exist: the model works whenever it has work; each policy inference is then about 60 ms slower.";
    case "manual":
      return "Manual: the service never starts a model; it uses the one already running.";
    default:
      return "";
  }
}

export function vllmLabel(state: string | undefined): string {
  switch (state) {
    case "ready":
      return "Running";
    case "asleep":
      return "Sleeping (memory given back)";
    case "starting":
      return "Starting";
    case "stopped":
      return "Stopped";
    case "error":
      return "Error";
    default:
      return state || "Unknown";
  }
}

/** Seconds → "3 h 12 min", "4 min 05 s", "12 s". */
export function shortDuration(seconds: number | null | undefined): string {
  if (seconds == null || !isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h} h ${String(m).padStart(2, "0")} min`;
  if (m) return `${m} min ${String(s % 60).padStart(2, "0")} s`;
  return `${s} s`;
}

/** An epoch (seconds) or ISO string as a local time, "—" when unknown. */
export function clock(value: number | string | null | undefined): string {
  if (value == null || value === "") return "—";
  const date =
    typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return isNaN(date.getTime()) ? String(value) : date.toLocaleTimeString();
}

const SESSION_RANK: Record<string, number> = {
  fault: 0,
  crashed: 1,
  running: 2,
  homing: 3,
  waiting_reset: 4,
  standby: 5,
};

/** Faults first, then running sessions, then the rest; by name within. */
export function sortSessions(sessions: LiveSession[]): LiveSession[] {
  const rank = (s: LiveSession) => SESSION_RANK[s.state] ?? 9;
  return [...sessions].sort(
    (a, b) =>
      rank(a) - rank(b) ||
      datasetOfSession(a).localeCompare(datasetOfSession(b)),
  );
}
