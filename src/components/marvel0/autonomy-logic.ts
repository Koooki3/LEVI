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
  /** The pace of a poll that has little to say: hosting switched off in the
   * control file, no `[aeri]` section, an evaluation client live (design
   * review R11 M4: a read of the readiness document starts about six
   * processes, so it is not made every 3 s for nothing). */
  slowPollMs: number;
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
  slowPollMs: 30_000,
};
/** How often the readiness document is read. It is the costly one (each read
 * the backend makes starts a handful of processes: git, nvidia-smi, systemctl),
 * so it is read quickly only while there is something to prepare:
 *  - hosting is off in the control file, or the site file has no usable
 *    `[aeri]` section: it is read once and then every `slowPollMs` (a person
 *    who edits the file sees the change within that time, or on Refresh);
 *  - a hosted evaluation client is live (only one session at a time, and the
 *    arm controller is up then): after the first read it is not read at all
 *    until the client ends (`readyPaused`). */
export function readyPollMs(
  ready: ReadyView | null,
  routeOff: boolean,
  limits: Limits = LIMITS,
): number {
  return hostingOff(ready, routeOff) ? limits.slowPollMs : limits.readyPollMs;
}
/** Nothing can be prepared or started: hosting is off in the control file (the
 * routes answer `aeri_disabled` or the document says `enabled: false`) or the
 * site file has no usable `[aeri]` section. */
export const hostingOff = (
  ready: ReadyView | null,
  routeOff: boolean,
): boolean =>
  routeOff || (ready !== null && (!ready.enabled || ready.site.state !== "ok"));
export const readyPaused = (evalLive: boolean, loaded: boolean): boolean =>
  evalLive && loaded;

/** How often the hosted D's document is read: every second while D lives, else
 * every 3 s, or every `slowPollMs` when hosting is off or an evaluation client
 * is live (D cannot start then, and the page reads on only to notice). */
export function runPollMs(
  run: RunView | null,
  quiet: boolean,
  limits: Limits = LIMITS,
): number {
  if (run?.d.alive === true) return limits.runPollMs;
  return quiet ? limits.slowPollMs : limits.idlePollMs;
}

/** The run document older than this is not shown as current. */
export const RUN_FRESH_MS = 5_000;
/** The readiness document older than this is not shown as current. */
export const READY_FRESH_MS = 12_000;

// -- phrases the page sends itself (nothing is typed; the backend still
// checks each one)

export const PREPARE_PHRASE = "prepare aeri run";
export const LIVE_STOP_PHRASE = "stop live service";
export const LEFTOVERS_PHRASE = "stop leftover aeri units";
/** The phrase of a start: the backend's template (`start aeri {name}`) with the
 * run's name put in. */
export const startPhrase = (
  name: string,
  template = "start aeri {name}",
): string => template.replace("{name}", name);
/** The phrase a level C key is confirmed with: the one the backend's document
 * names for that key, and nothing else. The page never builds it from the run's
 * name (a phrase it made up could differ from the one the terminal asks for and
 * would hide that); a C key whose document carries none cannot be sent. */
