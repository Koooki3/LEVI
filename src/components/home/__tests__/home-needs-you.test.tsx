import { click, flush, render, setupDom } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";

mock.module("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {} }),
  usePathname: () => "/",
}));

const { HomeDashboard } = await import("../home-dashboard");
const { SHELL_EVENTS } = await import("@/components/shell/shell-events");

setupDom();

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const task = (n: number) => ({
  run_id: `run${n}`,
  dataset: `local/set${n}`,
  workflow: "review",
  instruction: "x",
  episodes: [0, 1],
  status: "waiting_for_review",
  waiting_for: "human_review",
  finished: false,
});

function serve(tasks: unknown[], running: unknown[] = []) {
  globalThis.fetch = mock((input: RequestInfo | URL) => {
    const path = String(input);
    return Promise.resolve(
      path.includes("/catalog")
        ? json({ local: [] })
        : path.includes("/pool/jobs")
          ? json({ jobs: [] })
          : path.includes("/activity/tasks")
            ? json({ tasks })
            : path.includes("/api/levi/jobs")
              ? json(running)
              : json([]),
    );
  }) as unknown as typeof fetch;
}

const rowButtons = (host: HTMLElement) =>
  [...host.querySelectorAll(".levi-home-list button")] as HTMLButtonElement[];

describe("Needs you", () => {
  test("a row opens the Workbench on its task through an open event", async () => {
    serve([task(1), task(2)]);
    const events: Array<{ type: string; runId?: string }> = [];
    const listen = (event: Event) =>
      events.push({
        type: event.type,
        runId: (event as CustomEvent).detail?.runId,
      });
    window.addEventListener(SHELL_EVENTS.agentOpenTask, listen);
    window.addEventListener(SHELL_EVENTS.agentToggle, listen);
    window.addEventListener(SHELL_EVENTS.agentConnections, listen);
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const buttons = rowButtons(host);
    expect(buttons).toHaveLength(2);
    // Same visible words on every row, so the name says which task.
    expect(buttons[1].getAttribute("aria-label")).toContain("local/set2");
    expect(buttons[0].getAttribute("aria-label")).not.toBe(
      buttons[1].getAttribute("aria-label"),
    );
    await click(buttons[1]);
    expect(events).toEqual([
      { type: SHELL_EVENTS.agentOpenTask, runId: "run2" },
    ]);
    // The card's own button opens the task tab too, not Accounts.
    await click(
      [...host.querySelectorAll("button")].find(
        (b) => b.textContent?.trim() === "Open the Agent Workbench",
      )!,
    );
    expect(events.map((e) => e.type)).toEqual([
      SHELL_EVENTS.agentOpenTask,
      SHELL_EVENTS.agentOpenTask,
    ]);
    window.removeEventListener(SHELL_EVENTS.agentOpenTask, listen);
    window.removeEventListener(SHELL_EVENTS.agentToggle, listen);
    window.removeEventListener(SHELL_EVENTS.agentConnections, listen);
  });

  test("seven tasks: five rows, the badge says seven and the rest is named", async () => {
    serve(Array.from({ length: 7 }, (_, i) => task(i)));
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    expect(rowButtons(host)).toHaveLength(5);
    const card = [...host.querySelectorAll(".levi-home-card")].find((c) =>
      c.textContent?.includes("Needs you"),
    )!;
    expect(card.querySelector(".levi-home-count")!.textContent).toBe("7");
    expect(card.textContent).toContain("and 2 more");
  });
});

describe("Try again", () => {
  test("shows a busy state while the read runs", async () => {
    let release: (value: Response) => void = () => {};
    let calls = 0;
    globalThis.fetch = mock(() => {
      calls += 1;
      // The first round fails at once; the retry waits for `release`.
      return calls <= 3
        ? Promise.resolve(json({ detail: "boom" }, 500))
        : new Promise<Response>((resolve) => {
            release = resolve;
          });
    }) as unknown as typeof fetch;
    const { host } = await render(<HomeDashboard />);
    await flush(50);
    const again = () =>
      [...host.querySelectorAll("button")].find(
        (b) => b.textContent?.trim() === "Try again",
      ) as HTMLButtonElement;
    expect(again().getAttribute("aria-busy")).toBeNull();
    await click(again());
    expect(again().getAttribute("aria-busy")).toBe("true");
    release(json({ detail: "boom" }, 500));
    await flush(50);
  });
});
