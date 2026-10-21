// The autonomy system's documents (Step 5 of the marvel0 page): what the
// backend says about the readiness of an AERI run (`GET .../aeri`,
// `levi.marvel0.aeri.v1`) and about the terminal D this page hosts
// (`GET .../aeri/run`, `levi.marvel0.aeri_run.v1`). The shapes follow
// programs/rig-console/reviews/aeri-page-design-20261021.md section 8; the
// sets named here are the closed enumerations the backend and the page agree
// on (the backend's `aeri_codes.json` and its samples are checked against them
// by the contract tests). Pure: nothing here reads the DOM or fetches.
//
// What the page never does with a document: guess. An id outside the closed
// sets is dropped (a state falls back to `unknown`, a key is not offered), a
// value of the wrong type is null, and a document of another schema is not a
// document at all.
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

export const RUN_SCHEMA = "levi.marvel0.aeri_run.v1";
export const READY_SCHEMA = "levi.marvel0.aeri.v1";

// -- the closed sets

export const STATE_IDS = [
  "idle",
  "launching",
  "await_phrase",
  "await_phrase_variant",
  "services",
  "await_home_o",
  "await_home_p",
  "await_home_h",
  "await_inventory",
  "await_release",
  "forward_running",
  "closing",
  "reverse_running",
  "await_resume",
  "await_resume_confirm",
  "busy",
  "stopping",
  "stalled",
  "fault_locked",
  "orphaned",
  "unknown",
  "ended",
] as const;
export type StateId = (typeof STATE_IDS)[number];

/** The keys the page may offer: the actions of design section 4.1 (Stop and
 * End have their own fields in the document). Nothing else is ever a button. */
export const KEY_IDS = [
  "execute",
  "execute_variant",
  "inventory_yes",
  "inventory_no",
  "home_o",
  "home_p",
  "home_h",
  "release_round",
  "verdict_success",
  "verdict_failure",
  "verdict_discard",
  "resume_ask",
  "resume_confirm",
  "resume_cancel",
  "quit",
] as const;
export type KeyId = (typeof KEY_IDS)[number];

export const KEY_LEVELS = ["A", "B", "C"] as const;
export type KeyLevel = (typeof KEY_LEVELS)[number];
export const KEY_MOTIONS = ["none", "arm_moves", "may_move"] as const;
export type KeyMotion = (typeof KEY_MOTIONS)[number];
export const KEY_SLOTS = ["first", "second"] as const;
export type KeySlot = (typeof KEY_SLOTS)[number];
export const KEY_VARIANTS = ["confirm_reason", "direct"] as const;
export type KeyVariant = (typeof KEY_VARIANTS)[number];

export const STOP_PHASES = [
  "idle",
  "sent",
  "acked",
  "unconfirmed",
  "retried",
] as const;
export type StopPhase = (typeof STOP_PHASES)[number];

export const NOTICE_CODES = [
  "not_estop",
  "inbox_resume",
  "stop_unconfirmed",
  "presence_lost",
  "runner_stale",
  "screen_drift",
  "key_unconfirmed",
] as const;
export type NoticeCode = (typeof NOTICE_CODES)[number];

export const READY_LEVELS = ["ok", "warn", "refuse", "unknown"] as const;
export type ReadyLevel = (typeof READY_LEVELS)[number];

/** The passive readiness rows (design section 7). A row is worded by
 * `marvel0.autonomy.ready.<code>`; a code outside this list is shown with the
 * backend's own sentence, marked as English. */
export const READY_CODES = [
  "aeri_enabled",
  "product",
  "aeri_site",
  "code_pin",
  "profile_known",
  "prompts_checked",
  "templates_sha",
  "prepared",
  "run_name_free",
  "cameras_config",
  "gpu_free",
  "gpu_lock_free",
  "mem_available",
  "ports_free",
  "live_service",
  "arm_lock_free",
  "no_e_run",
  "no_policy_deploy",
  "no_aeri_unit_left",
  "swap_logs_clean",
  "no_other_aeri",
  "monitor_green",
  "stack_ready",
] as const;

