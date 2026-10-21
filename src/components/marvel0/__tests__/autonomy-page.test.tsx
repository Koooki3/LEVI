import { afterEach, describe, expect, mock, test } from "bun:test";
import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import { Marvel0CardView } from "../marvel0-card";
import type { Marvel0Card } from "../marvel0-logic";
import { quietMarvel0, quietSetup } from "./fixtures";
import { readyDoc, runDoc } from "./autonomy-fixtures";

// Step 5 on the page itself: the mount hands it what the page knows (whether
// the evaluation client of step 4 is live), and nothing else of the page is
// needed, so the other steps are stood in for as the status-card tests do.
let evalState: "idle" | "running" = "idle";
const idlePoll = {
  loaded: false,
  value: null,
  current: false,
  problem: null,
  lastOk: null,
  refresh: async () => {},
};
mock.module("@/components/marvel0/use-stack-poll", () => ({
  STACK_FRESH_MS: 8000,
  useStackPoll: () => idlePoll,
}));
mock.module("@/components/marvel0/use-run-poll", () => ({
  RUN_POLL_MS: 1000,
  RUN_FRESH_MS: 5000,
  useRunPoll: () => ({
    ...idlePoll,
    lastSeen:
      evalState === "running" ? { state: "running", mayMove: true } : null,
  }),
}));
mock.module("@/components/marvel0/stack-panel", () => ({
  StackPanel: ({ cards }: { cards: Marvel0Card[] }) => (
    <>
      {cards.map((card) => (
        <Marvel0CardView key={card.id} card={card} />
      ))}
    </>
  ),
}));
mock.module("@/components/marvel0/marvel0-actions", () => ({
  Marvel0Actions: () => null,
}));
const { default: Marvel0Page } = await import("@/app/marvel0/page");
setupDom();

const E = en as Record<string, string>;
let restore = () => {};
afterEach(() => {
  restore();
  restore = () => {};
  evalState = "idle";
});

function core(answers: Record<string, unknown>) {
  const original = globalThis.fetch;
  const calls: string[] = [];
  globalThis.fetch = (async (input: unknown, init?: RequestInit) => {
    const path = String(input);
    calls.push(`${init?.method ?? "GET"} ${path}`);
    if (!(path in answers)) return new Response("{}", { status: 404 });
    return new Response(JSON.stringify(answers[path]), { status: 200 });
  }) as typeof fetch;
  restore = () => {
    globalThis.fetch = original;
  };
  return calls;
}
const allMet = () =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
  });
const answers = () => ({
  "/api/levi/setup/status": quietSetup(),
  "/api/levi/marvel0/status": quietMarvel0(),
  "/api/levi/marvel0/aeri": allMet(),
  "/api/levi/marvel0/aeri/run": runDoc("idle"),
});
const start = (host: HTMLElement) =>
  host.querySelector(
    "[data-testid=marvel0-autonomy-start]",
  ) as HTMLButtonElement | null;

describe("Step 5 on the marvel0 page", () => {
  test("it is the fifth step, after the run, and reads only its two routes", async () => {
    const calls = core(answers());
    const { host } = await render(<Marvel0Page />);
    await waitFor(() => start(host), { label: "the Start button of step 5" });
    const heading = host.querySelector("#marvel0-step-autonomy")!;
    expect(heading.textContent).toBe(`Step 5${E["marvel0.autonomy.step"]}`);
    expect(start(host)!.disabled).toBe(false);
    const reads = new Set(
      calls.filter((c) => c.includes("/aeri")).map((c) => c.split(" ")[1]),
    );
    expect(reads).toEqual(
      new Set(["/api/levi/marvel0/aeri", "/api/levi/marvel0/aeri/run"]),
    );
    expect(calls.filter((c) => c.startsWith("POST"))).toEqual([]);
  });

  test("while the evaluation client of step 4 is live, Start is off and says why: only one of the two sessions can be alive", async () => {
    evalState = "running";
    core(answers());
    const { host } = await render(<Marvel0Page />);
    await waitFor(() => start(host), { label: "the Start button of step 5" });
    expect(start(host)!.disabled).toBe(true);
    expect(
      host.querySelector("[data-testid=marvel0-autonomy-start-off]")!
        .textContent,
    ).toContain(E["marvel0.autonomy.local.eval_live"]);
  });

  test("the chain at the top follows a D that lives: the run node is not 'not started' and the strip says what D holds", async () => {
    // No client run (the run poll is idle), a D in the middle of a round.
    core({
      ...answers(),
      "/api/levi/marvel0/aeri/run": runDoc("forward_running"),
    });
    const { host } = await render(<Marvel0Page />);
    await waitFor(
      () =>
        start(host) || host.querySelector("[data-testid=marvel0-autonomy-run]"),
      {
        label: "step 5",
      },
    );
    await waitFor(
      () =>
        host
          .querySelector("[data-testid=marvel0-strip-run]")
          ?.getAttribute("data-state") === "ready",
      { label: "the run node to follow D" },
    );
    // With D launching, the node is working and the line under the chain says why.
    expect(
      host
        .querySelector("[data-testid=marvel0-strip-run]")!
        .getAttribute("data-state"),
    ).toBe("ready");
  });

  test("with no D alive the run node is what the client's document says", async () => {
    core(answers());
    const { host } = await render(<Marvel0Page />);
    await waitFor(() => start(host), { label: "the Start button of step 5" });
    expect(
      host
        .querySelector("[data-testid=marvel0-strip-run]")!
        .getAttribute("data-state"),
    ).not.toBe("ready");
  });
});
