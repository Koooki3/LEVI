import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";
import { WizardPage } from "../wizard-page";
import { ApiError, type WizardApi } from "../wizard-api";
import type {
  Capabilities,
  LaunchPlan,
  PoliciesResponse,
} from "../wizard-types";

setupDom();

/** Type into a field (React under happy-dom takes change events from a keyup). */
async function type(field: Element, text: string) {
  const proto =
    field instanceof HTMLTextAreaElement
      ? HTMLTextAreaElement.prototype
      : HTMLInputElement.prototype;
  await act(async () => {
    (field as HTMLElement).focus();
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(field, text);
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(
      new KeyboardEvent("keyup", { bubbles: true, key: "e" }),
    );
  });
}

const buttonNamed = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

const capabilities: Capabilities = {
  adapters: [{ id: "fake", kind: "fake", available: true }],
  modes: {
    dry_run: { available: true },
    autonomous: {
      available: false,
      reason: "No robot adapter on this machine",
    },
  },
  reset_modes: ["human_assisted"],
  scene_checks: ["operator_attested", "provider"],
  defaults: { max_steps: 400, episodes: 5 },
};
const policies = (reset: boolean): PoliciesResponse => ({
  checkpoints: [
    {
      id: "p1",
      name: "pi05 sft",
      role: "forward",
      config: "pi05_cfg",
      sha256_status: "recorded",
      notes: ["not verified on a GPU server"],
    },
    {
      id: "p2",
      name: "pi05 recap",
      role: "forward",
      config: "pi05_cfg2",
      sha256_status: "verified",
      notes: [],
    },
    ...(reset
      ? [
          {
            id: "r1",
            name: "reset-v1",
            role: "reset" as const,
            config: "reset_cfg",
            sha256_status: "none" as const,
            notes: [],
          },
        ]
      : []),
  ],
  reset_available: reset,
});
const plan = (over: Partial<LaunchPlan> = {}): LaunchPlan => ({
  plan_sha256: "a".repeat(64),
  run_id: "run-1",
  execution_mode: "dry_run",
  reset_mode: "human_assisted",
  scene_check: "operator_attested",
  roles: ["forward"],
  episodes: 5,
  launchable: true,
  refusals: [],
  checks: [{ code: "job_valid", ok: true, severity: "info", detail: "ok" }],
  isc: null,
  uses_live: { c5: false, gate: false },
  motion: false,
  launch_token: "tok",
  token_expires_at: Date.now() + 120_000,
  ...over,
});

function fakeApi(over: Partial<WizardApi> & { reset?: boolean } = {}) {
  const { reset = false, ...rest } = over;
  return {
    getCapabilities: mock(async () => capabilities),
    getPolicies: mock(async () => policies(reset)),
    getSetupGuide: mock(async () => ({ steps: [] })),
    createJob: mock(async () => ({ id: "job-1" })),
    planLaunch: mock(async () => plan()),
    launchRun: mock(async () => ({ run_id: "run-1" })),
    planCampaign: mock(async () => ({
      campaign_sha256: "c".repeat(64),
      settings_sha256: "d".repeat(64),
      segments: [{ no: 1, arms: ["A", "B"], cards: ["c1"] }],
      switches: 3,
      power: {
        detectable_difference: 0.31,
        rows: [
          {
            n: 20,
            wilson_width_at_half: 0.4,
            baselines: [
              {
                baseline: 0.5,
                unpaired_fisher: 0.42,
                paired_mcnemar: { "0.0": 0.44, "0.3": 0.39 },
              },
            ],
          },
        ],
      },
      refusals: [],
      checks: [],
    })),
    startCampaign: mock(async () => ({ campaign_id: "camp-1" })),
    ...rest,
  } as unknown as WizardApi;
}

async function toSettings(host: HTMLElement) {
  await click(buttonNamed(host, "Next")!); // environment -> choice
  await waitFor(() => host.textContent?.includes("pi05 sft"), {
    label: "the policy list",
  });
}

