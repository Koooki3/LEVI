import { click, fire, render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import type { DatasetRow, LiveSession } from "../types";

const rows: Record<string, DatasetRow> = {
  pi05__watermelon: {
    episodes: 2,
    pending: 0,
    annotating: 0,
    done: 2,
    failed: 0,
    state: "idle",
    group: "pi05",
    task_folder: "watermelon",
  },
  pi06__plates: {
    episodes: 2,
    pending: 1,
    annotating: 0,
    done: 1,
    failed: 0,
    state: "pending",
    group: "pi06",
    task_folder: "plates",
  },
};
const sessions: LiveSession[] = [
  {
    group: "pi05",
    task_folder: "watermelon",
    state: "finished",
    run_id: "run-1",
  },
  { group: "pi06", task_folder: "plates", state: "running", run_id: "run-2" },
  {
    group: "hidden-fault-model",
    task_folder: "fault",
    state: "fault",
    reason: "global fault must stay visible",
  },
];
let wanted: string[] = [];
let pollOverride: Record<string, unknown> = {};
mock.module("../use-live", () => ({
  useLivePoll: () => ({
    ...basePoll(),
    ...pollOverride,
  }),
  useDatasetDetails: (names: string[]) => {
    wanted = names;
    return { details: {}, refresh: () => {} };
  },
}));
function basePoll() {
  return {
    status: {
      enabled: true,
      alive: true,
      service: { datasets: rows, sessions, state: "idle" },
    },
    sessions: { enabled: true, sessions },
    lastOk: Date.now(),
    failures: 0,
    delay: 10000,
    error: "",
    refresh: () => {},
  };
}
let aggregateStatisticsRenders = 0;
mock.module("../stats-panel", () => ({
  StatsPanel: () => {
    aggregateStatisticsRenders += 1;
    return null;
  },
}));
mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { default: LivePage } = await import("@/app/live/page");
setupDom();

function field(host: HTMLElement, label: string): HTMLSelectElement {
  const match = Array.from(host.querySelectorAll("label")).find(
    (node) => node.textContent === label,
  )!;
  return document.getElementById(match.htmlFor) as HTMLSelectElement;
}
async function select(host: HTMLElement, label: string, value: string) {
  const control = field(host, label);
  control.value = value;
  await fire(control, new Event("change", { bubbles: true }));
}

describe("live page list integration", () => {
  test("statistics live in the session details, so overview never mounts an aggregate request", async () => {
    aggregateStatisticsRenders = 0;
    const { host } = await render(<LivePage />);
    expect(aggregateStatisticsRenders).toBe(0);
    expect(host.querySelector("#live-stats-region")).toBeNull();
    expect(host.querySelector('a[href="#live-stats-region"]')).toBeNull();
  });

  test("default collapsed rows request no dataset details; only an expanded visible row is wanted", async () => {
    const { host } = await render(<LivePage />);
    expect(wanted).toEqual([]);
    expect(
      Array.from(host.querySelectorAll("button[aria-expanded]")).every(
        (button) => button.getAttribute("aria-expanded") === "false",
      ),
    ).toBe(true);
    const pipeline = host.querySelector("#live-pipeline")!.closest("section")!;
    const toggle = Array.from(
      pipeline.querySelectorAll("button[aria-expanded]"),
    ).find((button) => button.textContent?.includes("pi05 / watermelon"))!;
    await click(toggle);
    expect(wanted).toEqual(["pi05__watermelon"]);
    await select(host, "Model", "pi06");
    expect(wanted).toEqual([]);
  });
  test("combined filters count both lists and never hide the global fault banner", async () => {
    const { host } = await render(<LivePage />);
    await select(host, "Model", "pi05");
    await select(host, "Task", "watermelon");
    await select(host, "Status", "finished");
    expect(
      host.querySelector("[aria-labelledby=live-sessions]")?.textContent,
    ).toContain("run-1");
    expect(
      host.querySelector("[aria-labelledby=live-sessions]")?.textContent,
    ).not.toContain("run-2");
    // One count format: "(shown / total)" in each section's heading.
    expect(host.querySelector("#live-sessions")?.textContent).toContain(
      "(1 / 3)",
    );
    expect(host.querySelector("#live-pipeline")?.textContent).toContain(
      "(1 / 2)",
    );
    expect(host.querySelector(".pg-live-banner")?.textContent).toContain(
      "global fault must stay visible",
    );
    const clear = Array.from(host.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Clear filters"),
    )!;
    await click(clear);
    expect(host.querySelector("#live-sessions")?.textContent).toContain(
      "(3 / 3)",
    );
  });
  test("no-match feedback and clear filters preserve access to the full lists", async () => {
    const { host } = await render(<LivePage />);
    await select(host, "Model", "pi05");
    await select(host, "Task", "plates");
    expect(host.textContent).toContain(
      "No evaluation sessions match these filters.",
    );
    expect(host.textContent).toContain(
      "No annotation pipelines match these filters.",
    );
    const clear = Array.from(host.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Clear filters"),
    )!;
    await click(clear);
    expect(field(host, "Model").value).toBe("");
    expect(field(host, "Task").value).toBe("");
    expect(host.textContent).not.toContain(
      "No evaluation sessions match these filters.",
    );
  });

  test("filtering keeps an expanded session open", async () => {
    pollOverride = {};
    const { host } = await render(<LivePage />);
    const sessionsBox = host.querySelector("[aria-labelledby=live-sessions]")!;
    const toggle = Array.from(
      sessionsBox.querySelectorAll("button[aria-expanded]"),
    ).find((button) => button.textContent?.includes("pi05 / watermelon"))!;
    await click(toggle);
    await select(host, "Model", "pi05");
    const again = Array.from(
      host
        .querySelector("[aria-labelledby=live-sessions]")!
        .querySelectorAll("button[aria-expanded]"),
    ).find((button) => button.textContent?.includes("pi05 / watermelon"))!;
    expect(again.getAttribute("aria-expanded")).toBe("true");
  });
});

describe("live page states", () => {
  test("before the first answer nothing claims a missing monitor or empty lists", async () => {
    pollOverride = { status: null, sessions: null, lastOk: null };
    const { host } = await render(<LivePage />);
    const text = host.textContent!;
    expect(text).not.toContain("Monitor not running");
    expect(text).not.toContain("No evaluation session is reporting");
    expect(text).not.toContain("No dataset has been seen yet.");
    expect(host.querySelector("[aria-busy=true]")).not.toBeNull();
    pollOverride = {};
  });

  test("a core that never answered says so, without false facts", async () => {
    pollOverride = {
      status: null,
      sessions: null,
      lastOk: null,
      failures: 1,
      error: "HTTP 502",
    };
    const { host } = await render(<LivePage />);
    const text = host.textContent!;
    expect(text).toContain("The LEVI core is not answering");
    expect(text).not.toContain("Monitor not running");
    expect(text).not.toContain("No evaluation session is reporting");
    pollOverride = {};
  });

  test("an active session with a silent monitor is said at the top", async () => {
    pollOverride = {
      sessions: {
        enabled: true,
        sessions: [{ group: "m", task_folder: "t", state: "running" }],
        fr3: { state: "offline" },
      },
      sessionsOkAt: 2,
      statusOkAt: 1,
    };
    const { host } = await render(<LivePage />);
    expect(host.textContent).toContain(
      "the FR3 health monitor is not reporting",
    );
    expect(host.querySelector(".pg-live-summary")?.textContent).toContain(
      "Monitor offline",
    );
    pollOverride = {};
  });

  test("the sections are in reading order, and the jump links name them as their headings do", async () => {
    pollOverride = {};
    const { host } = await render(<LivePage />);
    const links = Array.from(host.querySelectorAll(".pg-live-jump a")).map(
      (a) => a.getAttribute("href"),
    );
    expect(links).toEqual([
      "#live-sessions",
      "#live-pipeline",
      "#live-results-region",
      "#live-fr3",
      "#live-service-region",
    ]);
    const order = Array.from(
      host.querySelectorAll(".pg-live-layout .pg-live-area"),
    ).map((node) =>
      Array.from(node.classList)
        .find((c) => c.startsWith("pg-live-area--"))
        ?.slice("pg-live-area--".length),
    );
    expect(order).toEqual([
      "sessions",
      "pipeline",
      "results",
      "fr3",
      "service",
    ]);
    // The jump links follow the document order of their targets.
    const targets = links.map((href) => host.querySelector(href!)!);
    for (let i = 1; i < targets.length; i++)
      expect(
        targets[i - 1].compareDocumentPosition(targets[i]) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
  });
});
