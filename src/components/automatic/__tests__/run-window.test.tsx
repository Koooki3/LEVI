import {
  click,
  flush,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, mock, test } from "bun:test";
import { card, mockFetch, question, snapshot } from "./fixtures";
import type { PendingCard, RunSnapshot } from "../types";

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
  snap: RunSnapshot,
  extra: Record<string, (body: unknown) => unknown> = {},
) {
  const m = mockFetch({
    "GET runs/run-1": () => snap,
    "GET runs/run-1/events?after=0&limit=200": () => ({
      events: [
        {
          seq: 1,
          at: 1000,
          kind: "transition",
          from: "PREFLIGHT",
          to: "VERIFY_INITIAL",
          reason: null,
        },
        {
          seq: 2,
          at: 2000,
          kind: "note",
          from: null,
          to: null,
          reason: "early_stop goal_verified",
        },
      ],
      next: 2,
    }),
    "GET runs/run-1/metrics": () => ({
      reset_mode: "human_assisted",
      comparable: ["autonomous"],
      autonomous: { success: { n: 3, of: 6, rate: 0.5, wilson95: [0.2, 0.8] } },
      agreement: {
        agreement: { n: 4, of: 5, rate: 0.8, wilson95: [0.4, 0.97] },
      },
    }),
    ...extra,
  });
  restore = m.restore;
  return m;
}

describe("the run window", () => {
  test("a manual-reset run greys the reset states and says why, in words", async () => {
    open(snapshot({ state: "FORWARD_ACTIVE" }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=RESET_ACTIVE]"));
    for (const state of ["RESET_ACTIVE", "RESET_VERIFY", "RESET_FINALIZE"]) {
      const node = host.querySelector(`[data-state=${state}]`)!;
      expect(node.getAttribute("data-status")).toBe("disabled");
      expect(node.textContent).toContain("not used: you reset by hand");
    }
    const current = host.querySelector("[aria-current=step]")!;
    expect(current.getAttribute("data-state")).toBe("FORWARD_ACTIVE");
    expect(current.textContent).toContain("now");
    expect(host.textContent).toContain("Manual reset (evaluated policy only)");
    expect(host.textContent).toContain("Dry run");
  });
  test("a policy-reset run keeps the reset states available", async () => {
    open(
      snapshot({ state: "RESET_ACTIVE", reset_mode: "single_reset_policy" }),
    );
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.querySelector("[data-state=RESET_ACTIVE]"));
    expect(host.querySelector("[data-status=disabled]")).toBeNull();
    expect(host.textContent).toContain("Reset policy");
  });
  test("while the last episode is unlabelled, neither the verdict, the ending nor the early-stop events show", async () => {
    open(snapshot({ pending_card: card() }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.textContent?.includes("You are needed"));
    await waitFor(() => host.querySelector(".ar-timeline li:nth-child(2)"));
    const text = host.textContent ?? "";
    expect(text).not.toContain("early stop");
    expect(text).not.toContain("early_stop");
    expect(text).not.toContain("goal_verified");
    expect(text).not.toContain("Automatic verdict");
    expect(text).not.toContain("rollout says");
    expect(text).toContain("The automatic verdict is hidden");
    expect(text).toContain("hidden until you label");
    // The agreement group is withheld from the metrics too.
    await waitFor(() => host.querySelector(".ds-table"));
    expect(host.querySelector(".ds-table")!.textContent).not.toContain(
      "agreement",
    );
  });
  test("labelling posts the value and shows the revealed card the server returns", async () => {
    const revealed: PendingCard = card({
      operator_label: {
        current: "failure",
        automatic_verdict: "success",
        hidden_until_labelled: false,
      },
    });
    const m = open(snapshot({ pending_card: card() }), {
      "POST runs/run-1/labels": () => ({
        label: "failure",
        revealed: true,
        card: revealed,
      }),
    });
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => button(host, "Failure"));
    await click(button(host, "Failure")!);
    await waitFor(() => host.textContent?.includes("Automatic verdict"));
    const post = m.calls.find((c) => c.path === "runs/run-1/labels")!;
    expect(post.body).toEqual({ episode_id: "ep-0007", value: "failure" });
    expect(host.textContent).toContain("success");
    expect(host.textContent).toContain("early stop by the detector");
  });
  test("a draft contract is flagged as not confirmed, with each condition in words", async () => {
    open(snapshot({ pending_card: card() }));
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.textContent?.includes("You are needed"));
    expect(host.textContent).toContain("Not confirmed by a user");
    expect(host.textContent).toContain("the plate is on the rack");
    expect(host.querySelector("img")?.getAttribute("src")).toContain(
      "/frames/",
    );
    expect(host.textContent).toContain("manual reset no. 2");
  });
  test("a run that has not written yet (404) is shown as starting", async () => {
    const m = mockFetch({});
    restore = m.restore;
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => host.textContent?.includes("The run is starting"));
  });
  test("stop asks first and sends one command with the phrase", async () => {
    const m = open(snapshot({ state: "FORWARD_ACTIVE" }), {
      "POST runs/run-1/stop": () => ({ result: "applied" }),
    });
    const { host } = await render(<RunWindow runId="run-1" />);
    await waitFor(() => button(host, "Stop run"));
    await click(button(host, "Stop run")!);
    const dialog = document.querySelector("[role=alertdialog]")!;
    await click(button(dialog, "Stop run")!);
    await waitFor(() => host.textContent?.includes("The stop was sent"));
    const stops = m.calls.filter((c) => c.path === "runs/run-1/stop");
    expect(stops.length).toBe(1);
    expect(stops[0].body).toMatchObject({ confirm: "stop" });
  });
});

