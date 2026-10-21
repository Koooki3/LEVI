// The documents of the arm stack's controls and of the hosted client as the
// backend writes them: the named views the backend's own regeneration scripts
// produce (tests/marvel0/regen_stack_act_sample.py and regen_run_sample.py),
// so a change of the backend's format fails the page's tests, not only the
// backend's. A test changes parts of a copy.
import runSamples from "./marvel0-run.samples.json";
import stackSamples from "./marvel0-stack-act.samples.json";

export const STACK = "/api/levi/marvel0/stack";
export const RUN = "/api/levi/marvel0/run";

type Doc = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any

const views = stackSamples.views as unknown as Record<string, Doc>;
const runs = runSamples as unknown as Record<string, Doc>;

export const STACK_VIEWS = Object.keys(views);
export const RUN_VIEWS = Object.keys(runs);
export const STACK_ANSWERS = stackSamples.answers as unknown as Record<
  string,
  Doc
>;
export const STACK_REFUSALS = stackSamples.refusals as unknown as Record<
  string,
  { status: number; detail: Doc }
>;

/** A copy of one named view of `GET /api/levi/marvel0/stack`. */
export function stackDoc(name = "idle", change?: (doc: Doc) => void): Doc {
  const doc = structuredClone(views[name]);
  if (!doc) throw new Error(`no stack view ${name}`);
  change?.(doc);
  return doc;
}

/** A copy of one named view of `GET /api/levi/marvel0/run`. */
export function runDoc(name = "idle_ready", change?: (doc: Doc) => void): Doc {
  const doc = structuredClone(runs[name]);
  if (!doc) throw new Error(`no run view ${name}`);
  change?.(doc);
  return doc;
}
