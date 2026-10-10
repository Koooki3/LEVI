// The guided campaign on the page: place cards, say a segment is done, take
// the controller back, confirm layout cards, and the fallback for a to-do the
// page does not know.
import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { describe, expect, mock, test } from "bun:test";
import { act } from "react";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { CampaignOverview } = await import("../campaign-overview");
const { viewOf, confirmKindOf, isKnownTodo, reasonKey, stateKey, cardChoices } =
  await import("../campaign-logic");
const { buildCampaignRequest, defaultForm } = await import("../wizard-logic");
import type { WizardApi } from "../wizard-api";
import type { CampaignSnapshot, PendingCardEpisode } from "../wizard-types";

setupDom();

const COMMAND = "python eval.py --eval-num 5 --eval-note 'c-1 s02 X2'";

const snap = (over: Partial<CampaignSnapshot> = {}): CampaignSnapshot => ({
  id: "camp-1",
  state: "ENV_CONFIRM",
  execution_mode: "guided",
  arms: [
    {
      id: "A",
      code: "X1",
      done: 3,
      remaining: 7,
      deviated: 0,
      discarded: 0,
      unconfirmed: 0,
    },
    {
      id: "B",
      code: "X2",
      done: 2,
      remaining: 8,
      deviated: 0,
      discarded: 0,
      unconfirmed: 2,
    },
  ],
  segment: { no: 2, total: 4, arm_code: "X2" },
  todo: null,
  safety: { faults: 0, fused: false },
  blinded: true,
  controller: { alive: true },
  peeks: 0,
  wait_reason: null,
  ...over,
});

const pendingCards: PendingCardEpisode[] = [
  {
    key: "ep-1",
    segment: 1,
    run_id: "r1",
    number: 1,
    candidate_card: "c001",
  },
  {
    key: "ep-2",
    segment: 1,
    run_id: "r1",
    number: 2,
    candidate_card: "c002",
  },
];

function api(state: () => CampaignSnapshot, extra: Partial<WizardApi> = {}) {
  return {
    getCampaign: mock(async () => state()),
    confirmCampaign: mock(async () => ({ result: "applied" })),
    campaignCommand: mock(async () => ({ result: "applied" })),
    attachCampaign: mock(async () => ({ campaign_id: "camp-1" })),
    getCampaignCards: mock(async () => ({ pending: pendingCards })),
    confirmCampaignCard: mock(async () => ({ result: "applied" })),
    ...extra,
  } as unknown as Parameters<typeof CampaignOverview>[0]["api"];
}

