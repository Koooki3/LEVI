// The arm stack card's view model, from `GET /api/levi/marvel0/stack`
// (`levi.marvel0.stack.v1`, levi/marvel0/stack.py). Pure: what the card shows
// is decided here, and every word is a catalogue key (`marvel0.stack.*`) or the
// server's own text marked as such. A value of the wrong type reads as
// unknown, never as a guess. Nothing here sends a request.
import type { Say } from "./marvel0-logic";
import { bool, list, num, rec, str, type Rec } from "./marvel0-json";

export const STACK_SCHEMA = "levi.marvel0.stack.v1";
export const STACK_PATH = "/api/levi/marvel0/stack";
export const STACK_POLL_MS = 2_000;

/** The order of the rows: M first (it comes up first), then A, B, C, the
 * policy server and the client. */
export const MEMBERS = ["M", "A", "B", "C", "policy", "client"] as const;
export type Member = (typeof MEMBERS)[number];
export const GATED = ["A", "B", "C"] as const;
export type Gated = (typeof GATED)[number];

export type MemberState =
  | "running"
  | "starting"
  | "stopping"
  | "stopped"
  | "failed"
  | "foreign"
  | "elsewhere"
  | "unknown";
const MEMBER_STATES: readonly MemberState[] = [
  "running",
  "starting",
  "stopping",
  "stopped",
  "failed",
  "foreign",
  "elsewhere",
  "unknown",
];
export type Tone = "neutral" | "success" | "warning" | "danger" | "info";
export const STATE_TONE: Record<MemberState, Tone> = {
  running: "success",
  starting: "info",
  stopping: "info",
  stopped: "neutral",
  failed: "danger",
  foreign: "warning",
  elsewhere: "info",
  unknown: "warning",
};

export interface Evidence {
  label: Say;
  value: Say;
}
export interface MemberRow {
  id: Member;
  name: Say;
  state: MemberState;
  tone: Tone;
  stateLabel: Say;
  why: Say;
  evidence: Evidence[];
}
export type CheckOutcome = "pass" | "fail" | "unknown" | "warn" | "person";
export interface CheckRow {
  code: string;
  label: Say;
  outcome: CheckOutcome;
  outcomeLabel: Say;
}
export interface GateRow {
  id: Gated;
  name: Say;
  verdict: "would_start" | "would_refuse";
  verdictLabel: Say;
  checks: CheckRow[];
}
export interface MonitorView {
  canStart: boolean;
  canStop: boolean;
  startBlockers: Say[];
  stopBlockers: Say[];
  auto: boolean;
  plan: Say | null;
  last: Say | null;
}
export interface MarkerView {
  state: "present" | "absent" | "unknown";
  label: Say;
  canClear: boolean;
  blockers: Say[];
}
export type StackVerdict = "busy" | "idle" | "unknown";
export interface StackView {
  sampledAt: number | null;
  /** Rule R: never "idle" while a layer is unknown (anything unreadable is
   * "unknown" too). */
  verdict: StackVerdict;
  verdictLabel: Say;
  real: boolean;
  members: MemberRow[];
  gates: GateRow[];
  monitor: MonitorView;
  marker: MarkerView;
  lock: Say;
  notices: Say[];
  phrases: Record<"monitor_start" | "monitor_stop" | "marker_clear", string>;
}

const key = (k: string, params?: Record<string, string | number | Say>): Say =>
  params ? { key: k, params } : { key: k };
const P = "marvel0.stack.";

/** A server code as catalogue text; an unknown code is shown as written
 * (the server's own name, which a test keeps in step with the catalogue). */
export function codeSay(
  prefix: string,
  code: unknown,
  known: Set<string>,
): Say {
  const name = str(code, 80);
  if (!name) return key(`${P}state.unknown`);
  const k = `${P}${prefix}${name}`;
  return known.has(k) ? key(k) : { raw: name };
}

function yesNo(value: unknown): Say {
  if (value === true) return key(`${P}value.yes`);
  if (value === false) return key(`${P}value.no`);
  const text = str(value, 40);
  if (text) return { raw: text };
  const n = num(value);
  return n === null ? key(`${P}value.unknown`) : { raw: String(n) };
}

function memberState(value: unknown): MemberState {
  return MEMBER_STATES.includes(value as MemberState)
    ? (value as MemberState)
    : "unknown";
}

function memberRow(
  id: Member,
  raw: Rec | null,
  known: Set<string>,
  health: Rec | null,
): MemberRow {
  const state = memberState(raw?.state);
  const code = str(raw?.code, 80);
  const age = num(health?.age_s);
  const why: Say =
    code && known.has(`${P}code.${code}`)
      ? key(`${P}code.${code}`, { age: { ago: age } })
      : code
        ? { raw: code }
        : key(`${P}code.${id === "M" ? "m" : id.toLowerCase()}.unknown`);
  const evidence = list(raw?.evidence)
    .map(rec)
    .filter((e): e is Rec => e !== null)
    .map((e) => ({
      label: codeSay("evidence.", e.code, known),
      value: yesNo(e.value),
    }));
  return {
    id,
    name: key(`${P}name.${id}`),
    state,
    tone: STATE_TONE[state],
    stateLabel: key(`${P}state.${state}`),
    why,
    evidence,
  };
}

