import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { PoolJobProgress } from "../pool-progress";
import type { PoolJob } from "../types";

setupDom();

const job = (over: Partial<PoolJob> & { done?: number } = {}): PoolJob => {
  const { done = 10, ...rest } = over;
  return {
    id: "export-1",
    kind: "export",
    status: "running",
    progress: {
      stages: ["Plan", "Copy", "Verify"],
      stage: "Copy",
      stage_index: 1,
      done,
      total: 100,
      current: "",
      warnings: [],
      elapsed_seconds: 5,
      eta_seconds: 20,
    },
    ...rest,
  } as PoolJob;
};

describe("a job's progress for a screen reader", () => {
  test("the block is not a live region; one status names the stage", async () => {
    const { host, rerender } = await render(<PoolJobProgress job={job()} />);
    expect(host.querySelector("[aria-live]")).toBeNull();
    const statuses = host.querySelectorAll("[role=status]");
    expect(statuses).toHaveLength(1);
    expect(statuses[0].textContent).toBe("Copy");
    // Every second the numbers move; the announcement does not.
    await rerender(<PoolJobProgress job={job({ done: 11 })} />);
    expect(host.querySelector("[role=status]")!.textContent).toBe("Copy");
    // A new stage is announced; a terminal state is announced.
    const next = job();
    next.progress!.stage_index = 2;
    next.progress!.stage = "Verify";
    await rerender(<PoolJobProgress job={next} />);
    expect(host.querySelector("[role=status]")!.textContent).toBe("Verify");
    await rerender(
      <PoolJobProgress job={{ ...next, status: "failed" } as PoolJob} />,
    );
    expect(host.querySelector("[role=status]")!.textContent).toBe("failed");
  });

  test("done, current and to do chips differ by more than colour", async () => {
    const { host } = await render(<PoolJobProgress job={job()} />);
    const [plan, copy, verify] = [...host.querySelectorAll(".pg-steps li")];
    expect(plan.querySelector("svg")).not.toBeNull();
    expect(plan.textContent).toContain("(done)");
    expect(plan.hasAttribute("aria-current")).toBe(false);
    expect(copy.getAttribute("aria-current")).toBe("step");
    expect(copy.textContent).toContain("(In progress)");
    expect(verify.querySelector("svg")).toBeNull();
    expect(verify.textContent).toContain("(to do)");
    expect(verify.hasAttribute("aria-current")).toBe(false);
  });
});