const buttonNamed = (host: HTMLElement, text: string) =>
  Array.from(host.querySelectorAll("button")).find(
    (b) => b.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

async function tickAll(host: HTMLElement) {
  for (const box of host.querySelectorAll<HTMLInputElement>(
    ".ac-todo input[type=checkbox]",
  ))
    await click(box);
}

async function acceptDialog() {
  const dialog = document.querySelector('[role="alertdialog"]')!;
  const accept = Array.from(dialog.querySelectorAll("button")).find(
    (b) => b.textContent === "Confirm",
  )!;
  await click(accept);
  await flush(20);
}

describe("rules of the guided campaign", () => {
  test("the three confirm kinds and the unknown one", () => {
    expect(confirmKindOf({ kind: "segment_done" })).toBe("segment_done");
    expect(confirmKindOf({ kind: "place_cards" })).toBe("env");
    expect(confirmKindOf({ kind: "recover_run" })).toBe("env");
    expect(confirmKindOf({ kind: "something_new" })).toBeNull();
    expect(isKnownTodo({ kind: "segment_done" })).toBe(true);
    expect(isKnownTodo({ kind: "something_new" })).toBe(false);
    expect(isKnownTodo({ kind: null })).toBe(false);
  });

  test("the snapshot's new fields", () => {
    const v = viewOf(
      snap({
        controller: { alive: false },
        peeks: 2,
        wait_reason: "child_fault",
        execution_mode: "dry_run",
        child_run_id: "run-9",
      }),
    );
    expect(v.controllerDown).toBe(true);
    expect(v.canAttach).toBe(true);
    expect(v.peeks).toBe(2);
    expect(v.waitReason).toBe("child_fault");
    expect(v.executionMode).toBe("dry_run");
    expect(v.childRunId).toBe("run-9");
    // An old server that says nothing about the controller is not "down".
    const old = viewOf({ ...snap(), controller: undefined });
    expect(old.controllerDown).toBe(false);
    expect(old.canAttach).toBe(false);
    // A campaign that has ended needs no controller.
    expect(
      viewOf(snap({ state: "REPORTED", controller: { alive: false } }))
        .canAttach,
    ).toBe(false);
  });

  test("a code the page does not know falls back, never to the raw code", () => {
    expect(reasonKey("segment_short")).toBe(
      "automatic.campaign.reason.segment_short",
    );
    expect(reasonKey("brand_new")).toBe("automatic.campaign.reason.other");
    expect(stateKey("BRAND_NEW")).toBe("automatic.campaign.state.other");
  });

  test("card choices: the suggestion first, no repeats", () => {
    expect(cardChoices("c2", ["c1", "c2", "c3"])).toEqual(["c2", "c1", "c3"]);
    expect(cardChoices(null, ["c1"])).toEqual(["c1"]);
  });

  test("the plan request carries the execution mode, guided by default", () => {
    const form = { ...defaultForm(), armIds: ["a", "b"], referenceId: "a" };
    expect(buildCampaignRequest(form, "job-1").execution_mode).toBe("guided");
    expect(
      buildCampaignRequest({ ...form, campaignMode: "dry_run" }, "job-1")
        .execution_mode,
    ).toBe("dry_run");
  });
});

describe("the guided to-do list", () => {
  test("place_cards shows the cards and the command, and confirms as env", async () => {
    const a = api(() =>
      snap({
        todo: {
          kind: "place_cards",
          challenge: "ch-1",
          arm_code: "X2",
          cards: ["c001", "c002"],
          command: COMMAND,
          eval_note: "c-1 s02 X2",
        },
      }),
    );
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.textContent?.includes("Put the layout cards"));
    expect(host.querySelector(".ac-todo .aw-command__text")?.textContent).toBe(
      COMMAND,
    );
    expect(host.querySelector(".ac-todo")?.textContent).toContain("c002");
    expect(host.querySelector(".ac-todo")?.textContent).not.toContain(
      "place_cards",
    );
    await tickAll(host);
    await click(buttonNamed(host, "Confirm")!);
    await acceptDialog();
    const call = (a!.confirmCampaign as ReturnType<typeof mock>).mock.calls[0];
    expect(call[2]).toBe("env");
    expect(call[3]).toBe("ch-1");
  });

  test("segment_done shows progress and needs a dialog before it sends", async () => {
    const a = api(() =>
      snap({
        state: "ARM_RUNNING",
        todo: {
          kind: "segment_done",
          challenge: "ch-2",
          arm_code: "X2",
          cards: ["c001"],
          command: COMMAND,
          progress: { done: 3, planned: 5 },
          pending_cards: [{ key: "ep-1", candidate_card: "c001" }],
        },
      }),
    );
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.textContent?.includes("then say it is done"));
    const todo = host.querySelector(".ac-todo")!;
    expect(todo.textContent).toContain("Episodes written so far: 3 of 5");
    expect(todo.textContent).toContain("1 still wait for a layout card");
    expect(todo.querySelector(".aw-command__text")?.textContent).toBe(COMMAND);
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
    await tickAll(host);
    await click(buttonNamed(host, "Confirm")!);
    // Nothing is sent before the dialog is accepted.
    expect(
      (a!.confirmCampaign as ReturnType<typeof mock>).mock.calls.length,
    ).toBe(0);
    await acceptDialog();
    const call = (a!.confirmCampaign as ReturnType<typeof mock>).mock.calls[0];
    expect(call[0]).toBe("camp-1");
    expect(call[2]).toBe("segment_done");
    expect(call[3]).toBe("ch-2");
  });

  test("a to-do of an unknown kind is shown as text with a disabled button", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({
            todo: {
              kind: "teleport_arm",
              challenge: "ch-3",
              detail: "Move the arm somewhere.",
            },
          }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() =>
      host.textContent?.includes("A request this page does not know"),
    );
    const todo = host.querySelector(".ac-todo")!;
    expect(todo.textContent).toContain("Move the arm somewhere.");
    expect(todo.textContent).toContain("cannot be confirmed from this page");
    expect(todo.textContent).not.toContain("teleport_arm");
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
  });

  test("a short segment is accepted with an option the server asked for", async () => {
    const a = api(() =>
      snap({
        state: "WAIT_HUMAN",
        wait_reason: "segment_short",
        todo: {
          kind: "recover_run",
          challenge: "ch-4",
          reason: "segment_short",
          arm_code: "X2",
        },
      }),
    );
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.textContent?.includes("needs recovery"));
    expect(host.textContent).toContain(
      "the segment has fewer episodes than planned",
    );
    expect(host.textContent).not.toContain("segment_short");
    // The stop-rule override is not offered for this reason.
    expect(host.textContent).not.toContain("Override the stop rule");
    const boxes = Array.from(
      host.querySelectorAll<HTMLInputElement>(".ac-todo input[type=checkbox]"),
    );
    // One required tick; the options exclude one another: radios, "just go
    // on" selected by default, then accept short.
    expect(boxes.length).toBe(1);
    const radios = Array.from(
      host.querySelectorAll<HTMLInputElement>(".ac-todo input[type=radio]"),
    );
    expect(radios.length).toBe(3);
    expect(radios[0].checked).toBe(true);
    await click(boxes[0]);
    await click(radios[1]);
    expect(radios[0].checked).toBe(false);
    expect(radios[1].checked).toBe(true);
    await click(buttonNamed(host, "Confirm")!);
    await acceptDialog();
    const call = (a!.confirmCampaign as ReturnType<typeof mock>).mock.calls[0];
    expect(call[2]).toBe("env");
    expect(call[4]).toEqual({ accept_short_segment: true });
  });

  test("the ticks survive a renewed challenge and reset for another step", async () => {
    let todo: NonNullable<CampaignSnapshot["todo"]> = {
      kind: "place_cards",
      challenge: "ch-1",
      segment: 1,
      cards: ["c1"],
    };
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => snap({ todo }))}
        intervalMs={30}
      />,
    );
    await waitFor(() => host.querySelector(".ac-todo"));
    await tickAll(host);
    const ticked = () =>
      Array.from(
        host.querySelectorAll<HTMLInputElement>(
          ".ac-todo input[type=checkbox]",
        ),
      ).every((b) => b.checked);
    expect(ticked()).toBe(true);
    // The same step, a fresh challenge: still ticked.
    todo = { ...todo, challenge: "ch-2" };
    await flush(120);
    expect(ticked()).toBe(true);
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(false);
    // Another segment is another question.
    todo = { ...todo, challenge: "ch-3", segment: 2 };
    await waitFor(() => !ticked());
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
  });

  test("with the controller down a to-do cannot be confirmed", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({
            controller: { alive: false },
            todo: { kind: "place_cards", challenge: "ch-5", cards: ["c1"] },
          }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.querySelector(".ac-todo"));
    await tickAll(host);
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
    expect(host.querySelector(".ac-todo")?.textContent).toContain(
      "controller is not running",
    );
  });
});