function checkRow(raw: Rec, known: Set<string>): CheckRow {
  const level = str(raw.level, 20);
  const ok = raw.ok;
  const outcome: CheckOutcome =
    level === "person"
      ? "person"
      : level === "warn"
        ? "warn"
        : ok === true
          ? "pass"
          : ok === false
            ? "fail"
            : "unknown";
  const label = codeSay("check.", raw.code, known);
  const params: Record<string, string> = {};
  for (const [name, value] of Object.entries(rec(raw.params) ?? {})) {
    const text =
      str(value, 120) ?? (num(value) === null ? null : String(value));
    params[name] = text ?? "—";
  }
  return {
    code: str(raw.code, 80) ?? "",
    label:
      "key" in label && Object.keys(params).length
        ? key(label.key, params)
        : label,
    outcome,
    outcomeLabel: key(`${P}check.${outcome}`),
  };
}

function gateRow(id: Gated, raw: Rec | null, known: Set<string>): GateRow {
  // Anything but an explicit "would_start" is a refusal.
  const verdict =
    raw?.verdict === "would_start" ? "would_start" : "would_refuse";
  return {
    id,
    name: key(`${P}name.${id}`),
    verdict,
    verdictLabel: key(`${P}verdict.${verdict}`),
    checks: list(raw?.checks)
      .map(rec)
      .filter((c): c is Rec => c !== null)
      .map((c) => checkRow(c, known)),
  };
}

const refusals = (value: unknown, known: Set<string>): Say[] =>
  list(value).map((code) => codeSay("refuse.", code, known));

/** The card's model, or null when the answer is not a stack document. */
export function stackView(raw: unknown, known: Set<string>): StackView | null {
  const doc = rec(raw);
  if (!doc || doc.schema !== STACK_SCHEMA) return null;
  const terminals = rec(doc.terminals);
  const health = rec(doc.health);
  const gates = rec(doc.gates);
  const monitor = rec(doc.monitor);
  const marker = rec(doc.marker);
  const lock = rec(doc.lock);
  const real = doc.profile === "real";
  const phrases = rec(doc.phrases);
  const markerState =
    marker?.state === "present" || marker?.state === "absent"
      ? marker.state
      : "unknown";
  const plan = rec(monitor?.plan);
  const last = rec(monitor?.last_auto);
  const notices: Say[] = [];
  for (const entry of list(doc.notices)) {
    const code = str(rec(entry)?.code, 80);
    if (code === "notice.gates_scope") notices.push(key(`${P}gates_scope`));
    else if (code === "notice.logout_stops_stack")
      notices.push(key(`${P}logout`));
  }
  if (!notices.some((n) => "key" in n && n.key === `${P}gates_scope`))
    notices.unshift(key(`${P}gates_scope`)); // said whatever the server sent
  if (!real) notices.unshift(key(`${P}fake`));
  const lockState =
    lock?.state === "free" || lock?.state === "held" ? lock.state : "unknown";
  const sent = rec(doc.verdict)?.verdict;
  const verdict: StackVerdict =
    sent === "busy" || sent === "idle" ? sent : "unknown";
  return {
    sampledAt: num(doc.sampled_at),
    verdict,
    verdictLabel: key(`${P}verdict.${verdict}`),
    real,
    members: MEMBERS.map((id) =>
      memberRow(id, rec(terminals?.[id]), known, health),
    ),
    gates: GATED.map((id) => gateRow(id, rec(gates?.[id]), known)),
    monitor: {
      canStart: real && bool(monitor?.can_start) === true,
      canStop: real && bool(monitor?.can_stop) === true,
      startBlockers: refusals(monitor?.start_blockers, known),
      stopBlockers: refusals(monitor?.stop_blockers, known),
      auto: bool(monitor?.auto) === true,
      plan: plan ? codeSay("plan.", plan.code, known) : null,
      last: last ? codeSay("plan.", last.code, known) : null,
    },
    marker: {
      state: markerState,
      label:
        markerState === "present"
          ? key(`${P}marker.present`, { age: { ago: num(marker?.age_s) } })
          : key(`${P}marker.${markerState}`),
      canClear: real && bool(marker?.can_clear) === true,
      blockers: refusals(marker?.clear_blockers, known),
    },
    lock: key(`${P}lock.${lockState}`),
    notices,
    phrases: {
      monitor_start: str(phrases?.monitor_start, 60) ?? "start health monitor",
      monitor_stop: str(phrases?.monitor_stop, 60) ?? "stop health monitor",
      marker_clear: str(phrases?.marker_clear, 60) ?? "clear marker",
    },
  };
}

/** A request id the server accepts (8-80 of A-Z a-z 0-9 . _ : -), kept by
 * the caller across a retry so a repeat is answered, never done twice. */
export function newRequestId(random: () => number = Math.random): string {
  const part = () => Math.floor(random() * 0x100000000).toString(36);
  return `stack-${Date.now().toString(36)}-${part()}${part()}`;
}

/** The typed phrase matches exactly (spaces at the ends do not count). */
export const phraseMatches = (typed: string, phrase: string): boolean =>
  typed.trim() === phrase;
