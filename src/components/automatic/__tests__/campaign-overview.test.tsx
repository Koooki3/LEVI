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
const { viewOf, formatEta, confirmKindOf } = await import("../campaign-logic");
import type { WizardApi } from "../wizard-api";
import type { CampaignSnapshot } from "../wizard-types";

setupDom();

const snap = (over: Partial<CampaignSnapshot> = {}): CampaignSnapshot => ({
  id: "camp-1",
  state: "ARM_RUNNING",
  arms: [
    { id: "A", code: "X1", done: 3, remaining: 7, deviated: 1, discarded: 0 },
    { id: "B", code: "X2", done: 2, remaining: 8, deviated: 0, discarded: 1 },
  ],
  segment: { no: 2, total: 4, arm_code: "X2" },
  todo: null,
  safety: { faults: 0, fused: false },
  blinded: true,
  ...over,
});

async function type(field: Element, text: string) {
  await act(async () => {
    (field as HTMLElement).focus();
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      "value",
    )!.set!.call(field, text);
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

describe("campaign view rules", () => {
  test("blind results show codes, not arm ids, and no field that is not named", () => {
    const leaky = {
      ...snap(),
      success_rate: 0.9,
      arms: [
        {
          id: "A",
          code: "X1",
          done: 1,
          remaining: 1,
          deviated: 0,
          discarded: 0,
          success_rate: 0.9,
        },
      ],
    } as unknown as CampaignSnapshot;
    const view = viewOf(leaky);
    expect(view.blinded).toBe(true);
    expect(view.arms[0].id).toBeNull();
    expect(JSON.stringify(view)).not.toContain("0.9");
  });

  test("anything but an explicit blinded=false is treated as blind", () => {
    expect(viewOf({ ...snap(), blinded: undefined as never }).blinded).toBe(
      true,
    );
    expect(viewOf(snap({ blinded: false })).arms[0].id).toBe("A");
  });

  test("pause is for a moving campaign, resume for a paused one, neither after a fuse", () => {
    expect(viewOf(snap()).canPause).toBe(true);
    expect(viewOf(snap({ state: "PAUSED" })).canResume).toBe(true);
    expect(viewOf(snap({ state: "PAUSED" })).canPause).toBe(false);
    const fused = viewOf(snap({ safety: { faults: 2, fused: true } }));
    expect(fused.canPause).toBe(false);
    expect(viewOf(snap({ state: "REPORTED" })).reportReady).toBe(true);
  });

  test("eta and confirmation kinds", () => {
    expect(formatEta(90)).toBe("2 min");
    expect(formatEta(4800)).toBe("1 h 20 min");
    expect(confirmKindOf({ kind: "switch_policy" })).toBe("switch_policy");
    expect(confirmKindOf({ kind: "place_cards" })).toBe("env");
    expect(confirmKindOf({ kind: null })).toBeNull();
  });
});

function api(state: () => CampaignSnapshot, extra: Partial<WizardApi> = {}) {
  return {
    getCampaign: mock(async () => state()),
    confirmCampaign: mock(async () => ({ result: "applied" })),
    campaignCommand: mock(async () => ({ result: "applied" })),
    ...extra,
  } as unknown as Pick<
    WizardApi,
    "getCampaign" | "confirmCampaign" | "campaignCommand"
  >;
}

describe("the campaign overview", () => {
  test("blind: arm codes and counts, an explanation, and no success rate", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => ({ ...snap(), success_rate: 0.77 }) as CampaignSnapshot)}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("X1"));
    const text = host.textContent ?? "";
    expect(text).toContain("X2");
    expect(text).toContain("Success rates are hidden");
    expect(text).not.toContain("0.77");
    expect(text).not.toMatch(/\d\s?%/);
    expect(text).toContain("Segment 2 of 4, now arm X2");
    // Arm ids stay out while blind.
    expect(text).not.toContain("X1 · A");
  });

  test("unblind needs the phrase and sends it", async () => {
    const a = api(() => snap());
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => host.querySelector("details.ac-unblind"));
    const reveal = () => buttonNamed(host, "Reveal the results")!;
    expect(reveal().disabled).toBe(true);
    await type(host.querySelector(".ac-unblind input")!, "unblind");
    await click(reveal());
    await flush(20);
    const calls = (a.campaignCommand as ReturnType<typeof mock>).mock.calls;
    expect(calls.length).toBe(1);
    expect(calls[0][1]).toBe("unblind");
    expect(typeof calls[0][2]).toBe("string");
  });

  test("a policy switch is two steps: tick, then confirm in a dialog", async () => {
    const a = api(() =>
      snap({
        state: "WAIT_HUMAN",
        todo: { kind: "switch_policy", challenge: "ch-1", arm_code: "X2" },
      }),
    );
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() =>
      host.textContent?.includes("Switch the policy server to arm X2"),
    );
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
    expect(host.textContent).toContain("Tick every item first.");
    for (const box of host.querySelectorAll<HTMLInputElement>(
      ".ac-todo input[type=checkbox]",
    ))
      await click(box);
    await click(buttonNamed(host, "Confirm")!);
    // The dialog asks again; nothing is sent before it is accepted.
    expect(
      (a.confirmCampaign as ReturnType<typeof mock>).mock.calls.length,
    ).toBe(0);
    const dialog = document.querySelector('[role="alertdialog"]')!;
    const accept = Array.from(dialog.querySelectorAll("button")).find(
      (b) => b.textContent === "Confirm",
    )!;
    await click(accept);
    await flush(20);
    const call = (a.confirmCampaign as ReturnType<typeof mock>).mock.calls[0];
    expect(call[0]).toBe("camp-1");
    expect(call[2]).toBe("switch_policy");
    expect(call[3]).toBe("ch-1");
  });

  test("a todo with no challenge cannot be confirmed and says so", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => snap({ todo: { kind: "place_cards", cards: ["c3"] } }))}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("c3"));
    expect(host.textContent).toContain("gave no challenge");
    expect(buttonNamed(host, "Confirm")!.disabled).toBe(true);
  });

  test("a fused campaign says so and cannot be resumed", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() =>
          snap({ state: "PAUSED", safety: { faults: 2, fused: true } }),
        )}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.textContent?.includes("safety fuse has tripped"));
    expect(buttonNamed(host, "Resume")!.disabled).toBe(true);
  });

  test("pause sends a command id and refreshes", async () => {
    const a = api(() => snap());
    const { host } = await render(
      <CampaignOverview campaignId="camp-1" api={a} intervalMs={50} />,
    );
    await waitFor(() => buttonNamed(host, "Pause at the segment end"));
    await click(buttonNamed(host, "Pause at the segment end")!);
    await flush(20);
    const calls = (a.campaignCommand as ReturnType<typeof mock>).mock.calls;
    expect(calls[0][1]).toBe("pause");
  });

  test("a finished campaign links to its report", async () => {
    const { host } = await render(
      <CampaignOverview
        campaignId="camp-1"
        api={api(() => snap({ state: "REPORTED", blinded: false }))}
        intervalMs={50}
      />,
    );
    await waitFor(() => host.querySelector('a[href$="/report"]'));
    expect(host.textContent).toContain("X1 · A");
    expect(host.textContent).not.toContain("Success rates are hidden");
  });
});
