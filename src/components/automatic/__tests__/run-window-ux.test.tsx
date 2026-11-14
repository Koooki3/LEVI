import { fire, render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
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

function open(
  snap: RunSnapshot,
  extra: Record<string, (body: unknown) => unknown> = {},
) {
  const m = mockFetch({
    "GET runs/run-1": () => snap,
    "GET runs/run-1/events?after=0&limit=200": () => ({ events: [], next: 0 }),
    "GET runs/run-1/metrics": () => ({
      reset_mode: "human_assisted",
      comparable: [],
    }),
    ...extra,
  });
  restore = m.restore;
  return m;
}

describe("a run whose runner is gone", () => {
  test("is not drawn as a healthy run: danger runner badge and a status note", async () => {
    open(
      snapshot({ state: "FORWARD_ACTIVE", runner: { alive: false, pid: 1 } }),
    );
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=FORWARD_ACTIVE]"));
    const badges = Array.from(host.querySelectorAll(".ds-badge"));
    const runner = badges.find((b) =>
      b.textContent?.includes("Runner not running"),
    )!;
    expect(runner.className).toContain("ds-badge--danger");
    const state = badges.find((b) =>
      b.textContent?.includes("Policy running"),
    )!;
    expect(state.className).not.toContain("ds-badge--info");
    const note = host.querySelector("[role=status].ar-note--danger")!;
    expect(note.textContent).toContain("The runner has stopped");
    expect(note.textContent).toContain("last one the run wrote");
    expect(note.textContent).toContain("Check the robot");
  });
  test("a finished run with a stopped runner shows no alarm", async () => {
    open(snapshot({ state: "COMPLETED", runner: { alive: false } }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=COMPLETED]"));
    expect(host.querySelector(".ar-note--danger")).toBeNull();
    expect(host.textContent).not.toContain("The runner has stopped");
  });
  test("a live runner shows the plain alive badge", async () => {
    open(snapshot({ state: "FORWARD_ACTIVE" }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=FORWARD_ACTIVE]"));
    expect(host.querySelector(".ar-note--danger")).toBeNull();
    expect(host.textContent).toContain("Runner alive");
  });
});

describe("a run page whose polling fails", () => {
  test("says how old the data is, dims the alive badge and keeps Stop enabled", async () => {
    let calls = 0;
    open(snapshot({ state: "FORWARD_ACTIVE" }), {
      "GET runs/run-1": () => {
        calls += 1;
        if (calls === 1) return snapshot({ state: "FORWARD_ACTIVE" });
        return { __status: 503, body: { detail: "down" } };
      },
    });
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-testid=poll-freshness]"));
    const fresh = () => host.querySelector("[data-testid=poll-freshness]")!;
    expect(fresh().textContent).toMatch(/^Updated .* ago$/);
    expect(fresh().textContent).not.toContain("retrying");
    await fire(document, new Event("visibilitychange"));
    await waitFor(() => fresh().getAttribute("data-failing") === "true");
    expect(fresh().textContent).toContain("ago · retrying");
    const badges = Array.from(host.querySelectorAll(".ds-badge"));
    const alive = badges.find((b) => b.textContent?.includes("Runner alive"))!;
    expect(alive.textContent).toContain("(last known)");
    expect(alive.className).not.toContain("ds-badge--success");
    const stop = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "Stop run",
    )!;
    expect(stop.hasAttribute("disabled")).toBe(false);
    expect(stop.getAttribute("aria-disabled")).not.toBe("true");
  });
});
