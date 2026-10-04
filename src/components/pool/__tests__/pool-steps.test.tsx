import { render, setupDom } from "../../ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { PoolSteps, poolStep } from "../pool-steps";

setupDom();

describe("training pool step bar", () => {
  test("the step follows the composition and the export", () => {
    expect(poolStep(0, false)).toBe(1);
    expect(poolStep(2, false)).toBe(2);
    expect(poolStep(2, true)).toBe(3);
    expect(poolStep(0, true)).toBe(3);
  });

  test("three steps, the current one marked, earlier ones done", async () => {
    const { host } = await render(<PoolSteps current={2} />);
    const steps = Array.from(host.querySelectorAll("button.pg-step"));
    expect(steps.map((b) => b.textContent)).toEqual([
      "Select",
      "2Compose",
      "3Export",
    ]);
    expect(steps.map((b) => b.getAttribute("aria-current"))).toEqual([
      null,
      "step",
      null,
    ]);
    expect(steps.map((b) => b.getAttribute("data-state"))).toEqual([
      "done",
      "current",
      "todo",
    ]);
    expect(host.querySelector("nav")!.getAttribute("aria-label")).toBe(
      "Export steps",
    );
  });
});
