// The controls of the arm stack, from the `act` part of `GET
// /api/levi/marvel0/stack` (`levi.marvel0.stack.v1`, levi/marvel0/stack_act.py):
// per terminal M, A, B and C its state, the ready lines seen and still
// missing, whether it can be started or stopped and, when not, the backend's
// own blockers; the arm-controller marker with its wait; the gripper test; the
// guided sequence; the checks the page may run. Pure: what each button shows
// and why it is off is decided here, from what the backend said. A value of
// the wrong type reads as unknown and switches the control off; the page
// never works out a blocker itself.
import {
  blockersOf,
  codedSay,
  paramsOf,
  stopping,
  type Blocker,
  type Known,
} from "./ctl-text";
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
import type { StackView } from "./stack-status-logic";

export const COMPS = ["M", "A", "B", "C"] as const;
export type CompId = (typeof COMPS)[number];
export const lower = (id: CompId): string => id.toLowerCase();

export const COMP_STATES = [
  "idle",
  "starting",
  "ready",
  "failed",
  "dead",
  "unknown",
  "foreign_running",
  "stopping",
] as const;
export type CompState = (typeof COMP_STATES)[number];
export type Tone = "neutral" | "success" | "warning" | "danger" | "info";

export const STATE_TONE: Record<CompState, Tone> = {
  idle: "neutral",
  starting: "info",
  ready: "success",
  failed: "danger",
  dead: "danger",
  unknown: "warning",
  foreign_running: "warning",
  stopping: "info",
};
/** The catalogue key of each state's word. */
export const stateKey = (state: CompState): string =>
  `marvel0.ctl.state.${state}`;

/** A ready line: the evidence the backend waits for, as `{key, kind, text}`. */
export interface ReadyLine {
  key: string;
  /** `log`, `health` or `socket`: where the evidence is read. */
  kind: string;
  worded: Worded<Say>;
}
export interface Readiness {
  /** The lines already seen. */
  seen: ReadyLine[];
  /** The ones still to come. */
  missing: ReadyLine[];
  /** Seconds since the start. */
  waitedS: number | null;
  /** Seconds after which the backend calls a start failed. */
  timeoutS: number | null;
}

export interface CompView {
  id: CompId;
  state: CompState;
  /** Why the state is what it is, in the page's words or the server's. */
  reason: Worded<Say> | null;
  /** What went wrong after a start, when it did. */
  failure: Worded<Say> | null;
  unit: string | null;
  readiness: Readiness | null;
  canStart: boolean;
  startBlockers: Blocker[];
  canStop: boolean;
  stopBlockers: Blocker[];
  /** The phrases to type, as the backend names them. */
  confirm: { start: string | null; stop: string | null };
  /** The checks of the person a start asks for, by key. */
  attest: string[];
  /** The phrase that overrides the client rule of a stop (C only). */
  override: string | null;
  /** The stop is refused only because a client is on the robot server and
   * the override phrase would lift that. */
  canStopWithOverride: boolean;
  /** A remark to show after or beside a stop (the arm stays under control). */
  stopNote: string | null;
}

export interface MarkerAct {
  state: "present" | "absent" | "unknown";
  ageS: number | null;
  /** The page made it (it may remove it); a marker someone else made can be
   * adopted. */
  ours: boolean | null;
  /** Seconds the marker must exist before A may start. */
  waitS: number | null;
  /** Seconds of the wait that were left when the sample was taken. */
  remainingS: number | null;
  /** The marker is absent on a machine the page acts on: "Set marker" is
   * shown (on, or off with `touchBlockers` beside it). */
  showTouch: boolean;
  canTouch: boolean;
  /** What keeps "set marker" from being on besides the marker's own state (a
   * guided stop that runs), in the backend's words. */
  touchBlockers: Blocker[];
  canAdopt: boolean;
  /** Releasing is the S4-0 card's: only a marker of the page, and only when A
   * is gone and the arm shows no control connection. */
  canRelease: boolean;
  releaseBlockers: Blocker[];
}

export interface GripperLast {
  action: "open" | "close" | "position" | null;
  ok: boolean | null;
  /** The backend's word for how it ended (`reached`, `stalled`, ...). */
  code: string | null;
  reached: boolean | null;
  stalled: boolean | null;
  /** Epoch seconds. */
  at: number | null;
}
export interface GripperAct {
  /** A test is running now. */
  running: boolean;
  canRun: boolean;
  blockers: Blocker[];
  last: GripperLast | null;
  attest: string[];
}