export function keyPhrase(key: KeyView): string | null {
  return key.level === "C" && key.phrase ? key.phrase : null;
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
/** A Stop was sent and D has not said it received it (`sent`, `unconfirmed`,
 * `retried`): the arm may still be moving. `acked` is D's own acknowledgement,
 * whoever sent the signal. */
export const stopUnacknowledged = (phase: StopPhase): boolean =>
  phase === "sent" || phase === "unconfirmed" || phase === "retried";

/** The sentence under the state badge. The three states that follow a Stop
 * say what the Stop has reached, not what it is meant to do: until D has
 * acknowledged it (stop phase `acked`) the arm may still be moving and D is not
 * known to be winding down, so the sentence says "sent, not confirmed" and
 * leaves the physical e-stop as the thing to rely on (review R10 #10). */
export function stateHintKey(
  id: StateId,
  phase: StopPhase = "idle",
  reason: string | null = null,
  /** Whether the document offers a Stop now. "One more try is offered" is said
   * only when it does (review N2: the phase alone said it for an adopted D
   * whose retry the backend had not opened). */
  stopAllowed = true,
): string {
  // `no_keyboard` is two different situations, told apart by the document's own
  // word on Stop: another live core of LEVI owns the run (Stop refused,
  // `driven_elsewhere`): this page only watches it; or the page cannot follow a
  // D that is alive (a record of the first build with a site file that does not
  // read): Stop is offered and goes to D's own process. "Sends no Stop from here"
  // would be false for the second.
  if (id === "unknown" && reason === "no_keyboard")
    return stopAllowed
      ? "marvel0.autonomy.hint.unknown_unfollowed"
      : "marvel0.autonomy.hint.unknown_other_core";
  const unconfirmed = stopUnacknowledged(phase);
  if (id === "stopping")
    return phase === "acked"
      ? "marvel0.autonomy.hint.stopping"
      : phase === "idle"
        ? "marvel0.autonomy.hint.stopping_ending"
        : phase === "unconfirmed" && !stopAllowed
          ? "marvel0.autonomy.hint.stopping_unoffered"
          : `marvel0.autonomy.hint.stopping_${phase}`;
  if (id === "orphaned" && unconfirmed)
    return "marvel0.autonomy.hint.orphaned_unconfirmed";
  if (id === "stalled" && unconfirmed)
    return "marvel0.autonomy.hint.stalled_unconfirmed";
  return `marvel0.autonomy.hint.${id}`;
}

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

/** What the chain at the top of the page needs of D: whether it lives, in which
 * state, and whether that was read lately. Nothing else crosses to the page. */
export interface AeriFact {
  alive: boolean;
  state: StateId;
  current: boolean;
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

/** The prompt each key answers (the kinds of `state.prompt.kind`; design
 * section 4.1). The backend offers a key only for its own prompt; the page
 * checks it again at the moment of the click, so that a window opened for one
 * prompt can never send its key to another one. */
export const KEY_PROMPTS: Record<KeyId, readonly string[]> = {
  execute: ["phrase"],
  execute_variant: ["phrase_variant"],
  inventory_yes: ["inventory"],
  inventory_no: ["inventory"],
  home_o: ["home_o"],
  home_p: ["home_p"],
  home_h: ["home_h"],
  release_round: ["release"],
  verdict_success: ["round"],
  verdict_failure: ["round"],
  verdict_discard: ["round"],
  resume_ask: ["resume"],
  resume_confirm: ["confirm_resume"],
  resume_cancel: ["confirm_resume"],
  quit: [
    "inventory",
    "home_o",
    "home_p",
    "home_h",
    "release",
    "resume",
    "confirm_resume",
  ],
};

/** The prompt D shows now is the one this key answers. */
export function keyFitsPrompt(run: RunView, id: KeyId): boolean {
  const prompt = run.state.prompt;
  return prompt !== null && KEY_PROMPTS[id].includes(prompt.kind);
}

/** The checks that stand in front of a level C key, as separate ticks. */
export interface KeyCheck {
  /** The tick's code (only the page's own; none of them is sent). */
  code: string;
  /** Where the sentence comes from: the terminal's own prompt text, or one of
   * the three checks every window of the arm stack asks. */
  from: "terminal" | "window";
  /** The catalogue key of the sentence. */
  label: string;
}

/** What terminal D itself asks a person to check before it takes the phrase,
 * one tick per thing it names (the profile's `console_text`; a test reads the
 * words of the profile and fails when one of these is no longer in them).
 *
 * `run_confirm_warning` (printed before every start): "Confirm the reviewed
 * scene, limits, checkpoints, active providers and hardware readiness."
 *
 * `swap_unclean_head` (printed first when an earlier policy switch did not end
 * cleanly, which is what turns the phrase into the long one): "check each unit
 * (systemctl --user status <unit>), the policy ports (ss -ltnp), nvidia-smi and
 * that the arm is at its home pose". */
export const TERMINAL_CHECKS = {
  start: ["scene", "limits", "checkpoints", "providers", "hardware"],
  switch: ["switch_units", "switch_ports", "switch_gpu", "switch_home"],
  /** `sub_release` (printed at every release prompt): "Round {n}/{total}: when
   * the scene is ready press Enter to release this round's forward subtask
   * loop". D asks for one thing, the scene, so there is one tick; its words
   * also name the area and the e-stop because that is what a ready scene is
   * (review N5: the release window had a delay and no check at all). */
  release: ["scene_ready"],
} as const;

/** The three checks of the windows of the arm stack (start of C): a hand on
 * the physical e-stop, hands and objects clear, no evaluation client. */
export const WINDOW_CHECKS = [
  "estop_hand",
  "hands_clear",
  "no_client",
] as const;

const terminalCheck = (code: string): KeyCheck => ({
  code,
  from: "terminal",
  label: `marvel0.autonomy.dcheck.${code}`,
});
const windowCheck = (code: string): KeyCheck => ({
  code,
  from: "window",
  label: `marvel0.ctl.attest.${code}`,
});

/** The ticks of the window of a key: none for the keys below level C (they have
 * their own sentences and the delay) except the release of a round, which has
 * the one thing D itself asks there (the scene is ready); and for each level C
 * key everything the terminal asks, in the order it asks it, then the three
 * checks of the stack windows. `end` is the third level C action: the terminal
 * asks nothing there, so it has the three alone. The codes are also what the
 * backend is sent as `attest` (`attestOf`), so they are stable. */
export function keyChecks(id: KeyId | "end"): KeyCheck[] {
  if (id === "release_round") return TERMINAL_CHECKS.release.map(terminalCheck);
  const terminal: readonly string[] =
    id === "execute"
      ? TERMINAL_CHECKS.start
      : id === "execute_variant"
        ? [...TERMINAL_CHECKS.switch, ...TERMINAL_CHECKS.start]
        : [];
  if (id !== "execute" && id !== "execute_variant" && id !== "end") return [];
  return [...terminal.map(terminalCheck), ...WINDOW_CHECKS.map(windowCheck)];
}

/** The codes of the ticks a key's window requires, in order: what the backend
 * is sent as `attest` (empty for a key with no ticks). */
export const requiredAttest = (id: KeyId | "end"): string[] =>
  keyChecks(id).map((check) => check.code);

/** The `attest` of a key or end request from the ticks a window had set: all of
 * the key's required codes, in order, or null when one is missing. Never a
 * partial list. */
export function attestOf(
  id: KeyId | "end",
  ticked: Record<string, boolean>,
): string[] | null {
  const required = requiredAttest(id);
  return required.every((code) => ticked[code] === true) ? required : null;
}

/** Whether `given` is exactly the key's required codes (the second layer of
 * `pressKey` and `end`: a request is never built with fewer). */
export const attestComplete = (
  id: KeyId | "end",
  given: readonly string[] | undefined,
): boolean => {
  const required = requiredAttest(id);
  return (
    required.length === (given?.length ?? 0) &&
    required.every((code, index) => given?.[index] === code)
  );
};

/** The lines D printed above the prompt it waits on, verbatim: its screen's
 * last lines, trailing blanks dropped. The document has no field for the
 * reviewed summary or for the logs of an earlier policy switch that did not end
 * cleanly (D prints both just above the phrase prompt), so a window that asks
 * for the phrase quotes the screen itself instead of picking lines out of it
 * (review N5). */
export const CONTEXT_LINES = 30;
export function promptContext(run: RunView, max = CONTEXT_LINES): string[] {
  const tail = [...run.screen.tail];
  while (tail.length > 0 && tail[tail.length - 1].trim() === "") tail.pop();
  // The prompt's own line is quoted apart, right under this block.
  const own = run.state.prompt?.line?.trim();
  if (own && tail.length > 0 && tail[tail.length - 1].trim() === own)
    tail.pop();
  return tail.slice(-max);
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
  // Hosting switched off in the control file while D runs: a key that moves
  // the arm stays off whatever the key says (the backend refuses it too, with
  // `aeri_disabled`; this is the page's own second layer, review N3). Stop and
  // End are not keys; a key that moves nothing is left to the backend.
  if (!run.enabled && key.motion !== "none") return off(local("switched_off"));
  // The backend offers a key for its own prompt only; a document that offers
  // one for another prompt than the one D shows is not trusted (a window open
  // for one prompt must never send its key to the next).
  if (!keyFitsPrompt(run, key.id)) return off(local("prompt_mismatch"));
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

// -- Stop

/** What the last good read said about Stop, kept however old it is: Stop does
 * not wait for a fresh answer (a D that is not read is the case in which it
 * matters most). */
export interface StopSeen {
  runId: string | null;
  alive: boolean;
  allowed: boolean;
  phase: StopPhase;
  /** The backend's code for why no Stop is offered (null when one is). */
  whyCode: string | null;
}
export const stopSeen = (run: RunView): StopSeen => ({
  runId: run.runId,
  alive: run.d.alive,
  allowed: run.stop.allowed,
  phase: run.stop.phase,
  whyCode: run.stop.whyCode,
});

/** A Stop this page sent: it stays off until the document shows the stop phase
 * moving on (or the run is another one). */
export interface StopSent {
  runId: string | null;
  phase: StopPhase;
}

/** What the Stop frame is in:
 *  - `ready`: a Stop is offered and none was sent;
 *  - `sent`, `acked`, `retried` (`spent`): what D has said back to a Stop that
 *    was sent, by whom ever (the page's own click, the adoption of a run after
 *    a restart, or `aeri_page_stop.py`);
 *  - `retry`: D did not answer and the backend offers one more;
 *  - `unoffered`: D did not answer and the backend offers no more (an adopted
 *    D whose retry is not open, a run another core owns);
 *  - `off`: no Stop is offered and none was sent (a D locked after a fault, a
 *    run another core owns, a site file the backend cannot read). */
export type StopMode =
  | "hidden"
  | "ready"
  | "sent"
  | "acked"
  | "retry"
  | "spent"
  | "unoffered"
  | "off";

/** The Stop frame: shown while D lives; clickable only while the backend
 * offers it and this page has not already sent it for that phase. The mode is
 * derived from the stop phase AND from whether the backend offers a Stop now
 * (review N2: a phase alone said "one more try is offered" next to a disabled
 * button). The page cannot send a second Stop of its own accord: a run gets one
 * (and one retry the backend opens when the first one was never acknowledged). */
export function stopControl(
  seen: StopSeen | null,
  sent: StopSent | null,
): { mode: StopMode; enabled: boolean } {
  if (!seen || !seen.alive) return { mode: "hidden", enabled: false };
  const lockedHere =
    sent !== null && sent.runId === seen.runId && sent.phase === seen.phase;
  const mode: StopMode =
    seen.phase === "idle"
      ? seen.allowed || lockedHere
        ? "ready"
        : "off"
      : seen.phase === "unconfirmed"
        ? seen.allowed
          ? "retry"
          : "unoffered"
        : seen.phase === "retried"
          ? "spent"
          : seen.phase === "acked"
            ? "acked"
            : "sent";
  return { mode, enabled: seen.allowed && !lockedHere };
}

/** The sentence for why no Stop is offered, for the codes whose meaning on the
 * Stop frame is not the meaning they have for a start (`driven_elsewhere` there
 * is "another terminal drives this run name"). Null for the others: those are
 * worded from the document itself. */
const STOP_WHY = new Set(["stop_spent", "driven_elsewhere", "d_gone"]);
export const stopWhyKey = (code: string | null): string | null =>
  code !== null && STOP_WHY.has(code)
    ? `marvel0.autonomy.stop.why.${code}`
    : null;

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
