// The camera preview's document (`GET .../preview`, levi/marvel0/preview.py) in
// the shape the Run card uses, and the words of its codes. Pure: no React and
// no fetching. A value of the wrong type makes the whole document null, which
// the card shows as "not readable", never a guess.
import { bool, num, rec, str, type Worded } from "./marvel0-json";
import type { Say } from "./marvel0-logic";
import { codedSay, type Known } from "./ctl-text";

export const PREVIEW_SCHEMA = "levi.marvel0.preview.v1";
export const PREVIEW_STATES = [
  "off",
  "starting",
  "on",
  "stopping",
  "refused",
  "failed",
] as const;
export type PreviewState = (typeof PREVIEW_STATES)[number];

/** Why a preview that was on ended without a failure (the backend's
 * `STOPPED_BY`); `requested` is the person's own Stop and needs no note. */
export const STOPPED_BY = [
  "requested",
  "idle",
  "max_lifetime",
  "client_start",
  "service",
] as const;
export type StoppedBy = (typeof STOPPED_BY)[number];

export interface PreviewView {
  state: PreviewState;
  /** Why it cannot start (`refused`) or why it failed (`failed`). */
  reason: { code: string; text: string } | null;
  /** A start would be taken now. */
  mayStart: boolean;
  stoppedBy: StoppedBy | null;
  limits: { idleS: number; maxS: number };
}

/** The states in which the cameras are held (pictures are asked for). */
export const holdsCameras = (state: PreviewState): boolean =>
  state === "starting" || state === "on" || state === "stopping";
/** The states in which the pictures are asked for. */
export const showsPictures = (state: PreviewState): boolean =>
  state === "starting" || state === "on";

function oneOf<T extends string>(set: readonly T[], value: unknown): T | null {
  return typeof value === "string" && (set as readonly string[]).includes(value)
    ? (value as T)
    : null;
}

export function previewView(data: unknown): PreviewView | null {
  const doc = rec(data);
  if (!doc || doc.schema !== PREVIEW_SCHEMA) return null;
  const state = oneOf(PREVIEW_STATES, doc.state);
  const mayStart = bool(doc.may_start);
  const limits = rec(doc.limits);
  const idleS = num(limits?.idle_s);
  const maxS = num(limits?.max_s);
  if (!state || mayStart === null || idleS === null || maxS === null)
    return null;
  const why = rec(doc.reason);
  const code = str(why?.code, 80);
  return {
    state,
    reason: code ? { code, text: str(why?.text, 400) ?? code } : null,
    mayStart,
    stoppedBy: oneOf(STOPPED_BY, doc.stopped_by),
    limits: { idleS, maxS },
  };
}

/** The sentences of the reasons: refusals to start and failures. */
export const REASON_PREFIX = ["marvel0.preview.reason."] as const;

/** A reason of the document in the page's words, else as the server wrote it. */
export function reasonWorded(
  reason: { code: string; text: string },
  known: Known,
): Worded<Say> {
  return codedSay(REASON_PREFIX, reason.code, reason.text, {}, known);
}
