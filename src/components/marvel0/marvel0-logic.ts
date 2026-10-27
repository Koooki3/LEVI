// Pure mapping for the marvel_workstation page (/marvel_workstation): the two status answers
// (`GET /api/levi/setup/status` and `GET /api/levi/marvel0/status`) in, one view
// model per component out. No React and no fetching, so every rule can be
// tested on its own.
//
// Nothing here is a safety signal. A part that is missing, malformed or
// unreadable is "unknown" with a reason, never "ok" and never "not started":
// the page does not guess. "stale" (the evidence is old) is kept apart from
// "red" (the evidence says something is wrong). Text that came from a server
// is passed on as written, in English: a part's `detail` and the health
// file's reasons in a card's `notes` (once the file has stopped updating, on
// the monitor's own card only), and an idle reason whose `code` the
// page does not know. A reason whose code it knows is worded from the
// catalogue out of its `params`, and so is the note of a part the verdict
// names with such a code. The summary and the facts of a card are catalogue
// keys with their values (or identifiers such as a process or unit name), so
// the view model holds no language and a Chinese page prints no English
// sentence it can word itself.
import { ENDED_STATES } from "@/components/live/live-logic";

export type Marvel0State = "ok" | "red" | "stale" | "not_started" | "unknown";
export type IdleState = "idle" | "busy" | "unknown";

/** Catalogue key of the badge words of each state. */
export const STATE_KEY: Record<Marvel0State, string> = {
  ok: "marvel0.state.ok",
  red: "marvel0.state.red",
  stale: "marvel0.state.stale",
  not_started: "marvel0.state.not_started",
  unknown: "marvel0.state.unknown",
};
export const STATE_TONE: Record<
  Marvel0State,
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

export interface Marvel0Fact {
  label: Say;
  value: Say;
}

/** A string is text as a server wrote it (English whatever the page's
 * language); a `Say` is the page's own words. */
export type Marvel0Note = string | Say;

export type Marvel0CardId =
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

export interface Marvel0Card {
  id: Marvel0CardId;
  /** Catalogue key of the card's name. */
  title: string;
  state: Marvel0State;
  /** One line: why the badge says what it says. */
  summary: Say;
  facts: Marvel0Fact[];
  /** Under the card: text the servers sent along (reasons, errors), as
   * written, and the page's own words for what it knows by a code. */
  notes: Marvel0Note[];
  /** Catalogue key of the static hint: where the manual commands are. */
  hint: string;
}

export interface Marvel0IdleReason {
  source: Say;
  state: Say;
  /** What the reason says, in the page's words, from the server's `code`
   * and `params`. Null when the page does not know the code, or the params
   * do not fit it: then `text` is shown. */
  say: Say | null;
  /** The server's own sentence, in English whatever the page's language. */
  text: string;
}

export interface Marvel0Banner {
  state: IdleState;
  reasons: Marvel0IdleReason[];
  /** Why there is no answer (the route failed, the answer is malformed). */
  note: Say | null;
  /** One line under the reasons: what the one reason there is probably
   * means, when the rest of the page says so. */
  hint: Say | null;
}

/** The page's steps, in order: 1 the arm stack (M, Desk, A, B, C), 2 the
 * policy server, 3 the task and its command; then the services on this
 * machine. */
export type Marvel0GroupId = "arm" | "policy" | "client" | "host";

export interface Marvel0Group {
  id: Marvel0GroupId;
  /** Catalogue key. */
  title: string;
  cards: Marvel0Card[];
}

/** One stop of the stack status strip at the top of the page. */
export interface StackStep {
  id: Marvel0CardId;
  /** Catalogue key of the short name in the strip. */
  label: string;
  state: Marvel0State;
}

/** What the stack status strip shows: the stack in order (M → Desk → A → B
 * → C → policy server → client) and, when several stops are stale or unknown
 * for one reason, that reason in one line. This is the strip's whole input:
 * a later source (a dedicated, passive stack status route) only has to fill
 * it. Built here from the passive parts of the two status routes (health
 * file, marker, units, the kernel's socket table); never from a process
 * list. */
export interface StackStripData {
  steps: StackStep[];
  cause: Say | null;
}

/** The order and short names of the strip. */
export const STACK_ORDER: ReadonlyArray<{ id: Marvel0CardId; label: string }> =
  [
    { id: "monitor", label: "marvel0.strip.monitor" },
    { id: "desk", label: "marvel0.strip.desk" },
    { id: "arm", label: "marvel0.strip.arm" },
    { id: "gripper", label: "marvel0.strip.gripper" },
    { id: "robot_server", label: "marvel0.strip.server" },
    { id: "policy", label: "marvel0.strip.policy" },
    { id: "demo", label: "marvel0.strip.demo" },
  ];

export interface Marvel0View {
  banner: Marvel0Banner;
  groups: Marvel0Group[];
  /** The stack status strip, from the same cards. */
  stack: StackStripData;
  /** Page-level remarks: a request failed, a sample is old. */
  notes: Say[];
}

export interface Marvel0Input {
  /** The answer of `GET /api/levi/setup/status`, null when there is none. */
  setup: unknown;
  /** The answer of `GET /api/levi/marvel0/status`, null when there is none. */
  marvel0: unknown;
  /** Text of the failure that left `setup` / `marvel0` empty. */
  setupError?: string;
  marvel0Error?: string;
}

// -- polling (the hook in use-marvel0-poll.ts asks these)