/** How the last gripper test ended: it reached its position, it stalled, or
 * it did not reach it for another reason (or the backend did not say). */
export type GripperOutcome = "reached" | "stalled" | "failed";
export function gripperOutcome(last: GripperLast): GripperOutcome {
  if (last.stalled === true) return "stalled";
  return last.reached === true && last.ok !== false ? "reached" : "failed";
}
/** The last gripper test did not pass. B can still be ready (the controller is
 * up), so the strip and B's card say "Ready, test failed" and keep it in view
 * until a later test passes (the backend's `last` is the latest test). */
export function gripperTestFailed(gripper: GripperAct): boolean {
  return gripper.last !== null && gripperOutcome(gripper.last) !== "reached";
}

export const SEQUENCE_STATES = [
  "idle",
  "running",
  "waiting_confirm",
  "done",
  "failed",
  "cancelled",
] as const;
export type SequenceState = (typeof SEQUENCE_STATES)[number];
/** Which guided sequence the state is about: the start (M, marker, A, then B
 * and C by the person) or the stop of the whole stack (C, A, B in that order). */
export type SequenceKind = "start" | "stop";
export const SEQUENCE_STEP_STATES = [
  "pending",
  "running",
  "done",
  "skipped",
  "failed",
  "cancelled",
  "not_reached",
] as const;
export type SequenceStepState = (typeof SEQUENCE_STEP_STATES)[number];
/** One step of a guided start or stop and how far it is. A stop's steps are
 * terminals (`stop_c`, `stop_a`, `stop_b`); a start's are the chain `monitor`,
 * `probe`, `marker`, `wait`, `arm`, `await_a`, `b_probe`, `b`, `c_preflight`,
 * `c`. `terminal` is the terminal the step belongs to (the FCI check, the
 * marker and the wait belong to A). */
export interface SequenceStep {
  step: string;
  terminal: CompId;
  state: SequenceStepState;
  /** Seconds the step has taken in all; null until it has run (a step that was
   * skipped or not reached took none). The page shows where the time goes. */
  tookS: number | null;
}

/** What the start before A waits for, while it waits (the marker's age, the
 * heavy jobs of this computer, the arm's sockets). */
export interface SequenceWait {
  /** How old the marker is, and the age it must reach. */
  ageS: number | null;
  minS: number | null;
  /** Seconds to go; null while a heavy job runs (it yields within about
   * 30 s of the marker) or when nothing can be counted. */
  remainingS: number | null;
  /** Heavy jobs of the gate: `running` (a count), `unknown` (the fixed wait of
   * old applies) or `clear`. */
  heavy: { state: "clear" | "running" | "unknown"; count: number } | null;
  /** What still holds A back, by code: `marker_young`, `heavy_jobs_running`,
   * `heavy_jobs_unknown`, `arm_sockets`, `marker_absent`. */
  holds: string[];
  /** A heavy job that has not stopped after this long ends the start. */
  capS: number | null;
  /** Seconds the wait has lasted. */
  waitedS: number | null;
}

export interface SequenceAct {
  /** An idle sequence, or an older backend, names no kind: that is the start. */
  kind: SequenceKind;
  state: SequenceState;
  /** The step it is on while it runs (`monitor`, `probe`, `marker`, `wait`,
   * `arm`, `await_a` for the start; `stop_c`, `stop_a`, `stop_b` for the
   * stop). */
  step: string | null;
  /** The terminal the person has to start next while it waits. */
  next: CompId | null;
  reason: Worded<Say> | null;
  /** Seconds of the marker's wait left, while it waits for it. */
  remainingS: number | null;
  /** The steps in the order they run; empty while idle (and for an older
   * backend, which sent none for a start). */
  steps: SequenceStep[];
  /** What the start waits for before A (null outside that part of a start). */
  wait: SequenceWait | null;
  /** Seconds the steps took in all (set when it ended), and how long it has
   * been going. */
  tookS: number | null;
  elapsedS: number | null;
  /** How far the start goes by itself: to A only, or through B and C. */
  through: "a" | "c" | null;
  /** The start waits for the person's own Start of B / of C (`waiting_confirm`)
   * instead of starting it. Always true for a start that goes through A only. */
  pauseB: boolean;
  pauseC: boolean;
}

