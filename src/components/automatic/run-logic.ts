// Pure rules of the run window: the state graph, the blind-label rule, the
// resume result messages, polling and the metrics table. No React here, so the
// tests can call it directly. Texts are catalogue keys (`automatic.run.*`).
import type {
  CommandResult,
  ExecutionMode,
  MetricsReport,
  OperatorLabel,
  PendingCard,
  ResetMode,
  RunEvent,
  RunSnapshot,
} from "./types";

// ── State graph ───────────────────────────────────────────────────────────

/** The 13 states of `levi/domain/aeri.py` (`AERI_STATES`), by lane. */
export const STATE_LANES: { id: string; key: string; states: string[] }[] = [
  {
    id: "forward",
    key: "automatic.run.lane.forward",
    states: [
      "PREFLIGHT",
      "VERIFY_INITIAL",
      "FORWARD_ACTIVE",
      "FORWARD_STOPPING",
      "FORWARD_FINALIZE",
      "ROBOT_HOME",
      "SCENE_ASSESS",
    ],
  },
  {
    id: "reset",
    key: "automatic.run.lane.reset",
    states: ["RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"],
  },
  {
    id: "end",
    key: "automatic.run.lane.end",
    states: ["WAIT_HUMAN", "FAULT_LOCKED", "COMPLETED"],
  },
];
export const RESET_STATES = ["RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"];

export const STATE_LABEL: Record<string, string> = {
  INIT: "automatic.run.state.INIT",
  PREFLIGHT: "automatic.run.state.PREFLIGHT",
  VERIFY_INITIAL: "automatic.run.state.VERIFY_INITIAL",
  FORWARD_ACTIVE: "automatic.run.state.FORWARD_ACTIVE",
  FORWARD_STOPPING: "automatic.run.state.FORWARD_STOPPING",
  FORWARD_FINALIZE: "automatic.run.state.FORWARD_FINALIZE",
  ROBOT_HOME: "automatic.run.state.ROBOT_HOME",
  SCENE_ASSESS: "automatic.run.state.SCENE_ASSESS",
  RESET_ACTIVE: "automatic.run.state.RESET_ACTIVE",
  RESET_VERIFY: "automatic.run.state.RESET_VERIFY",
  RESET_FINALIZE: "automatic.run.state.RESET_FINALIZE",
  WAIT_HUMAN: "automatic.run.state.WAIT_HUMAN",
  FAULT_LOCKED: "automatic.run.state.FAULT_LOCKED",
  COMPLETED: "automatic.run.state.COMPLETED",
};

export type NodeStatus = "current" | "disabled" | "normal";
export interface GraphNode {
  state: string;
  status: NodeStatus;
}
export interface GraphLane {
  id: string;
  key: string;
  nodes: GraphNode[];
}

/** In `human_assisted` the three RESET_* states are never entered. */
export function resetUnused(mode: ResetMode | null | undefined): boolean {
  return mode === "human_assisted";
}

export function stateGraph(
  state: string,
  mode: ResetMode | null | undefined,
): GraphLane[] {
  const lanes: GraphLane[] = STATE_LANES.map((lane) => ({
    id: lane.id,
    key: lane.key,
    nodes: lane.states.map((name) => ({
      state: name,
      status:
        name === state
          ? "current"
          : resetUnused(mode) && RESET_STATES.includes(name)
            ? "disabled"
            : "normal",
    })),
  }));
  // A state this page does not know (INIT, or one added later) is still shown.
  const known = STATE_LANES.some((lane) => lane.states.includes(state));
  if (state && !known)
    lanes.unshift({
      id: "other",
      key: "automatic.run.lane.other",
      nodes: [{ state, status: "current" }],
    });
  return lanes;
}