export const MARVEL0_POLL_MS = 3000;
export const MARVEL0_POLL_MAX_MS = 30000;
/** An answer older than this is not shown as current: the cards go unknown. */
export const MARVEL0_FRESH_MS = 10000;

/** Milliseconds until the next request: 3 s, doubling after each failure up
 * to 30 s. */
export function marvel0Delay(failures: number): number {
  if (failures <= 0) return MARVEL0_POLL_MS;
  return Math.min(
    MARVEL0_POLL_MAX_MS,
    MARVEL0_POLL_MS * 2 ** Math.min(failures, 6),
  );
}

/** A request that has not answered in this long is given up (the page asks
 * again at its normal pace), so one that never settles cannot hold the
 * polling. It is longer than the slowest part the backend waits for (6 s) and,
 * with the poll gap, fits inside `MARVEL0_FRESH_MS`: an answer is dropped by the
 * failed round before its window ends. */
export const MARVEL0_REQUEST_MS = 6500;

/** How old the sample behind an answer already was when the server sent it,
 * in seconds: the setup status' `age_s`. Zero when the answer does not say
 * (the marvel0 status is taken on request and has none). */
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
  return now - at + carried <= MARVEL0_FRESH_MS;
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
/** The format of the marvel0 status this page was written for. */
export const MARVEL0_SCHEMA = "levi.marvel0.status.v1";
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
      ? "marvel0.value.unknown"
      : value
        ? "marvel0.value.yes"
        : "marvel0.value.no",
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
  marvel0: Rec | null,
  source: string,
  server: string[],
): Marvel0Note[] {
  const codes = UNREAD_CODES.get(source) ?? [];
  for (const entry of list(rec(marvel0?.robot_idle)?.reasons)) {
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

/** What a card that mirrors the monitor shows for the last reasons of a
 * health file that stopped updating: one line saying they are on the
 * monitor's own card, not the same lines on three cards. Nothing when the
 * file lists none. */
function sameAsMonitor(reasons: string[]): Marvel0Note[] {
  return reasons.length > 0 ? [k("marvel0.note.same_as_monitor")] : [];
}

/** The age of the health file, as a fact when it is known. */
function ageFact(part: Rec | null): Marvel0Fact[] {
  const age = num(part?.age_s);
  return age === null
    ? []
    : [{ label: k("marvel0.fact.last_update"), value: { ago: age } }];
}

function modeSay(part: Rec | null): Say {
  const name = str(part?.robot_mode_name, 40);
  const mode = num(part?.robot_mode);
  if (name) return raw(mode === null ? name : `${name} (${mode})`);
  return mode === null ? k("marvel0.value.unknown") : raw(String(mode));
}

/** What the health file cannot say: no file at all, or an unreadable part. */
function noHealth(
  id: Marvel0CardId,
  title: string,
  hint: string,
  health: Health,
): Marvel0Card {
  return {
    id,
    title,
    state: "unknown",
    summary:
      health.state === "missing"
        ? k("marvel0.health.missing_for_others")
        : k("marvel0.health.unreadable"),
    facts: [],
    notes: health.state === "missing" ? [] : detailOf(health.part),
    hint,
  };
}

/** What a red light that follows from the stack means for Desk: terminal A
 * not running (the monitor sees Desk only through A, so Desk's mode, FCI and
 * joint locks are not known), or A running with its hardware interface not
 * active (usually locked joints or FCI off in Desk). Null for any other red
 * light. Read from the monitor's red_cause, or, from an older monitor, from its
 * reasons. */
function deskRed(part: Rec | null): "needs_arm" | "inactive" | null {
  if (!redFollowsFromStack(part)) return null;
  const cause = str(part?.red_cause, 40);
  if (cause !== null) return cause === "stack_down" ? "needs_arm" : "inactive";
  const reasons = list(part?.reasons).map((entry) => str(entry) ?? "");
  const managerDown = reasons.some((r) =>
    r.startsWith("controller_manager_unreachable"),
  );
  return bool(part?.hardware_active) === false && !managerDown
    ? "inactive"
    : "needs_arm";
}

function deskCard(health: Health): Marvel0Card {
  const title = "marvel0.desk.title";
  const hint = "marvel0.desk.hint";
  if (health.state === "unknown" || health.state === "missing")
    return noHealth("desk", title, hint, health);
  const part = health.part;
  const hardware = bool(part?.hardware_active);
  const facts: Marvel0Fact[] = [
    { label: k("Robot mode"), value: modeSay(part) },
    {
      label: k("marvel0.fact.fci"),
      value:
        hardware === null
          ? k("marvel0.value.unknown")
          : k(
              hardware
                ? "marvel0.value.fci_active"
                : "marvel0.value.fci_inactive",
            ),
    },
    ...ageFact(part),
  ];
  const notes = healthNotes(health);
  if (health.state === "offline")
    return {
      id: "desk",
      title,
      state: "stale",
      summary: k("marvel0.health.offline_for_others"),
      facts,
      // Why it is offline (stopped updating, cannot be read), then where the
      // last reasons it wrote are.
      notes: [...detailOf(part), ...sameAsMonitor(notes)],
      hint,
    };
  if (health.state === "red") {
    // A red light that only says the stack is not up is not a Desk fault: the
    // card says what it cannot see, or what Desk usually lacks.
    const why = deskRed(part);
    if (why === "needs_arm")
      return {
        id: "desk",
        title,
        state: "unknown",
        summary: k("marvel0.desk.needs_arm"),
        facts,
        notes,
        hint,
      };
    if (why === "inactive")
      return {
        id: "desk",
        title,
        state: "not_started",
        summary: k("marvel0.desk.inactive"),
        facts,
        notes,
        hint,
      };
    return {
      id: "desk",
      title,
      state: "red",
      summary: k(RED_LIGHT),
      facts,
      notes,
      hint,
    };
  }
  const state: Marvel0State =
    hardware === null ? "unknown" : hardware ? "ok" : "not_started";
  return {
    id: "desk",
    title,
    state,
    summary: k(
      state === "ok"
        ? "marvel0.desk.ok"
        : state === "not_started"
          ? "marvel0.desk.inactive"
          : "marvel0.desk.no_field",
    ),
    facts,
    notes,
    hint,
  };
}

/** The health monitor's reasons that only say the arm stack is not running
 * (fr3_health_monitor.py): no hardware interface loaded, one that is not
 * active (`hardware_interface=<state>`), a controller manager that does not
 * answer, no robot state arriving, or none yet
 * (`waiting_for_first_robot_state`, while the monitor runs and has received
 * nothing). A reason is told by its exact name, which may be followed by a
 * space, a bracket or a parenthesis (the seconds or the note the monitor
 * appends); a name that only starts like one is another reason. */
const STACK_DOWN_REASON =
  /^(?:hardware_interface=|(?:hardware_interface_missing|controller_manager_unreachable|robot_state_no_data|waiting_for_first_robot_state)(?:$|[ (\[]))/;

/** Whether the red light follows from terminal A not running: it has reasons,
 * all of them of the kinds above, the robot reports no error of its own, and
 * the hardware interface is not known to be active (false or not said). With
 * the hardware up, "no robot state" or an unreachable controller manager is a
 * fault of the arm or its link, whatever the controller flag says. Anything
 * else is still a red light. */
function redFollowsFromStack(part: Rec | null): boolean {
  // A monitor since 2026-10-27 says it itself (red_cause): terminal A not
  // running, or its hardware interface not active, is the stack, not a fault.
  const cause = str(part?.red_cause, 40);
  if (cause !== null)
    return cause === "stack_down" || cause === "hardware_inactive";
  const reasons = list(part?.reasons).map((entry) => str(entry));
  if (reasons.length === 0 || list(part?.current_errors).length > 0)
    return false;
  const stackDown = reasons.every(
    (reason) => reason !== null && STACK_DOWN_REASON.test(reason),
  );
  return stackDown && bool(part?.hardware_active) !== true;
}

/** The marker the arm controller leaves while it runs (marvel0 status). */
function markerFact(marvel0: Rec | null): Marvel0Fact {
  const label = k("marvel0.fact.marker");
  const marker = rec(marvel0?.marker);
  const state = marker?.state;
  if (state === "absent") return { label, value: k("marvel0.marker.absent") };
  if (state === "present") {
    const age = num(marker?.age_s);
    return {
      label,
      value: join(
        k("marvel0.marker.present"),
        age === null ? null : { ago: age },
        age !== null && age >= MARKER_BUSY_S ? k("marvel0.marker.old") : null,
      ),
    };
  }
  return {
    label,
    value: marker
      ? join(k("marvel0.value.unknown"), k("marvel0.marker.unreadable"))
      : k("marvel0.marker.no_answer"),
  };
}

/** The marker part's own sentence when it is neither absent nor present. */
function markerNotes(marvel0: Rec | null): Marvel0Note[] {
  const marker = rec(marvel0?.marker);
  return marker?.state === "absent" || marker?.state === "present"
    ? []
    : unreadNotes(marvel0, "marker", detailOf(marker));
}

function armCard(health: Health, marvel0: Rec | null): Marvel0Card {
  const title = "marvel0.arm.title";
  const hint = "marvel0.arm.hint";
  const marker = markerFact(marvel0);
  if (health.state === "unknown" || health.state === "missing") {
    const bare = noHealth("arm", title, hint, health);
    return {
      ...bare,
      facts: [marker],
      notes: [...bare.notes, ...markerNotes(marvel0)],
    };
  }
  const part = health.part;
  const hardware = bool(part?.hardware_active);
  const controller = bool(part?.controller_active);
  const facts: Marvel0Fact[] = [
    { label: k("marvel0.fact.hardware"), value: yesNo(hardware) },
    { label: k("marvel0.fact.controller"), value: yesNo(controller) },
    marker,
    ...ageFact(part),
  ];
  const reasons = healthNotes(health);
  const markerNote = markerNotes(marvel0);
  const notes = [...reasons, ...markerNote];
  if (health.state === "offline")
    return {
      id: "arm",
      title,
      state: "stale",
      summary: k("marvel0.health.offline_for_others"),
      facts,
      notes: [...detailOf(part), ...sameAsMonitor(reasons), ...markerNote],
      hint,
    };
  if (health.state === "red") {
    // Not a fault of the arm: the stack was never started (or has gone).
    if (redFollowsFromStack(part))
      return {
        id: "arm",
        title,
        state: "not_started",
        summary: k("marvel0.arm.red_follows"),
        facts,
        notes,
        hint: "marvel0.arm.hint_red_follows",
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
  let state: Marvel0State = "unknown";
  let summary = "marvel0.arm.no_field";
  if (hardware === false) {
    state = "not_started";
    summary = "marvel0.arm.hardware_inactive";
  } else if (hardware === true && controller === true) {
    state = "ok";
    summary = "marvel0.arm.ok";
  } else if (hardware === true && controller === false) {
    state = "not_started";
    summary = "marvel0.arm.controller_inactive";
  }
  return { id: "arm", title, state, summary: k(summary), facts, notes, hint };
}

function gripperCard(): Marvel0Card {
  return {
    id: "gripper",
    title: "marvel0.gripper.title",
    state: "unknown",
    summary: k("marvel0.gripper.reason"),
    facts: [],
    notes: [],
    hint: "marvel0.gripper.hint",
  };
}

function monitorCard(health: Health): Marvel0Card {
  const part = health.part;
  const base = {
    id: "monitor" as const,
    title: "marvel0.monitor.title",
    hint: "marvel0.monitor.hint",
    notes: healthNotes(health),
  };
  const facts: Marvel0Fact[] = [...ageFact(part)];
  const rate = num(part?.command_success_rate);
  if (rate !== null)
    facts.push({
      label: k("marvel0.fact.success_rate"),
      value: raw(`${Math.round(rate * 100)} %`),
    });
  switch (health.state) {
    case "ok":
      return { ...base, state: "ok", summary: k("marvel0.monitor.ok"), facts };
    case "red":
      // The monitor itself works; a light that only says terminal A is not
      // running is told on the Desk and A cards. An inactive hardware
      // interface stays red here: usually Desk, but it can follow a fault.
      return deskRed(part) === "needs_arm"
        ? {
            ...base,
            state: "ok",
            summary: k("marvel0.monitor.ok_red_follows"),
            facts,
          }
        : { ...base, state: "red", summary: k(RED_LIGHT), facts };
    case "offline":
      return {
        ...base,
        state: "stale",
        summary: k("marvel0.monitor.offline"),
        facts,
        notes: [...detailOf(part), ...base.notes],
      };
    case "missing":
      return {
        ...base,
        state: "not_started",
        summary: k("marvel0.monitor.missing"),
        facts,
      };
    default:
      return {
        ...base,
        state: "unknown",
        summary: k("marvel0.health.unreadable"),
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
      why: k("marvel0.port.no_table"),
    };
  const row = list(part.ports)
    .map(rec)
    .find((entry) => entry !== null && entry.port === port);
  if (!row)
    return {
      listening: null,
      process: null,
      why: k("marvel0.port.not_watched"),
      notWatched: true,
    };
  const listening = bool(row.listening);
  if (listening === null)
    return { listening: null, process: null, why: k("marvel0.port.malformed") };
  return { listening, process: str(row.process, 60), why: null };
}

/** The server's sentence on why the port table cannot be read; every card
 * that shows a port as unknown carries it. */
function portsNotes(setup: Rec | null): string[] {
  return unreadNote(rec(setup?.ports));
}

function portFact(port: number, reading: PortReading): Marvel0Fact {
  const label = k("marvel0.fact.port", { port });
  if (reading.notWatched)
    return { label, value: k("marvel0.port.not_watched") };
  if (reading.listening === null)
    return {
      label,
      value: join(k("marvel0.value.unknown"), reading.why),
    };
  return {
    label,
    value: reading.listening
      ? join(
          k("marvel0.port.listening"),
          reading.process ? raw(reading.process) : null,
        )
      : k("marvel0.port.closed"),
  };
}

function robotServerCard(setup: Rec | null): Marvel0Card {
  const reading = readPort(setup, PORT_ROBOT_SERVER);
  const state: Marvel0State =
    reading.listening === null
      ? "unknown"
      : reading.listening
        ? "ok"
        : "not_started";
  return {
    id: "robot_server",
    title: "marvel0.server.title",
    state,
    summary:
      state === "unknown"
        ? (reading.why ?? k("marvel0.port.no_table"))
        : k(state === "ok" ? "marvel0.server.ok" : "marvel0.server.closed", {
            port: PORT_ROBOT_SERVER,
          }),
    facts: [portFact(PORT_ROBOT_SERVER, reading)],
    notes: portsNotes(setup),
    hint: "marvel0.server.hint",
  };
}

// -- units of the marvel0 status

interface Unit {
  name: string;
  active: string | null;
  sub: string | null;
}

/** The `levi-aeri-*` units the marvel0 status lists, or null when it does not
 * say (the answer is missing or its `units` part is not "ok"), with the
 * server's sentence on why. */
function readUnits(marvel0: Rec | null): {
  units: Unit[] | null;
  notes: Marvel0Note[];
} {
  const part = rec(marvel0?.units);
  if (!part || part.state !== "ok")
    return {
      units: null,
      notes: unreadNotes(marvel0, "units", unreadNote(part)),
    };
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
  if (active === null) return join(raw(name), k("marvel0.value.unknown"));
  return raw(sub ? `${name}: ${active} (${sub})` : `${name}: ${active}`);
}

function policyCard(setup: Rec | null, marvel0: Rec | null): Marvel0Card {
  const reading = readPort(setup, PORT_POLICY);
  const { units, notes } = readUnits(marvel0);
  const policyUnits = (units ?? []).filter((unit) =>
    unit.name.startsWith(POLICY_UNIT_PREFIX),
  );
  const failed = policyUnits.filter((unit) => unit.active === "failed");
  const running = policyUnits.filter((unit) =>
    RUNNING_UNIT.includes(unit.active ?? ""),
  );
  const facts: Marvel0Fact[] = [portFact(PORT_POLICY, reading)];
  if (units === null)
    facts.push({
      label: k("marvel0.fact.units"),
      value: join(k("marvel0.value.unknown"), k("marvel0.units.no_answer")),
    });
  else if (policyUnits.length === 0)
    facts.push({
      label: k("marvel0.fact.units"),
      value: k("marvel0.units.none"),
    });
  else
    for (const unit of policyUnits)
      facts.push({ label: k("marvel0.fact.unit"), value: unitSay(unit) });
  let state: Marvel0State;
  let summary: Say;
  if (reading.listening === true) {
    state = "ok";
    summary = k("marvel0.policy.ok", { port: PORT_POLICY });
  } else if (failed.length > 0) {
    state = "red";
    summary = k("marvel0.policy.failed");
  } else if (reading.listening === false) {
    state = "not_started";
    summary = k(
      running.length > 0 ? "marvel0.policy.loading" : "marvel0.policy.closed",
      { port: PORT_POLICY },
    );
  } else {
    // The port table cannot be read: a unit that runs does not show the
    // server answers, so the card does not say "ok".
    state = "unknown";
    summary = reading.why ?? k("marvel0.port.no_table");
  }
  return {
    id: "policy",
    title: "marvel0.policy.title",
    state,
    summary,
    facts,
    notes: [...portsNotes(setup), ...notes],
    hint: "marvel0.policy.hint",
  };
}

// -- evaluation sessions of the demo client (marvel0 status)

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

function demoCard(marvel0: Rec | null): Marvel0Card {
  const base = {
    id: "demo" as const,
    title: "marvel0.demo.title",
    hint: "marvel0.demo.hint",
  };
  const part = rec(marvel0?.eval_sessions);
  const sessions = list(part?.sessions)
    .map(sessionLine)
    .filter((line): line is SessionLine => line !== null)
    .sort((a, b) => a.age - b.age);
  // A part that could not read everything still lists the sessions it found.
  const complete = part?.state === "ok";
  const notes: Marvel0Note[] = complete
    ? detailOf(part)
    : unreadNotes(marvel0, "eval_sessions", detailOf(part));
  if (!part || (!complete && sessions.length === 0))
    return {
      ...base,
      state: "unknown",
      summary: k("marvel0.demo.no_answer"),
      facts: [],
      notes,
    };
  const facts: Marvel0Fact[] = sessions.slice(0, 6).map((line) => ({
    label: raw(line.group),
    value: join(line.status ? raw(line.status) : k("marvel0.value.unknown"), {
      ago: line.age,
    }),
  }));
  const roots = num(part.roots);
  if (roots !== null)
    facts.push({ label: k("marvel0.fact.roots"), value: raw(String(roots)) });
  const recent = sessions.filter((line) => line.age <= RECENT_S);
  // Newest first, so the first fault is the latest one.
  const faults = sessions.filter((line) => isFault(line.status));
  let state: Marvel0State;
  let summary: Say;
  if (recent.some((line) => isFault(line.status))) {
    state = "red";
    summary = k("marvel0.demo.fault", { s: RECENT_S });
  } else if (recent.some((line) => isRunning(line.status))) {
    state = "ok";
    summary = k("marvel0.demo.ok", { s: RECENT_S });
  } else if (faults.length > 0) {
    // A fault does not turn into "went quiet" after a minute: nothing says
    // it was dealt with, so the card keeps saying that one happened.
    state = "red";
    summary = k("marvel0.demo.fault_earlier", { age: { ago: faults[0].age } });
  } else if (sessions.some((line) => isRunning(line.status))) {
    state = "stale";
    summary = k("marvel0.demo.silent", { s: RECENT_S });
  } else {
    state = "not_started";
    summary = k(
      sessions.length > 0 ? "marvel0.demo.ended" : "marvel0.demo.none",
    );
  }
  // Sessions that ended do not show that nothing runs while the list may be
  // missing some.
  if (!complete && state === "not_started") {
    state = "unknown";
    summary = k("marvel0.demo.partial");
  }
  return { ...base, state, summary, facts, notes };
}

// -- live service and the model server (setup status)

function liveCard(setup: Rec | null): Marvel0Card {
  const part = rec(setup?.live);
  const core = readPort(setup, PORT_LIVE_CORE);
  const judge = readPort(setup, PORT_ONLINE_JUDGE);
  const facts: Marvel0Fact[] = [];
  if (part?.state === "ok") {
    const service = str(part.service_state, 40);
    if (service)
      facts.push({ label: k("marvel0.fact.service"), value: raw(service) });
    const age = num(part.age_s);
    if (age !== null)
      facts.push({ label: k("marvel0.fact.last_update"), value: { ago: age } });
    const paused = bool(part.labelling_paused);
    if (paused !== null)
      facts.push({
        label: k("marvel0.fact.labelling_paused"),
        value: yesNo(paused),
      });
    const gate = rec(part.gate);
    const open = bool(gate?.open);
    const code = str(gate?.code, 60);
    if (open !== null)
      facts.push({
        label: k("marvel0.fact.gpu_gate"),
        value: join(
          k(open ? "marvel0.value.open" : "marvel0.value.closed"),
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
    title: "marvel0.live.title",
    hint: "marvel0.live.hint",
    facts,
    notes,
  };
  if (part?.state === "ok") {
    if (part.alive === true)
      return { ...base, state: "ok", summary: k("marvel0.live.ok") };
    if (str(part.service_state) === "stopped")
      return {
        ...base,
        state: "not_started",
        summary: k("marvel0.live.stopped"),
      };
    return { ...base, state: "stale", summary: k("marvel0.live.silent") };
  }
  // No status file to read: the core's port is the only evidence left, and a
  // listening port with no readable status is not "ok".
  if (core.listening === false)
    return {
      ...base,
      state: "not_started",
      summary: k("marvel0.live.closed", { port: PORT_LIVE_CORE }),
    };
  return {
    ...base,
    state: "unknown",
    summary: k("marvel0.live.no_status"),
    notes: [...detailOf(part), ...notes],
  };
}

function vlmCard(setup: Rec | null): Marvel0Card {
  const main = readPort(setup, PORT_VLLM);
  const alt = readPort(setup, PORT_VLM_ALT);
  const live = rec(setup?.live);
  const facts: Marvel0Fact[] = [
    portFact(PORT_VLLM, main),
    portFact(PORT_VLM_ALT, alt),
  ];
  const model = live?.state === "ok" ? str(live.vllm_state, 40) : null;
  if (model)
    facts.push({ label: k("marvel0.fact.model_state"), value: raw(model) });
  const base = {
    id: "vlm" as const,
    title: "marvel0.vlm.title",
    hint: "marvel0.vlm.hint",
    facts,
    notes: portsNotes(setup),
  };
  if (main.listening === true || alt.listening === true)
    return { ...base, state: "ok", summary: k("marvel0.vlm.ok") };
  // The alternate port is optional: it is not in the table unless the live
  // configuration names it, so only the main port decides "not started".
  if (main.listening === false)
    return { ...base, state: "not_started", summary: k("marvel0.vlm.closed") };
  return {
    ...base,
    state: "unknown",
    summary: main.why ?? k("marvel0.port.no_table"),
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

function lockFacts(setup: Rec | null): Marvel0Fact[] {
  const part = rec(setup?.gpu_locks);
  const label = k("GPU lock");
  if (!part || part.state !== "ok")
    return [
      {
        label,
        value: join(k("marvel0.value.unknown"), k("marvel0.gpu.no_locks")),
      },
    ];
  const out: Marvel0Fact[] = [];
  for (const entry of list(part.locks)) {
    const lock = rec(entry);
    const name = str(lock?.label, 40);
    if (!lock || !name) continue;
    if (lock.state === "held") {
      const holders = list(lock.holders).map(holderText).join(", ");
      out.push({
        label,
        value: k("marvel0.lock.held", { label: name, holder: holders || "?" }),
      });
    } else if (lock.state === "free")
      out.push({ label, value: k("marvel0.lock.free", { label: name }) });
    else if (lock.state === "absent")
      out.push({ label, value: k("marvel0.lock.absent", { label: name }) });
    else out.push({ label, value: k("marvel0.lock.unknown", { label: name }) });
  }
  return out.length > 0 ? out : [{ label, value: k("marvel0.gpu.no_locks") }];
}

function gpuCard(setup: Rec | null): Marvel0Card {
  const part = rec(setup?.gpu);
  const base = {
    id: "gpu" as const,
    title: "GPU",
    hint: "marvel0.gpu.hint",
    notes: lockNotes(setup),
  };
  const locks = lockFacts(setup);
  const gpus = part?.state === "ok" ? list(part.gpus).map(rec) : [];
  if (!part || part.state !== "ok" || !gpus.some((gpu) => gpu !== null))
    return {
      ...base,
      state: "unknown",
      summary: k("marvel0.gpu.no_answer"),
      facts: locks,
      notes: [...detailOf(part), ...base.notes],
    };
  const facts: Marvel0Fact[] = [];
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
    label: k("marvel0.fact.gpu_apps"),
    value:
      apps.length === 0 ? k("marvel0.gpu.no_apps") : raw(String(apps.length)),
  });
  for (const app of apps.slice(0, 6)) {
    if (!app) continue;
    const memory = num(app.memory_mib);
    facts.push({
      label: k("marvel0.fact.gpu_app"),
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
      label: k("marvel0.fact.sample"),
      value: k("marvel0.gpu.reused"),
    });
  return { ...base, state: "ok", summary: k("marvel0.gpu.ok"), facts };
}

// -- the "robot idle?" banner (marvel0 status)

// A Map, not an object: a server's source named "constructor" or "__proto__"
// must not find anything.
const SOURCE_KEY = new Map([
  ["guard", "marvel0.source.guard"],
  ["marker", "marvel0.source.marker"],
  ["eval_sessions", "Evaluation sessions"],
  ["units", "marvel0.source.units"],
]);

function sourceSay(source: string): Say {
  const key = SOURCE_KEY.get(source);
  return key ? k(key) : raw(source);
}

// -- what a reason says, from its code (the marvel0 status' `robot_idle`)

/** A reason's `age_s` as an age; "unknown" when the server left it out. */
const ageOf = (params: Rec): Say => {
  const age = num(params.age_s);
  return age === null ? k("marvel0.value.unknown") : { ago: age };
};
/** A reason's text value as the server gave it; "unknown" when it did not. */
const wordOf = (params: Rec, name: string): Say => {
  const value = str(params[name], 120);
  return value === null ? k("marvel0.value.unknown") : raw(value);
};

// A Map, like SOURCE_KEY: a code named "constructor" must not find anything.
// Each builder returns null when a value the sentence cannot do without is
// missing; the server's own sentence is shown then.
const REASON_SAY = new Map<string, (params: Rec) => Say | null>([
  [
    "marker.present",
    (p) => k("marvel0.reason.marker_present", { age: ageOf(p) }),
  ],
  ["marker.old", (p) => k("marvel0.reason.marker_old", { age: ageOf(p) })],
  ["marker.unreadable", () => k("marvel0.reason.marker_unreadable")],
  ["marker.no_folder", () => k("marvel0.reason.marker_no_folder")],
  [
    "sessions.recent",
    (p) => {
      const group = str(p.group, 120);
      return group === null
        ? null
        : k("marvel0.reason.sessions_recent", {
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
        : k("marvel0.reason.sessions_silent", {
            group,
            age: ageOf(p),
            status: wordOf(p, "status"),
          });
    },
  ],
  ["sessions.unreadable", () => k("marvel0.reason.sessions_unreadable")],
  [
    "units.active",
    (p) => {
      const name = str(p.name, 120);
      const active = str(p.active, 120);
      return name === null || active === null
        ? null
        : k("marvel0.reason.units_active", {
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
        : k("marvel0.reason.units_state_unknown", { name, active });
    },
  ],
  ["units.unreadable", () => k("marvel0.reason.units_unreadable")],
  [
    "guard.robot_process",
    (p) => {
      const names = str(p.names, 300);
      return names === null
        ? null
        : k("marvel0.reason.guard_robot_process", { names });
    },
  ],
  [
    "guard.controller_active",
    () => k("marvel0.reason.guard_controller_active"),
  ],
  ["guard.unreadable", () => k("marvel0.reason.guard_unreadable")],
  ["guard.not_quiet", () => k("marvel0.reason.guard_not_quiet")],
  ["guard.old_sample", () => k("marvel0.reason.guard_old_sample")],
  ["guard.truncated", () => k("marvel0.reason.guard_truncated")],
  ["guard.controller_value", () => k("marvel0.reason.guard_controller_value")],
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
      return k("marvel0.reason.busy");
    case null:
    case "unknown":
      return k("marvel0.reason.unknown");
    default:
      return raw(state);
  }
}

/** Whether the parts under an idle verdict were read: the marker is there
 * or is not, and the sessions and units were listed. An idle verdict over a
 * part nobody could read is a guess. */
function partsRead(marvel0: Rec): boolean {
  const marker = rec(marvel0.marker)?.state;
  return (
    (marker === "absent" || marker === "present") &&
    rec(marvel0.eval_sessions)?.state === "ok" &&
    rec(marvel0.units)?.state === "ok"
  );
}

/** Whether an entry of the idle reasons, one the banner lists, says the
 * arm-controller marker is there: the code `marker.present`, or a busy reason
 * of the marker from an older server, which sends no code. A marker that is
 * old, unreadable or without its folder is another reason and says so
 * itself. */
function isMarkerPresent(item: Rec | null): boolean {
  return (
    item?.source === "marker" &&
    item.state === "busy" &&
    str(item.text, 300) !== null &&
    (item.code === undefined || item.code === "marker.present")
  );
}

/** A marker younger than this is not called a leftover: setup.md has the
 * operator touch it, wait 60 s and only then start the arm stack, so for that
 * time the marker is there, the health monitor is not running and no robot
 * process exists, and the hint would tell the operator to remove the marker
 * they just placed. */
export const MARKER_HINT_MIN_S = 600;

/** How old the marker is, from the age the server gave its reason, or
 * ``null`` when there is none the page can read. */
function markerAgeS(item: Rec | null): number | null {
  const age = rec(item?.params)?.age_s;
  return typeof age === "number" && Number.isFinite(age) && age >= 0
    ? age
    : null;
}

/** The hint under a busy banner whose only reasons are a marker that is
 * there for at least ``MARKER_HINT_MIN_S``, while the health monitor is not
 * running (its file stopped updating, or is missing) in a sample that is not
 * old: without the arm stack the marker is most likely one left behind when
 * it was shut down. Nothing is said when anything else shows activity, the
 * monitor reports, or the health file merely cannot be read. */
function markerHint(
  entries: unknown[],
  state: IdleState,
  health: Health,
  old: boolean,
): Say | null {
  const monitorDown =
    health.state === "missing" ||
    (health.state === "offline" && num(health.part?.age_s) !== null);
  const items = entries.map((entry) => rec(entry));
  const onlyMarker =
    items.length > 0 && items.every((item) => isMarkerPresent(item));
  const ages = items.map((item) => markerAgeS(item));
  const aged = ages.every((age) => age !== null && age >= MARKER_HINT_MIN_S);
  return state === "busy" && !old && monitorDown && onlyMarker && aged
    ? k("marvel0.hint.marker_leftover")
    : null;
}

function bannerOf(
  marvel0: Rec | null,
  error: string | undefined,
  health: Health,
  old: boolean,
): Marvel0Banner {
  if (!marvel0)
    return {
      state: "unknown",
      reasons: [],
      note: join(k("marvel0.banner.no_answer"), error ? raw(error) : null),
      hint: null,
    };
  const idle = rec(marvel0.robot_idle);
  const state = idle?.state;
  if (state !== "idle" && state !== "busy" && state !== "unknown")
    return {
      state: "unknown",
      reasons: [],
      note: k("marvel0.banner.malformed"),
      hint: null,
    };
  const found: Array<{ rank: number; reason: Marvel0IdleReason }> = [];
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
    if (marvel0.schema !== undefined && marvel0.schema !== MARVEL0_SCHEMA)
      return {
        state: "unknown",
        reasons,
        note: k("marvel0.banner.idle_schema"),
        hint: null,
      };
    if (!partsRead(marvel0))
      return {
        state: "unknown",
        reasons,
        note: k("marvel0.banner.idle_unverified"),
        hint: null,
      };
    // An idle verdict that lists a reason contradicts itself; the reasons
    // stay on the page for the reader to weigh.
    if (list(idle?.reasons).length > 0)
      return {
        state: "unknown",
        reasons,
        note: k("marvel0.banner.idle_with_reasons"),
        hint: null,
      };
  }
  return {
    state,
    reasons,
    note: null,
    hint: markerHint(list(idle?.reasons), state, health, old),
  };
}

// -- an old sample of the machine status

/** Whether the setup status is an old sample: the server says it is its
 * previous one (`stale`), or it was already older than the freshness window
 * when it came. `age` is its age in seconds, null when the answer does not
 * say. */
function oldSample(setup: Rec | null): { age: number | null } | null {
  if (!setup) return null;
  const age = num(setup.age_s);
  const beyond = age !== null && age * 1000 > MARVEL0_FRESH_MS;
  return setup.stale === true || beyond ? { age } : null;
}

/** A card read from an old sample: stale, naming the sample's age. What the
 * sample said stays as the first fact, so the readings are there but are not
 * the card's verdict. A card that had nothing to show stays unknown. */
function asOld(card: Marvel0Card, age: number | null): Marvel0Card {
  if (card.state === "unknown") return card;
  return {
    ...card,
    state: "stale",
    summary: k("marvel0.card.old_sample", { age: { ago: age } }),
    facts: [
      { label: k("marvel0.fact.in_sample"), value: card.summary },
      ...card.facts,
    ],
  };
}

// -- the stack status strip

/** One line for the top of the page when the health monitor is the reason
 * Desk, terminal A and terminal M cannot say anything from now: its file
 * stopped updating, or there is none. Null otherwise (an old sample of the
 * whole machine status has its own page note). */
function stackCause(health: Health, old: boolean): Say | null {
  if (old) return null;
  if (health.state === "offline") {
    const age = num(health.part?.age_s);
    return k("marvel0.cause.monitor_offline", {
      age: age === null ? k("marvel0.value.unknown") : { ago: age },
    });
  }
  if (health.state === "missing") return k("marvel0.cause.monitor_missing");
  return null;
}

/** The strip from the cards, in `STACK_ORDER`. */
export function stackFromCards(
  cards: Marvel0Card[],
  cause: Say | null,
): StackStripData {
  const byId = new Map(cards.map((card) => [card.id, card]));
  return {
    steps: STACK_ORDER.map(({ id, label }) => {
      // A stop without its card (a source that does not send it) is a
      // state the page cannot tell, not a missing value.
      const card = byId.get(id);
      return { id, label, state: card ? card.state : "unknown" };
    }),
    cause,
  };
}

// -- the whole page

/** Everything the page shows, from the two answers. A missing answer makes
 * the parts that need it "unknown" with the reason; it never fails. */
export function marvel0View(input: Marvel0Input): Marvel0View {
  const setup = rec(input.setup);
  const marvel0 = rec(input.marvel0);
  const old = oldSample(setup);
  const notes: Say[] = [];
  if (!setup)
    notes.push(
      join(
        k("marvel0.note.no_setup"),
        input.setupError ? raw(input.setupError) : null,
      ),
    );
  else if (setup.stale === true)
    notes.push(
      join(k("marvel0.note.stale_sample"), {
        ago: num(setup.age_s) ?? 0,
      }),
    );
  else if (old)
    notes.push(join(k("marvel0.note.old_sample"), { ago: old.age }));
  else if (setup.state === "sampling") notes.push(k("marvel0.note.sampling"));
  const health = readHealth(setup);
  // The cards read from the machine status; the gripper stack has no source
  // and the demo client is read from the marvel0 status.
  const aged = (card: Marvel0Card) => (old ? asOld(card, old.age) : card);
  // Step 1 in the order of the strip: M, Desk, A, B, C.
  const arm: Marvel0Card[] = [
    aged(monitorCard(health)),
    aged(deskCard(health)),
    aged(armCard(health, marvel0)),
    gripperCard(),
    aged(robotServerCard(setup)),
  ];
  const policy: Marvel0Card[] = [aged(policyCard(setup, marvel0))];
  const client: Marvel0Card[] = [demoCard(marvel0)];
  const host: Marvel0Card[] = [
    aged(liveCard(setup)),
    aged(vlmCard(setup)),
    aged(gpuCard(setup)),
  ];
  return {
    banner: bannerOf(marvel0, input.marvel0Error, health, old !== null),
    groups: [
      { id: "arm", title: "marvel0.step.arm", cards: arm },
      { id: "policy", title: "marvel0.step.policy", cards: policy },
      { id: "client", title: "marvel0.step.client", cards: client },
      { id: "host", title: "marvel0.group.host", cards: host },
    ],
    stack: stackFromCards(
      [...arm, ...policy, ...client],
      stackCause(health, old !== null),
    ),
    notes,
  };
}
