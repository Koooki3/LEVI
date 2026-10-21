// Helpers of the DOM tests of Step 5: the step rendered over a fake client, with
// short timings, and the few gestures it needs.
import { act } from "react";
import { render, waitFor } from "@/components/ds/__tests__/dom";
import { LocaleProvider } from "@/components/levi-locale";
import { AutonomyStep } from "../autonomy-step";
import { LIMITS, type Limits } from "../autonomy-logic";
import { tick } from "./actions-helpers";
import type { ApiResult } from "../deploy-api";
import { fakeAutonomy, type Doc } from "./autonomy-fixtures";

/** Short timings, so a countdown or a poll takes milliseconds. The real ones
 * are tested by their constants (autonomy-logic.test.ts) and once end to end
 * with the defaults (autonomy-run.test.tsx, 'the real delay'). */
export const FAST: Limits = {
  armDelayMs: 150,
  keyGapMs: 250,
  sentUnseenMs: 500,
  docStaleMs: 700,
  presenceMs: 60,
  runPollMs: 25,
  idlePollMs: 25,
  readyPollMs: 25,
  slowPollMs: 400,
};

export interface Open {
  /** Both reads are refused with this. */
  fail?: ApiResult;
  run?: Doc | null;
  ready?: Doc | null;
  limits?: Partial<Limits>;
  evalLive?: boolean;
  zh?: boolean;
}

export async function openStep({
  run,
  ready,
  limits = {},
  evalLive = false,
  zh = false,
  fail,
}: Open = {}) {
  if (zh) localStorage.setItem("levi-language", "zh");
  const f = fakeAutonomy({ run, ready });
  if (fail) {
    f.state.runFail = fail;
    f.state.readyFail = fail;
  }
  const element = (
    <AutonomyStep
      api={f.api}
      evalLive={evalLive}
      limits={{ ...FAST, ...limits }}
    />
  );
  const view = await render(
    zh ? <LocaleProvider>{element}</LocaleProvider> : element,
  );
  await waitFor(
    () =>
      f.state.reads.run > 0 &&
      f.state.reads.ready > 0 &&
      !view.host.querySelector("[aria-busy=true]"),
    { label: "the first reads" },
  );
  await tick(2, 5);
  return { ...view, ...f };
}

export const $ = (host: ParentNode, testId: string) =>
  host.querySelector(`[data-testid="${testId}"]`) as HTMLElement | null;
export const text = (node: Element | null) => node?.textContent ?? "";
export const dialog = () =>
  document.querySelector("[role=dialog]") as HTMLElement | null;
export const keyButton = (host: ParentNode, id: string) =>
  host.querySelector(`button[data-action="${id}"]`) as HTMLButtonElement | null;
export const shownKeys = (host: ParentNode) =>
  Array.from(host.querySelectorAll("div.rg-akey")).map((node) =>
    node.getAttribute("data-action"),
  );

/** The document the fake serves from now on (the run moves on). */
export async function serve(
  f: { state: { run: Doc | null } },
  run: Doc | null,
  wait = 4,
) {
  // The backend's state counter moves whenever the state does; the samples
  // all carry one fixed number, so a document served as "the next state" gets
  // a later one (a test that wants the same counter sets `f.state.run`).
  const before = f.state.run?.state?.seq;
  if (run && typeof before === "number" && run.state.seq <= before)
    run.state.seq = before + 1;
  f.state.run = run;
  await tick(wait, 30);
}

export async function wait(ms: number) {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
}

export { LIMITS };