export const MODES = ["external", "local"] as const;
export type Mode = (typeof MODES)[number];

/** The key of each button as the terminal knows it, as the page words it: the
 * byte badge is what the backend sends in `keys[].byte`; a document whose byte
 * differs from this table is not offered (the label and the key must agree). */
export const KEY_BYTE: Record<KeyId, string> = {
  execute: "execute",
  execute_variant: "execute",
  inventory_yes: "y",
  inventory_no: "n",
  home_o: "o",
  home_p: "p",
  home_h: "h",
  release_round: "Enter",
  verdict_success: "s",
  verdict_failure: "f",
  verdict_discard: "d",
  resume_ask: "Enter",
  resume_confirm: "y",
  resume_cancel: "n",
  quit: "q",
};

/** The top-level fields of a run document; the closed set the backend sample
 * is compared with. */
export const RUN_FIELDS = [
  "schema",
  "sampled_at",
  "enabled",
  "run_id",
  "name",
  "mode",
  "task",
  "steps",
  "profile",
  "state",
  "agree",
  "progress",
  "inventory",
  "wait",
  "keys",
  "start",
  "stop",
  "presence",
  "end",
  "monitor",
  "d",
  "screen",
  "queue",
  "locks",
  "history",
  "fallback",
  "after_exit",
  "notices",
] as const;
export const READY_FIELDS = [
  "schema",
  "sampled_at",
  "enabled",
  "control",
  "site",
  "profile",
  "profiles",
  "limits",
  "readiness",
  "ready",
  "readiness_sha256",
  "prepared",
  "live_service",
  "leftovers",
  "confirm",
  "notices",
] as const;

// -- the run document

export interface Prompt {
  id: string;
  kind: string;
  n: number | null;
  total: number | null;
  /** The line D printed, quoted verbatim by the backend. */
  line: string | null;
}

export interface KeyView {
  id: KeyId;
  /** The byte the backend sends, as it names it (`Enter`, `y`, ...). */
  byte: string;
  level: KeyLevel;
  motion: KeyMotion;
  slot: KeySlot;
  variant: KeyVariant | null;
  allowed: boolean;
  whyNot: Worded<Say> | null;
  /** The backend's code of `whyNot` (`state_not_allowed`, `key_sent`, ...). */
  whyCode: string | null;
  /** One-time token of the key for the state it was read in. */
  token: string | null;
  /** Epoch ms before which the backend refuses the key; null: no delay. */
  notBefore: number | null;
  /** The phrase of a level C key, when the document names it. */
  phrase: string | null;
}

export interface InventoryItem {
  id: string;
  colour: string | null;
  kind: string | null;
  on: string | null;
  graspable: boolean | null;
}

export interface WaitView {
  reason: string | null;
  /** The wait needs a second confirmation before D resumes. */
  confirm: boolean;
  restoreScene: boolean;
  /** D's own words for what it waits for and what to do (the console
   * language it was started in). */
  text: string | null;
  todo: string | null;
  recent: Array<{ code: string; detail: string | null }>;
}

export interface HistoryRow {
  /** Epoch ms; null when the backend's time did not parse. */
  at: number | null;
  rawTime: string | null;
  key: string;
  byte: string | null;
  state: string | null;
  round: number | null;
}

export interface RunProfile {
  id: string;
  startHome: boolean;
  consoleLang: string | null;
  codeSha: string | null;
}

