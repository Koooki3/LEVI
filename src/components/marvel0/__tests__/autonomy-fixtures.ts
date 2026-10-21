// The documents of Step 5 as the backend writes them. The named views are the
// backend's own samples: tests/marvel0/fixtures/aeri_run.sample.json (package
// A1, `GET .../aeri/run`, one view per state and notable variant) and
// aeri_ready.sample.json (package A2, `GET .../aeri`, the prepare, live-stop and
// leftovers answers and refusals), so a change of the backend's format fails
// the page's tests and not only the backend's. When the backend's regeneration
// scripts have written their copies next to the other samples
// (`marvel0-aeri-run.samples.json`, `marvel0-aeri.samples.json`) those are read
// instead. A test changes parts of a copy.
import { existsSync, readFileSync } from "fs";
import { join } from "path";
import type { AutonomyApi } from "../autonomy-api";
import { errorOf, type ApiResult } from "../deploy-api";

const root = join(import.meta.dir, "../../../..");
const here = import.meta.dir;

/* eslint-disable @typescript-eslint/no-explicit-any */
export type Doc = Record<string, any>;

function load(candidates: string[]): { path: string; value: Doc } {
  for (const path of candidates)
    if (existsSync(path))
      return { path, value: JSON.parse(readFileSync(path, "utf8")) as Doc };
  throw new Error(`none of ${candidates.join(", ")} exists`);
}

const RUN_FILE = load([
  join(here, "marvel0-autonomy-run.samples.json"),
  join(here, "marvel0-aeri-run.samples.json"),
  join(root, "tests/marvel0/fixtures/aeri_run.sample.json"),
]);
const READY_FILE = load([
  join(here, "marvel0-autonomy.samples.json"),
  join(here, "marvel0-aeri.samples.json"),
  join(root, "tests/marvel0/fixtures/aeri_ready.sample.json"),
]);
const ANSWERS_FILE = load([join(here, "marvel0-aeri-run.answers.json")]);
const REVIEW_FILE = load([join(here, "autonomy-review-docs.json")]);
const CODES_FILE = load([join(root, "tests/marvel0/fixtures/aeri_codes.json")]);

/** The backend's own answers to the writes of the hosted terminal D and its
 * refusals (`marvel0-aeri-run.answers.json`, from the real code). */
export const RUN_ANSWERS: Doc = ANSWERS_FILE.value.answers;
export const RUN_REFUSALS: Record<string, { status: number; detail: Doc }> =
  ANSWERS_FILE.value.refusals;
export const RUN_SAMPLE_PATH = RUN_FILE.path;
export const READY_SAMPLE_PATH = READY_FILE.path;
export const CODES: Doc = CODES_FILE.value;

const runs: Record<string, Doc> = RUN_FILE.value.samples ?? RUN_FILE.value;
const readies: Record<string, Doc> = READY_FILE.value.get ?? {};

export const RUN_VIEWS = Object.keys(runs);
export const READY_VIEWS = Object.keys(readies);
export const PREPARE: Doc = READY_FILE.value.prepare ?? {};
export const LIVE_STOP: Doc = READY_FILE.value.live_stop ?? {};
export const LEFTOVERS_STOP: Doc = READY_FILE.value.leftovers_stop ?? {};

/** A copy of one named view of `GET .../aeri/run`. */
export function runDoc(name = "idle", change?: (doc: Doc) => void): Doc {
  const doc = structuredClone(runs[name]);
  if (!doc) throw new Error(`no run view ${name}`);
  change?.(doc);
  return doc;
}

/** A copy of a document the second SAFETY review recorded from the real
 * `Supervisor.view()` (autonomy-review-docs.json says how). */
export type ReviewDoc =
  | "p1_just_adopted"
  | "p1_after_ack_window"
  | "p3_site_broken";
export function reviewDoc(name: ReviewDoc, change?: (doc: Doc) => void): Doc {
  const doc = structuredClone(REVIEW_FILE.value.docs[name]) as Doc;
  change?.(doc);
  return doc;
}

/** A copy of one named view of `GET .../aeri`. A sample that only lists "the
 * parts that differ from prepared_ready" (`readiness_rows` replace the rows of
 * the same code) is laid over that one. */
