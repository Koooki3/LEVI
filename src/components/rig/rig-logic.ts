// Pure mapping for the Rig console (/rig): the two status answers
// (`GET /api/levi/setup/status` and `GET /api/levi/rig/status`) in, one view
// model per component out. No React and no fetching, so every rule can be
// tested on its own.
//
// Nothing here is a safety signal. A part that is missing, malformed or
// unreadable is "unknown" with a reason, never "ok" and never "not started":
// the page does not guess. "stale" (the evidence is old) is kept apart from
// "red" (the evidence says something is wrong). Text that came from a server
// is passed on as written, in English: a part's `detail` and the health
// file's reasons in a card's `notes`, and an idle reason whose `code` the
// page does not know. A reason whose code it knows is worded from the
// catalogue out of its `params`, and so is the note of a part the verdict
// names with such a code. The summary and the facts of a card are catalogue
// keys with their values (or identifiers such as a process or unit name), so
// the view model holds no language and a Chinese page prints no English
// sentence it can word itself.
import { ENDED_STATES } from "@/components/live/live-logic";

export type RigState = "ok" | "red" | "stale" | "not_started" | "unknown";
export type IdleState = "idle" | "busy" | "unknown";

/** Catalogue key of the badge words of each state. */
export const STATE_KEY: Record<RigState, string> = {
  ok: "rig.state.ok",
  red: "rig.state.red",
  stale: "rig.state.stale",
  not_started: "rig.state.not_started",
  unknown: "rig.state.unknown",
};
export const STATE_TONE: Record<
  RigState,
  "success" | "danger" | "warning" | "neutral"
> = {
  ok: "success",
  red: "danger",
  stale: "warning",
  not_started: "neutral",
  unknown: "neutral",
};

/** A piece of text for the page: a catalogue key with the values of its
 * `{placeholders}` (a value may be a piece of text itself), text exactly as
 * a server wrote it, an age in seconds (the page words it with `ago`; null
 * is a dash), or several pieces in a row. */
export type Say =
  | { key: string; params?: Record<string, string | number | Say> }
  | { raw: string }
  | { ago: number | null }
  | { join: Say[] };

const k = (key: string, params?: Record<string, string | number | Say>): Say =>
  params ? { key, params } : { key };
const raw = (text: string): Say => ({ raw: text });
const join = (...parts: Array<Say | null>): Say => ({
  join: parts.filter((part): part is Say => part !== null),
});

export interface RigFact {
  label: Say;
  value: Say;
}

/** A string is text as a server wrote it (English whatever the page's
 * language); a `Say` is the page's own words. */
export type RigNote = string | Say;

export type RigCardId =
  | "desk"
  | "arm"
  | "gripper"
  | "robot_server"
  | "monitor"
  | "policy"
  | "demo"
  | "live"
  | "vlm"
  | "gpu";

export interface RigCard {
  id: RigCardId;
  /** Catalogue key of the card's name. */
  title: string;
  state: RigState;
  /** One line: why the badge says what it says. */
  summary: Say;
  facts: RigFact[];
  /** Under the card: text the servers sent along (reasons, errors), as
   * written, and the page's own words for what it knows by a code. */
  notes: RigNote[];
  /** Catalogue key of the static hint: where the manual commands are. */
  hint: string;
}

export interface RigIdleReason {
  source: Say;
  state: Say;
  /** What the reason says, in the page's words, from the server's `code`
   * and `params`. Null when the page does not know the code, or the params
   * do not fit it: then `text` is shown. */
  say: Say | null;
  /** The server's own sentence, in English whatever the page's language. */
  text: string;
}

export interface RigBanner {
  state: IdleState;
  reasons: RigIdleReason[];
  /** Why there is no answer (the route failed, the answer is malformed). */
  note: Say | null;
}

export interface RigGroup {
  id: "robot" | "host";
  /** Catalogue key. */
  title: string;
  cards: RigCard[];
}

export interface RigView {
  banner: RigBanner;
  groups: RigGroup[];
  /** Page-level remarks: a request failed, a sample is old. */
  notes: Say[];
}

export interface RigInput {
  /** The answer of `GET /api/levi/setup/status`, null when there is none. */
  setup: unknown;
  /** The answer of `GET /api/levi/rig/status`, null when there is none. */
  rig: unknown;
  /** Text of the failure that left `setup` / `rig` empty. */
  setupError?: string;
  rigError?: string;
}

// -- polling (the hook in use-rig-poll.ts asks these)

export const RIG_POLL_MS = 3000;
export const RIG_POLL_MAX_MS = 30000;
/** An answer older than this is not shown as current: the cards go unknown. */
export const RIG_FRESH_MS = 10000;

/** Milliseconds until the next request: 3 s, doubling after each failure up
 * to 30 s. */
export function rigDelay(failures: number): number {
  if (failures <= 0) return RIG_POLL_MS;
  return Math.min(RIG_POLL_MAX_MS, RIG_POLL_MS * 2 ** Math.min(failures, 6));
}

/** A request that has not answered in this long is given up (the page asks
 * again at its normal pace), so one that never settles cannot hold the
 * polling. It is longer than the slowest part the backend waits for (6 s) and,
 * with the poll gap, fits inside `RIG_FRESH_MS`: an answer is dropped by the
 * failed round before its window ends. */
export const RIG_REQUEST_MS = 6500;

/** How old the sample behind an answer already was when the server sent it,
 * in seconds: the setup status' `age_s`. Zero when the answer does not say
 * (the rig status is taken on request and has none). */
