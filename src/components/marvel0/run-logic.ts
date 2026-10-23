// The hosted evaluation client, from `GET /api/levi/marvel0/run`
// (`levi.marvel0.run.v1`, levi/marvel0/run.py): its state, the episode and
// step, the passive checks before a start and, per action, whether it may be
// used now and why not. Pure: what each button shows is decided here from what
// the backend said; the page never works out a state or a reason itself.
//
// There is no key `s` or `p` anywhere: at the client's start gate they run a
// task (2026-10-20: a stray `s` ran the screws task twice), so the page only
// has the actions below, and the backend sends only the byte each one means.
import { codedSay, paramsOf, type Known } from "./ctl-text";
import {
  bool,
  list,
  num,
  rec,
  str,
  type Rec,
  type Worded,
} from "./marvel0-json";
import type { Say } from "./marvel0-logic";

export const RUN_SCHEMA = "levi.marvel0.run.v1";

export const RUN_STATES = [
  "idle",
  "preflight",
  "starting",
  "at_gate",
  "homing",
  "running",
  "awaiting_label",
  "finishing",
  "done",
  "failed",
  "held",
  "lost",
] as const;
export type RunState = (typeof RUN_STATES)[number];

export const ACTIONS = [
  "start",
  "enter",
  "hold",
  "success",
  "failure",
  "discard",
  "end_at_gate",
  "exit_home",
  "end",
] as const;
export type ActionId = (typeof ACTIONS)[number];

export type Tone = "neutral" | "success" | "warning" | "danger" | "info";
export const RUN_TONE: Record<RunState, Tone> = {
  idle: "neutral",
  preflight: "info",
  starting: "info",
  at_gate: "info",
  homing: "warning",
  running: "success",
  awaiting_label: "warning",
  finishing: "info",
  done: "neutral",
  failed: "danger",
  held: "warning",
  lost: "danger",
};
export const runStateKey = (state: RunState): string =>
  `marvel0.run.state.${state}`;

/** The states in which the arm may move now or is about to: the HOLD button
 * is on in all of them (and in `lost`, where the client may still be alive).
 * At the start gate the arm stands still until the person presses Enter, but
 * the client is up and one click freezes it, so it is on there too. */
const MAY_MOVE: readonly RunState[] = [
  "starting",
  "at_gate",
  "homing",
  "running",
  "awaiting_label",
  "finishing",
  "lost",
];
export const mayMove = (state: RunState): boolean => MAY_MOVE.includes(state);
/** A run that is over or not started: nothing to hold, nothing to end. */
export const isQuiet = (state: RunState): boolean =>
  state === "idle" || state === "done" || state === "failed";

export interface RunAction {
  allowed: boolean;
  whyNot: Worded<Say> | null;
  /** The arm moves home without any limit check after it (the client's home
   * move is blind). */
  blind: boolean;
}

export interface Episode {
  no: number | null;
  step: number | null;
  max: number | null;
  counted: number | null;
  target: number | null;
}

/** The saved command form of one run, whitelisted by the backend. */
export interface RunRecord {
  runId: string | null;
  phase: string | null;
  lockReleased: boolean | null;
  form: Record<string, unknown> | null;
}

export interface PreflightRow {
  code: string;
  /** `ok`, `refuse`, `unknown`, or `warn` (a hint that never refuses). */
  level: "ok" | "refuse" | "unknown" | "warn";
  worded: Worded<Say>;
}

export interface RunView {
  /** The site file lets the page host the client. */
  enabled: boolean;
  state: RunState;
  /** The backend says the arm may move now or is about to. */
  mayMove: boolean;
  reason: Worded<Say> | null;
  episode: Episode | null;
  /** Identity and original form of the hosted run, when the backend provides it. */
  run: RunRecord | null;
  preflight: PreflightRow[];
  actions: Record<ActionId, RunAction>;
  /** Epoch seconds of the sample. */
  sampledAt: number | null;
  /** The phrases to type, by action, as the backend names them. */
  phrases: Record<string, string>;
  /** Notices of the backend, by code (what the checks are worth, that the
   * monitor is stale, ...). */
  notices: string[];
}

const P = "marvel0.run.";

function runState(value: unknown): RunState {
  return RUN_STATES.includes(value as RunState) ? (value as RunState) : "idle";
}

function action(raw: unknown, known: Known): RunAction {
  const item = rec(raw);
  const why = item?.why_not;
  let whyNot: Worded<Say> | null = null;
  if (typeof why === "string" && why.trim())
    whyNot = codedSay(`${P}why.`, str(why, 80), why, {}, known);
  else if (rec(why)) {
    const w = rec(why) as Rec;
    const code = str(w.code, 80);
    whyNot = codedSay(
      `${P}why.`,
      code,
      str(w.text, 300) ?? code ?? "",
      paramsOf(w.params),
      known,
    );
  }
  return {
    allowed: bool(item?.allowed) === true,
    whyNot,
    blind: bool(item?.blind) === true,
  };
}

function episode(raw: unknown): Episode | null {
  const item = rec(raw);
  if (!item) return null;
  return {
    no: num(item.no),
    step: num(item.step),
    max: num(item.max_steps) ?? num(item.max),
    counted: num(item.counted),
    target: num(item.target),
  };
}

function runRecord(raw: unknown): RunRecord | null {
  const item = rec(raw);
  if (!item) return null;
  const form = rec(item.form);
  return {
    runId: str(item.run_id, 120),
    phase: str(item.phase, 40),
    lockReleased: bool(item.lock_released),
    form: form ? { ...form } : null,
  };
}

