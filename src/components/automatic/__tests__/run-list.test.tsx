import { fire, render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { mockFetch } from "./fixtures";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { RunList } = await import("../run-list");
setupDom();

let restore = () => {};
afterEach(() => restore());

describe("the run list", () => {
  test("shows both labels of each run and links to its window", async () => {
    const m = mockFetch({
      "GET runs": () => ({
        runs: [
          {
            run_id: "run-a",
            state: "WAIT_HUMAN",
            reset_mode: "human_assisted",
            execution_mode: "dry_run",
            episodes_done: 2,
            episodes_total: 10,
          },
          {
            run_id: "run-b",
            state: "COMPLETED",
            reset_mode: "single_reset_policy",
            execution_mode: "dry_run",
            episodes_done: 10,
            episodes_total: 10,
          },
          {
            run_id: "run-c",
            state: "FORWARD_ACTIVE",
            reset_mode: "subtask_loop",
            execution_mode: "dry_run",
            episodes_done: 1,
            episodes_total: 3,
          },
        ],
      }),
    });
    restore = m.restore;
    const { host } = await render(<RunList />);
    await waitFor(() => host.querySelector(".ar-runs"));
    const text = host.textContent ?? "";
    expect(text).toContain("Manual reset (evaluated policy only)");
    expect(text).toContain("Reset policy");
    expect(text).toContain("Waiting for you");
    // A subtask loop counts rounds, not episodes.
    expect(text).toContain("Subtask loop (no reset policy)");
    expect(text).toContain("Rounds 1 of 3");
    expect(host.querySelector("a")!.getAttribute("href")).toBe(
      "/automatic/runs/run-a",
    );
  });
  test("an empty list says so", async () => {
    const m = mockFetch({ "GET runs": () => ({ runs: [] }) });
    restore = m.restore;
    const { host } = await render(<RunList />);
    await waitFor(() => host.textContent?.includes("No runs yet."));
  });

  test("marks an unfinished run whose journal went quiet, never a finished one", async () => {
    const m = mockFetch({
      "GET runs": () => ({
        runs: [
          {
            run_id: "run-old",
            state: "FORWARD_ACTIVE",
            reset_mode: "human_assisted",
            execution_mode: "dry_run",
            episodes_done: 1,
            episodes_total: 10,
            updated_at: Date.now() - 3 * 3_600_000,
          },
          {
            run_id: "run-fresh",
            state: "FORWARD_ACTIVE",
            reset_mode: "human_assisted",
            execution_mode: "dry_run",
            episodes_done: 1,
            episodes_total: 10,
            updated_at: Date.now() - 5_000,
          },
          {
            run_id: "run-done",
            state: "COMPLETED",
            reset_mode: "human_assisted",
            execution_mode: "dry_run",
            episodes_done: 10,
            episodes_total: 10,
            updated_at: Date.now() - 9 * 3_600_000,
          },
        ],
      }),
    });
    restore = m.restore;
    const { host } = await render(<RunList />);
    await waitFor(() => host.querySelector(".ar-runs"));
    const rows = Array.from(host.querySelectorAll(".ar-run"));
    expect(rows[0].textContent).toContain("No new event for 3 h 0 min");
    expect(rows[1].textContent).not.toContain("No new event");
    expect(rows[2].textContent).not.toContain("No new event");
  });

  test("keeps the last list on a failed poll and says how old it is", async () => {
    let calls = 0;
    const m = mockFetch({
      "GET runs": () => {
        calls += 1;
        if (calls > 1) return { __status: 500, body: { detail: "boom" } };
        return {
          runs: [
            {
              run_id: "run-a",
              state: "FORWARD_ACTIVE",
              reset_mode: "human_assisted",
              execution_mode: "dry_run",
              episodes_done: 2,
              episodes_total: 10,
            },
          ],
        };
      },
    });
    restore = m.restore;
    const { host } = await render(<RunList />);
    await waitFor(() => host.querySelector(".ar-runs"));
    await fire(document, new Event("visibilitychange"));
    await waitFor(() =>
      host.querySelector("[data-testid=poll-freshness][data-failing=true]"),
    );
    expect(host.querySelector(".ar-runs")).not.toBeNull();
    expect(
      host.querySelector("[data-testid=poll-freshness]")!.textContent,
    ).toContain("ago · retrying");
  });
});