/** The stop of the whole stack: C, then A, then B (M stays), as one click.
 * The backend decides whether it is offered and says why not. */
export interface StopAllAct {
  /** The page acts on the real machine: the button and its reasons are shown.
   * (A stop needs no control file, so this is the profile alone.) */
  offered: boolean;
  canStop: boolean;
  /** Why it is not offered, in the backend's codes. */
  blockers: Blocker[];
}

export interface FciCheck {
  /** `open`, `closed` or `unreachable`. */
  state: string;
  ageS: number | null;
  /** The answer is recent enough for a start. */
  fresh: boolean;
}
export interface ServerCheck {
  ok: boolean | null;
  qos: boolean | null;
  controllers: boolean | null;
  eeFrame: boolean | null;
  ageS: number | null;
  fresh: boolean;
}
export interface OperationAct {
  /** `gripper`, `preflight_a`, `preflight_c`, `start`, `stop`... */
  kind: string;
  terminal: string | null;
  state: "running" | "done" | "failed";
  code: string | null;
  /** Epoch seconds the operation ended; null while it runs. */
  finishedAt: number | null;
}

export interface ActView {
  enabled: boolean;
  /** The product LEVI acts on the real machine; anything else only shows. */
  real: boolean;
  comps: Record<CompId, CompView>;
  marker: MarkerAct;
  gripper: GripperAct;
  sequence: SequenceAct;
  stopAll: StopAllAct;
  fci: FciCheck | null;
  server: ServerCheck | null;
  /** An operation of the page that runs now, or ended last. */
  operation: OperationAct | null;
  /** Notices of the backend, by code, with their facts. */
  notices: Array<{ code: string; params: Record<string, string | number> }>;
  /** The phrases to type, by name, as the backend names them. */
  phrases: Record<string, string>;
  /** Epoch seconds of the sample. */
  sampledAt: number | null;
}

const REASON = ["marvel0.ctl.reason.", "marvel0.stack.code."];
/** Why a guided start or stop ended: its own reasons, then the codes of a
 * refusal (a stop that met a client ends there with that code). */
const SEQUENCE_REASON = ["marvel0.ctl.seq.reason.", "marvel0.ctl.block."];
const FAILURE = ["marvel0.ctl.failure."];

function compState(value: unknown): CompState {
  return COMP_STATES.includes(value as CompState)
    ? (value as CompState)
    : "unknown";
}

function coded(
  prefixes: string[],
  raw: unknown,
  known: Known,
  params: Record<string, string | number> = {},
): Worded<Say> | null {
  const code = str(raw, 80);
  return code ? codedSay(prefixes, code, code, params, known) : null;
}

function lines(raw: unknown, known: Known): ReadyLine[] {
  return list(raw).flatMap((entry): ReadyLine[] => {
    const item = rec(entry);
    const key = str(item?.key, 80);
    if (!item || !key) return [];
    return [
      {
        key,
        kind: str(item.kind, 20) ?? "log",
        worded: codedSay(
          "marvel0.ctl.ready.",
          key,
          str(item.text, 200) ?? key,
          {},
          known,
        ),
      },
    ];
  });
}

function readiness(raw: unknown, known: Known): Readiness | null {
  const item = rec(raw);
  if (!item) return null;
  return {
    seen: lines(item.seen, known),
    missing: lines(item.missing, known),
    waitedS: num(item.waited_s),
    timeoutS: num(item.timeout_s),
  };
}

function strings(raw: unknown): string[] {
  return list(raw)
    .map((entry) => str(entry, 80))
    .filter((entry): entry is string => entry !== null);
}

function comp(
  id: CompId,
  raw: Rec | null,
  known: Known,
  real: boolean,
): CompView {
  const confirm = rec(raw?.confirm);
  const attest = rec(raw?.attest);
  return {
    id,
    state: compState(raw?.state),
    reason: coded(REASON, raw?.reason, known),
    failure: coded(FAILURE, raw?.failure, known),
    unit: str(raw?.unit, 120),
    readiness: readiness(raw?.readiness, known),
    // The backend decides; the page only adds that nothing is offered when it
    // does not act (a worktree instance) or sent no answer for this terminal.
    canStart: real && raw !== null && bool(raw.can_start) === true,
    startBlockers: blockersOf(raw?.start_blockers, known),
    canStop: real && raw !== null && bool(raw.can_stop) === true,
    stopBlockers: blockersOf(raw?.stop_blockers, known),
    confirm: { start: str(confirm?.start, 80), stop: str(confirm?.stop, 80) },
    attest: strings(attest?.start),
    override: str(raw?.override, 80),
    canStopWithOverride:
      real && raw !== null && bool(raw.can_stop_with_override) === true,
    stopNote: str(raw?.stop_note, 80),
  };
}

