import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { mockFetch, snapshot } from "./fixtures";
import type { RunSnapshot } from "../types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { RunWindow } = await import("../run-window");
setupDom();

let restore = () => {};
afterEach(() => restore());

const button = (root: ParentNode, text: string) =>
  Array.from(root.querySelectorAll("button")).find(
    (node) => node.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

function open(
  snap: RunSnapshot,
  extra: Record<string, (body: unknown) => unknown> = {},
) {
  const m = mockFetch({
    "GET runs/run-1": () => snap,
    "GET runs/run-1/events?after=0&limit=200": () => ({
      events: [
        {
          seq: 1,
          at: 1000,
          kind: "transition",
          from: "PREFLIGHT",
          to: "VERIFY_INITIAL",
          reason: null,
        },
        {
          seq: 2,
          at: 2000,
          kind: "note",
          from: null,
          to: null,
          reason: "early_stop goal_verified",
        },
      ],
      next: 2,
    }),
    "GET runs/run-1/metrics": () => ({
      reset_mode: "human_assisted",
      comparable: ["autonomous"],
      autonomous: { success: { n: 3, of: 6, rate: 0.5, wilson95: [0.2, 0.8] } },
      agreement: {
        agreement: { n: 4, of: 5, rate: 0.8, wilson95: [0.4, 0.97] },
      },
    }),
    ...extra,
  });
  restore = m.restore;
  return m;
}

describe("the run window", () => {
  test("a manual-reset run greys the reset states and says why, in words", async () => {
    open(snapshot({ state: "FORWARD_ACTIVE" }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=RESET_ACTIVE]"));
    for (const state of ["RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"]) {
      const node = host.querySelector(`[data-state=${state}]`)!;
      expect(node.getAttribute("data-status")).toBe("disabled");
      expect(node.textContent).toContain("not used: you reset by hand");
    }
    const current = host.querySelector("[aria-current=step]")!;
    expect(current.getAttribute("data-state")).toBe("FORWARD_ACTIVE");
    expect(current.textContent).toContain("now");
    expect(host.textContent).toContain("Manual reset (evaluated policy only)");
    expect(host.textContent).toContain("Dry run");
  });
  test("a policy-reset run keeps the reset states available", async () => {
    open(
      snapshot({ state: "RESET_ACTIVE", reset_mode: "single_reset_policy" }),
    );
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=RESET_ACTIVE]"));
    expect(host.querySelector("[data-status=disabled]")).toBeNull();
    expect(host.textContent).toContain("Reset policy");
  });
  test("a run that has not written yet (404) is shown as starting", async () => {
    const m = mockFetch({});
    restore = m.restore;
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.textContent?.includes("The run is starting"));
  });
  test("stop asks first and sends one command with the phrase", async () => {
    const m = open(snapshot({ state: "FORWARD_ACTIVE" }), {
      "POST runs/run-1/stop": () => ({ result: "applied" }),
    });
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => button(host, "Stop run"));
    await click(button(host, "Stop run")!);
    const dialog = document.querySelector("[role=alertdialog]")!;
    await click(button(dialog, "Stop run")!);
    await waitFor(() => host.textContent?.includes("The stop was sent"));
    const stops = m.calls.filter((c) => c.path === "runs/run-1/stop");
    expect(stops.length).toBe(1);
    expect(stops[0].body).toMatchObject({ confirm: "stop" });
  });
});