export const EXECUTION_MODE_LABEL: Record<ExecutionMode, string> = {
  dry_run: "automatic.run.mode.dry_run",
  shadow: "automatic.run.mode.shadow",
  assisted: "automatic.run.mode.assisted",
  autonomous: "automatic.run.mode.autonomous",
};
export const RESET_MODE_LABEL: Record<ResetMode, string> = {
  single_reset_policy: "automatic.run.reset.single_reset_policy",
  human_assisted: "automatic.run.reset.human_assisted",
};
/** Modes that move the robot (the first release has no adapter for them). */
export function movesRobot(mode: ExecutionMode | string | undefined): boolean {
  return !!mode && mode !== "dry_run" && mode !== "shadow";
}

// ── Blind labels ──────────────────────────────────────────────────────────

/** True while the operator has not labelled the last episode: the automatic
 * verdict and how the episode ended must not be shown or hinted at. */
export function isBlind(card: PendingCard | null | undefined): boolean {
  return !!card && card.operator_label.hidden_until_labelled !== false;
}
/** The automatic verdict, only after the operator's label. */
export function visibleVerdict(card: PendingCard): string | null {
  return isBlind(card) ? null : card.operator_label.automatic_verdict;
}
/** How the last episode ended (an early stop is a hint of the verdict). */
export function visibleEnding(card: PendingCard): {
  endedBy: string | null;
  stopReason: string | null;
  rolloutLabel: string | null;
} {
  if (isBlind(card) || !card.last_episode)
    return { endedBy: null, stopReason: null, rolloutLabel: null };
  return {
    endedBy: card.last_episode.ended_by ?? null,
    stopReason: card.last_episode.stop_reason ?? null,
    rolloutLabel: card.last_episode.rollout_label ?? null,
  };
}
/** After a label is posted the answer's card is shown until a poll catches
 * up; a poll that still says "blind" for the same episode is older. */
export function mergeCard(
  polled: PendingCard | null,
  posted: PendingCard | null,
): PendingCard | null {
  if (!posted) return polled;
  if (!polled) return null;
  const same =
    polled.last_episode?.episode_id === posted.last_episode?.episode_id;
  return same && isBlind(polled) && !isBlind(posted) ? posted : polled;
}
/** The fields that could tell the verdict of an unlabelled episode. */
const REVEALING = /early[_ -]?stop|goal[_ -]?verified|verdict|outcome/i;
export function maskEvent(event: RunEvent, blind: boolean): RunEvent {
  if (!blind) return event;
  const reveals = [event.kind, event.reason, event.to].some(
    (field) => !!field && REVEALING.test(field),
  );
  return reveals ? { ...event, reason: null, kind: "hidden" } : event;
}

export const LABEL_KEY: Record<OperatorLabel, string> = {
  success: "automatic.run.label.success",
  failure: "automatic.run.label.failure",
  discarded: "automatic.run.label.discarded",
  unclear: "automatic.run.label.unclear",
};
export const LABELS: OperatorLabel[] = [
  "success",
  "failure",
  "discarded",
  "unclear",
];

// ── Resume and stop results ───────────────────────────────────────────────

export const RESULT_KEY: Record<string, string> = {
  applied: "automatic.run.result.applied",
  repeated: "automatic.run.result.repeated",
  stale_sequence: "automatic.run.result.stale_sequence",
  confirmations_missing: "automatic.run.result.confirmations_missing",
  not_waiting: "automatic.run.result.not_waiting",
  not_running: "automatic.run.result.not_running",
  refused: "automatic.run.result.refused",
};
export function resultKey(result: CommandResult | null): string | null {
  if (!result) return null;
  if (result.code && result.code in RESULT_KEY) return RESULT_KEY[result.code];
  return RESULT_KEY[result.result] ?? RESULT_KEY.refused;
}
/** A result that ends the dialog (done or already done). */
export function resultDone(result: CommandResult | null): boolean {
  return result?.result === "applied" || result?.result === "repeated";
}

// ── Scene check ───────────────────────────────────────────────────────────