function markerAct(
  raw: Rec | null,
  real: boolean,
  enabled: boolean,
  passive: StackView | null,
  known: Known,
): MarkerAct {
  const state =
    raw?.state === "present" || raw?.state === "absent" ? raw.state : "unknown";
  const ours = bool(raw?.ours);
  const touchBlockers = blockersOf(raw?.touch_blockers, known);
  return {
    state,
    ageS: num(raw?.age_s),
    ours,
    waitS: num(raw?.wait_s),
    remainingS: num(raw?.remaining_s),
    showTouch: real && enabled && state === "absent",
    canTouch:
      real &&
      enabled &&
      state === "absent" &&
      stopping(touchBlockers).length === 0,
    touchBlockers,
    canAdopt: real && enabled && state === "present" && ours === false,
    canRelease:
      real &&
      state === "present" &&
      ours === true &&
      passive?.marker.canClear === true,
    releaseBlockers: [],
  };
}

function gripperAct(raw: Rec | null, known: Known, real: boolean): GripperAct {
  const last = rec(raw?.last);
  const action =
    last?.action === "open" ||
    last?.action === "close" ||
    last?.action === "position"
      ? last.action
      : null;
  return {
    running: raw?.state === "running",
    canRun: real && bool(raw?.can_run) === true,
    blockers: blockersOf(raw?.blockers, known),
    last: last
      ? {
          action,
          ok: bool(last.ok),
          code: str(last.code, 40),
          reached: bool(last.reached),
          stalled: bool(last.stalled),
          at: num(last.at),
        }
      : null,
    attest: strings(raw?.attest),
  };
}

function sequenceAct(raw: Rec | null, known: Known): SequenceAct {
  const next = str(raw?.next, 4)?.toUpperCase();
  return {
    kind: raw?.kind === "stop" ? "stop" : "start",
    state: SEQUENCE_STATES.includes(raw?.state as SequenceState)
      ? (raw!.state as SequenceState)
      : "idle",
    step: str(raw?.step, 40),
    next: COMPS.includes(next as CompId) ? (next as CompId) : null,
    reason: coded(
      SEQUENCE_REASON,
      raw?.reason,
      known,
      paramsOf(raw?.reason_params),
    ),
    remainingS: num(raw?.remaining_s),
    steps: sequenceSteps(raw?.steps),
    wait: sequenceWait(rec(raw?.wait)),
    tookS: num(raw?.took_s),
    elapsedS: num(raw?.elapsed_s),
    through: raw?.through === "a" || raw?.through === "c" ? raw.through : null,
    pauseB: bool(rec(raw?.pauses)?.b) === true,
    pauseC: bool(rec(raw?.pauses)?.c) === true,
  };
}

function stopAllAct(
  act: Rec,
  checks: Rec | null,
  known: Known,
  offered: boolean,
): StopAllAct {
  return {
    offered,
    // The backend decides; nothing is offered when this page does not act.
    canStop: offered && bool(checks?.can_stop_all) === true,
    blockers: blockersOf(act.stop_all_blockers, known),
  };
}

function sequenceWait(raw: Rec | null): SequenceWait | null {
  if (!raw) return null;
  const heavy = rec(raw.heavy);
  const state = heavy?.state;
  return {
    ageS: num(raw.age_s),
    minS: num(raw.min_s),
    remainingS: num(raw.remaining_s),
    heavy: heavy
      ? {
          state: state === "running" || state === "unknown" ? state : "clear",
          count: num(heavy.count) ?? 0,
        }
      : null,
    holds: strings(raw.holds),
    capS: num(raw.cap_s),
    waitedS: num(raw.waited_s),
  };
}

function sequenceSteps(raw: unknown): SequenceStep[] {
  return list(raw).flatMap((entry): SequenceStep[] => {
    const item = rec(entry);
    const step = str(item?.step, 40);
    const terminal = str(item?.terminal, 4)?.toUpperCase();
    const state = item?.state;
    if (!item || !step || !COMPS.includes(terminal as CompId)) return [];
    return [
      {
        step,
        terminal: terminal as CompId,
        state: SEQUENCE_STEP_STATES.includes(state as SequenceStepState)
          ? (state as SequenceStepState)
          : "pending",
        tookS: num(item.took_s),
      },
    ];
  });
}