describe("the resume dialog", () => {
  async function opened(
    resume: (body: unknown) => unknown,
    snap = snapshot({ pending_card: card() }),
  ) {
    const m = open(snap, { "POST runs/run-1/resume": resume });
    const view = await render(<RunWindow runId="run-1" />);
    await waitFor(() => button(view.host, "Resume the run…"));
    await click(button(view.host, "Resume the run…")!);
    const dialog = (await waitFor(() =>
      document.querySelector("[role=dialog]"),
    )) as HTMLElement;
    return { ...view, m, dialog };
  }
  const tick = async (dialog: HTMLElement) => {
    for (const box of Array.from(
      dialog.querySelectorAll("input[type=checkbox]"),
    ))
      await click(box);
  };

  test("needs both confirmations before it can be sent", async () => {
    const { dialog, m } = await opened(() => ({ result: "applied" }));
    const confirm = button(dialog, "Confirm and resume")!;
    expect(confirm.disabled).toBe(true);
    await click(dialog.querySelectorAll("input[type=checkbox]")[0]);
    expect(button(dialog, "Confirm and resume")!.disabled).toBe(true);
    expect(m.calls.some((c) => c.path === "runs/run-1/resume")).toBe(false);
  });
  test("a double press sends one command; the body carries the card's sequence and the challenge", async () => {
    let release: (v: unknown) => void = () => {};
    const gate = new Promise((resolve) => (release = resolve));
    const { dialog, m } = await opened(() => gate);
    await tick(dialog);
    const confirm = button(dialog, "Confirm and resume")!;
    await click(confirm);
    await click(confirm);
    release({ result: "applied" });
    await flush();
    const sent = m.calls.filter((c) => c.path === "runs/run-1/resume");
    expect(sent.length).toBe(1);
    expect(sent[0].body).toMatchObject({
      expected_seq: 41,
      environment_handled: true,
      health_rechecked: true,
      challenge: "chal-1",
    });
  });
  test("a stale sequence is explained and the retry is a new command", async () => {
    const { dialog, m } = await opened(() => ({
      result: "refused",
      code: "stale_sequence",
    }));
    await tick(dialog);
    await click(button(dialog, "Confirm and resume")!);
    await waitFor(() => dialog.textContent?.includes("The run moved on"));
    await click(button(dialog, "Confirm and resume")!);
    await flush();
    const ids = m.calls
      .filter((c) => c.path === "runs/run-1/resume")
      .map((c) => (c.body as { command_id: string }).command_id);
    expect(ids.length).toBe(2);
    expect(ids[0]).not.toBe(ids[1]);
  });
  test("a lost answer is retried with the same command id", async () => {
    let n = 0;
    const { dialog, m } = await opened(() => {
      n += 1;
      if (n === 1) throw new Error("network");
      return { result: "repeated" };
    });
    await tick(dialog);
    await click(button(dialog, "Confirm and resume")!);
    await flush();
    await click(button(dialog, "Confirm and resume")!);
    await flush();
    const ids = m.calls
      .filter((c) => c.path === "runs/run-1/resume")
      .map((c) => (c.body as { command_id: string }).command_id);
    expect(ids.length).toBe(2);
    expect(ids[0]).toBe(ids[1]);
  });
  test("missing confirmations from the server are shown in words", async () => {
    const { dialog } = await opened(() => ({
      result: "refused",
      code: "confirmations_missing",
    }));
    await tick(dialog);
    await click(button(dialog, "Confirm and resume")!);
    await waitFor(() =>
      dialog.textContent?.includes("Both confirmations are needed"),
    );
  });
});