export interface RunView {
  /** The control file lets the page host D. */
  enabled: boolean;
  runId: string | null;
  name: string | null;
  mode: Mode | null;
  task: string | null;
  steps: number | null;
  profile: RunProfile | null;
  state: {
    id: StateId;
    /** The backend said an id this page does not know: shown as `unknown`. */
    foreign: boolean;
    reason: Worded<Say> | null;
    /** The backend's code of the reason (`no_keyboard`, `screen_drift`...). */
    reasonCode: string | null;
    seq: number;
    since: number | null;
    mayMove: boolean;
    settled: boolean;
    prompt: Prompt | null;
  };
  agree: { journal: string; screen: string; termios: string } | null;
  progress: {
    round: number | null;
    rounds: number | null;
    /** Rounds finished so far. */
    done: number | null;
    released: number | null;
    verdictLatched: boolean;
    discarded: number | null;
    /** The forward subtask's step and its cap; null outside the forward loop
     * (and in profiles whose D prints no such line). */
    step: number | null;
    maxSteps: number | null;
  } | null;
  inventory: { confirmed: boolean; items: InventoryItem[] } | null;
  wait: WaitView | null;
  keys: KeyView[];
  /** Whether a start is offered now (idle or ended; the readiness rows of the
   * other document are separate), and the phrase of the start. */
  start: {
    allowed: boolean;
    whyNot: Worded<Say> | null;
    attest: string[];
    phrase: string;
  };
  stop: {
    allowed: boolean;
    phase: StopPhase;
    sentAt: number | null;
    whyNot: Worded<Say> | null;
  };
  presence: { ok: boolean; ageS: number | null; needS: number | null } | null;
  end: { allowed: boolean; whyNot: Worded<Say> | null; phrase: string };
  monitor: { state: string; ageS: number | null } | null;
  d: {
    alive: boolean;
    pid: number | null;
    keyboard: boolean;
    runnerState: string | null;
    runnerAgeS: number | null;
    exit: number | null;
    exitSignal: number | null;
  };
  screen: { tail: string[]; bytes: number | null; drift: boolean };
  queue: { pending: number; oldestAgeS: number | null } | null;
  locks: {
    arm: string | null;
    gpu: string | null;
    ports: Array<{ port: string; who: string }>;
  } | null;
  history: HistoryRow[];
  stopCli: string | null;
  afterExit: {
    units: string[] | null;
    ports: Array<{ port: string; who: string }> | null;
    swapClean: boolean | null;
  } | null;
  notices: Array<{ code: string; level: string }>;
}

const oneOf = <T extends string>(
  set: readonly T[],
  value: unknown,
): T | null => (set.includes(value as T) ? (value as T) : null);

/** A time as epoch ms (the backend writes ISO 8601 with an offset). */
export function timeOf(value: unknown): number | null {
  const text = str(value, 64);
  if (!text) return null;
  const ms = Date.parse(text);
  return Number.isFinite(ms) ? ms : null;
}

/** Why a key is off or a reason in the backend's words: a bare code, or
 * `{code, text, params}`. */
export function whyOf(
  raw: unknown,
  prefixes: readonly string[],
  known: Known,
): Worded<Say> | null {
  if (typeof raw === "string") {
    const code = str(raw, 80);
    return code ? codedSay(prefixes, code, code, {}, known) : null;
  }
  const item = rec(raw);
  const code = str(item?.code, 80);
  if (!item || !code) return null;
  return codedSay(
    prefixes,
    code,
    str(item.text, 300) ?? code,
    paramsOf(item.params),
    known,
  );
}

/** The code of a reason: the bare string or the `code` of an object. */
export function whyCodeOf(raw: unknown): string | null {
  return typeof raw === "string" ? str(raw, 80) : str(rec(raw)?.code, 80);
}

/** The sentences of the reasons a key is not offered and of the refusals of a
 * write: this step's own first, then the evaluation client's (the checks the
 * two share: request ids, one-time tokens, the health monitor). */
export const ERR_PREFIXES = [
  "marvel0.autonomy.err.",
  "marvel0.run.refuse.",
  "marvel0.run.why.",
] as const;
export const REASON_PREFIXES = ["marvel0.autonomy.reason."] as const;

