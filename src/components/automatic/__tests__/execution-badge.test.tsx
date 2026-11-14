import { render, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { plan as launchPlan } from "./fixtures";
import type { LaunchPlan as WizardPlan } from "../wizard-types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { ExecutionBadge, ModeChips } = await import("../run-parts");
const { PlanCard: RunPlanCard } = await import("../launch-plan");
const { PlanCard: WizardPlanCard } = await import("../wizard-plan");
const { CampaignOverview } = await import("../campaign-overview");
setupDom();

const badgeWith = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll(".ds-badge")).find((b) =>
    b.textContent?.includes(text),
  )!;

describe("one ExecutionBadge for 'does this move the robot'", () => {
  test("a robot-moving mode is a warning with an icon; dry run and shadow are neutral", async () => {
    const { host } = await render(
      <>
        <ExecutionBadge execution="autonomous" />
        <ExecutionBadge execution="assisted" />
        <ExecutionBadge execution="dry_run" />
        <ExecutionBadge execution="shadow" />
      </>,
    );
    const badges = Array.from(host.querySelectorAll(".ds-badge"));
    expect(badges.map((b) => b.className.match(/ds-badge--(\w+)/)![1])).toEqual(
      ["warning", "warning", "neutral", "neutral"],
    );
    for (const badge of badges)
      expect(badge.querySelector("svg")).not.toBeNull();
  });

  test("the run list chips use it", async () => {
    const { host } = await render(
      <>
        <ModeChips execution="dry_run" reset="human_assisted" />
        <ModeChips execution="autonomous" reset="human_assisted" />
      </>,
    );
    expect(badgeWith(host, "Dry run").className).toContain("ds-badge--neutral");
    expect(badgeWith(host, "Autonomous").className).toContain(
      "ds-badge--warning",
    );
  });

  test("the launch plan's motion row uses it", async () => {
    const { host, rerender } = await render(
      <RunPlanCard
        plan={launchPlan({ motion: false })}
        now={0}
        launching={false}
        onLaunch={() => {}}
      />,
    );
    const row = () =>
      Array.from(host.querySelectorAll("dt")).find((d) =>
        d.textContent?.includes("Moves the robot"),
      )!.nextElementSibling!;
    expect(row().querySelector(".ds-badge--neutral")).not.toBeNull();
    await rerender(
      <RunPlanCard
        plan={launchPlan({ motion: true })}
        now={0}
        launching={false}
        onLaunch={() => {}}
      />,
    );
    expect(row().querySelector(".ds-badge--warning")).not.toBeNull();
  });

  test("the wizard plan's motion badge is neutral without motion, no longer success", async () => {
    const plan = (motion: boolean) =>
      ({
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
        motion,
        launch_token: "t",
        token_expires_at: Date.now() + 60_000,
      }) as unknown as WizardPlan;
    const { host, rerender } = await render(
      <WizardPlanCard plan={plan(false)} now={Date.now()} />,
    );
    expect(badgeWith(host, "The robot does not move").className).toContain(
      "ds-badge--neutral",
    );
    await rerender(<WizardPlanCard plan={plan(true)} now={Date.now()} />);
    expect(badgeWith(host, "The robot would move").className).toContain(
      "ds-badge--warning",
    );
  });

  test("the campaign page's mode badge: dry run neutral, guided warning", async () => {
    const campaign = (mode: string) => ({
      id: "camp-1",
      state: "ENV_CONFIRM",
      execution_mode: mode,
      arms: [],
      segment: { no: 1, total: 1, arm_code: "X" },
      todo: null,
      safety: { faults: 0, fused: false },
      blinded: true,
      controller: { alive: true },
      peeks: 0,
      wait_reason: null,
    });
    const api = (mode: string) =>
      ({
        getCampaign: async () => campaign(mode),
        getCampaignCards: async () => ({ pending: [] }),
      }) as never;
    const dry = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api("dry_run")}
        intervalMs={5000}
      />,
    );
    await waitFor(() => badgeWith(dry.host, "Dry run"));
    expect(badgeWith(dry.host, "Dry run").className).toContain(
      "ds-badge--neutral",
    );
    const guided = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api("guided")}
        intervalMs={5000}
      />,
    );
    await waitFor(() => badgeWith(guided.host, "Guided"));
    expect(badgeWith(guided.host, "Guided").className).toContain(
      "ds-badge--warning",
    );
  });
});