describe("the controller and the peeks", () => {
  test("a missing controller shows the attach button, which attaches once", async () => {
    let alive = false;
    const a = api(() => snap({ controller: { alive } }), {
      attachCampaign: mock(async () => {
        alive = true;
        return { campaign_id: "camp-1" };
      }),
    });
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => buttonNamed(host, "Attach the controller"));
    expect(host.textContent).toContain("The controller is not running.");
    // Pause is not offered while there is nobody to carry it out.
    expect(buttonNamed(host, "Pause at the segment end")!.disabled).toBe(true);
    await click(buttonNamed(host, "Attach the controller")!);
    await flush(20);
    const calls = (a!.attachCampaign as ReturnType<typeof mock>).mock.calls;
    expect(calls.length).toBe(1);
    expect(calls[0][0]).toBe("camp-1");
    expect(typeof calls[0][1]).toBe("string");
    await waitFor(() => !buttonNamed(host, "Attach the controller"));
  });

  test("no attach button when the controller runs or the campaign ended", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({
            state: "REPORTED",
            blinded: false,
            controller: { alive: false },
          }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("Report ready"));
    expect(buttonNamed(host, "Attach the controller")).toBeUndefined();
  });

  test("a finished campaign says so: no running segment, no pause button", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({
            state: "REPORTED",
            blinded: false,
            segment: { no: 4, total: 4, arm_code: "X1" },
          }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("Report ready"));
    expect(host.textContent).toContain("All 4 segments are finished.");
    expect(host.textContent).not.toContain("now arm");
    expect(buttonNamed(host, "Pause at the segment end")).toBeUndefined();
  });

  test("peeks are marked and the conclusion is called exploratory", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => snap({ peeks: 2, blinded: false }))}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("looked at before the end"));
    expect(host.textContent).toContain("Peeks: 2");
    expect(host.textContent).toContain("exploratory");
  });

  test("the state and the wait reason are words, not codes", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({ state: "WAIT_HUMAN", wait_reason: "child_crashed" }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("Waiting for a person"));
    expect(host.textContent).toContain("a run inside the campaign crashed");
    expect(host.textContent).not.toContain("WAIT_HUMAN");
    expect(host.textContent).not.toContain("child_crashed");
  });
});

