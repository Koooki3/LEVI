// Pure helpers for the live page: what counts as a fault, how fast to poll,
// how to put the GPU gate in plain words. No React, no fetching, so they can
// be tested on their own.
import type {
  DatasetDetail,
  DatasetRow,
  DemoRow,
  Fr3Health,
  LiveSession,
  LiveStatusResponse,
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
    row.excluded ?? 0,
    row.review_runs_open ?? 0,
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

/** The release-review runs a dataset's verdicts come from that are still
 * open for a person (the service keeps the newest few per dataset). */
export function reviewRuns(detail: DatasetDetail | undefined): ReviewRun[] {
  const runs = new Map<string, ReviewRun>();
  // The service says which runs are still open; older ones were cancelled.
  const open = detail?.review_runs ? new Set(detail.review_runs) : null;
  for (const d of detail?.demos ?? []) {
    const v = d.verdict;
    const id = v?.run_id;
    if (!v || !id || (open && !open.has(id))) continue;
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

/** Why the model server is not started or woken (gpu.decision.code). */
const DECISION_NOTES: Record<string, [string, string]> = {
  settling: [
    "Waiting for the policy server to settle",
    "A policy server just appeared or went away. The service waits a few seconds because a starting policy server grabs GPU memory.",
  ],
  vram: [
    "Waiting for GPU memory",
    "There is not enough free GPU memory to start or wake the local model yet.",
  ],
  insufficient_vram: [
    "Not enough free GPU memory for the model",
    "Even the smallest context the model can serve does not fit in the free memory beside the policy server. The service starts it when more memory is free.",
  ],
  lock: [
    "Another job holds the GPU lock",
    "The service starts the model when the lock is free.",
  ],
  manual: [
    "No model server answers",
    "The GPU mode is manual and no model server answers on the configured port.",
  ],
  external: [
    "Using a model server that is already running",
    "The service did not start it and leaves it alone.",
  ],
  external_busy: [
    "Another model server is using the port",
    "This service did not start it and leaves it alone.",
  ],
  error: [
    "The model server could not be woken",
    "The service could not read the GPU state or wake the model server; see the last error.",
  ],
  prewarm_waiting_for_policy: [
    "Pre-warm is waiting: the policy server was started first",
    "A policy server is already running and no evaluation session has appeared, so the gate is shut and the model cannot start. Order matters: live service with --prewarm first, then the policy server, then the evaluation. Restart in that order, or the model will cold-start when the client reaches standby.",
  ],
  standby_settling: [
    "The evaluation client just reached standby: waiting a moment before starting the model",
    "Its first episode may follow within seconds and a cold start is a heavy GPU load, so the service waits about 20 s. Starting the service with --prewarm before the evaluation avoids this wait.",
  ],
  gpu_not_free: [
    "Waiting for the GPU memory to be released",
    "The model server was stopped but its memory is not free yet. The service keeps the GPU lock and checks again until it is sure the card is free.",
  ],
  evaluation_active: [
    "The model is not started while an evaluation is under way",
    "Starting the model is a heavy GPU load whose effect on the policy has not been measured, so it starts only before a session (standby) or after it ends. Start the service with --prewarm while the robot is idle to have it ready beforehand.",
  ],
  gate_closed: [
    "Waiting for the robot policy to pause before starting the model",
    "Starting the model is a heavy GPU load of about a minute, so it never starts while the policy is inferring. It starts between episodes.",
  ],
  backoff: [
    "The last model start failed; trying again later",
    "The service retries with growing waits and stops after a few failures (then it needs you).",
  ],
  needs_attention: [
    "Labelling is paused until a person resumes it",
    "The model server failed to start several times in a row. Fix the cause in the model log, then run the resume command shown above.",
  ],
  asleep: [
    "The model server is asleep",
    "Its GPU memory is given back; it wakes in under a second when work arrives.",
  ],
  policy_large: [
    "The model server sleeps: the policy server holds too much GPU memory",
    "The policy server was started with a larger memory share than the model can share the card with. Restart it with XLA_PYTHON_CLIENT_MEM_FRACTION=.22 (the model is labelling again once it fits); until then the model labels only after that server exits.",
  ],
};

/** Why the model is or is not labelling right now, in words a person who is
 * running the robot understands. */
export function explainGate(
  service: ServiceStatus | null | undefined,
): GateExplanation | null {
  const gpu = service?.gpu;
  if (!service || !gpu) return null;
  const gate = gpu.gate;
  // A paused service says so before anything about the gate.
  if (gpu.decision?.code === "needs_attention") {
    const n = DECISION_NOTES.needs_attention;
    return { title: n[0], detail: n[1], tone: "warn" };
  }
  if (gate && gate.open === false) {
    if (gate.code === "policy_inferring")
      return {
        title: "Labelling is paused while the robot policy runs",
        detail:
          "The model and the policy share one GPU. A model request during inference would slow the policy and make the recorded time steps jitter, so labelling waits. It continues between episodes (returning home, waiting for the reset) and after the session ends.",
        tone: "warn",
      };
    if (gate.code === "episode_imminent")
      return {
        title: "Labelling pauses ahead of the next episode",
        detail:
          "A session is about to start its next episode (the reset wait is nearly over). The service stops sending model requests a moment before, so the policy never waits for the model.",
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
  const note = d?.code ? DECISION_NOTES[d.code] : undefined;
  if (d && d.allowed === false && note)
    return { title: note[0], detail: note[1], tone: "warn" };
  if (d && d.allowed === false && service.state === "gpu_wait")
    return {
      title: "Waiting for the local model to start",
      detail:
        "The model is not ready yet; the service starts it when the GPU allows.",
      tone: "warn",
    };
  if (service.state === "annotating")
    return {
      title: "The model is labelling",
      detail: "The GPU gate is open and a worker is labelling rollouts.",
      tone: "pass",
    };
  if (d?.code === "asleep" && note)
    return { title: note[0], detail: note[1], tone: "" };
  return {
    title: "Labelling may run whenever work arrives",
    detail:
      "The GPU gate is open. The model starts when a finished rollout is waiting.",
    tone: "pass",
  };
}

/** Seconds until the next episode of a session waiting for the reset: null
 * when it is not waiting or the wait or its start is unknown; zero or less
 * when the wait is over. */
export function resetRemaining(
  session: Pick<LiveSession, "state" | "reset_wait_s" | "waiting_reset_since">,
  nowSeconds: number,
): number | null {
  if (session.state !== "waiting_reset") return null;
  const wait = session.reset_wait_s;
  const since = session.waiting_reset_since;
  if (!wait || !since) return null;
  return since + wait - nowSeconds;
}

/** The main loop is called stuck after this long without a tick. */
export const LOOP_STALE_S = 300;

/** What a pause means and what to do about it, by `labelling_paused.code`. */
export const PAUSE_NOTES: Record<
  string,
  { title: string; detail: string; todo: string; command?: string }
> = {
  vllm_failed: {
    title: "The model server failed to start several times",
    detail: "The service gave up and will not try again by itself.",
    todo: "Fix the cause (see the reason; the model log is live/logs/vllm-launch.log in the live workspace), then resume:",
    command: "levi live resume",
  },
  vllm_error: {
    title: "The last model start failed",
    detail:
      "The service retries on its own with growing waits. If it keeps failing it pauses and asks you to resume.",
    todo: "Nothing to do yet. If it does not recover, read the reason and the model log (live/logs/vllm-launch.log in the live workspace).",
  },
  insufficient_vram: {
    title: "There is not enough free GPU memory for the model",
    detail:
      "Even the shortest context the model can serve does not fit beside the other GPU users.",
    todo: "Free GPU memory (stop other GPU jobs), or restart the policy server with the smaller memory share from the guide (XLA_PYTHON_CLIENT_MEM_FRACTION=.22). Labelling resumes by itself.",
  },
  policy_large: {
    title: "The policy server holds too much GPU memory",
    detail:
      "The model cannot share the card with a policy server started with a large memory share, so it sleeps and nothing is labelled.",
    todo: "Restart the policy server with XLA_PYTHON_CLIENT_MEM_FRACTION=.22 (about 7.6 GB). Labelling resumes by itself.",
  },
  vram: {
    title: "The model server has no room on the GPU",
    detail:
      "A sleeping model server cannot wake, or a start has no room, and this has lasted several minutes.",
    todo: "Free GPU memory (stop other GPU jobs, check nvidia-smi). Labelling resumes by itself.",
  },
  lock: {
    title: "Another job holds the GPU lock",
    detail:
      "Another agent has held the workspace GPU lock for several minutes, so the model cannot start.",
    todo: "Wait for that job to finish, or ask whoever runs it. Labelling resumes by itself once the lock is free.",
  },
  external_busy: {
    title: "Someone else's model server is on the port",
    detail:
      "A vLLM this service did not start is answering on its port, so the service leaves it alone and labels nothing.",
    todo: "Stop that server, or set vllm.adopt_external = true in live.toml if you want the service to use it. Labelling resumes by itself once the port is free.",
  },
  unknown_client: {
    title: "A policy server runs that no evaluation session vouches for",
    detail: "The service keeps the GPU free for it, so nothing is labelled.",
    todo: "Start the evaluation client with LEVI labelling on (it reports its state), or stop the stray policy server. Labelling resumes by itself.",
  },
};

export interface NeedsPerson {
  /** Nothing is being labelled and it will not pass by itself. */
  paused: { code?: string; reason?: string; since?: number } | null;
  /** Seconds since the main loop last ticked, when that is too long. */
  loopStalledS: number | null;
  /** The service gave up on something (`levi live resume`). */
  attention: { code?: string; reason?: string } | null;
  /** Datasets waiting for a person in the LEVI page. */
  awaiting: { name: string; kind: "plan" | "changes" | null }[];
  /** The page or core the service starts did not come up. */
  frontendFailed: { error: string; attempts: number } | null;
}

export function needsPerson(
  service: ServiceStatus | null | undefined,
  rows: Record<string, DatasetRow>,
  nowSeconds = Date.now() / 1000,
): NeedsPerson {
  const awaiting = Object.entries(rows)
    .filter(([, r]) => r.state === "awaiting_approval")
    .map(([name, r]) => ({ name, kind: r.awaiting ?? null }));
  const front = service?.frontend;
  const loopAge = service?.loop_at ? nowSeconds - service.loop_at : null;
  return {
    paused: service?.labelling_paused ?? null,
    loopStalledS:
      loopAge != null && loopAge > LOOP_STALE_S ? Math.round(loopAge) : null,
    // `labelling_paused` says the same, with the reason a person needs.
    attention: service?.labelling_paused ? null : (service?.attention ?? null),
    awaiting,
    frontendFailed:
      front?.state === "failed"
        ? { error: front.error ?? "", attempts: front.attempts ?? 0 }
        : null,
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

/** The navigation entry's light: green idle/normal, blue labelling, amber
 * paused or needs a person (including a blocked run), red FR3 fault or red
 * light, grey when the status cannot be read. */
export type PulseLight = "green" | "blue" | "amber" | "red" | "grey";

/** What the one-sentence hover explains (a key into `PULSE_NOTES`). */
export type PulseReason =
  | "unreachable"
  | "offline"
  | "fault"
  | "paused"
  | "blocked_person"
  | "awaiting"
  | "annotating"
  | "idle";

export interface Pulse {
  light: PulseLight;
  reason: PulseReason;
  /** Things waiting for a person (shown as a number when above 0). */
  count: number;
}

/** English sentences (looked up in the locale catalogs by the component). */
export const PULSE_NOTES: Record<PulseReason, string> = {
  unreachable: "The live status cannot be read right now.",
  offline:
    "The live service is not running. Open the live page for the command to start it.",
  fault:
    "The FR3 arm shows the red light, or an evaluation session reported a fault. Open the live page.",
  paused:
    "Labelling is paused and needs a person. Open the live page to see why.",
  blocked_person:
    "A run stopped for the robot's policy and needs you to press Resume.",
  awaiting: "A plan or draft is waiting for you in the LEVI page.",
  annotating: "Labelling finished episodes in the background.",
  idle: "Running normally; nothing is being labelled right now.",
};

/** The pulse of the live service, from `/api/levi/live/status`. `failures`
 * is the number of failed polls in a row (two make the answer stale). */
export function livePulse(
  status: LiveStatusResponse | null | undefined,
  failures = 0,
  nowSeconds = Date.now() / 1000,
): Pulse {
  if (!status?.enabled || failures >= 2)
    return { light: "grey", reason: "unreachable", count: 0 };
  if (!status.alive) return { light: "amber", reason: "offline", count: 1 };
  const service = status.service ?? null;
  const rows = service?.datasets ?? {};
  const fault = detectFault(service?.fr3, service?.sessions ?? []);
  const need = needsPerson(service, rows, nowSeconds);
  const blocked = status.blocked_runs;
  const stopped = need.paused || need.attention || need.loopStalledS != null;
  const items =
    (need.paused ? 1 : 0) +
    (!need.paused && need.attention ? 1 : 0) +
    (need.loopStalledS != null ? 1 : 0) +
    (need.frontendFailed ? 1 : 0) +
    need.awaiting.length +
    // A run the gate stopped that goes on by itself (`waiting`) needs nobody:
    // every robot episode stops one, and a light that turns amber for each
    // would teach people to ignore it. The live page lists them.
    (blocked?.needs_person?.length ?? 0);
  const faults = fault.sessions.length + (fault.redLight ? 1 : 0);
  if (fault.active || status.fr3_red)
    return { light: "red", reason: "fault", count: faults + items };
  const reason: PulseReason | null =
    stopped || need.frontendFailed
      ? "paused"
      : (blocked?.needs_person?.length ?? 0) > 0
        ? "blocked_person"
        : need.awaiting.length > 0
          ? "awaiting"
          : null;
  if (reason) return { light: "amber", reason, count: items };
  const busy =
    service?.state === "annotating" ||
    service?.state === "gpu_wait" ||
    service?.state === "active" ||
    Object.values(rows).some(
      (r) => r.state === "annotating" || r.annotating > 0,
    );
  return busy
    ? { light: "blue", reason: "annotating", count: 0 }
    : { light: "green", reason: "idle", count: 0 };
}

/** The runs the live gate stopped, as the live page tells them: those that
 * continue by themselves and those that need a person to press Resume. */
export function blockedRunsSummary(
  status: LiveStatusResponse | null | undefined,
): { waiting: number; needsPerson: number } {
  const blocked = status?.blocked_runs;
  return {
    waiting: blocked?.waiting?.length ?? 0,
    needsPerson: blocked?.needs_person?.length ?? 0,
  };
}

// --- removing episodes (restorable) -------------------------------------------

/** States of an episode a person may remove: taken into the dataset and not
 * being worked on. A rejected or stuck demo was never taken in. */
export const REMOVABLE_STATES = ["mirrored", "done", "failed", "skipped_human"];

/** Why an episode cannot be removed now: `busy` (it is in the batch in
 * progress), `not_part` (never taken into the dataset), or null. */
export type RemovalBlock = "busy" | "not_part" | null;

export function removalBlock(
  demo: DemoRow,
  batch: string[] | null | undefined,
): RemovalBlock {
  if (demo.excluded) return null;
  if ((batch ?? []).includes(demo.demo)) return "busy";
  return REMOVABLE_STATES.includes(demo.state ?? "") ? null : "not_part";
}

/** The episodes of `demos` that can be removed now. */
export function removableDemos(
  demos: DemoRow[] | undefined,
  batch: string[] | null | undefined,
): string[] {
  return (demos ?? [])
    .filter((d) => removalBlock(d, batch) === null)
    .map((d) => d.demo);
}

/** The list the page shows: the episodes in the dataset, or the removed ones. */
export function shownDemos(
  detail: DatasetDetail | undefined,
  showRemoved: boolean,
): DemoRow[] {
  return (showRemoved ? detail?.excluded_demos : detail?.demos) ?? [];
}

export function toggleSelection(
  selected: ReadonlySet<string>,
  demo: string,
): Set<string> {
  const next = new Set(selected);
  if (next.has(demo)) next.delete(demo);
  else next.add(demo);
  return next;
}

/** "Select all": everything in `candidates`, or nothing when it is all
 * selected already. */
export function toggleAll(
  selected: ReadonlySet<string>,
  candidates: string[],
): Set<string> {
  return candidates.length > 0 && candidates.every((d) => selected.has(d))
    ? new Set()
    : new Set(candidates);
}

/** Drop what can no longer be chosen (a batch started, a list refreshed). */
export function pruneSelection(
  selected: ReadonlySet<string>,
  candidates: string[],
): Set<string> {
  const allowed = new Set(candidates);
  return new Set([...selected].filter((d) => allowed.has(d)));
}

/** The names in a sentence: the first few, then "+n more". */
export function nameList(demos: string[], limit = 6): string {
  const shown = demos.slice(0, limit).join(", ");
  return demos.length > limit ? `${shown} +${demos.length - limit}` : shown;
}