function preflightRow(raw: unknown, known: Known): PreflightRow[] {
  const item = rec(raw);
  const code = str(item?.code, 80);
  if (!item || !code) return [];
  const level =
    item.level === "ok" || item.level === "warn" || item.level === "refuse"
      ? item.level
      : "unknown";
  return [
    {
      code,
      level,
      worded: codedSay(
        `${P}check.`,
        code,
        str(item.text, 300) ?? code,
        paramsOf(item.params),
        known,
      ),
    },
  ];
}

/** The hosted client's view, or null when the answer is not a run document
 * in the format this page was written for. */
export function runView(raw: unknown, known: Known): RunView | null {
  const doc = rec(raw);
  if (!doc || doc.schema !== RUN_SCHEMA) return null;
  const actions = rec(doc.actions);
  const phrases: Record<string, string> = {};
  for (const [name, value] of Object.entries(rec(doc.phrases) ?? {})) {
    const text = str(value, 80);
    if (text) phrases[name] = text;
  }
  const why = doc.reason;
  const reasonCode =
    typeof why === "string" ? str(why, 80) : str(rec(why)?.code, 80);
  const shown = episode(doc.episode);
  const exit = num(rec(doc.exit)?.code);
  // The facts a reason's sentence names, from the document's own fields.
  const reasonParams: Record<string, string | number> = {
    ...paramsOf(rec(why)?.params),
    ...(shown?.no != null ? { episode: shown.no } : {}),
    ...(exit !== null ? { code: exit } : {}),
  };
  return {
    enabled: bool(doc.enabled) !== false,
    state: runState(doc.state),
    mayMove: bool(doc.may_move) === true,
    reason: reasonCode
      ? codedSay(
          `${P}reason.`,
          reasonCode,
          str(rec(why)?.text, 300) ?? reasonCode,
          reasonParams,
          known,
        )
      : null,
    episode: shown,
    run: runRecord(doc.run),
    preflight: list(doc.preflight).flatMap((entry) =>
      preflightRow(entry, known),
    ),
    actions: Object.fromEntries(
      ACTIONS.map((id) => [id, action(actions?.[id], known)]),
    ) as Record<ActionId, RunAction>,
    sampledAt: num(doc.sampled_at),
    phrases,
    notices: list(doc.notices).flatMap((entry) => {
      const code = str(rec(entry)?.code ?? entry, 80);
      return code ? [code] : [];
    }),
  };
}

// -- what the card shows

/** The actions that carry the state's next key, in the order they are shown:
 * the one the person presses when everything went well first. Never `hold`,
 * `end` or the blind exit, which have their places. */
export function armedActions(state: RunState): ActionId[] {
  switch (state) {
    case "at_gate":
      return ["enter"];
    case "running":
      return ["success", "failure"];
    case "awaiting_label":
      return ["success", "failure", "discard"];
    default:
      return [];
  }
}

/** The catalogue key of each armed action's button. */
export const ACTION_KEY: Record<ActionId, string> = {
  start: "marvel0.run.act.start",
  enter: "marvel0.run.act.enter",
  hold: "marvel0.run.act.hold",
  success: "marvel0.run.act.success",
  failure: "marvel0.run.act.failure",
  discard: "marvel0.run.act.discard",
  end_at_gate: "marvel0.run.act.end_at_gate",
  exit_home: "marvel0.run.act.exit_home",
  end: "marvel0.run.act.end",
};

/** The purpose a one-time challenge is asked for, per action. */
export type Purpose = "start" | "enter" | "end_at_gate" | "label" | "exit_home";
export function purposeOf(action: ActionId): Purpose | null {
  switch (action) {
    case "start":
      return "start";
    case "enter":
      return "enter";
    case "end_at_gate":
      return "end_at_gate";
    case "success":
    case "failure":
    case "discard":
      return "label";
    case "exit_home":
      return "exit_home";
    default:
      return null;
  }
}

/** The hold button is on whenever the arm may move, in the state last seen:
 * it does not wait for a fresh answer (a client that is not read is the case
 * in which it matters most) and no other condition switches it off, a hold
 * already on its way included (it is idempotent). Nothing seen yet: off. */
export function holdEnabled(lastSeen: Seen | null): boolean {
  return lastSeen !== null && (lastSeen.mayMove || mayMove(lastSeen.state));
}

/** What the last good read said about whether the arm may move. */
export interface Seen {
  state: RunState;
  mayMove: boolean;
}

/** The step bar: how far the episode is, or null when it has no length. */
export function stepBar(
  episode: Episode | null,
): { value: number; max: number } | null {
  if (
    !episode ||
    episode.step === null ||
    episode.max === null ||
    episode.max <= 0
  )
    return null;
  return { value: Math.min(episode.step, episode.max), max: episode.max };
}

/** The phrase to type for an action, else the design record's. */
const DEFAULT_PHRASES: Record<string, string> = {
  exit_home: "exit and go home",
  end: "end client run",
};
export function runPhrase(view: RunView | null, name: string): string {
  return (
    view?.phrases[name] ?? DEFAULT_PHRASES[name] ?? name.replace(/_/g, " ")
  );
}
/** The phrase that starts the client of one rollout group. */
export function startPhrase(view: RunView | null, group: string): string {
  return (view?.phrases.start ?? "start client {group}").replace(
    "{group}",
    group,
  );
}
