import { flush, render, setupDom, waitFor } from "../ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import AgentTaskConsole from "../agent-task-console";
import AgentPilot from "../agent-pilot";

setupDom();

const original = globalThis.fetch;
const originalInterval = globalThis.setInterval;
const originalSource = (globalThis as { EventSource?: unknown }).EventSource;
afterEach(() => {
  globalThis.fetch = original;
  globalThis.setInterval = originalInterval;
  (globalThis as { EventSource?: unknown }).EventSource = originalSource;
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
  Object.defineProperty(document, "visibilityState", {
    value: "visible",
    configurable: true,
  });
});

const json = (body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body)));

const task = (status: string) => ({
  id: "task-7",
  status,
  request: "review the cups",
  spec: { dataset: "local/demo", episodes: [0, 1] },
  problems: [],
  steps: [],
  interpretation: { tokens: 10, token_source: "model", seconds: 1 },
});

const PROVIDERS = [{ name: "local-llm", kind: "ollama" }];

/** `tasks.get` answers with `current.status`; the test counts the calls. */
function serve(current: { status: string }) {
  const counts = { get: 0 };
  globalThis.fetch = mock((_url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.name === "tasks.get") {
      counts.get += 1;
      return json(task(current.status));
    }
    return json({});
  }) as unknown as typeof fetch;
  localStorage.setItem("levi-agent-task", "task-7");
  return counts;
}
function pollFast() {
  globalThis.setInterval = ((fn: () => void, ms?: number) =>
    originalInterval(fn, ms === 3000 ? 20 : ms)) as typeof setInterval;
}

describe("the task console's live region", () => {
  test("only the id and status line is announced, not the whole spec", async () => {
    serve({ status: "awaiting_approval" });
    const { host } = await render(<AgentTaskConsole providers={PROVIDERS} />);
    await waitFor(() => host.textContent?.includes("task-7"), {
      label: "the task",
    });
    expect(host.querySelector('[aria-live="polite"]')).toBeNull();
    const status = host.querySelectorAll('[role="status"]');
    expect(status).toHaveLength(1);
    expect(status[0].textContent).toContain("task-7");
    expect(status[0].textContent).not.toContain("episodes");
    expect(host.querySelector("pre")!.closest('[role="status"]')).toBeNull();
  });

  test("the Pilot event list is not a live region", async () => {
    (globalThis as { EventSource?: unknown }).EventSource = class {
      close() {}
    };
    globalThis.fetch = mock((url: string) =>
      String(url).endsWith("/tools")
        ? json({ events: [] })
        : String(url).endsWith("/manifest")
          ? json({ status: "empty", artifacts: [] })
          : json([]),
    ) as unknown as typeof fetch;
    const { host } = await render(
      <AgentPilot
        runId="r1"
        approved
        repo="local/x"
        edited={false}
        follow={false}
      />,
    );
    await flush(20);
    const events = host.querySelector(".ag-pilot-events")!;
    expect(events.getAttribute("aria-live")).not.toBe("polite");
    expect(host.querySelector('[aria-live="polite"]')).toBeNull();
  });
});

describe("following a task that is running", () => {
  test("reads tasks.get while approved or running, and stops once it is done", async () => {
    pollFast();
    const current = { status: "running" };
    const counts = serve(current);
    const { host } = await render(<AgentTaskConsole providers={PROVIDERS} />);
    await waitFor(() => host.textContent?.includes("task-7"), {
      label: "the task",
    });
    const first = counts.get;
    await waitFor(() => counts.get >= first + 2, { label: "polling" });
    current.status = "succeeded";
    await waitFor(() => host.textContent?.includes("succeeded"), {
      label: "the finished state",
    });
    await flush(60);
    const settled = counts.get;
    await flush(150);
    expect(counts.get).toBe(settled);
  });

  test("a task that waits for approval is not polled", async () => {
    pollFast();
    const counts = serve({ status: "awaiting_approval" });
    const { host } = await render(<AgentTaskConsole providers={PROVIDERS} />);
    await waitFor(() => host.textContent?.includes("task-7"), {
      label: "the task",
    });
    await flush(150);
    expect(counts.get).toBe(1);
  });

  test("a hidden tab is not polled", async () => {
    pollFast();
    const counts = serve({ status: "running" });
    const { host } = await render(<AgentTaskConsole providers={PROVIDERS} />);
    await waitFor(() => host.textContent?.includes("task-7"), {
      label: "the task",
    });
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    });
    await flush(60);
    const at = counts.get;
    await flush(150);
    expect(counts.get).toBe(at);
  });
});
