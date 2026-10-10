import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
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
});
