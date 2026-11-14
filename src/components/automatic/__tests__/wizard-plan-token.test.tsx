import { render, setupDom } from "@/components/ds/__tests__/dom";
import { describe, expect, test } from "bun:test";
import { PlanCard } from "../wizard-plan";
import type { LaunchPlan } from "../wizard-types";

setupDom();

const plan = {
  plan_sha256: "p".repeat(64),
  run_id: "run-x",
  execution_mode: "dry_run",
  reset_mode: "human_assisted",
  roles: ["forward"],
  episodes: 3,
  launchable: true,
  refusals: [],
  checks: [],
  isc: null,
  uses_live: { c5: false, gate: false },
  motion: false,
  launch_token: "t",
  token_expires_at: 100_000,
} as unknown as LaunchPlan;

describe("the plan's token countdown", () => {
  test("the ticking text is not a live region; only the expiry is announced", async () => {
    const { host, rerender } = await render(
      <PlanCard plan={plan} now={70_000} />,
    );
    const live = () => Array.from(host.querySelectorAll("[role=status]"));
    const ticking = Array.from(host.querySelectorAll("p")).find((p) =>
      p.textContent?.includes("30 more seconds"),
    )!;
    expect(ticking).toBeTruthy();
    expect(ticking.closest("[role=status]")).toBeNull();
    expect(live().map((n) => n.textContent)).toEqual([""]);
    await rerender(<PlanCard plan={plan} now={85_000} />);
    expect(live().map((n) => n.textContent)).toEqual([""]);
    await rerender(<PlanCard plan={plan} now={100_000} />);
    expect(live().map((n) => n.textContent)).toEqual([
      "The plan has expired. Plan again.",
    ]);
    expect(host.textContent).not.toContain("more seconds");
  });
});