export function sampleAgeS(answer: unknown): number {
  const age = num(rec(answer)?.age_s);
  return age !== null && age > 0 ? age : 0;
}

/** Whether an answer received at `at` (epoch ms) may still be shown. The
 * time its sample had already passed when it came (`ageS`) counts as well:
 * a cached sample that arrived 6 s old is 10 s old 4 s later. */
export function isFresh(
  at: number | null,
  now: number,
  ageS?: number | null,
): boolean {
  if (at === null) return false;
  const carried = typeof ageS === "number" && ageS > 0 ? ageS * 1000 : 0;
  return now - at + carried <= RIG_FRESH_MS;
}

// -- ports and sessions the cards look at

export const PORT_ROBOT_SERVER = 5000;
export const PORT_POLICY = 8000;
export const PORT_LIVE_CORE = 7881;
export const PORT_ONLINE_JUDGE = 7882;
export const PORT_VLLM = 8100;
export const PORT_VLM_ALT = 8110;

/** A session that updated within this many seconds is running now. */
export const RECENT_S = 60;
/** A marker this old or older may be a leftover of a crash: the server calls
 * it "cannot tell" and never idle (its rule, repeated here only to say so
 * next to the marker). */
export const MARKER_BUSY_S = 12 * 3600;
export const POLICY_UNIT_PREFIX = "levi-aeri-policy-";
/** The format of the rig status this page was written for. */
export const RIG_SCHEMA = "levi.rig.status.v1";
/** How many reasons of the idle verdict the banner lists. */
const MAX_REASONS = 12;

// -- reading loosely typed JSON

type Rec = Record<string, unknown>;

const rec = (value: unknown): Rec | null =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Rec)
    : null;
const str = (value: unknown, max = 300): string | null =>
  typeof value === "string" && value.trim() ? value.trim().slice(0, max) : null;
const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const bool = (value: unknown): boolean | null =>
  typeof value === "boolean" ? value : null;
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

/** The live page's own words for the health monitor's red light. */
const RED_LIGHT = "The FR3 health monitor reports the red light.";

const gib = (mib: number) => (mib / 1024).toFixed(1);
const yesNo = (value: boolean | null): Say =>
  k(
    value === null
      ? "rig.value.unknown"
      : value
        ? "rig.value.yes"
        : "rig.value.no",
  );

/** The server's own sentence about a part, for a card's notes: English
 * whatever the page's language, so it is never a summary or a fact. */
function detailOf(part: Rec | null): string[] {
  const detail = str(part?.detail);
  return detail ? [detail] : [];
}

/** The same for a part that could not be read: an "ok" part has nothing to
 * explain. */
function unreadNote(part: Rec | null): string[] {
  return part?.state === "ok" ? [] : detailOf(part);
}

/** The codes with which the idle verdict says a part could not be read. */
const UNREAD_CODES = new Map([
  ["marker", ["marker.unreadable", "marker.no_folder"]],
  ["eval_sessions", ["sessions.unreadable"]],
  ["units", ["units.unreadable"]],
]);

/** The note of a part that could not be read: the page's words when the idle
 * verdict names the part with a code the page knows, else the server's own
 * sentence (`server`). */
function unreadNotes(
  rig: Rec | null,
  source: string,
  server: string[],
): RigNote[] {
  const codes = UNREAD_CODES.get(source) ?? [];
  for (const entry of list(rec(rig?.robot_idle)?.reasons)) {
    const item = rec(entry);
    if (item?.source !== source) continue;
    if (typeof item.code !== "string" || !codes.includes(item.code)) continue;
    const say = reasonSay(item);
    if (say) return [say];
  }
  return server;
}

// -- the FR3 health file (setup status, part `fr3_health`)

type HealthState = "ok" | "red" | "offline" | "missing" | "unknown";
interface Health {
  state: HealthState;
  part: Rec | null;
}

function readHealth(setup: Rec | null): Health {
  const part = rec(setup?.fr3_health);
  const state = part?.state;
  if (
    state === "ok" ||
    state === "red" ||
    state === "offline" ||
    state === "missing"
  )
    return { state, part };
  return { state: "unknown", part };
}

function healthNotes(health: Health): string[] {
  const part = health.part;
  if (!part) return [];
  const found = [...list(part.reasons), ...list(part.current_errors)]
    .map((entry) => str(entry))
    .filter((entry): entry is string => entry !== null);
  return found.slice(0, 4);
}

/** The age of the health file, as a fact when it is known. */
function ageFact(part: Rec | null): RigFact[] {
  const age = num(part?.age_s);
  return age === null
    ? []
    : [{ label: k("rig.fact.last_update"), value: { ago: age } }];
}

function modeSay(part: Rec | null): Say {
  const name = str(part?.robot_mode_name, 40);
  const mode = num(part?.robot_mode);
  if (name) return raw(mode === null ? name : `${name} (${mode})`);
  return mode === null ? k("rig.value.unknown") : raw(String(mode));
}

/** What the health file cannot say: no file at all, or an unreadable part. */
function noHealth(
  id: RigCardId,
  title: string,
  hint: string,
  health: Health,
): RigCard {
  return {
    id,
    title,
    state: "unknown",
    summary:
      health.state === "missing"
        ? k("rig.health.missing_for_others")
        : k("rig.health.unreadable"),
    facts: [],
    notes: health.state === "missing" ? [] : detailOf(health.part),
    hint,
  };
}