function fciCheck(raw: unknown): FciCheck | null {
  const item = rec(raw);
  const state = str(item?.state, 20);
  return item && state
    ? { state, ageS: num(item.age_s), fresh: bool(item.fresh) === true }
    : null;
}

function serverCheck(raw: unknown): ServerCheck | null {
  const item = rec(raw);
  return item
    ? {
        ok: bool(item.ok),
        qos: bool(item.qos),
        controllers: bool(item.controllers),
        eeFrame: bool(item.ee_frame),
        ageS: num(item.age_s),
        fresh: bool(item.fresh) === true,
      }
    : null;
}

function operationAct(raw: unknown): OperationAct | null {
  const item = rec(raw);
  const kind = str(item?.kind, 40);
  const state = item?.state;
  if (!item || !kind) return null;
  return {
    kind,
    terminal: str(item.terminal, 4),
    state: state === "running" || state === "failed" ? state : "done",
    code: str(item.code, 80),
    finishedAt: num(item.finished_at),
  };
}

function phraseMap(raw: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [name, value] of Object.entries(rec(raw) ?? {})) {
    const text = str(value, 80);
    if (text) out[name] = text;
  }
  return out;
}

/** The control view of the stack document, or null when it has no `act` part
 * (an older backend, or an instance without the actions): the page then shows
 * the passive card alone. `passive` is the S4-0 view of the same document; the
 * marker's release is its. */
export function actView(
  raw: unknown,
  known: Known,
  passive: StackView | null = null,
): ActView | null {
  const doc = rec(raw);
  const act = rec(doc?.act);
  if (!doc || !act) return null;
  const enabled = bool(act.enabled) === true;
  const real = doc.profile === "real" && enabled;
  const terminals = rec(act.terminals);
  const checks = rec(act.checks);
  return {
    enabled,
    real,
    comps: {
      M: comp("M", rec(terminals?.m), known, real),
      A: comp("A", rec(terminals?.a), known, real),
      B: comp("B", rec(terminals?.b), known, real),
      C: comp("C", rec(terminals?.c), known, real),
    },
    marker: markerAct(
      rec(act.marker),
      doc.profile === "real",
      enabled,
      passive,
      known,
    ),
    gripper: gripperAct(rec(act.gripper_test), known, real),
    sequence: sequenceAct(rec(act.sequence), known),
    stopAll: stopAllAct(act, checks, known, doc.profile === "real"),
    fci: fciCheck(checks?.fci),
    server: serverCheck(checks?.c_preflight),
    operation: operationAct(act.operation),
    notices: list(act.notices).flatMap((entry) => {
      const item = rec(entry);
      const code = str(item?.code, 80);
      return code ? [{ code, params: paramsOf(item?.params) }] : [];
    }),
    phrases: { ...phraseMap(doc.phrases), ...phraseMap(act.phrases) },
    sampledAt: num(doc.sampled_at),
  };
}

// -- the phrases (the design record, section 5.2, as the backend names them)

const DEFAULT_PHRASES: Record<string, string> = {
  monitor_start: "start health monitor",
  monitor_stop: "stop health monitor",
  arm_start: "start arm stack",
  arm_stop: "stop arm stack",
  gripper_start: "start gripper stack",
  gripper_stop: "stop gripper stack",
  robot_server_start: "start robot server",
  robot_server_stop: "stop robot server",
  sequence_start: "start arm stack",
  sequence_stop: "stop whole stack",
  gripper_open: "gripper open",
  gripper_close: "gripper close",
  marker_touch: "set marker",
  marker_adopt: "adopt marker",
  marker_clear: "clear marker",
  client_override: "stop robot server with client connected",
};

/** The phrase to type for one named action: the backend's, else the one of
 * the design record. */
export function phraseFor(act: ActView | null, name: string): string {
  return act?.phrases[name] ?? DEFAULT_PHRASES[name] ?? name.replace(/_/g, " ");
}

