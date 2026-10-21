// What the Step 5 card shows and what its buttons may do, decided from the
// documents of autonomy-doc.ts. Pure: the clock is passed in, nothing is read
// from the DOM, nothing is sent. The page never works out a state, a key or a
// reason itself; it takes the backend's, and adds only the local checks that
// are about THIS browser (how old the page's read is, whether this page sent a
// key already, the 1.5 s delay after a key appears, the 3 s gap after a key).
// The backend repeats every one of them; these are the second layer.
//
// The words of the page's own sentences are catalogue keys
// (`marvel0.autonomy.*`); a sentence is a `Say`/`Worded`, never a string.
import type { Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import {
  KEY_IDS,
  type KeyId,
  type FormLimits,
  type KeyView,
  type Mode,
  type ReadyRow,
  type ReadyView,
  type RunView,
  type StateId,
  type StopPhase,
} from "./autonomy-doc";
import type { Tone } from "./run-logic";

// -- constants (design sections 4.2 and 9; the numbers are guesses to be
// measured on the first real run, so they are in one place)

export interface Limits {
  /** A key that has just appeared is not clickable for this long. */
  armDelayMs: number;
  /** After one key was sent, the next one waits this long. */
  keyGapMs: number;
  /** A key sent and not seen taken after this long is "not seen taken". */
  sentUnseenMs: number;
  /** A read older than this switches the keys off. */
  docStaleMs: number;
  /** The presence heartbeat of a visible, focused tab. */
  presenceMs: number;
  /** Polling of the run while D lives / while it does not. */
  runPollMs: number;
  idlePollMs: number;
  /** Polling of the readiness document. */
  readyPollMs: number;
}
export const LIMITS: Limits = {
  armDelayMs: 1_500,
  keyGapMs: 3_000,
  sentUnseenMs: 5_000,
  docStaleMs: 2_000,
  presenceMs: 5_000,
  runPollMs: 1_000,
  idlePollMs: 3_000,
  readyPollMs: 3_000,
};
/** The run document older than this is not shown as current. */
export const RUN_FRESH_MS = 5_000;
/** The readiness document older than this is not shown as current. */
export const READY_FRESH_MS = 12_000;

// -- phrases the page sends itself (nothing is typed; the backend still
// checks each one)

export const PREPARE_PHRASE = "prepare aeri run";
export const LIVE_STOP_PHRASE = "stop live service";
export const LEFTOVERS_PHRASE = "stop leftover aeri units";
export const DEFAULT_END_PHRASE = "end terminal D";
/** The phrase of a start: the backend's template (`start aeri {name}`) with the
 * run's name put in. */
export const startPhrase = (
  name: string,
  template = "start aeri {name}",
): string => template.replace("{name}", name);
/** The phrase of `execute` / `execute_variant`: the backend's when the
 * document names it, else the one the terminal asks for. */
export function executePhrase(key: KeyView, name: string | null): string {
  if (key.phrase) return key.phrase;
  const base = `execute ${name ?? ""}`.trim();
  return key.id === "execute_variant"
    ? `${base} after policy switch check`
    : base;
}

// -- states

export const STATE_TONE: Record<StateId, Tone> = {
  idle: "neutral",
  launching: "info",
  await_phrase: "warning",
  await_phrase_variant: "warning",
  services: "info",
  await_home_o: "warning",
  await_home_p: "warning",
  await_home_h: "warning",
  await_inventory: "warning",
  await_release: "warning",
  forward_running: "success",
  closing: "info",
  reverse_running: "success",
  await_resume: "warning",
  await_resume_confirm: "warning",
  busy: "info",
  stopping: "warning",
  stalled: "danger",
  fault_locked: "danger",
  orphaned: "warning",
  unknown: "danger",
  ended: "neutral",
};
export const stateKey = (id: StateId): string => `marvel0.autonomy.state.${id}`;
export const modeKey = (mode: Mode): string => `marvel0.autonomy.mode.${mode}`;
export const modeHintKey = (mode: Mode): string =>
  `marvel0.autonomy.mode_hint.${mode}`;
export const levelKey = (level: string): string =>
  `marvel0.autonomy.level.${level}`;
export const homingKey = (step: 1 | 2 | 3): string =>
  `marvel0.autonomy.homing.${step}`;
export const exitKey = (kind: string): string =>
  `marvel0.autonomy.exit.${kind}`;
export const stateHintKey = (id: StateId): string =>
  `marvel0.autonomy.hint.${id}`;

/** The three separate homing confirmations: 1, 2 or 3; 0 outside them. */
export function homingStep(id: StateId): 0 | 1 | 2 | 3 {
  return id === "await_home_o"
    ? 1
    : id === "await_home_p"
      ? 2
      : id === "await_home_h"
        ? 3
        : 0;
}

/** D is a process this page can still talk to or watch. */
export const isLive = (run: RunView | null): boolean => run?.d.alive === true;
/** There is a run record (live or ended); false for "nothing hosted" (state
 * `idle`, no run id). */
export const hasRun = (run: RunView | null): boolean =>
  run !== null && run.runId !== null && run.state.id !== "idle";

/** The states in which the backend says no key is expected from the person
 * but D is moving (or may): the frame of the card is warning-coloured. */
export const mayMove = (run: RunView | null): boolean =>
  run !== null && run.d.alive && run.state.mayMove;

/** The line D is waiting on, quoted from its screen: the backend's quote of
 * the prompt line, else the last non-empty line of the tail while a prompt is
 * open. */
export function promptLine(run: RunView): string | null {
  if (!run.state.prompt) return null;
  if (run.state.prompt.line) return run.state.prompt.line;
  for (let i = run.screen.tail.length - 1; i >= 0; i -= 1) {
    const line = run.screen.tail[i].trim();
    if (line) return line;
  }
  return null;
}

/** The page of the run in LEVI's automatic section, given only after the run
 * ended: before that it would show the vision model's judgements, which would
 * sway the person's own labels (AERI.md section 8). */
export function runPageHref(run: RunView | null): string | null {
  if (!run || !run.name || run.state.id !== "ended" || run.d.alive) return null;
  return `/automatic/runs/${encodeURIComponent(run.name)}`;
}

/** What an exit code of D means, by class (design section 3.2). */
export type ExitKind =
  | "completed"
  | "unfinished"
  | "refused"
  | "cancelled"
  | "host"
  | "signal"
  | "other";
export function exitKind(
  code: number | null,
  signal: number | null = null,
): ExitKind | null {
  // Ended by a signal with no exit code (a SIGKILL of the hang-up chain).
  if (code === null) return signal === null ? null : "signal";
  if (code === 0) return "completed";
  if (code === 1) return "unfinished";
  if (code === 2) return "refused";
  if (code === 130) return "cancelled";
  if (code === 97 || code === 98) return "host";
  if (code > 128 && code < 192) return "signal";
  return "other";
}

// -- the keys

/** The keys to show: those the backend allows, and those it refuses for a
 * reason a person should see (a key already sent, a verdict used, the monitor).
 * Keys that simply do not belong to this state are left out, and so are the
 * three homing keys when the profile has no such prompts. */
export function visibleKeys(run: RunView): KeyView[] {
  const homeless = run.profile !== null && !run.profile.startHome;
  return run.keys
    .filter((key) => {
      if (homeless && key.id.startsWith("home_")) return false;
      return key.allowed || key.whyCode !== "state_not_allowed";
    })
    .sort(
      (a, b) =>
        Number(a.id === "quit") - Number(b.id === "quit") ||
        KEY_IDS.indexOf(a.id) - KEY_IDS.indexOf(b.id),
    );
}

/** A successor key sits apart from the first one: the terminal flushes its
 * input before it reads the second key, so the page must not let one clicking
 * hand press both in the same place (design section 4.2-7). */
export function groupBySlot(keys: KeyView[]): {
  first: KeyView[];
  second: KeyView[];
} {
  return {
    first: keys.filter((key) => key.slot === "first"),
    second: keys.filter((key) => key.slot === "second"),
  };
}

/** The key this page sent and what it has seen of it. One key per prompt: as
 * long as the prompt is the same, no other key is offered. */
export interface SentKey {
  runId: string;
  seq: number;
  action: KeyId;
  /** Epoch ms of the click. */
  at: number;
  /** The last row of the click history when the key was sent. */
  histSig: string;
  /** The request got no answer at all: it may or may not have arrived. */
  lost: boolean;
}

export const historySig = (run: RunView): string => {
  const last = run.history[run.history.length - 1];
  return last ? `${last.rawTime ?? ""}|${last.key}|${run.history.length}` : "";
};

export type LockPhase = "sent" | "taken" | "unseen";

/** What this page knows of the key it sent. `null` once the prompt has moved
 * on (the state counter changed: the backend counts a state, a prompt, a used
 * verdict, the stop phase and the keyboard) or the run is another one. */
export function lockPhase(
  sent: SentKey | null,
  run: RunView,
  now: number,
  limits: Limits = LIMITS,
): LockPhase | null {
  if (!sent || sent.runId !== run.runId || sent.seq !== run.state.seq)
    return null;
  const last = run.history[run.history.length - 1];
  const taken =
    historySig(run) !== sent.histSig &&
    last !== undefined &&
    last.key === sent.action;
  if (taken) return "taken";
  if (run.notices.some((n) => n.code === "key_unconfirmed")) return "unseen";
  if (sent.lost) return "unseen";
  return now - sent.at >= limits.sentUnseenMs ? "unseen" : "sent";
}

export type KeyMode = "ready" | "wait" | "off" | "locked";

export interface KeyVM {
  key: KeyView;
  mode: KeyMode;
  /** Milliseconds until a `wait` key may be clicked. */
  waitMs: number;
  /** Why the key is off or locked: the backend's sentence or one of this
   * page's own (`marvel0.autonomy.local.*`). */
  why: Worded<Say> | null;
  lock: "sending" | LockPhase | null;
}

export interface KeyCtx {
  now: number;
  /** Epoch ms of the last good read; null before the first. */
  docAt: number | null;
  /** The last read worked. */
  current: boolean;
  /** When this page first showed each key as allowed, by `armKey`. */
  firstSeen: ReadonlyMap<string, number>;
  /** When this page last sent (or saw taken) a key; null: none yet. */
  lastKeyAt: number | null;
  sent: SentKey | null;
  /** The key in flight (a request is on its way). */
  working: KeyId | null;
  limits?: Limits;
}

/** The map key under which the first time a key was shown is remembered: a key
 * is new whenever the run, the state counter or the action is. */
export const armKey = (run: RunView, key: KeyView): string =>
  `${run.runId ?? ""}:${run.state.seq}:${key.id}`;

const local = (code: string): Worded<Say> => ({
  say: { key: `marvel0.autonomy.local.${code}` },
  text: code,
});

/** One key's state for the button: which of ready, waiting (with the time
 * left), off (with why) or locked (sent, not resent). */
export function keyVM(run: RunView, key: KeyView, ctx: KeyCtx): KeyVM {
  const limits = ctx.limits ?? LIMITS;
  const off = (why: Worded<Say> | null): KeyVM => ({
    key,
    mode: "off",
    waitMs: 0,
    why,
    lock: null,
  });
  if (ctx.working !== null)
    return ctx.working === key.id
      ? { key, mode: "locked", waitMs: 0, why: null, lock: "sending" }
      : off(local("busy"));
  const phase = lockPhase(ctx.sent, run, ctx.now, limits);
  if (phase !== null)
    return ctx.sent?.action === key.id
      ? { key, mode: "locked", waitMs: 0, why: null, lock: phase }
      : off({
          say: { key: "marvel0.autonomy.err.key_sent" },
          text: "key_sent",
        });
  if (!key.allowed)
    return off(
      key.whyNot ?? {
        say: { key: "marvel0.autonomy.err.state_not_allowed" },
        text: "state_not_allowed",
      },
    );
  const age = ctx.docAt === null ? Infinity : ctx.now - ctx.docAt;
  if (!ctx.current || age > limits.docStaleMs) return off(local("doc_stale"));
  if (key.level === "A")
    return { key, mode: "ready", waitMs: 0, why: null, lock: null };
  const first = ctx.firstSeen.get(armKey(run, key));
  const waits = [
    first === undefined
      ? limits.armDelayMs
      : first + limits.armDelayMs - ctx.now,
    // The backend's time against this browser's clock: a clock that is far off
    // must not keep a key off for minutes, so the wait this adds is bounded by
    // the longest delay the backend itself ever sets (the gap after a key).
    key.notBefore === null
      ? 0
      : Math.min(key.notBefore - ctx.now, limits.keyGapMs),
    ctx.lastKeyAt === null ? 0 : ctx.lastKeyAt + limits.keyGapMs - ctx.now,
  ];
  const waitMs = Math.max(0, ...waits);
  return waitMs > 0
    ? { key, mode: "wait", waitMs, why: null, lock: null }
    : { key, mode: "ready", waitMs: 0, why: null, lock: null };
}

/** The keys that must be given the clock: B and C keys not yet clickable. */
export const needsClock = (vms: KeyVM[]): boolean =>
  vms.some((vm) => vm.mode === "wait" || vm.lock === "sent");

// -- Stop

/** What the last good read said about Stop, kept however old it is: Stop does
 * not wait for a fresh answer (a D that is not read is the case in which it
 * matters most). */
export interface StopSeen {
  runId: string | null;
  alive: boolean;
  allowed: boolean;
  phase: StopPhase;
}
export const stopSeen = (run: RunView): StopSeen => ({
  runId: run.runId,
  alive: run.d.alive,
  allowed: run.stop.allowed,
  phase: run.stop.phase,
});

/** A Stop this page sent: it stays off until the document shows the stop phase
 * moving on (or the run is another one). */
export interface StopSent {
  runId: string | null;
  phase: StopPhase;
}

export type StopMode =
  | "hidden"
  | "ready"
  | "sent"
  | "acked"
  | "retry"
  | "spent";

/** The Stop frame: shown while D lives; clickable only while the backend
 * offers it and this page has not already sent it for that phase. The page
 * cannot send a second Stop of its own accord: a run gets one (and one retry
 * the backend opens when the first one was never acknowledged). */
export function stopControl(
  seen: StopSeen | null,
  sent: StopSent | null,
): { mode: StopMode; enabled: boolean } {
  if (!seen || !seen.alive) return { mode: "hidden", enabled: false };
  const lockedHere =
    sent !== null && sent.runId === seen.runId && sent.phase === seen.phase;
  const mode: StopMode =
    seen.phase === "idle"
      ? "ready"
      : seen.phase === "unconfirmed"
        ? "retry"
        : seen.phase === "retried"
          ? "spent"
          : seen.phase === "acked"
            ? "acked"
            : "sent";
  return { mode, enabled: seen.allowed && !lockedHere };
}

// -- the readiness rows and the start

export interface StartOff {
  code: string;
  worded: Worded<Say>;
}

const rowOff = (row: ReadyRow): StartOff => ({
  code: row.code,
  worded: row.worded,
});

/** The rows that refuse a start: refusals and unknowns (an unknown refuses
 * like a refusal), in the order the backend listed them. */
export const blockingRows = (rows: ReadyRow[]): ReadyRow[] =>
  rows.filter((row) => row.level === "refuse" || row.level === "unknown");

/** Why "Start terminal D" is off, empty when it may be clicked. The backend
 * checks every row again at the challenge and at the start (and its own
 * `start.allowed` of the run document, which covers a live D, an evaluation
 * client and a terminal that is driven elsewhere). */
export function startOff(
  ready: ReadyView | null,
  ctx: {
    evalLive: boolean;
    runLive: boolean;
    stale: boolean;
    /** The run document: its `start` block says whether a start is offered. */
    run?: RunView | null;
  },
): StartOff[] {
  const own = (code: string): StartOff => ({
    code,
    worded: { say: { key: `marvel0.autonomy.local.${code}` }, text: code },
  });
  const out: StartOff[] = [];
  if (!ready) return [own("unreadable")];
  if (!ready.enabled) return [own("disabled")];
  if (ready.site.state !== "ok") return [own("not_configured")];
  if (ctx.runLive) out.push(own("run_live"));
  if (ctx.evalLive) out.push(own("eval_live"));
  if (ctx.stale) out.push(own("ready_stale"));
  if (ctx.run && !ctx.run.start.allowed && !ctx.runLive)
    out.push({
      code: ctx.run.start.whyNot ? "run_start" : "start_not_offered",
      worded: ctx.run.start.whyNot ?? own("start_not_offered").worded,
    });
  if (!ready.prepared) out.push(own("not_prepared"));
  else if (!ready.prepared.intact) out.push(own("prepared_changed"));
  if (ready.profile && !ready.profile.known) out.push(own("profile_unknown"));
  out.push(...blockingRows(ready.rows).map(rowOff));
  return out;
}

// -- the prepare form (the backend validates again)

/** The task pattern of `aeri-external/tools/new_run.py`; the backend sends its
 * own in `limits.task.pattern` and that one wins. */
export const TASK_PATTERN = "^[a-z0-9 ,.'-]{3,120}$";

export interface PrepareForm {
  mode: Mode;
  task: string;
  rounds: string;
  steps: string;
  /** Empty keeps the template's wait. */
  gripperConfirmS: string;
  startHome: boolean;
}

export type PrepareField = "task" | "rounds" | "steps" | "gripper_confirm_s";

export const DEFAULT_LIMITS: FormLimits = {
  rounds: { min: 1, max: 50, default: 5 },
  steps: { min: 1, max: 3000, default: 200 },
  gripper: { min: 1, max: 10, default: 4 },
  taskPattern: "[a-z0-9 ,.'-]{3,120}",
};

const wholeNumber = (text: string): number | null =>
  /^\d{1,6}$/.test(text.trim()) ? Number(text.trim()) : null;
const decimal = (text: string): number | null =>
  /^\d{1,3}(\.\d{1,3})?$/.test(text.trim()) ? Number(text.trim()) : null;

/** The catalogue key of the hint for a field that cannot be sent as written
 * (a number out of the backend's range carries its bounds as `{min}` and
 * `{max}`: see `rangeOf`). Empty when the form can be sent. Only sanity checks:
 * the backend holds the limits and refuses again. */
export function prepareErrors(
  form: PrepareForm,
  limits: FormLimits = DEFAULT_LIMITS,
): Partial<Record<PrepareField, string>> {
  const out: Partial<Record<PrepareField, string>> = {};
  let pattern: RegExp;
  try {
    pattern = new RegExp(`^${limits.taskPattern}$`);
  } catch {
    pattern = new RegExp(TASK_PATTERN);
  }
  const task = form.task.trim();
  if (!pattern.test(task) || !/[a-z]/.test(task))
    out.task = "marvel0.autonomy.form.task_bad";
  const rounds = wholeNumber(form.rounds);
  if (
    rounds === null ||
    rounds < limits.rounds.min ||
    rounds > limits.rounds.max
  )
    out.rounds = "marvel0.autonomy.form.range_bad";
  const steps = wholeNumber(form.steps);
  if (steps === null || steps < limits.steps.min || steps > limits.steps.max)
    out.steps = "marvel0.autonomy.form.range_bad";
  if (form.gripperConfirmS.trim() !== "") {
    const gripper = decimal(form.gripperConfirmS);
    if (
      gripper === null ||
      gripper < limits.gripper.min ||
      gripper > limits.gripper.max
    )
      out.gripper_confirm_s = "marvel0.autonomy.form.range_bad";
  }
  return out;
}

/** The bounds a field's range hint names. */
export function rangeOf(
  field: PrepareField,
  limits: FormLimits = DEFAULT_LIMITS,
): { min: number; max: number } | null {
  return field === "rounds"
    ? limits.rounds
    : field === "steps"
      ? limits.steps
      : field === "gripper_confirm_s"
        ? limits.gripper
        : null;
}

/** The body of `POST .../aeri/prepare` (design section 8: closed). */
export function prepareBody(
  requestId: string,
  form: PrepareForm,
  confirm: string = PREPARE_PHRASE,
): Record<string, unknown> {
  return {
    request_id: requestId,
    mode: form.mode,
    task: form.task.trim(),
    rounds: Number(form.rounds.trim()),
    steps: Number(form.steps.trim()),
    gripper_confirm_s:
      form.gripperConfirmS.trim() === ""
        ? null
        : Number(form.gripperConfirmS.trim()),
    start_home: form.startHome,
    confirm,
  };
}

/** The form's starting values: the backend's defaults. */
export function initialForm(ready: ReadyView | null): PrepareForm {
  const limits = ready?.limits ?? DEFAULT_LIMITS;
  return {
    mode: "external",
    task: "",
    rounds: String(limits.rounds.default),
    steps: String(limits.steps.default),
    gripperConfirmS: String(limits.gripper.default),
    startHome: false,
  };
}

/** A short digest for the card (the full one is on the line of the command). */
export const shortSha = (sha: string | null): string =>
  sha ? sha.slice(0, 12) : "";