function deskCard(health: Health): RigCard {
  const title = "rig.desk.title";
  const hint = "rig.desk.hint";
  if (health.state === "unknown" || health.state === "missing")
    return noHealth("desk", title, hint, health);
  const part = health.part;
  const hardware = bool(part?.hardware_active);
  const facts: RigFact[] = [
    { label: k("Robot mode"), value: modeSay(part) },
    {
      label: k("rig.fact.fci"),
      value:
        hardware === null
          ? k("rig.value.unknown")
          : k(hardware ? "rig.value.fci_active" : "rig.value.fci_inactive"),
    },
    ...ageFact(part),
  ];
  const notes = healthNotes(health);
  if (health.state === "offline")
    return {
      id: "desk",
      title,
      state: "stale",
      summary: k("rig.health.offline_for_others"),
      facts,
      // Why it is offline (stopped updating, cannot be read), then the last
      // reasons it wrote.
      notes: [...detailOf(part), ...notes],
      hint,
    };
  if (health.state === "red")
    return {
      id: "desk",
      title,
      state: "red",
      summary: k(RED_LIGHT),
      facts,
      notes,
      hint,
    };
  const state: RigState =
    hardware === null ? "unknown" : hardware ? "ok" : "not_started";
  return {
    id: "desk",
    title,
    state,
    summary: k(
      state === "ok"
        ? "rig.desk.ok"
        : state === "not_started"
          ? "rig.desk.inactive"
          : "rig.desk.no_field",
    ),
    facts,
    notes,
    hint,
  };
}

/** The health monitor's reasons that only say the arm stack is not running
 * (fr3_health_monitor.py): no hardware interface loaded, one that is not
 * active (`hardware_interface=<state>`), a controller manager that does not
 * answer, no robot state arriving. A reason is told by its exact name, which
 * may be followed by a space, a bracket or a parenthesis (the seconds or the
 * note the monitor appends); a name that only starts like one is another
 * reason. */
