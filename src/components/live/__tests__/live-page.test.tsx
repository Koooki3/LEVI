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
mock.module("../use-live", () => ({
  useLivePoll: () => ({
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
  }),
  useDatasetDetails: (names: string[]) => {
    wanted = names;
    return { details: {}, refresh: () => {} };
  },
}));
mock.module("../stats-panel", () => ({ StatsPanel: () => null }));
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
    ).find((button) =>
      button.getAttribute("aria-label")?.includes("pi05 / watermelon"),
    )!;
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
    expect(host.querySelector(".pg-live-filter-footer")?.textContent).toContain(
      "Showing 1 of 3 evaluation sessions",
    );
    expect(host.querySelector(".pg-live-filter-footer")?.textContent).toContain(
      "1 of 2 annotation pipelines",
    );
    expect(host.querySelector(".pg-live-banner")?.textContent).toContain(
      "global fault must stay visible",
    );
    const clear = Array.from(host.querySelectorAll("button")).find((button) =>
      button.textContent?.includes("Clear filters"),
    )!;
    await click(clear);
    expect(host.querySelector(".pg-live-filter-footer")?.textContent).toContain(
      "Showing 3 of 3 evaluation sessions",
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
});