describe("the scene question", () => {
  async function asked(over = {}) {
    const m = open(
      snapshot({ state: "VERIFY_INITIAL", scene_question: question(over) }),
      {
        "POST runs/run-1/scene-answer": () => ({}),
      },
    );
    const view = await render(<RunWindow runId="run-1" />);
    await waitFor(() =>
      view.host.textContent?.includes("Check the initial scene"),
    );
    return { ...view, m };
  }
  const radio = (host: HTMLElement, legend: string, label: string) => {
    const set = Array.from(host.querySelectorAll("fieldset")).find((f) =>
      f.textContent?.includes(legend),
    )!;
    return Array.from(set.querySelectorAll("label"))
      .find((l) => l.textContent?.trim() === label)!
      .querySelector("input")!;
  };

  test("shows the frames and every predicate; sending needs the required ones", async () => {
    const { host } = await asked();
    expect(host.querySelectorAll(".ar-card img").length).toBe(1);
    expect(host.textContent).toContain("the lid is off");
    expect(host.textContent).toContain("optional");
    expect(button(host, "Send answers")!.disabled).toBe(true);
  });
  test("sends the answers with the question's own ids and nothing else, then locks", async () => {
    const { host, m } = await asked();
    await click(radio(host, "the plate is on the rack", "True"));
    await click(radio(host, "the cup is upright", "Cannot tell"));
    await click(button(host, "Send answers")!);
    await waitFor(() => host.textContent?.includes("Answers sent"));
    const post = m.calls.find((c) => c.path === "runs/run-1/scene-answer")!;
    expect(post.body).toEqual({
      request_id: "q-1",
      nonce: "n-1",
      frames_sha256: "f".repeat(64),
      predicates: { plate_on_rack: true, cup_upright: null, lid_off: null },
    });
    expect(button(host, "Send answers")!.disabled).toBe(true);
    await click(button(host, "Send answers")!);
    expect(
      m.calls.filter((c) => c.path === "runs/run-1/scene-answer").length,
    ).toBe(1);
  });
  test("a timed-out question cannot be answered and says it is withdrawn", async () => {
    const { host } = await asked({
      asked_at: Date.now() - 700_000,
      timeout_s: 600,
    });
    expect(host.textContent).toContain("Timed out");
    expect(button(host, "Send answers")!.disabled).toBe(true);
  });
});