function keyView(raw: unknown, known: Known): KeyView | null {
  const item = rec(raw);
  if (!item) return null;
  const id = oneOf(KEY_IDS, item.id);
  const level = oneOf(KEY_LEVELS, item.level);
  const motion = oneOf(KEY_MOTIONS, item.motion);
  const slot = oneOf(KEY_SLOTS, item.slot);
  const byte = str(item.byte, 20);
  // A key outside the closed sets, or one whose byte is not the one this page
  // labels it with, is not a button: the label and the key must agree.
  if (!id || !level || !motion || !slot || !byte || byte !== KEY_BYTE[id])
    return null;
  const allowed = bool(item.allowed) === true;
  return {
    id,
    byte,
    level,
    motion,
    slot,
    variant: oneOf(KEY_VARIANTS, item.variant),
    allowed,
    whyNot: whyOf(item.why_not, ERR_PREFIXES, known),
    whyCode: whyCodeOf(item.why_not),
    token: str(item.token, 120),
    notBefore: timeOf(item.not_before),
    phrase: str(item.phrase, 200),
  };
}

function portRows(raw: unknown): Array<{ port: string; who: string }> {
  return Object.entries(rec(raw) ?? {}).flatMap(([port, who]) => {
    const text = str(who, 60);
    return text ? [{ port: port.slice(0, 8), who: text }] : [];
  });
}

function historyRow(raw: unknown): HistoryRow[] {
  const item = rec(raw);
  const key = str(item?.key, 60);
  if (!item || !key) return [];
  return [
    {
      at: timeOf(item.time),
      rawTime: str(item.time, 64),
      key,
      byte: str(item.byte, 20),
      state: str(item.state, 60),
      round: num(item.round),
    },
  ];
}

function inventoryItem(raw: unknown): InventoryItem[] {
  const item = rec(raw);
  const id = str(item?.id, 80);
  if (!item || !id) return [];
  return [
    {
      id,
      colour: str(item.colour, 40),
      kind: str(item.kind, 60),
      on: str(item.on, 80),
      graspable: bool(item.graspable),
    },
  ];
}

/** A text with words in it, not a bare code name (`snake_case`). */
function sentenceOf(value: unknown): string | null {
  const text = str(value, 600);
  return text !== null && !/^[a-z0-9_]+$/.test(text) ? text : null;
}

function waitView(raw: unknown): WaitView | null {
  const item = rec(raw);
  if (!item) return null;
  return {
    reason: str(item.reason, 80),
    confirm: bool(item.confirm) === true,
    restoreScene: bool(item.restore_scene) === true,
    text: str(item.text, 400),
    // `todo` is a sentence of D's console, or the name of one (a code such as
    // `todo_stop_resume`): a bare name is not shown as if it were words.
    todo: sentenceOf(item.todo),
    recent: list(item.recent).flatMap((entry) => {
      const row = rec(entry);
      const code = str(row?.code, 80);
      return code ? [{ code, detail: str(row?.detail, 300) }] : [];
    }),
  };
}

/** A reason that arrives as a bare code with its facts in a sibling object
 * (`state.reason` + `state.params`). */
function reasonOf(
  code: unknown,
  params: unknown,
  prefixes: readonly string[],
  known: Known,
): Worded<Say> | null {
  const text = str(code, 80);
  return text ? codedSay(prefixes, text, text, paramsOf(params), known) : null;
}

/** The run document, or null when the answer is not one in the format this
 * page was written for. A document with no run (`run_id` null, state `idle`)
 * is valid: it says there is nothing hosted. */