export function readyDoc(
  name = "prepared_ready",
  change?: (doc: Doc) => void,
): Doc {
  const raw = readies[name];
  if (!raw) throw new Error(`no ready view ${name}`);
  let doc: Doc;
  if (raw.schema) doc = structuredClone(raw);
  else {
    doc = structuredClone(readies.prepared_ready);
    const {
      note: _note,
      readiness_rows: rows = [],
      ...rest
    } = structuredClone(raw);
    void _note;
    Object.assign(doc, rest);
    for (const row of rows as Doc[]) {
      const at = (doc.readiness as Doc[]).findIndex((r) => r.code === row.code);
      if (at >= 0) doc.readiness[at] = row;
      else doc.readiness.push(row);
    }
  }
  change?.(doc);
  return doc;
}

/** Every key of a run view, by id. */
export const keyOf = (doc: Doc, id: string): Doc | undefined =>
  (doc.keys as Doc[]).find((key) => key.id === id);

export const ok = (data: unknown, status = 200): ApiResult => ({
  ok: true,
  status,
  data,
});
export const refusal = (
  status: number,
  code: string,
  extra: Record<string, unknown> = {},
  message = "The server's English sentence",
): ApiResult => errorOf(status, { detail: { code, message, ...extra } });
export const noAnswer = (): ApiResult => ({
  ok: false,
  status: 0,
  code: "network",
  message: "fetch failed",
  extra: {},
  field: null,
  fields: [],
});

export interface Call {
  op: string;
  body: Record<string, unknown>;
}

/** A client that answers from the current documents and records every call. A
 * write answers like the backend's accepted answer unless `answer` changes it.
 * Reads never throw; a read set to null answers 503. */
export function fakeAutonomy(initial: {
  run?: Doc | null;
  ready?: Doc | null;
}) {
  const state = {
    run: (initial.run === undefined
      ? runDoc("idle")
      : initial.run) as Doc | null,
    ready: (initial.ready === undefined
      ? readyDoc("prepared_ready")
      : initial.ready) as Doc | null,
    calls: [] as Call[],
    order: [] as string[],
    reads: { run: 0, ready: 0 },
    /** Answers the reads with this refusal instead of the documents. */
    runFail: null as ApiResult | null,
    readyFail: null as ApiResult | null,
    /** Held back until released (a write on its way). */
    gate: null as null | Promise<void>,
    answer: ((call: Call): ApiResult | Promise<ApiResult> => {
      switch (call.op) {
        case "challenge":
          return ok({ ...RUN_ANSWERS.challenge, challenge: "tok-start" });
        case "presence":
          return ok(null, 204);
        case "prepare":
          return ok(PREPARE.ok?.body ?? { done: true });
        default:
          return ok({ accepted: true }, 202);
      }
    }) as (call: Call) => ApiResult | Promise<ApiResult>,
  };
  const write = (op: string) => async (body: unknown) => {
    const call = { op, body: body as Record<string, unknown> };
    state.calls.push(call);
    state.order.push(op);
    if (state.gate) await state.gate;
    return state.answer(call);
  };
  const api: AutonomyApi = {
    ready: async () => {
      state.reads.ready += 1;
      if (state.readyFail) return state.readyFail;
      return state.ready
        ? ok(structuredClone(state.ready))
        : errorOf(503, { detail: { code: "unavailable", message: "x" } });
    },
    run: async () => {
      state.reads.run += 1;
      if (state.runFail) return state.runFail;
      return state.run
        ? ok(structuredClone(state.run))
        : errorOf(503, { detail: { code: "unavailable", message: "x" } });
    },
    prepare: write("prepare"),
    liveStop: write("live_stop"),
    leftoversStop: write("leftovers_stop"),
    challenge: write("challenge"),
    start: write("start"),
    key: write("key"),
    stop: write("stop"),
    end: write("end"),
    presence: write("presence"),
    log: async () => errorOf(404, {}),
  };
  return { api, state };
}

/** The calls of one kind. */
export const callsOf = (
  state: { calls: Call[] },
  op: string,
): Record<string, unknown>[] =>
  state.calls.filter((call) => call.op === op).map((call) => call.body);
