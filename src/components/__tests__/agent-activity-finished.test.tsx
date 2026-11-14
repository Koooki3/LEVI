import { act } from "react";
import { flush, render, setupDom } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import AgentActivity, { type AgentTask } from "../agent-activity";

setupDom();

const task = (id: string, committed: boolean): AgentTask => ({
  run_id: id,
  dataset: "local/demo",
  workflow: "review",
  instruction: "x",
  episodes: [0],
  completed: [0],
  progress: 1,
  status: committed ? "succeeded" : "running",
  waiting_for: "",
  finished: committed,
  committed,
  evidence_ready: true,
  revision: null,
  tokens: null,
  token_source: null,
  usage_missing: false,
  evidence_frames: null,
  requests: null,
  artifacts: [],
  last_action: null,
});

let sources: Array<{
  onmessage: ((event: { data: string }) => void) | null;
}> = [];
const originalFetch = globalThis.fetch;
const originalSource = (globalThis as { EventSource?: unknown }).EventSource;
afterEach(() => {
  globalThis.fetch = originalFetch;
  (globalThis as { EventSource?: unknown }).EventSource = originalSource;
  sources = [];
});

/** The task list answers with whatever `current.tasks` holds. */
const current: { tasks: AgentTask[] } = { tasks: [] };
function stub(tasks: AgentTask[]) {
  current.tasks = tasks;
  globalThis.fetch = mock((url: string) => {
    const json = (value: unknown) =>
      Promise.resolve(new Response(JSON.stringify(value)));
    if (String(url).includes("/activity/tasks"))
      return json({ tasks: current.tasks });
    return json({ events: [] });
  }) as unknown as typeof fetch;
  (globalThis as { EventSource?: unknown }).EventSource = class {
    onopen = null;
    onerror = null;
    onmessage: ((event: { data: string }) => void) | null = null;
    constructor() {
      sources.push(this);
    }
    close() {}
  };
}

/** One journal event makes the panel look at the task list again. */
async function nudge(seq: number) {
  await act(async () => {
    sources[0].onmessage?.({
      data: JSON.stringify({
        seq,
        at: 1,
        tool: "runs.finish",
        action: "run",
        channel: "agent",
        principal: "p",
        status: "completed",
        dataset: "local/demo",
        run: "old",
        elapsed_seconds: 1,
        detail: {},
        artifacts: [],
        error: null,
      }),
    });
  });
  await flush(20);
}

describe("Task finished is announced only for a task seen finishing", () => {
  test("opening the tab with an old committed task announces nothing", async () => {
    stub([task("old", true)]);
    const { host } = await render(<AgentActivity open />);
    await flush(30);
    expect(host.textContent).not.toContain("Task finished");
    // A later look at the same, still committed task stays quiet too.
    await nudge(1);
    expect(host.textContent).not.toContain("Task finished");
  });

  test("a task that goes from running to committed while watched is announced", async () => {
    stub([task("t1", false)]);
    const { host } = await render(<AgentActivity open />);
    await flush(30);
    expect(host.textContent).not.toContain("Task finished");
    current.tasks = [task("t1", true)];
    await nudge(1);
    expect(host.textContent).toContain("Task finished");
  });
});