const STACK_DOWN_REASON =
  /^(?:hardware_interface=|(?:hardware_interface_missing|controller_manager_unreachable|robot_state_no_data)(?:$|[ (\[]))/;

/** Whether the red light follows from terminal A not running: it has reasons,
 * all of them of the kinds above, the robot reports no error of its own, and
 * the hardware interface is not known to be active (false or not said). With
 * the hardware up, "no robot state" or an unreachable controller manager is a
 * fault of the arm or its link, whatever the controller flag says. Anything
 * else is still a red light. */
function redFollowsFromStack(part: Rec | null): boolean {
  const reasons = list(part?.reasons).map((entry) => str(entry));
  if (reasons.length === 0 || list(part?.current_errors).length > 0)
    return false;
  const stackDown = reasons.every(
    (reason) => reason !== null && STACK_DOWN_REASON.test(reason),
  );
  return stackDown && bool(part?.hardware_active) !== true;
}

/** The marker the arm controller leaves while it runs (rig status). */
function markerFact(rig: Rec | null): RigFact {
  const label = k("rig.fact.marker");
  const marker = rec(rig?.marker);
  const state = marker?.state;
  if (state === "absent") return { label, value: k("rig.marker.absent") };
  if (state === "present") {
    const age = num(marker?.age_s);
    return {
      label,
      value: join(
        k("rig.marker.present"),
        age === null ? null : { ago: age },
        age !== null && age >= MARKER_BUSY_S ? k("rig.marker.old") : null,
      ),
    };
  }
  return {
    label,
    value: marker
      ? join(k("rig.value.unknown"), k("rig.marker.unreadable"))
      : k("rig.marker.no_answer"),
  };
}

/** The marker part's own sentence when it is neither absent nor present. */
function markerNotes(rig: Rec | null): RigNote[] {
  const marker = rec(rig?.marker);
  return marker?.state === "absent" || marker?.state === "present"
    ? []
    : unreadNotes(rig, "marker", detailOf(marker));
}

function armCard(health: Health, rig: Rec | null): RigCard {
  const title = "rig.arm.title";
  const hint = "rig.arm.hint";
  const marker = markerFact(rig);
  if (health.state === "unknown" || health.state === "missing") {
    const bare = noHealth("arm", title, hint, health);
    return {
      ...bare,
      facts: [marker],
      notes: [...bare.notes, ...markerNotes(rig)],
    };
  }
  const part = health.part;
  const hardware = bool(part?.hardware_active);
  const controller = bool(part?.controller_active);
  const facts: RigFact[] = [
    { label: k("rig.fact.hardware"), value: yesNo(hardware) },
    { label: k("rig.fact.controller"), value: yesNo(controller) },
    marker,
    ...ageFact(part),
  ];
  const notes = [...healthNotes(health), ...markerNotes(rig)];
  if (health.state === "offline")
    return {
      id: "arm",
      title,
      state: "stale",
      summary: k("rig.health.offline_for_others"),
      facts,
      notes: [...detailOf(part), ...notes],
      hint,
    };
  if (health.state === "red") {
    // Not a fault of the arm: the stack was never started (or has gone).
    if (redFollowsFromStack(part))
      return {
        id: "arm",
        title,
        state: "not_started",
        summary: k("rig.arm.red_follows"),
        facts,
        notes,
        hint: "rig.arm.hint_red_follows",
      };
    return {
      id: "arm",
      title,
      state: "red",
      summary: k(RED_LIGHT),
      facts,
      notes,
      hint,
    };
  }
  // The stack counts as started only when both are active.
  let state: RigState = "unknown";
  let summary = "rig.arm.no_field";
  if (hardware === false) {
    state = "not_started";
    summary = "rig.arm.hardware_inactive";
  } else if (hardware === true && controller === true) {
    state = "ok";
    summary = "rig.arm.ok";
  } else if (hardware === true && controller === false) {
    state = "not_started";
    summary = "rig.arm.controller_inactive";
  }
  return { id: "arm", title, state, summary: k(summary), facts, notes, hint };
}

function gripperCard(): RigCard {
  return {
    id: "gripper",
    title: "rig.gripper.title",
    state: "unknown",
    summary: k("rig.gripper.reason"),
    facts: [],
    notes: [],
    hint: "rig.gripper.hint",
  };
}

function monitorCard(health: Health): RigCard {
  const part = health.part;
  const base = {
    id: "monitor" as const,
    title: "rig.monitor.title",
    hint: "rig.monitor.hint",
    notes: healthNotes(health),
  };
  const facts: RigFact[] = [...ageFact(part)];
  const rate = num(part?.command_success_rate);
  if (rate !== null)
    facts.push({
      label: k("rig.fact.success_rate"),
      value: raw(`${Math.round(rate * 100)} %`),
    });
  switch (health.state) {
    case "ok":
      return { ...base, state: "ok", summary: k("rig.monitor.ok"), facts };
    case "red":
      return { ...base, state: "red", summary: k(RED_LIGHT), facts };
    case "offline":
      return {
        ...base,
        state: "stale",
        summary: k("rig.monitor.offline"),
        facts,
        notes: [...detailOf(part), ...base.notes],
      };
    case "missing":
      return {
        ...base,
        state: "not_started",
        summary: k("rig.monitor.missing"),
        facts,
      };
    default:
      return {
        ...base,
        state: "unknown",
        summary: k("rig.health.unreadable"),
        facts,
        notes: [...base.notes, ...detailOf(part)],
      };
  }
}

// -- ports (setup status, part `ports`)

interface PortReading {
  listening: boolean | null;
  process: string | null;
  /** Why `listening` is unknown. */
  why: Say | null;
  /** The table is readable but has no row for the port (the status does not
   * watch it, like the optional model port 8110). */
  notWatched?: boolean;
}

function readPort(setup: Rec | null, port: number): PortReading {
  const part = rec(setup?.ports);
  if (!part || part.state !== "ok")
    return {
      listening: null,
      process: null,
      why: k("rig.port.no_table"),
    };
  const row = list(part.ports)
    .map(rec)
    .find((entry) => entry !== null && entry.port === port);
  if (!row)
    return {
      listening: null,
      process: null,
      why: k("rig.port.not_watched"),
      notWatched: true,
    };
  const listening = bool(row.listening);
  if (listening === null)
    return { listening: null, process: null, why: k("rig.port.malformed") };
  return { listening, process: str(row.process, 60), why: null };
}

/** The server's sentence on why the port table cannot be read; every card
 * that shows a port as unknown carries it. */
function portsNotes(setup: Rec | null): string[] {
  return unreadNote(rec(setup?.ports));
}

function portFact(port: number, reading: PortReading): RigFact {
  const label = k("rig.fact.port", { port });
  if (reading.notWatched) return { label, value: k("rig.port.not_watched") };
  if (reading.listening === null)
    return {
      label,
      value: join(k("rig.value.unknown"), reading.why),
    };
  return {
    label,
    value: reading.listening
      ? join(
          k("rig.port.listening"),
          reading.process ? raw(reading.process) : null,
        )
      : k("rig.port.closed"),
  };
}

function robotServerCard(setup: Rec | null): RigCard {
  const reading = readPort(setup, PORT_ROBOT_SERVER);
  const state: RigState =
    reading.listening === null
      ? "unknown"
      : reading.listening
        ? "ok"
        : "not_started";
  return {
    id: "robot_server",
    title: "rig.server.title",
    state,
    summary:
      state === "unknown"
        ? (reading.why ?? k("rig.port.no_table"))
        : k(state === "ok" ? "rig.server.ok" : "rig.server.closed", {
            port: PORT_ROBOT_SERVER,
          }),
    facts: [portFact(PORT_ROBOT_SERVER, reading)],
    notes: portsNotes(setup),
    hint: "rig.server.hint",
  };
}

// -- units of the rig status

interface Unit {
  name: string;
  active: string | null;
  sub: string | null;
}

/** The `levi-aeri-*` units the rig status lists, or null when it does not
 * say (the answer is missing or its `units` part is not "ok"), with the
 * server's sentence on why. */
function readUnits(rig: Rec | null): {
  units: Unit[] | null;
  notes: RigNote[];
} {
  const part = rec(rig?.units);
  if (!part || part.state !== "ok")
    return { units: null, notes: unreadNotes(rig, "units", unreadNote(part)) };
  const units: Unit[] = [];
  for (const entry of list(part.items)) {
    const item = rec(entry);
    const name = str(item?.name, 120);
    if (!item || !name) continue;
    units.push({
      name,
      active: str(item.active, 30),
      sub: str(item.sub, 30),
    });
  }
  return { units, notes: [] };
}

const RUNNING_UNIT = ["active", "activating"];

/** "name: active (running)"; a unit that does not say its state is "name ·
 * unknown". */
function unitSay({ name, active, sub }: Unit): Say {
  if (active === null) return join(raw(name), k("rig.value.unknown"));
  return raw(sub ? `${name}: ${active} (${sub})` : `${name}: ${active}`);
}

function policyCard(setup: Rec | null, rig: Rec | null): RigCard {
  const reading = readPort(setup, PORT_POLICY);
  const { units, notes } = readUnits(rig);
  const policyUnits = (units ?? []).filter((unit) =>
    unit.name.startsWith(POLICY_UNIT_PREFIX),
  );
  const failed = policyUnits.filter((unit) => unit.active === "failed");
  const running = policyUnits.filter((unit) =>
    RUNNING_UNIT.includes(unit.active ?? ""),
  );
  const facts: RigFact[] = [portFact(PORT_POLICY, reading)];
  if (units === null)
    facts.push({
      label: k("rig.fact.units"),
      value: join(k("rig.value.unknown"), k("rig.units.no_answer")),
    });
  else if (policyUnits.length === 0)
    facts.push({ label: k("rig.fact.units"), value: k("rig.units.none") });
  else
    for (const unit of policyUnits)
      facts.push({ label: k("rig.fact.unit"), value: unitSay(unit) });
  let state: RigState;
  let summary: Say;
  if (reading.listening === true) {
    state = "ok";
    summary = k("rig.policy.ok", { port: PORT_POLICY });
  } else if (failed.length > 0) {
    state = "red";
    summary = k("rig.policy.failed");
  } else if (reading.listening === false) {
    state = "not_started";
    summary = k(
      running.length > 0 ? "rig.policy.loading" : "rig.policy.closed",
      { port: PORT_POLICY },
    );
  } else {
    // The port table cannot be read: a unit that runs does not show the
    // server answers, so the card does not say "ok".
    state = "unknown";
    summary = reading.why ?? k("rig.port.no_table");
  }
  return {
    id: "policy",
    title: "Policy server",
    state,
    summary,
    facts,
    notes: [...portsNotes(setup), ...notes],
    hint: "rig.policy.hint",
  };
}

// -- evaluation sessions of the demo client (rig status)

interface SessionLine {
  group: string;
  age: number;
  status: string | null;
}

function sessionLine(entry: unknown): SessionLine | null {
  const item = rec(entry);
  const group = str(item?.group, 80);
  const age = num(item?.age_s);
  if (!item || !group || age === null) return null;
  return { group, age: Math.max(0, age), status: str(item.status, 40) };
}

const FAULT_STATES = ["fault", "crashed"];
// A session with no state, or one this page does not know, is still updating:
// it counts as running.
const isRunning = (status: string | null) =>
  !ENDED_STATES.includes(status ?? "");
const isFault = (status: string | null) => FAULT_STATES.includes(status ?? "");

function demoCard(rig: Rec | null): RigCard {
  const base = {
    id: "demo" as const,
    title: "rig.demo.title",
    hint: "rig.demo.hint",
  };
  const part = rec(rig?.eval_sessions);
  const sessions = list(part?.sessions)
    .map(sessionLine)
    .filter((line): line is SessionLine => line !== null)
    .sort((a, b) => a.age - b.age);
  // A part that could not read everything still lists the sessions it found.
  const complete = part?.state === "ok";
  const notes: RigNote[] = complete
    ? detailOf(part)
    : unreadNotes(rig, "eval_sessions", detailOf(part));
  if (!part || (!complete && sessions.length === 0))
    return {
      ...base,
      state: "unknown",
      summary: k("rig.demo.no_answer"),
      facts: [],
      notes,
    };
  const facts: RigFact[] = sessions.slice(0, 6).map((line) => ({
    label: raw(line.group),
    value: join(line.status ? raw(line.status) : k("rig.value.unknown"), {
      ago: line.age,
    }),
  }));
  const roots = num(part.roots);
  if (roots !== null)
    facts.push({ label: k("rig.fact.roots"), value: raw(String(roots)) });
  const recent = sessions.filter((line) => line.age <= RECENT_S);
  // Newest first, so the first fault is the latest one.
  const faults = sessions.filter((line) => isFault(line.status));
  let state: RigState;
  let summary: Say;
  if (recent.some((line) => isFault(line.status))) {
    state = "red";
    summary = k("rig.demo.fault", { s: RECENT_S });
  } else if (recent.some((line) => isRunning(line.status))) {
    state = "ok";
    summary = k("rig.demo.ok", { s: RECENT_S });
  } else if (faults.length > 0) {
    // A fault does not turn into "went quiet" after a minute: nothing says
    // it was dealt with, so the card keeps saying that one happened.
    state = "red";
    summary = k("rig.demo.fault_earlier", { age: { ago: faults[0].age } });
  } else if (sessions.some((line) => isRunning(line.status))) {
    state = "stale";
    summary = k("rig.demo.silent", { s: RECENT_S });
  } else {
    state = "not_started";
    summary = k(sessions.length > 0 ? "rig.demo.ended" : "rig.demo.none");
  }
  // Sessions that ended do not show that nothing runs while the list may be
  // missing some.
  if (!complete && state === "not_started") {
    state = "unknown";
    summary = k("rig.demo.partial");
  }
  return { ...base, state, summary, facts, notes };
}

// -- live service and the model server (setup status)

function liveCard(setup: Rec | null): RigCard {
  const part = rec(setup?.live);
  const core = readPort(setup, PORT_LIVE_CORE);
  const judge = readPort(setup, PORT_ONLINE_JUDGE);
  const facts: RigFact[] = [];
  if (part?.state === "ok") {
    const service = str(part.service_state, 40);
    if (service)
      facts.push({ label: k("rig.fact.service"), value: raw(service) });
    const age = num(part.age_s);
    if (age !== null)
      facts.push({ label: k("rig.fact.last_update"), value: { ago: age } });
    const paused = bool(part.labelling_paused);
    if (paused !== null)
      facts.push({
        label: k("rig.fact.labelling_paused"),
        value: yesNo(paused),
      });
    const gate = rec(part.gate);
    const open = bool(gate?.open);
    const code = str(gate?.code, 60);
    if (open !== null)
      facts.push({
        label: k("rig.fact.gpu_gate"),
        value: join(
          k(open ? "rig.value.open" : "rig.value.closed"),
          // The code explains a closed gate; "open · open" says nothing.
          code && !open ? raw(code) : null,
        ),
      });
  }
  facts.push(
    portFact(PORT_LIVE_CORE, core),
    portFact(PORT_ONLINE_JUDGE, judge),
  );
  const notes: string[] = [];
  const gateReason = str(rec(part?.gate)?.reason, 200);
  if (gateReason) notes.push(gateReason);
  notes.push(...portsNotes(setup));
  const base = {
    id: "live" as const,
    title: "rig.live.title",
    hint: "rig.live.hint",
    facts,
    notes,
  };
  if (part?.state === "ok") {
    if (part.alive === true)
      return { ...base, state: "ok", summary: k("rig.live.ok") };
    if (str(part.service_state) === "stopped")
      return { ...base, state: "not_started", summary: k("rig.live.stopped") };
    return { ...base, state: "stale", summary: k("rig.live.silent") };
  }
  // No status file to read: the core's port is the only evidence left, and a
  // listening port with no readable status is not "ok".
  if (core.listening === false)
    return {
      ...base,
      state: "not_started",
      summary: k("rig.live.closed", { port: PORT_LIVE_CORE }),
    };
  return {
    ...base,
    state: "unknown",
    summary: k("rig.live.no_status"),
    notes: [...detailOf(part), ...notes],
  };
}

function vlmCard(setup: Rec | null): RigCard {
  const main = readPort(setup, PORT_VLLM);
  const alt = readPort(setup, PORT_VLM_ALT);
  const live = rec(setup?.live);
  const facts: RigFact[] = [
    portFact(PORT_VLLM, main),
    portFact(PORT_VLM_ALT, alt),
  ];
  const model = live?.state === "ok" ? str(live.vllm_state, 40) : null;
  if (model)
    facts.push({ label: k("rig.fact.model_state"), value: raw(model) });
  const base = {
    id: "vlm" as const,
    title: "rig.vlm.title",
    hint: "rig.vlm.hint",
    facts,
    notes: portsNotes(setup),
  };
  if (main.listening === true || alt.listening === true)
    return { ...base, state: "ok", summary: k("rig.vlm.ok") };
  // The alternate port is optional: it is not in the table unless the live
  // configuration names it, so only the main port decides "not started".
  if (main.listening === false)
    return { ...base, state: "not_started", summary: k("rig.vlm.closed") };
  return {
    ...base,
    state: "unknown",
    summary: main.why ?? k("rig.port.no_table"),
  };
}

// -- GPU (setup status, parts `gpu` and `gpu_locks`)

function holderText(holder: unknown): string {
  const item = rec(holder);
  const name = str(item?.process, 40);
  const pid = num(item?.pid);
  if (name && pid !== null) return `${name} (pid ${pid})`;
  if (name) return name;
  return pid !== null ? `pid ${pid}` : "?";
}

/** The server's sentence on why the lock files cannot be read. */
function lockNotes(setup: Rec | null): string[] {
  return unreadNote(rec(setup?.gpu_locks));
}

function lockFacts(setup: Rec | null): RigFact[] {
  const part = rec(setup?.gpu_locks);
  const label = k("GPU lock");
  if (!part || part.state !== "ok")
    return [
      {
        label,
        value: join(k("rig.value.unknown"), k("rig.gpu.no_locks")),
      },
    ];
  const out: RigFact[] = [];
  for (const entry of list(part.locks)) {
    const lock = rec(entry);
    const name = str(lock?.label, 40);
    if (!lock || !name) continue;
    if (lock.state === "held") {
      const holders = list(lock.holders).map(holderText).join(", ");
      out.push({
        label,
        value: k("rig.lock.held", { label: name, holder: holders || "?" }),
      });
    } else if (lock.state === "free")
      out.push({ label, value: k("rig.lock.free", { label: name }) });
    else if (lock.state === "absent")
      out.push({ label, value: k("rig.lock.absent", { label: name }) });
    else out.push({ label, value: k("rig.lock.unknown", { label: name }) });
  }
  return out.length > 0 ? out : [{ label, value: k("rig.gpu.no_locks") }];
}

function gpuCard(setup: Rec | null): RigCard {
  const part = rec(setup?.gpu);
  const base = {
    id: "gpu" as const,
    title: "GPU",
    hint: "rig.gpu.hint",
    notes: lockNotes(setup),
  };
  const locks = lockFacts(setup);
  const gpus = part?.state === "ok" ? list(part.gpus).map(rec) : [];
  if (!part || part.state !== "ok" || !gpus.some((gpu) => gpu !== null))
    return {
      ...base,
      state: "unknown",
      summary: k("rig.gpu.no_answer"),
      facts: locks,
      notes: [...detailOf(part), ...base.notes],
    };
  const facts: RigFact[] = [];
  for (const gpu of gpus) {
    if (!gpu) continue;
    const used = num(gpu.used_mib);
    const total = num(gpu.total_mib);
    const util = num(gpu.util_pct);
    facts.push({
      label: k("GPU memory"),
      value: raw(
        [
          str(gpu.name, 64),
          used !== null && total !== null
            ? `${gib(used)} / ${gib(total)} GiB`
            : null,
          util !== null ? `${util} %` : null,
        ]
          .filter(Boolean)
          .join(" · ") || "?",
      ),
    });
  }
  const apps = list(part.processes).map(rec);
  facts.push({
    label: k("rig.fact.gpu_apps"),
    value: apps.length === 0 ? k("rig.gpu.no_apps") : raw(String(apps.length)),
  });
  for (const app of apps.slice(0, 6)) {
    if (!app) continue;
    const memory = num(app.memory_mib);
    facts.push({
      label: k("rig.fact.gpu_app"),
      value: raw(
        [
          str(app.name, 60) ?? "?",
          str(app.role, 40),
          memory !== null ? `${gib(memory)} GiB` : null,
          num(app.pid) !== null ? `pid ${num(app.pid)}` : null,
        ]
          .filter(Boolean)
          .join(" · "),
      ),
    });
  }
  facts.push(...locks);
  if (part.reused === true)
    facts.push({
      label: k("rig.fact.sample"),
      value: k("rig.gpu.reused"),
    });
  return { ...base, state: "ok", summary: k("rig.gpu.ok"), facts };
}

// -- the "robot idle?" banner (rig status)

// A Map, not an object: a server's source named "constructor" or "__proto__"
// must not find anything.
const SOURCE_KEY = new Map([
  ["guard", "rig.source.guard"],
  ["marker", "rig.source.marker"],
  ["eval_sessions", "Evaluation sessions"],
  ["units", "rig.source.units"],
]);

function sourceSay(source: string): Say {
  const key = SOURCE_KEY.get(source);
  return key ? k(key) : raw(source);
}

// -- what a reason says, from its code (the rig status' `robot_idle`)

/** A reason's `age_s` as an age; "unknown" when the server left it out. */
const ageOf = (params: Rec): Say => {
  const age = num(params.age_s);
  return age === null ? k("rig.value.unknown") : { ago: age };
};
/** A reason's text value as the server gave it; "unknown" when it did not. */
const wordOf = (params: Rec, name: string): Say => {
  const value = str(params[name], 120);
  return value === null ? k("rig.value.unknown") : raw(value);
};

// A Map, like SOURCE_KEY: a code named "constructor" must not find anything.
// Each builder returns null when a value the sentence cannot do without is
// missing; the server's own sentence is shown then.
const REASON_SAY = new Map<string, (params: Rec) => Say | null>([
  ["marker.present", (p) => k("rig.reason.marker_present", { age: ageOf(p) })],
  ["marker.old", (p) => k("rig.reason.marker_old", { age: ageOf(p) })],
  ["marker.unreadable", () => k("rig.reason.marker_unreadable")],
  ["marker.no_folder", () => k("rig.reason.marker_no_folder")],
  [
    "sessions.recent",
    (p) => {
      const group = str(p.group, 120);
      return group === null
        ? null
        : k("rig.reason.sessions_recent", {
            group,
            age: ageOf(p),
            status: wordOf(p, "status"),
          });
    },
  ],
  [
    "sessions.silent",
    (p) => {
      const group = str(p.group, 120);
      return group === null
        ? null
        : k("rig.reason.sessions_silent", {
            group,
            age: ageOf(p),
            status: wordOf(p, "status"),
          });
    },
  ],
  ["sessions.unreadable", () => k("rig.reason.sessions_unreadable")],
  [
    "units.active",
    (p) => {
      const name = str(p.name, 120);
      const active = str(p.active, 120);
      return name === null || active === null
        ? null
        : k("rig.reason.units_active", {
            name,
            active,
            sub: wordOf(p, "sub"),
          });
    },
  ],
  [
    "units.state_unknown",
    (p) => {
      const name = str(p.name, 120);
      const active = str(p.active, 120);
      return name === null || active === null
        ? null
        : k("rig.reason.units_state_unknown", { name, active });
    },
  ],
  ["units.unreadable", () => k("rig.reason.units_unreadable")],
  [
    "guard.robot_process",
    (p) => {
      const names = str(p.names, 300);
      return names === null
        ? null
        : k("rig.reason.guard_robot_process", { names });
    },
  ],
  ["guard.controller_active", () => k("rig.reason.guard_controller_active")],
  ["guard.unreadable", () => k("rig.reason.guard_unreadable")],
  ["guard.not_quiet", () => k("rig.reason.guard_not_quiet")],
  ["guard.old_sample", () => k("rig.reason.guard_old_sample")],
  ["guard.truncated", () => k("rig.reason.guard_truncated")],
  ["guard.controller_value", () => k("rig.reason.guard_controller_value")],
]);

/** The page's words for an idle reason, from its `code` and `params`; null
 * when the page does not know the code or the params do not fit it. */
function reasonSay(item: Rec): Say | null {
  const code = item.code;
  if (typeof code !== "string") return null;
  return REASON_SAY.get(code)?.(rec(item.params) ?? {}) ?? null;
}

function reasonState(state: string | null): Say {
  switch (state) {
    case "busy":
      return k("rig.reason.busy");
    case null:
    case "unknown":
      return k("rig.reason.unknown");
    default:
      return raw(state);
  }
}

/** Whether the parts under an idle verdict were read: the marker is there
 * or is not, and the sessions and units were listed. An idle verdict over a
 * part nobody could read is a guess. */
function partsRead(rig: Rec): boolean {
  const marker = rec(rig.marker)?.state;
  return (
    (marker === "absent" || marker === "present") &&
    rec(rig.eval_sessions)?.state === "ok" &&
    rec(rig.units)?.state === "ok"
  );
}

function bannerOf(rig: Rec | null, error: string | undefined): RigBanner {
  if (!rig)
    return {
      state: "unknown",
      reasons: [],
      note: join(k("rig.banner.no_answer"), error ? raw(error) : null),
    };
  const idle = rec(rig.robot_idle);
  const state = idle?.state;
  if (state !== "idle" && state !== "busy" && state !== "unknown")
    return {
      state: "unknown",
      reasons: [],
      note: k("rig.banner.malformed"),
    };
  const found: Array<{ rank: number; reason: RigIdleReason }> = [];
  for (const entry of list(idle?.reasons)) {
    const item = rec(entry);
    const source = str(item?.source, 40);
    const text = str(item?.text, 300);
    if (!item || !source || !text) continue;
    const reason = str(item.state, 40);
    found.push({
      // What shows activity first, then what cannot tell, then the rest.
      rank:
        reason === "busy" ? 0 : reason === null || reason === "unknown" ? 1 : 2,
      reason: {
        source: sourceSay(source),
        state: reasonState(reason),
        say: reasonSay(item),
        text,
      },
    });
  }
  // The order of the server is kept within a kind; the cap comes after, so
  // a long run of one kind cannot push out the other.
  const reasons = found
    .sort((a, b) => a.rank - b.rank)
    .slice(0, MAX_REASONS)
    .map((entry) => entry.reason);
  // "Idle" is a claim about what is absent: it is not passed on from an
  // answer in another format, or one whose parts were not all read.
  if (state === "idle") {
    if (rig.schema !== undefined && rig.schema !== RIG_SCHEMA)
      return {
        state: "unknown",
        reasons,
        note: k("rig.banner.idle_schema"),
      };
    // An idle verdict that lists a reason contradicts itself; the reasons
    // stay on the page for the reader to weigh.
    if (!partsRead(rig) || list(idle?.reasons).length > 0)
      return {
        state: "unknown",
        reasons,
        note: k("rig.banner.idle_unverified"),
      };
  }
  return { state, reasons, note: null };
}

// -- an old sample of the machine status

/** Whether the setup status is an old sample: the server says it is its
 * previous one (`stale`), or it was already older than the freshness window
 * when it came. `age` is its age in seconds, null when the answer does not
 * say. */
function oldSample(setup: Rec | null): { age: number | null } | null {
  if (!setup) return null;
  const age = num(setup.age_s);
  const beyond = age !== null && age * 1000 > RIG_FRESH_MS;
  return setup.stale === true || beyond ? { age } : null;
}

/** A card read from an old sample: stale, naming the sample's age. What the
 * sample said stays as the first fact, so the readings are there but are not
 * the card's verdict. A card that had nothing to show stays unknown. */
function asOld(card: RigCard, age: number | null): RigCard {
  if (card.state === "unknown") return card;
  return {
    ...card,
    state: "stale",
    summary: k("rig.card.old_sample", { age: { ago: age } }),
    facts: [
      { label: k("rig.fact.in_sample"), value: card.summary },
      ...card.facts,
    ],
  };
}

// -- the whole page

/** Everything the page shows, from the two answers. A missing answer makes
 * the parts that need it "unknown" with the reason; it never fails. */
export function rigView(input: RigInput): RigView {
  const setup = rec(input.setup);
  const rig = rec(input.rig);
  const old = oldSample(setup);
  const notes: Say[] = [];
  if (!setup)
    notes.push(
      join(
        k("rig.note.no_setup"),
        input.setupError ? raw(input.setupError) : null,
      ),
    );
  else if (setup.stale === true)
    notes.push(
      join(k("rig.note.stale_sample"), {
        ago: num(setup.age_s) ?? 0,
      }),
    );
  else if (old) notes.push(join(k("rig.note.old_sample"), { ago: old.age }));
  else if (setup.state === "sampling") notes.push(k("rig.note.sampling"));
  const health = readHealth(setup);
  // The cards read from the machine status; the gripper stack has no source
  // and the demo client is read from the rig status.
  const aged = (card: RigCard) => (old ? asOld(card, old.age) : card);
  const robot: RigCard[] = [
    aged(deskCard(health)),
    aged(armCard(health, rig)),
    gripperCard(),
    aged(robotServerCard(setup)),
    aged(monitorCard(health)),
    aged(policyCard(setup, rig)),
    demoCard(rig),
  ];
  const host: RigCard[] = [
    aged(liveCard(setup)),
    aged(vlmCard(setup)),
    aged(gpuCard(setup)),
  ];
  return {
    banner: bannerOf(rig, input.rigError),
    groups: [
      { id: "robot", title: "rig.group.robot", cards: robot },
      { id: "host", title: "rig.group.host", cards: host },
    ],
    notes,
  };
}