export function runView(raw: unknown, known: Known): RunView | null {
  const doc = rec(raw);
  if (!doc || doc.schema !== RUN_SCHEMA) return null;
  const state = rec(doc.state);
  const stateId = oneOf(STATE_IDS, state?.id);
  const promptRaw = rec(state?.prompt);
  const promptId = str(promptRaw?.id, 120);
  const profileRaw = rec(doc.profile);
  const profileId = str(profileRaw?.id, 80);
  const progress = rec(doc.progress);
  const inventory = rec(doc.inventory);
  const start = rec(doc.start);
  const stop = rec(doc.stop);
  const presence = rec(doc.presence);
  const end = rec(doc.end);
  const monitor = rec(doc.monitor);
  const d = rec(doc.d);
  const screen = rec(doc.screen);
  const queue = rec(doc.queue);
  const locks = rec(doc.locks);
  const after = rec(doc.after_exit);
  const exitRaw = d?.exit;
  const agree = rec(doc.agree);
  return {
    enabled: bool(doc.enabled) !== false,
    runId: str(doc.run_id, 120),
    name: str(doc.name, 120),
    mode: oneOf(MODES, doc.mode),
    task: str(doc.task, 200),
    steps: num(doc.steps),
    profile: profileId
      ? {
          id: profileId,
          startHome: bool(profileRaw?.start_home) === true,
          consoleLang: str(profileRaw?.console_lang, 8),
          codeSha: str(profileRaw?.code_sha, 40),
        }
      : null,
    state: {
      id: stateId ?? "unknown",
      foreign: stateId === null,
      reason: reasonOf(state?.reason, state?.params, REASON_PREFIXES, known),
      reasonCode: str(state?.reason, 80),
      seq: num(state?.seq) ?? -1,
      since: timeOf(state?.since),
      mayMove: bool(state?.may_move) === true,
      settled: bool(state?.settled) === true,
      prompt: promptId
        ? {
            id: promptId,
            kind: str(promptRaw?.kind, 40) ?? "",
            n: num(promptRaw?.n),
            total: num(promptRaw?.total),
            line: str(promptRaw?.line, 600),
          }
        : null,
    },
    agree: agree
      ? {
          journal: str(agree.journal, 20) ?? "",
          screen: str(agree.screen, 20) ?? "",
          termios: str(agree.termios, 20) ?? "",
        }
      : null,
    progress: progress
      ? {
          round: num(progress.round),
          rounds: num(progress.rounds),
          done: num(progress.done),
          released: num(progress.released),
          verdictLatched: bool(progress.verdict_latched) === true,
          discarded: num(progress.discarded),
          step: num(progress.step),
          maxSteps: num(progress.max_steps),
        }
      : null,
    inventory: inventory
      ? {
          confirmed: bool(inventory.confirmed) === true,
          items: list(inventory.items).flatMap(inventoryItem),
        }
      : null,
    wait: waitView(doc.wait),
    keys: list(doc.keys).flatMap((entry) => {
      const key = keyView(entry, known);
      return key ? [key] : [];
    }),
    start: {
      allowed: bool(start?.allowed) === true,
      whyNot: whyOf(start?.why_not, ERR_PREFIXES, known),
      attest: list(start?.attest).flatMap((code) => {
        const text = str(code, 40);
        return text ? [text] : [];
      }),
      phrase: str(start?.phrase, 120) ?? "start aeri {name}",
    },
    stop: {
      allowed: bool(stop?.allowed) === true,
      phase: oneOf(STOP_PHASES, stop?.phase) ?? "idle",
      sentAt: timeOf(stop?.sent_at),
      whyNot: whyOf(stop?.why_not, ERR_PREFIXES, known),
    },
    presence: presence
      ? {
          ok: bool(presence.ok) === true,
          ageS: num(presence.age_s),
          needS: num(presence.need_s),
        }
      : null,
    end: {
      allowed: bool(end?.allowed) === true,
      whyNot: whyOf(end?.why_not, ERR_PREFIXES, known),
      phrase: str(end?.phrase, 80) ?? "end terminal D",
    },
    monitor: monitor
      ? {
          state: str(monitor.state, 20) ?? "unknown",
          ageS: num(monitor.age_s),
        }
      : null,
    d: {
      alive: bool(d?.alive) === true,
      pid: num(d?.pid),
      keyboard: bool(d?.keyboard) === true,
      runnerState: str(rec(d?.runner)?.state, 20),
      runnerAgeS: num(rec(d?.runner)?.age_s),
      exit: num(exitRaw) ?? num(rec(exitRaw)?.code),
      exitSignal: num(rec(exitRaw)?.signal),
    },
    screen: {
      tail: list(screen?.tail)
        .flatMap((line) => (typeof line === "string" ? [line] : []))
        .slice(-60),
      bytes: num(screen?.bytes),
      drift: bool(screen?.drift) === true,
    },
    queue: queue
      ? {
          pending: Math.max(0, num(queue.pending) ?? 0),
          oldestAgeS: num(queue.oldest_age_s),
        }
      : null,
    locks: locks
      ? {
          arm: str(locks.arm, 40),
          gpu: str(locks.gpu, 40),
          ports: portRows(locks.ports),
        }
      : null,
    history: list(doc.history).flatMap(historyRow),
    stopCli: str(rec(doc.fallback)?.stop_cli, 200),
    afterExit: after
      ? {
          units: Array.isArray(after.units)
            ? after.units.flatMap((unit) => {
                const text = str(unit, 120);
                return text ? [text] : [];
              })
            : null,
          ports: rec(after.ports) ? portRows(after.ports) : null,
          swapClean: bool(after.swap_clean),
        }
      : null,
    notices: list(doc.notices).flatMap((entry) => {
      const code = str(rec(entry)?.code ?? entry, 60);
      return code
        ? [{ code, level: str(rec(entry)?.level, 20) ?? "info" }]
        : [];
    }),
  };
}