describe("the wizard, one run", () => {
  test("walks to a launched run and goes to its page", async () => {
    const api = fakeApi();
    const go: string[] = [];
    const { host } = await render(
      <WizardPage api={api} navigate={(href) => go.push(href)} />,
    );
    await flush(10);
    await toSettings(host);

    // No reset policy on this machine: only the human reset, with the reason.
    expect(host.textContent).toContain("only the human reset can be chosen");
    expect(host.querySelectorAll('input[name="aw-reset"]').length).toBe(1);

    // Next without a policy shows the problem and stays.
    await click(buttonNamed(host, "Next")!);
    expect(host.textContent).toContain("Choose the policy to evaluate.");
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);

    // Settings: an empty instruction is refused, a filled one goes on.
    await click(buttonNamed(host, "Next")!);
    expect(host.textContent).toContain("Write the task instruction.");
    expect(host.textContent).toContain("No robot adapter on this machine");
    const modeRadios = host.querySelectorAll<HTMLInputElement>(
      'input[name="aw-exec-mode"]',
    );
    expect(modeRadios[1].disabled).toBe(true);
    await type(host.querySelector("textarea")!, "Put the cup in the box");
    await click(buttonNamed(host, "Next")!);
    await flush(50);

    // Plan: job made, plan shown, launch needs the phrase.
    await waitFor(() => host.textContent?.includes("Plan digest"), {
      label: "the plan card",
    });
    expect((api.createJob as ReturnType<typeof mock>).mock.calls.length).toBe(
      1,
    );
    const created = (api.createJob as ReturnType<typeof mock>).mock
      .calls[0][0] as Record<string, unknown>;
    expect(created.task).toEqual({ instruction: "Put the cup in the box" });
    expect(typeof created.request_id).toBe("string");

    const start = buttonNamed(host, "Start the run")!;
    expect(start.disabled).toBe(true);
    await click(start);
    expect((api.launchRun as ReturnType<typeof mock>).mock.calls.length).toBe(
      0,
    );
    await type(host.querySelector(".aw-phrase input")!, "launch");
    expect(buttonNamed(host, "Start the run")!.disabled).toBe(false);
    await click(buttonNamed(host, "Start the run")!);
    await flush(10);

    const call = (api.launchRun as ReturnType<typeof mock>).mock.calls[0];
    expect(call[0]).toBe("job-1");
    expect(call[1].launch_token).toBe("tok");
    expect(go).toEqual(["/automatic/runs/run-1"]);
  });

  test("the scene is not attested by default and the switch says why it is off (P1)", async () => {
    const api = fakeApi();
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    const attest = host.querySelector<HTMLInputElement>(
      '[role="switch"], input[type="checkbox"]',
    )!;
    expect(attest.checked).toBe(false);
    expect(attest.disabled).toBe(true);
    expect(host.textContent).toContain("needs an initial-state contract");
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);
    await waitFor(() => host.textContent?.includes("Plan digest"));
    const created = (api.createJob as ReturnType<typeof mock>).mock
      .calls[0][0] as { reset: { scene_check: string } };
    expect(created.reset.scene_check).toBe("provider");
  });

  test("the server's refusal of an attested scene is put in words (P1)", async () => {
    const api = fakeApi({
      planLaunch: mock(async () => {
        throw new ApiError(422, "job_invalid", "The job cannot be planned", [
          {
            message:
              "$: reset.scene_check operator_attested needs task.initial_state_spec",
          },
        ]);
      }) as unknown as WizardApi["planLaunch"],
    });
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);
    await waitFor(() =>
      host.textContent?.includes("The plan could not be made"),
    );
    expect(host.textContent).toContain("needs an initial-state contract");
    expect(host.textContent).not.toContain("operator_attested");
    expect(host.textContent).not.toContain("task.initial_state_spec");
    expect(host.textContent).not.toContain("$:");
  });

  test("the plan card names its checks and values in words, with no codes or paths (P2)", async () => {
    const api = fakeApi({
      planLaunch: mock(async () =>
        plan({
          execution_mode: "dry_run",
          reset_mode: "human_assisted",
          scene_check: "provider",
          roles: ["forward"],
          checks: [
            {
              code: "E_JOB_INVALID",
              ok: true,
              severity: "info",
              detail: "job file /srv/jobs/wizard/aeri-1.yaml",
            },
            {
              code: "E_JOB_OUTSIDE_ROOTS",
              ok: true,
              severity: "info",
              detail: "inside LEVI_AERI_JOB_ROOTS",
            },
            {
              code: "E_NO_ROBOT_ADAPTER",
              ok: true,
              severity: "info",
              detail: "dry run: no adapter needed",
            },
          ],
        }),
      ) as unknown as WizardApi["planLaunch"],
    });
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);
    await waitFor(() => host.textContent?.includes("Plan digest"));
    const card = host.querySelector(".aw-plan")!.textContent ?? "";
    expect(card).toContain("The job file is valid.");
    expect(card).toContain("No robot adapter is needed for a dry run.");
    expect(card).toContain("Dry run");
    expect(card).toContain("Manual reset (evaluated policy only)");
    expect(card).toContain("Evaluated policy");
    for (const raw of [
      "E_JOB_INVALID",
      "E_NO_ROBOT_ADAPTER",
      "/srv/jobs",
      "LEVI_AERI_JOB_ROOTS",
      "dry_run",
      "human_assisted",
    ])
      expect(card).not.toContain(raw);
  });

  test("a plan the server refuses cannot be started, and says why", async () => {
    const api = fakeApi({
      planLaunch: mock(async () =>
        plan({
          launchable: false,
          refusals: ["no_robot_adapter"],
          checks: [
            { code: "adapter", ok: false, severity: "error", detail: "none" },
          ],
        }),
      ) as unknown as WizardApi["planLaunch"],
    });
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);
    await waitFor(() => host.textContent?.includes("Plan digest"));
    expect(host.textContent).toContain("No robot adapter is installed.");
    expect(host.textContent).toContain("no_robot_adapter");
    await type(host.querySelector(".aw-phrase input")!, "launch");
    expect(buttonNamed(host, "Start the run")!.disabled).toBe(true);
    expect(host.textContent).toContain("The server refuses this plan");
  });

  test("a reset policy on the machine is offered next to the human reset", async () => {
    const api = fakeApi({ reset: true });
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    expect(host.querySelectorAll('input[name="aw-reset"]').length).toBe(2);
    expect(host.textContent).toContain("reset-v1");
    expect(host.textContent).not.toContain(
      "only the human reset can be chosen",
    );
  });

  test("a rejected job shows the server's field errors", async () => {
    const api = fakeApi({
      createJob: mock(async () => {
        throw new ApiError(422, "job_invalid", "The job is invalid", [
          { field: "run.max_steps", message: "must be positive" },
        ]);
      }) as unknown as WizardApi["createJob"],
    });
    const { host } = await render(<WizardPage api={api} navigate={() => {}} />);
    await flush(10);
    await toSettings(host);
    await click(host.querySelectorAll('input[name="aw-policy"]')[0]!);
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);
    await waitFor(() => host.textContent?.includes("must be positive"));
    // The field is named as the form names it, not by its file path.
    expect(host.textContent).toContain("Maximum steps per episode");
    expect(host.textContent).not.toContain("run.max_steps");
    expect(host.textContent).not.toContain("job_invalid");
    expect(host.querySelector(".aw-phrase")).toBeNull();
  });
});