describe("layout card confirmation", () => {
  test("unconfirmed episodes are listed, and a choice is sent with its episode", async () => {
    const a = api(() => snap());
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.querySelector(".ac-cards"));
    expect(host.textContent).toContain("2 episodes without a confirmed card");
    expect(host.textContent).toContain("do not enter the paired analysis");
    const selects =
      host.querySelectorAll<HTMLSelectElement>(".ac-cards select");
    expect(selects.length).toBe(2);
    // The suggestion is preselected; a different card can be chosen.
    expect(selects[0].value).toBe("c001");
    await act(async () => {
      selects[1].value = "c001";
      selects[1].dispatchEvent(new Event("change", { bubbles: true }));
    });
    const saves = Array.from(
      host.querySelectorAll<HTMLButtonElement>(".ac-cards tbody button"),
    );
    await click(saves[1]);
    await flush(20);
    const calls = (a!.confirmCampaignCard as ReturnType<typeof mock>).mock
      .calls;
    expect(calls.length).toBe(1);
    expect(calls[0].slice(0, 1)).toEqual(["camp-1"]);
    expect(calls[0][2]).toBe("ep-2");
    expect(calls[0][3]).toBe("c001");
    expect(host.querySelector(".ac-cards")?.textContent).toContain("Saved");
  });

  test("'no card' is sent as null", async () => {
    const a = api(() => snap());
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.querySelector(".ac-cards select"));
    const select = host.querySelector<HTMLSelectElement>(".ac-cards select")!;
    await act(async () => {
      select.value = "__none__";
      select.dispatchEvent(new Event("change", { bubbles: true }));
    });
    await click(host.querySelector(".ac-cards tbody button"));
    await flush(20);
    const call = (a!.confirmCampaignCard as ReturnType<typeof mock>).mock
      .calls[0];
    expect(call[2]).toBe("ep-1");
    expect(call[3]).toBeNull();
    expect(host.querySelector(".ac-cards")?.textContent).toContain(
      "no card, not paired",
    );
  });

  test("a dry run has no card panel", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => snap({ execution_mode: "dry_run" }))}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("Dry run"));
    expect(host.querySelector(".ac-cards")).toBeNull();
  });
});