// -- the readiness document

export interface ReadyRow {
  code: string;
  level: ReadyLevel;
  /** The row in the page's words, or the backend's sentence when the page
   * has none. */
  worded: Worded<Say>;
  params: Record<string, string | number>;
}

export interface PreparedView {
  name: string;
  mode: Mode | null;
  task: string | null;
  rounds: number | null;
  steps: number | null;
  gripperConfirmS: number | null;
  startHome: boolean;
  profile: string | null;
  planSha: string | null;
  bindingSha: string | null;
  /** The files written at prepare still match what was recorded. */
  intact: boolean;
  /** Codes of what changed since (`files_changed`, `code_changed`, ...). */
  problems: string[];
  /** The command of the terminal fallback; the hosted D runs the same one. */
  execute: string | null;
}

/** The limits the backend holds for the form (sanity checks on the page; the
 * backend refuses anything outside them again). */
export interface FormLimits {
  rounds: { min: number; max: number; default: number };
  steps: { min: number; max: number; default: number };
  gripper: { min: number; max: number; default: number };
  taskPattern: string;
}

export interface ReadyView {
  enabled: boolean;
  /** `[aeri]` is missing from the control file or malformed (the codes of
   * its errors), else empty. */
  controlState: string;
  controlErrors: string[];
  /** The site file has a valid `[aeri]` section. */
  site: {
    state: string;
    errors: Array<{ field: string | null; code: string }>;
  };
  profile: {
    id: string;
    known: boolean;
    startHome: boolean;
    head: string | null;
    clean: boolean | null;
  } | null;
  limits: FormLimits | null;
  rows: ReadyRow[];
  ready: boolean;
  prepared: PreparedView | null;
  liveService: {
    state: string;
    port: number | null;
    canStop: boolean;
    whyNot: Worded<Say> | null;
  } | null;
  leftovers: {
    state: string;
    units: Array<{ name: string; kind: string | null }>;
    swapState: string | null;
    canStop: boolean;
    whyNot: Worded<Say> | null;
  } | null;
  /** The phrases the page sends with its writes; the backend's own. */
  confirm: { prepare: string; liveStop: string; leftovers: string };
}

function preparedView(raw: unknown): PreparedView | null {
  const item = rec(raw);
  const name = str(item?.name, 120);
  if (!item || !name) return null;
  return {
    name,
    mode: oneOf(MODES, item.mode),
    task: str(item.task, 200),
    rounds: num(item.rounds),
    steps: num(item.steps),
    gripperConfirmS: num(item.gripper_confirm_s),
    startHome: bool(item.start_home) === true,
    profile: str(item.profile, 80),
    planSha: str(item.plan_sha256, 80),
    bindingSha: str(item.binding_sha256, 80),
    // A prepared run that does not say it is intact is not treated as intact.
    intact: bool(item.intact) === true,
    problems: list(item.problems).flatMap((code) => {
      const text = str(code, 60);
      return text ? [text] : [];
    }),
    execute: str(item.execute, 4000),
  };
}

