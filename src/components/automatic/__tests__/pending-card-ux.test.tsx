import {
  click,
  fire,
  flush,
  press,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { card, mockFetch, snapshot } from "./fixtures";
import type { RunSnapshot } from "../types";

mock.module("next/link", () => ({
  default: ({ children, ...props }: Record<string, unknown>) => (
    <a {...(props as object)}>{children as never}</a>
  ),
}));
const { RunWindow } = await import("../run-window");
setupDom();

let restore = () => {};
afterEach(() => restore());

const button = (root: ParentNode, text: string) =>
  Array.from(root.querySelectorAll("button")).find(
    (node) => node.textContent?.trim() === text,
  ) as HTMLButtonElement | undefined;

function open(
  current: { snap: RunSnapshot },
  extra: Record<string, (body: unknown) => unknown> = {},
) {
  const m = mockFetch({
    "GET runs/run-1": () => current.snap,
    "GET runs/run-1/events?after=0&limit=200": () => ({ events: [], next: 0 }),
    "GET runs/run-1/metrics": () => ({ reset_mode: "human_assisted" }),
    ...extra,
  });
  restore = m.restore;
  return m;
}

async function openDialog(
  current: { snap: RunSnapshot },
  extra: Record<string, (body: unknown) => unknown> = {},
) {
  const m = open(current, extra);
  const view = await render(<RunWindow runId="run-1" />);
  await waitFor(() => button(view.host, "Resume the run…"));
  await click(button(view.host, "Resume the run…")!);
  const dialog = (await waitFor(() =>
    document.querySelector("[role=dialog]"),
  )) as HTMLElement;
  return { ...view, m, dialog };
}
const tick = async (dialog: HTMLElement) => {
  for (const box of Array.from(dialog.querySelectorAll("input[type=checkbox]")))
    await click(box);
};
const repoll = () => fire(document, new Event("visibilitychange"));

describe("the resume dialog is tied to the card it opened on", () => {
  test("a newer card does not re-aim an open dialog: it is marked not current and cannot be confirmed", async () => {
    const current = { snap: snapshot({ pending_card: card() }) };
    const { dialog, m } = await openDialog(current);
    await tick(dialog);
    expect(button(dialog, "Confirm and resume")!.disabled).toBe(false);
    current.snap = snapshot({
      seq: 50,
      pending_card: card({ resume_seq: 49 }),
    });
    await repoll();
    await waitFor(() => dialog.textContent?.includes("no longer current"));
    expect(dialog.querySelector("[role=alert]")).not.toBeNull();
    expect(button(dialog, "Confirm and resume")!.disabled).toBe(true);
    await click(button(dialog, "Confirm and resume")!);
    expect(m.calls.some((c) => c.path === "runs/run-1/resume")).toBe(false);
  });
  test("a card that disappears does not remove the dialog", async () => {
    const current = { snap: snapshot({ pending_card: card() }) };
    const { dialog } = await openDialog(current);
    current.snap = snapshot({ seq: 60, state: "FORWARD_ACTIVE" });
    await repoll();
    await waitFor(() => dialog.textContent?.includes("no longer current"));
    expect(document.querySelector("[role=dialog]")).toBe(dialog);
    await click(button(dialog, "Cancel")!);
    expect(document.querySelector("[role=dialog]")).toBeNull();
  });
  test("a current card sends the sequence captured when the dialog opened", async () => {
    const current = { snap: snapshot({ pending_card: card() }) };
    const { dialog, m } = await openDialog(current, {
      "POST runs/run-1/resume": () => ({ result: "applied" }),
    });
    await tick(dialog);
    await click(button(dialog, "Confirm and resume")!);
    await flush();
    const sent = m.calls.filter((c) => c.path === "runs/run-1/resume");
    expect(sent[0].body).toMatchObject({ expected_seq: 41 });
  });
});

describe("the resume dialog while a command is on its way", () => {
  test("Escape, the scrim and Cancel do not close it; it closes when the answer arrives", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const current = { snap: snapshot({ pending_card: card() }) };
    const { dialog } = await openDialog(current, {
      "POST runs/run-1/resume": () => gate,
    });
    await tick(dialog);
    await click(button(dialog, "Confirm and resume")!);
    await press(document.body, "Escape");
    expect(document.querySelector("[role=dialog]")).toBe(dialog);
    const scrim = document.querySelector(".ds-scrim");
    if (scrim) await click(scrim);
    expect(document.querySelector("[role=dialog]")).toBe(dialog);
    expect(button(dialog, "Cancel")!.disabled).toBe(true);
    release({ result: "applied" });
    await waitFor(() => document.querySelector("[role=dialog]") === null);
  });
  test("Escape closes it while idle", async () => {
    const current = { snap: snapshot({ pending_card: card() }) };
    await openDialog(current);
    await press(document.body, "Escape");
    expect(document.querySelector("[role=dialog]")).toBeNull();
  });
});

describe("the terminal-only resume note", () => {
  test("draws the command as code, with no backticks showing", async () => {
    const current = {
      snap: snapshot({ pending_card: card(), resume_terminal_only: true }),
    };
    open(current);
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() =>
      host.textContent?.includes("Resume this run at its terminal"),
    );
    const note = Array.from(host.querySelectorAll(".ar-note")).find((n) =>
      n.textContent?.includes("terminal D"),
    )!;
    expect(note.textContent).not.toContain("`");
    expect(note.querySelector("code")!.textContent).toBe("scripts/aeri.py");
  });
});
