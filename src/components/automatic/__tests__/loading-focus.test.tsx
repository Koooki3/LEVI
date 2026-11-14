// A button that starts a request shows `loading` and nothing else: a native
// `disabled` on the clicked button would drop keyboard focus from it. The
// buttons next to it may be disabled.
import {
  click,
  flush,
  focus,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { card, mockFetch } from "./fixtures";
import type { CampaignSnapshot } from "../wizard-types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { CampaignOverview } = await import("../campaign-overview");
const { PendingCard } = await import("../pending-card");
const { CampaignReportView } = await import("../campaign-report");
const { ApiError } = await import("../wizard-api");
const { WizardRunConfirm } = await import("../wizard-run");
const { defaultForm } = await import("../wizard-logic");
setupDom();

let restore = () => {};
afterEach(() => restore());

const named = (host: ParentNode, text: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

const campaign = (over: Partial<CampaignSnapshot> = {}): CampaignSnapshot =>
  ({
    id: "camp-1",
    state: "ARM_RUNNING",
    execution_mode: "guided",
    arms: [],
    segment: { no: 2, total: 4, arm_code: "X2" },
    todo: null,
    safety: { faults: 0, fused: false },
    blinded: true,
    controller: { alive: true },
    peeks: 0,
    wait_reason: null,
    ...over,
  }) as CampaignSnapshot;

describe("the label buttons of the pending card", () => {
  test("the pressed one is only loading (it keeps focus), the others are disabled", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const m = mockFetch({ "POST runs/run-1/labels": () => gate });
    restore = m.restore;
    const { host } = await render(
      <PendingCard
        runId="run-1"
        card={card()}
        challenge="c"
        onLabelled={() => {}}
        onChanged={() => {}}
      />,
    );
    const success = named(host, "Success")!;
    await focus(success);
    await click(success);
    expect(success.getAttribute("aria-busy")).toBe("true");
    expect(success.hasAttribute("disabled")).toBe(false);
    expect(document.activeElement).toBe(success);
    expect(named(host, "Failure")!.hasAttribute("disabled")).toBe(true);
    release({ label: "success", revealed: false, card: card() });
    await waitFor(() => !success.getAttribute("aria-busy"));
  });
});

describe("the campaign page buttons", () => {
  test("Pause is only loading while the command runs, and the result is announced", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const api = {
      getCampaign: async () => campaign(),
      getCampaignCards: async () => ({ pending: [] }),
      campaignCommand: mock(() => gate),
    } as never;
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={api} intervalMs={5000} />,
    );
    await waitFor(() => named(host, "Pause at the segment end"));
    const pause = named(host, "Pause at the segment end")!;
    await focus(pause);
    await click(pause);
    expect(pause.getAttribute("aria-busy")).toBe("true");
    expect(pause.hasAttribute("disabled")).toBe(false);
    expect(document.activeElement).toBe(pause);
    release({ result: "applied" });
    await waitFor(() =>
      host
        .querySelector("section[aria-labelledby=ac-safety] [role=status]")
        ?.textContent?.includes("will pause at the end of this segment"),
    );
  });
  test("Resume is only loading while the command runs, and the result is announced", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const api = {
      getCampaign: async () => campaign({ state: "PAUSED" }),
      getCampaignCards: async () => ({ pending: [] }),
      campaignCommand: mock(() => gate),
    } as never;
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={api} intervalMs={5000} />,
    );
    await waitFor(() => named(host, "Resume"));
    const resume = named(host, "Resume")!;
    await click(resume);
    expect(resume.getAttribute("aria-busy")).toBe("true");
    expect(resume.hasAttribute("disabled")).toBe(false);
    release({ result: "applied" });
    await waitFor(() =>
      host
        .querySelector("section[aria-labelledby=ac-safety] [role=status]")
        ?.textContent?.includes("The campaign was resumed"),
    );
  });
  test("Attach is only loading while it runs", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const api = {
      getCampaign: async () =>
        campaign({ controller: { alive: false }, state: "ARM_RUNNING" }),
      getCampaignCards: async () => ({ pending: [] }),
      attachCampaign: mock(() => gate),
    } as never;
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={api} intervalMs={5000} />,
    );
    const label = await waitFor(() => host.querySelector(".pg-row button"));
    await click(label);
    expect(label.getAttribute("aria-busy")).toBe("true");
    expect(label.hasAttribute("disabled")).toBe(false);
    release({ campaign_id: "camp-1" });
    await flush(20);
  });
});

describe("the report generate button", () => {
  test("is only loading while the report is made", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const api = {
      getCampaignReport: mock(async () => {
        throw new ApiError(404, "no_report", "No report");
      }),
      getReportFileJson: mock(async () => ({})),
      getReportFileText: mock(async () => ""),
      generateCampaignReport: mock(() => gate),
    } as never;
    const { host } = await render(
      <CampaignReportView
        campaignId="camp-1"
        basis="autonomous_verdict"
        onBasisChange={() => {}}
        api={api}
      />,
    );
    const generate = await waitFor(() =>
      named(host, "Generate the report for this basis"),
    );
    await click(generate);
    expect(generate.getAttribute("aria-busy")).toBe("true");
    expect(generate.hasAttribute("disabled")).toBe(false);
    release({ result: "applied" });
    await flush(20);
  });
});

describe("the wizard's Plan again button", () => {
  test("is only loading while the plan is made again", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    let calls = 0;
    const plan = {
      plan_sha256: "a".repeat(64),
      run_id: "run-1",
      execution_mode: "dry_run",
      reset_mode: "human_assisted",
      roles: ["forward"],
      episodes: 5,
      launchable: true,
      refusals: [],
      checks: [],
      isc: null,
      uses_live: { c5: false, gate: false },
      motion: false,
      launch_token: "tok",
      token_expires_at: Date.now() + 120_000,
    };
    const api = {
      createJob: mock(async () => ({ id: "job-1" })),
      planLaunch: mock(() => (++calls === 1 ? Promise.resolve(plan) : gate)),
      launchRun: mock(async () => ({ run_id: "run-1" })),
    } as never;
    const { host } = await render(
      <WizardRunConfirm
        form={defaultForm()}
        policies={null}
        api={api}
        onLaunched={() => {}}
      />,
    );
    const again = await waitFor(() => {
      const b = named(host, "Plan again");
      return b && !b.getAttribute("aria-busy") ? b : null;
    });
    await focus(again);
    await click(again);
    expect(again.getAttribute("aria-busy")).toBe("true");
    expect(again.hasAttribute("disabled")).toBe(false);
    expect(document.activeElement).toBe(again);
    release(plan);
    await flush(20);
  });
});
