// Pure model of the voice card of the marvel0: the three answers of the voice
// routes read into types, the card's state machine (a reducer), and every
// rule about what a person may do with a proposal. No React, no fetching, no
// microphone: the rules can be tested on their own.
//
// A voice result is a PROPOSAL. It changes nothing until a person presses Use,
// and Use hands over one sentence of the allowed list the server sent, or the
// sentence the server generated in the datasets' most common phrasing (shown
// as generated; owner decision 2026-10-30), never the transcript. Rules kept
// here:
//   * only the newest request (a recording, an edited text, a model switch)
//     can become the proposal: every request has a sequence number and an
//     answer for another number is dropped;
//   * a proposal lives 60 s, and Use works only after it has been on screen
//     for 800 ms;
//   * a result with a stop, negation or unsafe flag has no candidates at all;
//   * a sentence is offered only if it is in the server's allowed list, or is
//     the generated sentence (or its dataset original) of a result without a
//     flag, for a model that is not of the restricted family.
import type { ApiFailure } from "./deploy-api";
import { sayFailure } from "./marvel0-failure";
import { bool, list, num, rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import type { CaptureErrorCode } from "./voice-capture";

export const STATUS_SCHEMA = "levi.marvel0.speech.status.v1";
export const TRANSCRIBE_SCHEMA = "levi.marvel0.speech.v1";
export const MATCH_SCHEMA = "levi.marvel0.speech.match.v1";

/** A proposal that was not used is dropped after this long. */
export const PROPOSAL_TTL_MS = 60_000;
/** Use works only after the proposal has been on screen this long. */
export const USE_DELAY_MS = 800;
/** The longest transcript the page shows or sends back for matching. */
export const TRANSCRIPT_MAX = 300;
/** The longest clip, in seconds, as the page words it. */
export const MAX_SECONDS = 10;

// -- reading loosely typed answers

// Control and format characters (bidi overrides, zero-width marks) make a text
// look different from what it is; none belongs to anything shown here.
const INVISIBLE = /[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/gu;

/** A text from the server or the recogniser, made safe to show: invisible and
 * control characters removed, runs of white space folded, cut at `max`. */
export function plain(value: unknown, max = 200): string | null {
  if (typeof value !== "string") return null;
  return str(value.replace(INVISIBLE, " ").replace(/\s+/g, " "), max);
}

/** Task values are identifiers: never clean, fold or truncate them. */
function exactPrompt(value: unknown): string | null {
  return typeof value === "string" &&
    value.length > 0 &&
    value.length <= TRANSCRIPT_MAX &&
    value === value.trim() &&
    !/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(value)
    ? value
    : null;
}

export type EngineState =
  | "unavailable"
  | "off"
  | "loading"
  | "ready"
  | "busy"
  | "failed";
const ENGINE_STATES: EngineState[] = [
  "unavailable",
  "off",
  "loading",
  "ready",
  "busy",
  "failed",
];

export interface SpeechStatus {
  allowedRevision: string | null;
  state: EngineState;
  /** The server's reason (a stable code and an English sentence). */
  reason: { code: string; text: string } | null;
  installed: { worker: boolean; model: boolean };
  model: string | null;
  maxSeconds: number;
  gate: { allowed: boolean; reasons: string[] };
}

/** `GET /api/levi/marvel0/speech`, or null when it is not in the format this
 * page was written for. */
export function readSpeechStatus(data: unknown): SpeechStatus | null {
  const body = rec(data);
  if (!body || body.schema !== STATUS_SCHEMA) return null;
  const state = ENGINE_STATES.find((entry) => entry === body.state);
  const gate = rec(body.gate);
  const installed = rec(body.installed);
  const allowed = bool(gate?.allowed);
  if (!state || allowed === null || !installed) return null;
  const reason = rec(body.reason);
  const code = plain(reason?.code, 60);
  return {
    state,
    allowedRevision: plain(body.allowed_revision, 64),
    reason: code ? { code, text: plain(reason?.text, 300) ?? "" } : null,
    installed: {
      worker: bool(installed.worker) === true,
      model: bool(installed.model) === true,
    },
    model: plain(body.model, 80),
    maxSeconds: num(rec(body.limits)?.max_seconds) ?? MAX_SECONDS,
    gate: {
      allowed,
      reasons: list(gate?.reasons)
        .map((entry) => plain(entry, 40))
        .filter((entry): entry is string => entry !== null),
    },
  };
}

export type Tier = "A" | "B" | "C" | "V";
export type Flag = "stop" | "negation" | "unsafe";

export interface Fix {
  heard: string;
  as: string;
}
export interface Difference {
  code: string;
  said: string;
  expected: string;
}
export interface Candidate {
  /** One sentence of the allowed list. */
  prompt: string;
  score: number;
  fixes: Fix[];
  differences: Difference[];
}
export interface Match {
  revision: string | null;
  tier: Tier;
  flag: Flag | null;
  family: "restricted" | "all";
  best: Candidate | null;
  alternatives: Candidate[];
  /** Every sentence of the allowed list (Use can also hand over the
   * generated sentence below, labelled as such). */
  allowed: string[];
  /** A sentence written in the datasets' most common phrasing, or why there
   * is none (owner decision 2026-10-30). Absent from older answers. */
  generated?: Generated | null;
}

export type GeneratedStatus =
  | "ok"
  | "vetoed"
  | "restricted"
  | "no_task"
  | "unsupported"
  | "no_frame"
  | "untranslated"
  | "unclear";
export interface GeneratedSlot {
  said: string;
  english: string;
  /** "repair": the server guessed this word from a likely recognition
   * mistake; the card shows it as a guess. */
  source: "lexicon" | "repair" | "english" | "translation";
}
export interface GeneratedRepair {
  heard: string;
  as: string;
}
export interface Generated {
  status: GeneratedStatus;
  /** Null unless `status` is "ok". Not in the allowed list by design. */
  prompt: string | null;
  frames: string[];
  episodes: number;
  slots: GeneratedSlot[];
  untranslated: string[];
  /** A dataset instruction with the same meaning, offered beside. */
  datasetOriginal: string | null;
  /** Every word the server guessed (heard -> read as). */
  repairs: GeneratedRepair[];
}

const GENERATED_STATUSES: GeneratedStatus[] = [
  "ok",
  "vetoed",
  "restricted",
  "no_task",
  "unsupported",
  "no_frame",
  "untranslated",
  "unclear",
];
const SLOT_SOURCES: GeneratedSlot["source"][] = [
  "lexicon",
  "repair",
  "english",
  "translation",
];

function readGenerated(value: unknown): Generated | null {
  const item = rec(value);
  const status = GENERATED_STATUSES.find((entry) => entry === item?.status);
  if (!item || !status) return null;
  const prompt = status === "ok" ? exactPrompt(item.prompt) : null;
  if (status === "ok" && !prompt) return null;
  return {
    status,
    prompt,
    frames: list(item.frames)
      .map((entry) => plain(entry, 80))
      .filter((entry): entry is string => entry !== null)
      .slice(0, 4),
    episodes: Math.max(0, Math.floor(num(item.episodes) ?? 0)),
    slots: list(item.slots)
      .flatMap((entry) => {
        const slot = rec(entry);
        const said = plain(slot?.said, 40);
        const english = plain(slot?.english, 40);
        const source = SLOT_SOURCES.find((s) => s === slot?.source);
        return said && english && source ? [{ said, english, source }] : [];
      })
      .slice(0, 8),
    untranslated: list(item.untranslated)
      .map((entry) => plain(entry, 40))
      .filter((entry): entry is string => entry !== null)
      .slice(0, 8),
    datasetOriginal:
      status === "ok" ? exactPrompt(item.dataset_original) : null,
    repairs: list(item.repairs)
      .flatMap((entry) => {
        const repair = rec(entry);
        const heard = plain(repair?.heard, 40);
        const as = plain(repair?.as, 40);
        return heard && as ? [{ heard, as }] : [];
      })
      .slice(0, 8),
  };
}

const TIERS: Tier[] = ["A", "B", "C", "V"];
const FLAGS: Flag[] = ["stop", "negation", "unsafe"];
const MAX_ALLOWED = 200;

function readCandidate(value: unknown, allowed: Set<string>): Candidate | null {
  const item = rec(value);
  const prompt = exactPrompt(item?.prompt);
  // A sentence outside the allowed list is never offered.
  if (!item || !prompt || !allowed.has(prompt)) return null;
  return {
    prompt,
    score: Math.min(100, Math.max(0, num(item.score) ?? 0)),
    fixes: list(item.fixes)
      .flatMap((entry) => {
        const fix = rec(entry);
        const heard = plain(fix?.heard, 80);
        const as = plain(fix?.as, 80);
        return heard && as ? [{ heard, as }] : [];
      })
      .slice(0, 4),
    differences: list(item.differences)
      .flatMap((entry) => {
        const difference = rec(entry);
        const code = plain(difference?.code, 40);
        return code
          ? [
              {
                code,
                said: plain(difference?.said, 80) ?? "",
                expected: plain(difference?.expected, 80) ?? "",
              },
            ]
          : [];
      })
      .slice(0, 4),
  };
}

/** The match of an answer, or null when it cannot be trusted: another format,
 * or a tier that promises a sentence the answer does not carry. A flag that is
 * not one of the known ones is read as `unsafe`, and any flag makes the result
 * a V result whatever tier it says. */
export function readMatch(data: unknown): Match | null {
  const body = rec(data);
  const tier = TIERS.find((entry) => entry === body?.tier);
  const family =
    body?.family === "restricted" || body?.family === "all"
      ? body.family
      : null;
  if (!body || !tier || !family) return null;
  let flag: Flag | null = null;
  if (body.flag !== null && body.flag !== undefined) {
    flag = FLAGS.find((entry) => entry === body.flag) ?? "unsafe";
  }
  const allowed = [
    ...new Set(
      list(body.allowed)
        .map(exactPrompt)
        .filter((entry): entry is string => entry !== null),
    ),
  ].slice(0, MAX_ALLOWED);
  const set = new Set(allowed);
  const best = readCandidate(body.best, set);
  const alternatives = list(body.alternatives)
    .map((entry) => readCandidate(entry, set))
    .filter((entry): entry is Candidate => entry !== null)
    .slice(0, 2);
  const match: Match = {
    tier,
    flag,
    family,
    best,
    alternatives,
    allowed,
    revision: plain(body.allowed_revision, 64),
    generated: readGenerated(body.generated),
  };
  const effective = effectiveTier(match);
  if ((effective === "A" || effective === "B") && !best) return null;
  return match;
}

/** A flag or a V tier means no candidates, whatever else the answer says. */
export const effectiveTier = (match: Match): Tier =>
  match.flag !== null || match.tier === "V" ? "V" : match.tier;

/** The generated sentence and its dataset original, when Use may hand them
 * over: never for a V result or the restricted family, whatever the answer
 * says about them. */
export function generatedPrompts(match: Match): string[] {
  const generated = match.generated;
  if (
    !generated ||
    generated.status !== "ok" ||
    !generated.prompt ||
    effectiveTier(match) === "V" ||
    match.family !== "all"
  )
    return [];
  return [
    ...new Set(
      [generated.prompt, generated.datasetOriginal].filter(
        (entry): entry is string => entry !== null,
      ),
    ),
  ];
}

export type HeardReason =
  | "no_speech"
  | "language"
  | "low_confidence"
  | "empty"
  | "too_short"
  | "other";
const HEARD_REASONS: HeardReason[] = [
  "no_speech",
  "language",
  "low_confidence",
  "empty",
  "too_short",
];

export interface Heard {
  /** The model heard speech it could read. */
  heard: boolean;
  reason: HeardReason | null;
  transcript: string;
  language: string | null;
  model: string | null;
  match: Match | null;
}

/** The answer of `POST speech/transcribe` for the request `requestId`. A
 * request id that comes back as another one is not this request's answer. */
export function readHeard(data: unknown, requestId: string): Heard | null {
  const body = rec(data);
  if (
    !body ||
    body.schema !== TRANSCRIBE_SCHEMA ||
    body.ok !== true ||
    body.request_id !== requestId
  )
    return null;
  const heard = bool(body.heard);
  if (heard === null) return null;
  const model = plain(rec(body.engine)?.model, 80);
  if (!heard) {
    const reason = plain(body.reason, 40);
    return {
      heard: false,
      reason: HEARD_REASONS.find((entry) => entry === reason) ?? "other",
      transcript: "",
      language: null,
      model,
      match: null,
    };
  }
  const transcript = plain(body.transcript, TRANSCRIPT_MAX);
  const match = readMatch(body.match);
  // Heard something, but no usable match: not an answer to show.
  if (!transcript || !match) return null;
  const language = plain(body.language, 20);
  return {
    heard: true,
    reason: null,
    transcript,
    language:
      language === "English" || language === "Chinese" ? language : null,
    model,
    match,
  };
}

/** The answer of `POST speech/match`. */
export function readMatched(data: unknown): Match | null {
  const body = rec(data);
  if (!body || body.schema !== MATCH_SCHEMA) return null;
  return readMatch(body.match);
}

// -- the proposal

export interface Proposal {
  /** The sequence number of the request it came from. */
  id: number;
  transcript: string;
  /** The text was typed or edited by the person, not heard. */
  edited: boolean;
  language: string | null;
  model: string | null;
  match: Match;
  /** The model of the form this match was made for. */
  forModel: string;
  shownAt: number;
  /** Use may be pressed (the 800 ms are over). */
  armed: boolean;
  /** An edited text or a model switch is being matched again. */
  checking: boolean;
  picked: string | null;
  /** "Not this one" / "None of these": the whole allowed list is shown. */
  showAll: boolean;
  editing: boolean;
  draft: string;
  /** The 10 s limit ended the recording, so the end may be missing. */
  truncated: boolean;
}

export type ProposalView = "A" | "B" | "list" | "V";

export function proposalView(proposal: Proposal): ProposalView {
  const tier = effectiveTier(proposal.match);
  if (tier === "V") return "V";
  if (proposal.showAll || tier === "C") return "list";
  return tier;
}

/** The three sentences of a B result: the best and the alternatives. */
export function topChoices(match: Match): string[] {
  const all = [match.best, ...match.alternatives]
    .filter((entry): entry is Candidate => entry !== null)
    .map((entry) => entry.prompt);
  return [...new Set(all)].slice(0, 3);
}

/** The sentences the person can choose from right now. */
export function visiblePrompts(proposal: Proposal): string[] {
  switch (proposalView(proposal)) {
    case "V":
      return [];
    case "A":
      return proposal.match.best ? [proposal.match.best.prompt] : [];
    case "B":
      return topChoices(proposal.match);
    default:
      return proposal.match.allowed;
  }
}

/** The candidate a sentence belongs to, for its fixes and differences. */
export function candidateOf(match: Match, prompt: string): Candidate | null {
  return (
    [match.best, ...match.alternatives].find((c) => c?.prompt === prompt) ??
    null
  );
}

// -- the state machine

export type Phase = "idle" | "recording" | "uploading" | "proposal" | "error";

export type Problem =
  | { kind: "mic"; code: CaptureErrorCode }
  | { kind: "api"; failure: ApiFailure }
  | { kind: "format" };

export type Notice =
  | { kind: "expired" }
  | { kind: "used" }
  | { kind: "heard"; reason: HeardReason };

export interface SpeechState {
  phase: Phase;
  /** The newest request; an answer for another number is stale. */
  seq: number;
  /** Recording was asked for and the microphone has not delivered yet (the
   * browser may be asking the person). */
  opening: boolean;
  proposal: Proposal | null;
  problem: Problem | null;
  notice: Notice | null;
}

export const initialSpeech: SpeechState = {
  phase: "idle",
  seq: 0,
  opening: false,
  proposal: null,
  problem: null,
  notice: null,
};

export type SpeechAction =
  | { type: "begin"; seq: number }
  | { type: "recording" }
  | { type: "stopped" }
  | { type: "uploading"; seq: number }
  | { type: "mic_problem"; code: CaptureErrorCode }
  | {
      type: "answer";
      seq: number;
      at: number;
      heard: Heard;
      forModel: string;
      truncated: boolean;
    }
  | { type: "failed"; seq: number; problem: Problem }
  | { type: "check"; seq: number }
  | { type: "cancel_use"; id: number }
  | {
      type: "checked";
      seq: number;
      at: number;
      transcript: string;
      match: Match;
      forModel: string;
      edited: boolean;
    }
  | { type: "arm"; id: number }
  | { type: "expire"; id: number }
  | { type: "pick"; prompt: string }
  | { type: "show_all" }
  | { type: "edit"; on: boolean }
  | { type: "draft"; text: string }
  | { type: "used" };

function proposalOf(
  fields: Pick<
    Proposal,
    | "id"
    | "transcript"
    | "edited"
    | "language"
    | "model"
    | "match"
    | "forModel"
    | "truncated"
  > & { at: number },
): Proposal {
  const { at, ...rest } = fields;
  return {
    ...rest,
    shownAt: at,
    armed: false,
    checking: false,
    picked: null,
    showAll: false,
    editing: false,
    draft: rest.transcript,
  };
}

export function speechReducer(
  state: SpeechState,
  action: SpeechAction,
): SpeechState {
  switch (action.type) {
    case "begin":
      return {
        phase: "recording",
        seq: action.seq,
        opening: true,
        proposal: null,
        problem: null,
        notice: null,
      };
    case "recording":
      return state.phase === "recording" ? { ...state, opening: false } : state;
    case "stopped":
      return state.phase === "recording"
        ? { ...state, phase: "idle", opening: false }
        : state;
    case "uploading":
      return action.seq === state.seq
        ? { ...state, phase: "uploading", opening: false }
        : state;
    case "mic_problem":
      return {
        ...state,
        phase: "error",
        opening: false,
        proposal: null,
        problem: { kind: "mic", code: action.code },
      };
    case "answer": {
      if (action.seq !== state.seq || state.phase !== "uploading") return state;
      const { heard } = action;
      if (!heard.heard || !heard.match)
        return {
          ...state,
          phase: "idle",
          notice: { kind: "heard", reason: heard.reason ?? "other" },
        };
      return {
        ...state,
        phase: "proposal",
        proposal: proposalOf({
          id: action.seq,
          transcript: heard.transcript,
          edited: false,
          language: heard.language,
          model: heard.model,
          match: heard.match,
          forModel: action.forModel,
          truncated: action.truncated,
          at: action.at,
        }),
      };
    }
    case "failed":
      if (action.seq !== state.seq) return state;
      return {
        ...state,
        phase: "error",
        proposal: null,
        problem: action.problem,
      };
    case "check":
      if (!state.proposal) return { ...state, seq: action.seq };
      return {
        ...state,
        seq: action.seq,
        proposal: { ...state.proposal, checking: true, armed: false },
      };
    case "cancel_use":
      // Only an already armed Use request can be cancelled this way. Matching
      // a changed model/transcript still requires its own fresh response.
      return state.proposal?.id === action.id
        ? {
            ...state,
            proposal: { ...state.proposal, checking: false, armed: true },
          }
        : state;
    case "checked": {
      if (action.seq !== state.seq) return state;
      return {
        ...state,
        phase: "proposal",
        problem: null,
        proposal: proposalOf({
          id: action.seq,
          transcript: action.transcript,
          edited: action.edited,
          language: action.edited ? null : (state.proposal?.language ?? null),
          model: action.edited ? null : (state.proposal?.model ?? null),
          match: action.match,
          forModel: action.forModel,
          truncated: action.edited
            ? false
            : (state.proposal?.truncated ?? false),
          at: action.at,
        }),
      };
    }
    case "arm":
      return state.proposal?.id === action.id && !state.proposal.checking
        ? { ...state, proposal: { ...state.proposal, armed: true } }
        : state;
    case "expire":
      return state.proposal?.id === action.id
        ? {
            ...state,
            phase: "idle",
            proposal: null,
            notice: { kind: "expired" },
          }
        : state;
    case "pick":
      return state.proposal &&
        visiblePrompts(state.proposal).includes(action.prompt)
        ? { ...state, proposal: { ...state.proposal, picked: action.prompt } }
        : state;
    case "show_all":
      return state.proposal
        ? {
            ...state,
            proposal: { ...state.proposal, showAll: true, picked: null },
          }
        : state;
    case "edit":
      return state.proposal
        ? {
            ...state,
            proposal: {
              ...state.proposal,
              editing: action.on,
              draft: state.proposal.transcript,
            },
          }
        : state;
    case "draft":
      return state.proposal
        ? { ...state, proposal: { ...state.proposal, draft: action.text } }
        : state;
    case "used":
      return {
        ...state,
        phase: "idle",
        proposal: null,
        problem: null,
        notice: { kind: "used" },
      };
  }
}

/** Whether Use may hand `prompt` over now. The button is disabled before the
 * 800 ms, and this is checked again at the press, so a press that slipped
 * through (a key that ended the recording, a late event) does nothing. */
export function canUse(
  state: SpeechState,
  prompt: string,
  now: number,
): boolean {
  const proposal = state.proposal;
  if (state.phase !== "proposal" || !proposal) return false;
  const age = now - proposal.shownAt;
  return (
    proposal.armed &&
    !proposal.checking &&
    age >= USE_DELAY_MS &&
    age < PROPOSAL_TTL_MS &&
    ((visiblePrompts(proposal).includes(prompt) &&
      proposal.match.allowed.includes(prompt)) ||
      generatedPrompts(proposal.match).includes(prompt))
  );
}

// -- words

const MIC_KEY: Record<CaptureErrorCode, string> = {
  insecure_context: "marvel0.speech.mic.insecure",
  unsupported: "marvel0.speech.mic.unsupported",
  denied: "marvel0.speech.mic.denied",
  no_device: "marvel0.speech.mic.no_device",
  busy: "marvel0.speech.mic.busy",
  not_ready: "marvel0.speech.mic.not_ready",
  too_short: "marvel0.speech.mic.too_short",
  silent: "marvel0.speech.mic.silent",
  audio_graph: "marvel0.speech.mic.audio_graph",
  disconnected: "marvel0.speech.mic.disconnected",
  encode: "marvel0.speech.mic.encode",
  error: "marvel0.speech.mic.error",
};

export function micSay(code: CaptureErrorCode): Say {
  return { key: MIC_KEY[code] };
}

/** The codes of the voice routes (design record 5.3). The ones every marvel0
 * route can answer with (`network`, `person_only`, `ui_token`, `site_*`,
 * `request_id_invalid`, `validation`, `http_*`) are worded by marvel0-failure. */
const SPEECH_CODE_KEY: Record<string, string> = {
  speech_allowed_set_invalid: "marvel0.err.speech.allowed_set_invalid",
  speech_body_invalid: "marvel0.err.speech.body_invalid",
  speech_not_installed: "marvel0.err.speech.not_installed",
  speech_model_missing: "marvel0.err.speech.model_missing",
  speech_policy_starting: "marvel0.err.speech.policy_starting",
  speech_client_connected: "marvel0.err.speech.client_connected",
  speech_arm_in_use: "marvel0.err.speech.arm_in_use",
  speech_gpu_insufficient: "marvel0.err.speech.gpu_insufficient",
  speech_gpu_unreadable: "marvel0.err.speech.gpu_unreadable",
  speech_busy: "marvel0.err.speech.busy",
  audio_too_large: "marvel0.err.speech.audio_too_large",
  audio_format: "marvel0.err.speech.audio_format",
  audio_too_short: "marvel0.err.speech.audio_too_short",
  speech_failed: "marvel0.err.speech.failed",
  speech_timeout: "marvel0.err.speech.timeout",
  speech_manifest_mismatch: "marvel0.err.speech.manifest_mismatch",
};
export const SPEECH_CODES = Object.keys(SPEECH_CODE_KEY);

/** A refusal of a voice route in words; a code the page does not know shows
 * the server's own sentence (English) and is marked so by the caller. */
export function failureWords(failure: ApiFailure): Worded<Say> {
  const key = SPEECH_CODE_KEY[failure.code];
  return key ? { say: { key }, text: failure.message } : sayFailure(failure);
}

/** The server's reason for an unavailable voice input, by its code (with or
 * without the `speech_` prefix). */
export function reasonWords(
  reason: { code: string; text: string } | null,
): Worded<Say> | null {
  if (!reason) return null;
  const key =
    SPEECH_CODE_KEY[reason.code] ?? SPEECH_CODE_KEY[`speech_${reason.code}`];
  return { say: key ? { key } : null, text: reason.text };
}

const GATE_KEY: Record<string, string> = {
  allowed_set_invalid: "marvel0.speech.gate.allowed_set_invalid",
  policy_starting: "marvel0.speech.gate.policy_starting",
  client_connected: "marvel0.speech.gate.client_connected",
  arm_in_use: "marvel0.speech.gate.arm_in_use",
  gpu_insufficient: "marvel0.speech.gate.gpu_insufficient",
  gpu_unreadable: "marvel0.speech.gate.gpu_unreadable",
};
export const GATE_REASONS = Object.keys(GATE_KEY);

export type StatusRead =
  | { kind: "loading" }
  | { kind: "ok"; status: SpeechStatus }
  | { kind: "problem"; failure: ApiFailure | null };

export interface Availability {
  /** The microphone button may be pressed. */
  usable: boolean;
  /** Why it may not, each as a line of the card. */
  reasons: Array<Worded<Say>>;
  /** The speech model is not installed: the one-line install hint. */
  install: boolean;
}

const say = (key: string): Worded<Say> => ({ say: { key }, text: "" });

/** What the status allows. Fail closed: while the state is unknown, or when it
 * cannot be read, the button stays off. */
export function availability(read: StatusRead): Availability {
  if (read.kind === "loading")
    return {
      usable: false,
      reasons: [say("marvel0.speech.checking")],
      install: false,
    };
  if (read.kind === "problem")
    return {
      usable: false,
      reasons: [
        say("marvel0.speech.status_failed"),
        ...(read.failure ? [failureWords(read.failure)] : []),
      ],
      install: false,
    };
  const { status } = read;
  if (!status.installed.worker || !status.installed.model)
    return { usable: false, reasons: [], install: true };
  if (status.state === "unavailable")
    return {
      usable: false,
      reasons: [
        reasonWords(status.reason) ?? say("marvel0.speech.unavailable"),
      ],
      install: false,
    };
  if (!status.gate.allowed)
    return {
      usable: false,
      reasons: status.gate.reasons.length
        ? status.gate.reasons.map(
            (code): Worded<Say> =>
              GATE_KEY[code] ? say(GATE_KEY[code]) : { say: null, text: code },
          )
        : [say("marvel0.speech.gate.generic")],
      install: false,
    };
  return { usable: true, reasons: [], install: false };
}

const ENGINE_KEY: Record<EngineState, string> = {
  unavailable: "marvel0.speech.engine.unavailable",
  off: "marvel0.speech.engine.off",
  loading: "marvel0.speech.engine.loading",
  ready: "marvel0.speech.engine.ready",
  busy: "marvel0.speech.engine.busy",
  failed: "marvel0.speech.engine.failed",
};
export const engineSay = (state: EngineState): Say => ({
  key: ENGINE_KEY[state],
});

const HEARD_KEY: Record<HeardReason, string> = {
  no_speech: "marvel0.speech.heard.no_speech",
  language: "marvel0.speech.heard.language",
  low_confidence: "marvel0.speech.heard.low_confidence",
  empty: "marvel0.speech.heard.empty",
  too_short: "marvel0.speech.heard.too_short",
  other: "marvel0.speech.heard.other",
};

/** What a result without a usable transcript says. */
export function heardSay(reason: HeardReason): Say {
  return { key: HEARD_KEY[reason] };
}

export function languageSay(language: string | null): Say {
  return {
    key:
      language === "English"
        ? "marvel0.speech.lang.english"
        : language === "Chinese"
          ? "marvel0.speech.lang.chinese"
          : "marvel0.speech.lang.unknown",
  };
}

const DIFFERENCE_KEY: Record<string, string> = {
  missing_entity: "marvel0.speech.diff.missing_entity",
  extra_entity: "marvel0.speech.diff.extra_entity",
  color_mismatch: "marvel0.speech.diff.color_mismatch",
  size_mismatch: "marvel0.speech.diff.size_mismatch",
  order: "marvel0.speech.diff.order",
  action: "marvel0.speech.diff.action",
  steps: "marvel0.speech.diff.steps",
  unknown_word: "marvel0.speech.diff.unknown_word",
};
export const DIFFERENCE_CODES = Object.keys(DIFFERENCE_KEY);

/** What differs between what was heard and a sentence: the matcher's fixes
 * ("read X as Y") first, then its differences, each worded from its code. */
export function describeCandidate(candidate: Candidate): Say[] {
  return [
    ...candidate.fixes.map(
      (fix): Say => ({
        key: "marvel0.speech.fix",
        params: { heard: fix.heard, as: fix.as },
      }),
    ),
    ...candidate.differences.map(
      (difference): Say => ({
        key: DIFFERENCE_KEY[difference.code] ?? "marvel0.speech.diff.other",
        params: {
          said: difference.said || "?",
          expected: difference.expected || "?",
        },
      }),
    ),
  ];
}

export interface SayPart {
  text: string;
  /** A value from the transcript or the server: shown in <bdi>, never as markup. */
  value: boolean;
}

/** A sentence of the catalogue cut into its words and the values put into its
 * placeholders, so the page can set the values apart (a transcript may hold
 * text that reorders what is around it). */
export function sayParts(
  text: string,
  params: Record<string, string | number | Say> | undefined,
  nested: (say: Say) => string,
): SayPart[] {
  const parts: SayPart[] = [];
  let last = 0;
  for (const match of text.matchAll(/\{(\w+)\}/g)) {
    const name = match[1];
    if (!params || !(name in params)) continue;
    if (match.index > last)
      parts.push({ text: text.slice(last, match.index), value: false });
    const value = params[name];
    parts.push(
      typeof value === "object"
        ? { text: nested(value), value: false }
        : { text: String(value), value: true },
    );
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), value: false });
  return parts;
}