describe("the wizard, a multi-model plan", () => {
  test("needs two policies, shows power and switches, starts with its phrase", async () => {
    const api = fakeApi();
    const go: string[] = [];
    const { host } = await render(
      <WizardPage api={api} navigate={(href) => go.push(href)} />,
    );
    await flush(10);
    await click(
      Array.from(host.querySelectorAll<HTMLButtonElement>("[role=radio]")).find(
        (b) => b.textContent?.includes("Multi-model"),
      )!,
    );
    await toSettings(host);
    const boxes = host.querySelectorAll<HTMLInputElement>(
      '.aw-choice input[type="checkbox"]',
    );
    await click(boxes[0]);
    await click(buttonNamed(host, "Next")!);
    expect(host.textContent).toContain("Choose at least two policies");
    await click(boxes[1]);
    // The reference option appears for a chosen arm.
    expect(host.textContent).toContain("Reference arm");
    await click(buttonNamed(host, "Next")!);
    await type(host.querySelector("textarea")!, "go");
    await click(buttonNamed(host, "Next")!);

    await waitFor(() => host.textContent?.includes("Campaign digest"), {
      timeoutMs: 4000,
      label: "the campaign plan",
    });
    expect(host.textContent).toContain("0.31");
    // The planner's nested rows render as a table with named columns.
    const heads = Array.from(host.querySelectorAll(".aw-power th")).map(
      (th) => th.textContent,
    );
    expect(heads).toContain(
      "Smallest detectable difference, unpaired (Fisher)",
    );
    expect(
      heads.some((h) => h?.includes("paired (McNemar), correlation 0.3")),
    ).toBe(true);
    expect(heads.some((h) => h?.includes("paired_mcnemar"))).toBe(false);
    expect(host.textContent).toContain("not measured yet");
    const planned = (api.planCampaign as ReturnType<typeof mock>).mock
      .calls[0][0] as { arms: unknown[]; job_id: string };
    expect(planned.job_id).toBe("job-1");
    expect(planned.arms.length).toBe(2);
    // Guided is the default way to carry a campaign out; the rehearsal on
    // fakes is a choice, and the plan follows it.
    expect((planned as { execution_mode?: string }).execution_mode).toBe(
      "guided",
    );
    const modes = host.querySelectorAll<HTMLInputElement>(
      'input[name="aw-campaign-mode"]',
    );
    expect(modes.length).toBe(2);
    expect(modes[0].checked).toBe(true);
    await click(modes[1]);
    await waitFor(
      () =>
        (api.planCampaign as ReturnType<typeof mock>).mock.calls.length === 2,
      { timeoutMs: 4000, label: "the plan for the rehearsal" },
    );
    expect(
      (
        (api.planCampaign as ReturnType<typeof mock>).mock.calls[1][0] as {
          execution_mode?: string;
        }
      ).execution_mode,
    ).toBe("dry_run");
    await waitFor(() => host.querySelector(".aw-phrase input"), {
      timeoutMs: 4000,
      label: "the second plan",
    });

    expect(buttonNamed(host, "Start the campaign")!.disabled).toBe(true);
    await type(host.querySelector(".aw-phrase input")!, "start-campaign");
    await click(buttonNamed(host, "Start the campaign")!);
    await flush(10);
    const start = (api.startCampaign as ReturnType<typeof mock>).mock.calls[0];
    expect(start[1]).toBe("c".repeat(64));
    expect((start[0] as { execution_mode?: string }).execution_mode).toBe(
      "dry_run",
    );
    expect(go).toEqual(["/automatic/campaigns/camp-1"]);
  });
});