const NAME_OF: Record<CompId, string> = {
  M: "monitor",
  A: "arm",
  B: "gripper",
  C: "robot_server",
};
/** The phrase for starting or stopping a terminal. */
export function compPhrase(
  act: ActView | null,
  op: "start" | "stop",
  id: CompId,
): string {
  const own = act?.comps[id].confirm[op];
  if (own) return own;
  return phraseFor(act, `${NAME_OF[id]}_${op}`);
}

// -- what a control shows

/** What a button of a terminal says about itself: on, or off with the
 * backend's reasons. */
export interface Control {
  enabled: boolean;
  blockers: Blocker[];
  /** A stop that meets a client on the robot server asks for the override
   * phrase too. */
  needsOverride: boolean;
}

export function startControl(view: CompView): Control {
  return {
    enabled: view.canStart,
    blockers: view.startBlockers,
    needsOverride: false,
  };
}

/** The stop of C is refused while a client is connected, unless the override
 * phrase is typed: the backend says when that is the only thing in the way
 * (`can_stop_with_override`), and then the button stays on and the dialog asks
 * for the phrase. */
export function stopControl(view: CompView): Control {
  const overridable = !view.canStop && view.canStopWithOverride;
  return {
    enabled: view.canStop || overridable,
    blockers: view.stopBlockers,
    needsOverride: overridable,
  };
}

/** The seconds of the marker's wait that are left, counting on from the
 * sample by `sinceSampleS`. Null: no marker, no wait, or nothing to count. */
export function markerRemaining(
  marker: MarkerAct,
  sinceSampleS: number,
): number | null {
  if (marker.state !== "present" || marker.remainingS === null) return null;
  return Math.max(0, marker.remainingS - Math.max(0, sinceSampleS));
}

/** The progress of a starting terminal: how many of its ready lines are seen. */
export function readinessProgress(
  readiness: Readiness | null,
): { seen: number; total: number } | null {
  if (!readiness) return null;
  const total = readiness.seen.length + readiness.missing.length;
  return total === 0 ? null : { seen: readiness.seen.length, total };
}

/** An operation of the page runs now: the buttons that start another wait. */
export const operationRunning = (act: ActView): boolean =>
  act.operation?.state === "running";

/** The check before A is offered while A is not up and nothing else runs; the
 * backend refuses it (in words) when the arm already has a control
 * connection. */
export function canCheckFci(act: ActView): boolean {
  const a = act.comps.A.state;
  return (
    act.real && (a === "idle" || a === "unknown") && !operationRunning(act)
  );
}
/** The checks before C need A and B ready. */
export function canCheckServer(act: ActView): boolean {
  return (
    act.real &&
    act.comps.A.state === "ready" &&
    act.comps.B.state === "ready" &&
    !operationRunning(act)
  );
}
/** The stop of the whole stack is offered when the backend says so. A guided
 * START that is running does not hold it back (the way out does not depend on
 * the way in: the backend ends the start and any read-only check first); only a
 * guided STOP already running does (it would be the same request). */
export function canStopSequence(act: ActView): boolean {
  return (
    act.stopAll.canStop &&
    !(act.sequence.kind === "stop" && act.sequence.state === "running")
  );
}
/** Why the start of the whole stack is not offered, when the page can say
 * (the key under `marvel0.ctl.seq.needs_`): another operation runs, A, B and C
 * are all ready already, or A is in a state the chain cannot start from. Null:
 * it is offered, or a sequence that runs says it itself, or the page only
 * shows. */
export type StartNeed = "free" | "all_ready" | "a_unfit";
export function startNeed(act: ActView): StartNeed | null {
  if (!act.real) return null;
  if (operationRunning(act)) return "free";
  if (act.sequence.state === "running") return null;
  if (act.sequence.state === "waiting_confirm") return null;
  if (["A", "B", "C"].every((id) => act.comps[id as CompId].state === "ready"))
    return "all_ready";
  // The chain starts A when it is idle and goes on from one that is ready or
  // starting; a failed, vanished, foreign, stopping or unreadable A is for the
  // person to look at first.
  return ["idle", "ready", "starting"].includes(act.comps.A.state)
    ? null
    : "a_unfit";
}
/** The start of the whole stack is offered while nothing else runs and
 * something of A, B, C is not ready yet: it starts what is not up and skips
 * what is (press it again after a failure). The backend decides the rest and
 * refuses in words. */
export function canStartSequence(act: ActView): boolean {
  return (
    act.real &&
    startNeed(act) === null &&
    act.sequence.state !== "running" &&
    act.sequence.state !== "waiting_confirm"
  );
}