export type SceneAnswer = true | false | null;
/** Only the predicates the question named, each answered; "cannot see" is null. */
export function sceneAnswers(
  names: string[],
  chosen: Record<string, SceneAnswer | undefined>,
): Record<string, SceneAnswer> {
  const out: Record<string, SceneAnswer> = {};
  for (const name of names) out[name] = chosen[name] ?? null;
  return out;
}
export function sceneComplete(
  required: string[],
  chosen: Record<string, SceneAnswer | undefined>,
): boolean {
  return required.every((name) => chosen[name] !== undefined);
}
/** Seconds left to answer, never below zero. */
export function sceneSecondsLeft(
  askedAt: number,
  timeoutS: number,
  now: number,
): number {
  return Math.max(0, Math.ceil((askedAt + timeoutS * 1000 - now) / 1000));
}

// ── Polling ───────────────────────────────────────────────────────────────

const ACTIVE_MS = 2000;
const HIDDEN_MS = 15000;
const IDLE_MS = 10000;
const MAX_BACKOFF_MS = 30000;
export const FINISHED_STATES = ["COMPLETED"];

/** 2 s while the run is going, a lot slower in a hidden tab or after it ended;
 * a failed request doubles the wait up to 30 s. */
export function nextPollDelay(opts: {
  hidden: boolean;
  state?: string;
  failures?: number;
}): number {
  const base = opts.hidden
    ? HIDDEN_MS
    : opts.state && FINISHED_STATES.includes(opts.state)
      ? IDLE_MS
      : ACTIVE_MS;
  return Math.min(MAX_BACKOFF_MS, base * 2 ** Math.min(opts.failures ?? 0, 5));
}

export function snapshotBlind(snapshot: RunSnapshot | null): boolean {
  return isBlind(snapshot?.pending_card);
}

// ── Durations ─────────────────────────────────────────────────────────────

export function durationText(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds} s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ${seconds % 60} s`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min`;
}

// ── Metrics ───────────────────────────────────────────────────────────────

export interface MetricRow {
  path: string;
  /** The top-level group (`agreement`, `turnaround`, ...). */
  group: string;
  value: string;
  /** A rate with its Wilson 95 % interval. */
  interval?: [number, number];
  comparable: boolean | null;
}

function isRatio(value: unknown): value is {
  n: number;
  of: number;
  rate: number | null;
  wilson95: [number, number] | null;
} {
  if (!value || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return "n" in v && "of" in v && "rate" in v && "wilson95" in v;
}
const pct = (x: number) => `${(x * 100).toFixed(1)} %`;

/** Flatten the report into rows: a `{n, of, rate, wilson95}` object becomes one
 * row with its interval; scalars become rows; long lists and deep objects are
 * left out. `blind` removes the agreement group (it would compare the
 * operator's label with the automatic verdict). */
export function metricRows(report: MetricsReport, blind: boolean): MetricRow[] {
  const comparable = new Set(report.comparable ?? []);
  const specific = new Set(report.mode_specific ?? []);
  const rows: MetricRow[] = [];
  const kind = (path: string): boolean | null => {
    for (const name of comparable)
      if (path === name || path.startsWith(`${name}.`)) return true;
    for (const name of specific)
      if (path === name || path.startsWith(`${name}.`)) return false;
    return null;
  };
  const SKIP = new Set([
    "schema",
    "note",
    "comparable",
    "mode_specific",
    "mode_source",
    "truth",
    "reset_mode",
    "scene_check",
  ]);
  const walk = (value: unknown, path: string, depth: number) => {
    const group = path.split(".")[0];
    if (blind && group === "agreement") return;
    if (isRatio(value)) {
      const text =
        value.rate == null
          ? `${value.n} / ${value.of}`
          : `${value.n} / ${value.of} (${pct(value.rate)})`;
      rows.push({
        path,
        group,
        value: text,
        interval: value.wilson95 ?? undefined,
        comparable: kind(path),
      });
      return;
    }
    if (value === null || value === undefined) return;
    if (typeof value === "object") {
      if (Array.isArray(value) || depth >= 3) return;
      for (const [key, inner] of Object.entries(value))
        if (!(depth === 0 && SKIP.has(key)))
          walk(inner, path ? `${path}.${key}` : key, depth + 1);
      return;
    }
    rows.push({ path, group, value: String(value), comparable: kind(path) });
  };
  walk(report, "", 0);
  return rows;
}