function range(
  raw: unknown,
): { min: number; max: number; default: number } | null {
  const item = rec(raw);
  const min = num(item?.min);
  const max = num(item?.max);
  const dflt = num(item?.default);
  return min !== null && max !== null && dflt !== null
    ? { min, max, default: dflt }
    : null;
}

function formLimits(raw: unknown): FormLimits | null {
  const item = rec(raw);
  const rounds = range(item?.rounds);
  const steps = range(item?.steps);
  const gripper = range(item?.gripper_confirm_s);
  const pattern = str(rec(item?.task)?.pattern, 200);
  return rounds && steps && gripper && pattern
    ? { rounds, steps, gripper, taskPattern: pattern }
    : null;
}

function whyWithText(raw: unknown, known: Known): Worded<Say> | null {
  return whyOf(raw, ERR_PREFIXES, known);
}

/** The readiness document, or null when it is not in the format this page was
 * written for. A row with a level the page does not know counts as `unknown`
 * (which refuses, like a refusal). */
export function readyView(raw: unknown, known: Known): ReadyView | null {
  const doc = rec(raw);
  if (!doc || doc.schema !== READY_SCHEMA) return null;
  const control = rec(doc.control);
  const site = rec(doc.site);
  const profile = rec(doc.profile);
  const profileId = str(profile?.id, 80);
  const live = rec(doc.live_service);
  const left = rec(doc.leftovers);
  const confirm = rec(doc.confirm);
  return {
    enabled: bool(doc.enabled) === true,
    controlState: str(control?.state, 20) ?? "unknown",
    controlErrors: list(control?.errors).flatMap((entry) => {
      const text = str(rec(entry)?.code ?? entry, 80);
      return text ? [text] : [];
    }),
    site: {
      state: str(site?.state, 20) ?? "unknown",
      errors: list(site?.errors).flatMap((entry) => {
        const code = str(rec(entry)?.code ?? entry, 80);
        return code ? [{ field: str(rec(entry)?.field, 80), code }] : [];
      }),
    },
    profile: profileId
      ? {
          id: profileId,
          known: bool(profile?.known) === true,
          startHome: bool(profile?.start_home) === true,
          head: str(profile?.head, 40),
          clean: bool(profile?.clean),
        }
      : null,
    limits: formLimits(doc.limits),
    rows: list(doc.readiness).flatMap((entry): ReadyRow[] => {
      const item = rec(entry) as Rec | null;
      const code = str(item?.code, 80);
      if (!item || !code) return [];
      const params = paramsOf(item.params);
      return [
        {
          code,
          level: oneOf(READY_LEVELS, item.level) ?? "unknown",
          worded: codedSay(
            "marvel0.autonomy.ready.",
            code,
            str(item.text, 300) ?? code,
            params,
            known,
          ),
          params,
        },
      ];
    }),
    ready: bool(doc.ready) === true,
    prepared: preparedView(doc.prepared),
    liveService: live
      ? {
          state: str(live.state, 40) ?? "unknown",
          port: num(live.port),
          canStop: bool(live.can_stop) === true,
          whyNot: whyWithText(live.why_not, known),
        }
      : null,
    leftovers: left
      ? {
          state: str(left.state, 40) ?? "unknown",
          units: list(left.units).flatMap((entry) => {
            const name = str(rec(entry)?.name, 120);
            return name ? [{ name, kind: str(rec(entry)?.kind, 20) }] : [];
          }),
          swapState: str(rec(left.swap)?.state, 20),
          canStop: bool(left.can_stop) === true,
          whyNot: whyWithText(left.why_not, known),
        }
      : null,
    confirm: {
      prepare: str(confirm?.prepare, 80) ?? "prepare aeri run",
      liveStop: str(confirm?.live_stop, 80) ?? "stop live service",
      leftovers: str(confirm?.leftovers, 80) ?? "stop leftover aeri units",
    },
  };
}
