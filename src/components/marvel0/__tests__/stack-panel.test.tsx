import { act } from "react";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { errorOf, type ApiResult } from "../deploy-api";
import { marvel0View } from "../marvel0-logic";
import { StackPanel } from "../stack-panel";
import type { StackApi } from "../stack-api";
import { useStackPoll } from "../use-stack-poll";
import { button, tick, type } from "./actions-helpers";
import { STACK_ANSWERS, STACK_REFUSALS, stackDoc } from "./ctl-fixtures";
import { quietMarvel0, quietSetup } from "./fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
  delete (document as unknown as Record<string, unknown>).visibilityState;
});

type Doc = ReturnType<typeof stackDoc>;
const cards = marvel0View({
  setup: quietSetup(),
  marvel0: quietMarvel0(),
}).groups.find((g) => g.id === "arm")!.cards;

interface Call {
  op: string;
  comp?: string;
  body?: Record<string, unknown>;
}

/** A stack client over a document that the test changes; every write is
 * recorded and answered from `answer`. */
function fake(initial: Doc) {
  const state = {
    doc: initial as Doc | null,
    reads: 0,
    calls: [] as Call[],
    answer: ((): ApiResult => ({
      ok: true,
      status: 202,
      data: { done: "started" },
    })) as (call: Call) => ApiResult | Promise<ApiResult>,
  };
  const write = (op: string) => async (a: unknown, b?: unknown) => {
    const call: Call =
      typeof a === "string"
        ? { op, comp: a, body: b as Record<string, unknown> }
        : { op, body: a as Record<string, unknown> };
    state.calls.push(call);
    return state.answer(call);
  };
  const api: StackApi = {
    read: async () => {
      state.reads += 1;
      return state.doc
        ? { ok: true, status: 200, data: structuredClone(state.doc) }
        : errorOf(503, {});
    },
    start: write("start") as StackApi["start"],
    stop: write("stop") as StackApi["stop"],
    marker: write("marker") as StackApi["marker"],
    preflight: write("preflight") as StackApi["preflight"],
    gripper: write("gripper") as StackApi["gripper"],
    sequence: write("sequence") as StackApi["sequence"],
    sequenceCancel: write("sequenceCancel") as StackApi["sequenceCancel"],
    log: async () => errorOf(404, {}),
  };
  return { api, state };
}

function Harness({ api, pollMs = 25 }: { api: StackApi; pollMs?: number }) {
  const poll = useStackPoll(api, { pollMs });
  return <StackPanel api={api} poll={poll} cards={cards} />;
}

async function open(initial: Doc, zhLanguage = false) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const f = fake(initial);
  const element = <Harness api={f.api} />;
  const view = await render(
    zhLanguage ? <LocaleProvider>{element}</LocaleProvider> : element,
  );
  // The first answer is in when the age line counts from it.
  await waitFor(
    () =>
      view.host.querySelector(
        "[data-testid=marvel0-stack-age][data-late=false]",
      ),
    { label: "the arm stack" },
  );
  return { ...view, ...f };
}
const comp = (host: HTMLElement, id: string) =>
  host.querySelector(`[data-testid=marvel0-comp-${id}]`) as HTMLElement;
const text = (node: Element | null) => node?.textContent ?? "";
const dialog = (host: HTMLElement) =>
  host.querySelector("[role=dialog]") as HTMLElement | null;
const tickBox = async (d: HTMLElement, label: RegExp) => {
  const box = Array.from(d.querySelectorAll("label")).find((l) =>
    label.test(l.textContent ?? ""),
  )!;
  await click(box.querySelector("input")!);
};

describe("every button that is off says why, in the backend's words", () => {
  test("A before the monitor and the marker: Start is off, with the backend's blockers beside it", async () => {
    const { host } = await open(stackDoc("idle"));
    const a = comp(host, "A");
    expect(button(a, "Start")!.disabled).toBe(true);
    const why = a.querySelector("[data-testid=marvel0-block-start-A]")!;
    expect(text(why)).toContain("Start is off:");
    expect(text(why)).toContain(en["marvel0.ctl.block.gate.monitor_fresh"]);
    expect(
      Array.from(why.querySelectorAll("li")).map((li) =>
        li.getAttribute("data-code"),
      ),
    ).toContain("gate.marker_set");
    // M can be started: no reason, an enabled button.
    expect(button(comp(host, "M"), "Start")!.disabled).toBe(false);
    expect(
      comp(host, "M").querySelector("[data-testid=marvel0-block-start-M]"),
    ).toBeNull();
  });

  test("the first reason is in view and the others one click away, not off the page", async () => {
    const { host } = await open(stackDoc("idle"));
    const why = comp(host, "C").querySelector(
      "[data-testid=marvel0-block-start-C]",
    )!;
    const codes = Array.from(why.querySelectorAll("li")).map((li) =>
      li.getAttribute("data-code"),
    );
    expect(codes.length).toBeGreaterThan(2);
    // One reason outside the fold, every other one inside it.
    const outside = Array.from(why.querySelectorAll("li")).filter(
      (li) => !li.closest("details"),
    );
    expect(outside).toHaveLength(1);
    const more = why.querySelector("details.rg-block__more")!;
    expect(more.querySelectorAll("li")).toHaveLength(codes.length - 1);
    expect(more.querySelector("summary")!.textContent).toBe(
      `More reasons (${codes.length - 1})`,
    );
    // A single reason has no fold.
    expect(
      comp(host, "A")
        .querySelector("[data-testid=marvel0-block-start-A]")!
        .querySelectorAll("details").length,
    ).toBeLessThanOrEqual(1);
  });

  test("a card with controls keeps its readings behind a fold; a failed one shows them", async () => {
    const { host } = await open(stackDoc("a_ready"));
    const arm = host.querySelector("[data-testid=marvel0-card-arm]")!;
    const fold = arm.querySelector("details.rg-card__probe")!;
    expect(fold.hasAttribute("open")).toBe(false);
    expect(fold.querySelector("summary")!.textContent).toMatch(
      /^Readings \(\d+\)$/,
    );
    expect(fold.querySelector("dl.rg-facts")).not.toBeNull();
  });

  test("a terminal that runs shows no start reasons; a stop that the order forbids says so", async () => {
    const { host } = await open(stackDoc("c_ready"));
    expect(
      comp(host, "A").querySelector("[data-testid=marvel0-block-start-A]"),
    ).toBeNull();
    expect(
      text(comp(host, "A").querySelector("[data-testid=marvel0-block-stop-A]")),
    ).toContain(en["marvel0.ctl.block.stop_c_first"]);
    expect(
      button(comp(host, "A"), en["marvel0.ctl.btn.stop_A"])!.disabled,
    ).toBe(true);
    expect(
      button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!.disabled,
    ).toBe(false);
  });

  test("a gripper test that failed: B's card says 'Ready · test failed' in the warning colour, not a green Ready, until a later test passes", async () => {
    const failed = await open(stackDoc("gripper_failed"));
    const card = failed.host.querySelector(
      "[data-testid=marvel0-card-gripper]",
    )!;
    expect(text(card)).toContain(en["marvel0.chain.state.ready_test_failed"]);
    expect(card.className).toContain("rg-card--stale");
    const passed = await open(
      stackDoc("gripper_failed", (doc) => {
        doc.act.gripper_test.last = {
          action: "close",
          ok: true,
          code: "reached",
          reached: true,
          stalled: false,
          at: doc.sampled_at,
        };
      }),
    );
    const clear = passed.host.querySelector(
      "[data-testid=marvel0-card-gripper]",
    )!;
    expect(text(clear)).not.toContain(
      en["marvel0.chain.state.ready_test_failed"],
    );
    expect(clear.className).toContain("rg-card--ok");
  });

  test("one danger rule: Stop A and Stop C say what they leave behind and are outlined in the danger colour; the others stay plain", async () => {
    const { host } = await open(stackDoc("c_ready"));
    expect(en["marvel0.ctl.btn.stop_C"]).toBe(
      "Stop C: arm keeps its last target",
    );
    expect(zh["marvel0.ctl.btn.stop_C"]).toBe("停止 C：手臂保持最后的目标");
    for (const id of ["A", "C"] as const) {
      const stop = button(comp(host, id), en[`marvel0.ctl.btn.stop_${id}`])!;
      expect(stop.className).toContain("rg-btn--danger-outline");
      expect(stop.className).toContain("ds-btn--secondary");
      expect(stop.className).not.toContain("ds-btn--danger");
    }
    // M and B: stopping them leaves nothing for the arm to depend on.
    for (const id of ["M", "B"] as const) {
      const stop = button(comp(host, id), "Stop");
      if (stop) expect(stop.className).not.toContain("rg-btn--danger-outline");
    }
    // The checks are outlined buttons, not ghosts that read as labels.
    for (const [id, label] of [
      ["A", "Check FCI"],
      ["C", "Check before C"],
    ] as const) {
      const check = button(comp(host, id), label)!;
      expect(check.className).toContain("ds-btn--secondary");
    }
  });

  test("a reason the page has no words for is the server's sentence, marked as English", async () => {
    const { host } = await open(
      stackDoc("idle", (doc) => {
        doc.act.terminals.a.start_blockers = [
          {
            code: "brand_new",
            level: "required",
            text: "Only the server says this",
            params: {},
          },
        ];
      }),
    );
    const why = comp(host, "A").querySelector(
      "[data-testid=marvel0-block-start-A]",
    )!;
    expect(text(why)).toContain("Only the server says this");
    expect(why.querySelector("[lang=en]")?.textContent).toBe(
      "Only the server says this",
    );
  });

  test("a disabled button with no reason says that the backend gave none", async () => {
    const { host } = await open(
      stackDoc("idle", (doc) => {
        doc.act.terminals.c.start_blockers = [];
      }),
    );
    expect(
      text(
        comp(host, "C").querySelector("[data-testid=marvel0-block-start-C]"),
      ),
    ).toContain(en["marvel0.ctl.no_reason"]);
  });

  test("the gripper test's blockers are beside its buttons", async () => {
    const { host } = await open(stackDoc("a_ready"));
    const test_ = host.querySelector("[data-testid=marvel0-gripper-test]")!;
    expect(button(test_ as HTMLElement, "Open gripper")!.disabled).toBe(true);
    expect(
      text(test_.querySelector("[data-testid=marvel0-block-gripper]")),
    ).toContain(en["marvel0.ctl.block.gripper_b_not_ready"]);
  });

  test("Desk is shown and cannot be acted on", async () => {
    const { host } = await open(stackDoc("idle"));
    const desk = host.querySelector("[data-testid=marvel0-card-desk]")!;
    expect(
      text(desk.querySelector("[data-testid=marvel0-desk-readonly]")),
    ).toBe(en["marvel0.ctl.desk.by_hand"]);
    expect(desk.querySelectorAll("button")).toHaveLength(0);
  });
});

describe("outside the product, or with the controls off or unread, nothing can be pressed", () => {
  const enabledButtons = (host: HTMLElement) =>
    Array.from(
      host.querySelectorAll<HTMLButtonElement>(
        "[data-testid=marvel0-step-arm] button",
      ),
    ).filter(
      (b) => !b.disabled && !["Refresh"].includes(b.textContent?.trim() ?? ""),
    );

  test("a worktree or temporary instance only shows", async () => {
    const { host } = await open(
      stackDoc("ready_to_start_a", (doc) => {
        doc.profile = "fake";
      }),
    );
    expect(enabledButtons(host)).toEqual([]);
    expect(
      host.querySelector("[data-testid=marvel0-stack-fake]"),
    ).not.toBeNull();
  });

  test("the control file switched off: a note, and no start", async () => {
    const { host } = await open(stackDoc("control_disabled"));
    expect(
      text(host.querySelector("[data-testid=marvel0-stack-disabled]")),
    ).toContain("switched off");
    expect(button(comp(host, "A"), "Start")!.disabled).toBe(true);
    expect(text(comp(host, "A"))).toContain(
      en["marvel0.ctl.block.control_disabled"],
    );
  });

  test("an older backend without the controls: the cards only show", async () => {
    const doc = stackDoc("idle");
    delete doc.act;
    const { host } = await open(doc);
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-stack-noact]"),
      {
        label: "the note",
      },
    );
    expect(host.querySelector("[data-testid=marvel0-comp-A]")).toBeNull();
  });

  test("when the stack cannot be read the buttons go off and the page says so", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    expect(button(comp(host, "A"), "Start")!.disabled).toBe(false);
    state.doc = null;
    await waitFor(
      () => host.querySelector("[data-testid=marvel0-stack-stale]"),
      {
        label: "the stale note",
      },
    );
    expect(button(comp(host, "A"), "Start")!.disabled).toBe(true);
  });
});

describe("starting a terminal", () => {
  test("the dialog says what the action does, asks the checks of the person and the phrase, and sends one request", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    const d = dialog(host)!;
    expect(text(d)).toContain("Start: Arm controller (A)");
    // The fixed sentence, the hazard, what the gates are worth.
    expect(text(d)).toContain(en["marvel0.ctl.estop"]);
    expect(text(d)).toContain(en["marvel0.ctl.hazard.start_A"]);
    expect(text(d)).toContain(en["marvel0.stack.gates_scope"]);
    expect(text(d)).toContain(en["marvel0.ctl.unverified"]);
    // No control in the dialog is called an emergency stop.
    for (const b of d.querySelectorAll("button"))
      expect(b.textContent).not.toMatch(/e-?stop|emergency/i);
    const go = () => button(d, "Start")!;
    expect(go().disabled).toBe(true);
    await type(
      d.querySelector("input[type=text], input:not([type])"),
      "start arm stack",
    );
    expect(go().disabled).toBe(true); // the checks are not ticked
    await tickBox(d, /Desk is done/);
    expect(go().disabled).toBe(true);
    await tickBox(d, /hand is on the physical e-stop/);
    expect(go().disabled).toBe(false);
    await click(go());
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({ op: "start", comp: "A" });
    expect(state.calls[0].body).toMatchObject({
      confirm: "start arm stack",
      attest: { desk_done: true, estop_hand: true },
    });
    expect(
      String(state.calls[0].body!.request_id).length,
    ).toBeGreaterThanOrEqual(8);
    expect(dialog(host)).toBeNull();
  });

  test("the phrase must be typed exactly", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    const d = dialog(host)!;
    await tickBox(d, /Desk is done/);
    await tickBox(d, /hand is on the physical e-stop/);
    for (const wrong of [
      "start arm",
      "Start arm stack",
      "start arm stacks",
      "stop arm stack",
    ]) {
      await type(d.querySelector("input:not([type=checkbox])"), wrong);
      expect(button(d, "Start")!.disabled).toBe(true);
    }
    expect(state.calls).toHaveLength(0);
  });

  test("working shows at once after the click, before any answer; the other buttons wait", async () => {
    let release: (value: ApiResult) => void = () => {};
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    state.answer = () =>
      new Promise<ApiResult>((resolve) => {
        release = resolve;
      });
    await click(button(comp(host, "A"), "Start")!);
    const d = dialog(host)!;
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start arm stack",
    );
    await tickBox(d, /Desk is done/);
    await tickBox(d, /hand is on the physical e-stop/);
    await click(button(d, "Start")!);
    expect(state.calls).toHaveLength(1);
    // The write is open: the card says it is working, and no other control acts.
    expect(host.querySelector("[data-testid=marvel0-working]")).not.toBeNull();
    expect(button(comp(host, "M"), "Start")!.disabled).toBe(true);
    await act(async () =>
      release({ ok: true, status: 202, data: { done: "started" } }),
    );
    await waitFor(() => !host.querySelector("[data-testid=marvel0-working]"), {
      label: "working to end",
    });
  });

  test("a refusal is shown in words from its code, in English and in Chinese", async () => {
    for (const lang of [false, true]) {
      const { host, state } = await open(stackDoc("ready_to_start_a"), lang);
      const refusal = STACK_REFUSALS.start_a_blocked;
      state.answer = () => errorOf(refusal.status, { detail: refusal.detail });
      const t = (key: string) => (lang ? zh : en)[key as keyof typeof en];
      await click(
        button(comp(host, "A"), lang ? zh["marvel0.ctl.btn.start"] : "Start")!,
      );
      const d = dialog(host)!;
      await type(
        d.querySelector("input:not([type=checkbox])"),
        "start arm stack",
      );
      for (const label of d.querySelectorAll("label"))
        if (label.querySelector("input[type=checkbox]"))
          await click(label.querySelector("input")!);
      await click(button(d, lang ? zh["marvel0.ctl.btn.start"] : "Start")!);
      await tick(3, 10);
      const refusedEl = host.querySelector("[data-testid=marvel0-refused]")!;
      // The same boxed block as "Start is off:", with a danger edge, an alert
      // role and the label "Refused:".
      expect(refusedEl.className).toContain("rg-block rg-block--refused");
      expect(refusedEl.getAttribute("role")).toBe("alert");
      expect(text(refusedEl)).toContain(t("marvel0.ctl.refused"));
      // Worded from the catalogue, not the server's sentence marked as English.
      expect(refusedEl.querySelector("[lang=en]")).toBeNull();
      const shown = text(refusedEl);
      expect(shown).toContain(
        t("marvel0.ctl.block.gate.layers_known").replace("{unknown}", "B"),
      );
      // The server's English sentence is not shown where the page has words.
      if (lang) expect(shown).not.toContain("Part of the stack cannot be seen");
    }
  });

  test("a code the page has no words for shows the server's sentence", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    state.answer = () =>
      errorOf(409, {
        detail: { code: "brand_new_code", message: "Server only" },
      });
    await click(button(comp(host, "A"), "Start")!);
    const d = dialog(host)!;
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start arm stack",
    );
    for (const label of d.querySelectorAll("label"))
      if (label.querySelector("input[type=checkbox]"))
        await click(label.querySelector("input")!);
    await click(button(d, "Start")!);
    await tick(3, 10);
    expect(text(host.querySelector("[data-testid=marvel0-refused]"))).toContain(
      "Server only",
    );
  });

  test("with no answer at all the next press repeats the same request id", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    state.answer = () => errorOf(0, {}) as ApiResult;
    const send = async () => {
      await click(button(comp(host, "A"), "Start")!);
      const d = dialog(host)!;
      await type(
        d.querySelector("input:not([type=checkbox])"),
        "start arm stack",
      );
      for (const label of d.querySelectorAll("label"))
        if (label.querySelector("input[type=checkbox]"))
          await click(label.querySelector("input")!);
      await click(button(d, "Start")!);
      await tick(3, 10);
    };
    await send();
    // errorOf(0, {}) is an HTTP-0 failure: the id is kept.
    state.answer = () => ({ ok: true, status: 202, data: {} });
    await send();
    expect(state.calls).toHaveLength(2);
    expect(state.calls[0].body!.request_id).toBe(
      state.calls[1].body!.request_id,
    );
  });
});

describe("the other writes", () => {
  test("stopping C with a client on it asks for the override phrase as well", async () => {
    const { host, state } = await open(stackDoc("c_ready_with_client"));
    const stop = button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!;
    expect(stop.disabled).toBe(false);
    await click(stop);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.ctl.hazard.stop_C"]);
    expect(text(d)).toContain(en["marvel0.ctl.dialog.client_on_server"]);
    const confirm = () => button(d, "Stop")!;
    const inputs = () =>
      Array.from(
        d.querySelectorAll<HTMLInputElement>("input:not([type=checkbox])"),
      );
    await type(inputs()[0], "stop robot server");
    expect(confirm().disabled).toBe(true);
    await type(inputs()[1], "stop robot server with client connected");
    expect(confirm().disabled).toBe(false);
    await click(confirm());
    await tick(3, 10);
    expect(state.calls[0]).toMatchObject({ op: "stop", comp: "C" });
    expect(state.calls[0].body).toMatchObject({
      confirm: "stop robot server",
      client_override: "stop robot server with client connected",
    });
  });

  test("the gripper test is a tick and a click and sends the page's phrase", async () => {
    const { host, state } = await open(stackDoc("b_ready"));
    const test_ = host.querySelector(
      "[data-testid=marvel0-gripper-test]",
    ) as HTMLElement;
    await click(button(test_, "Open gripper")!);
    const d = dialog(host)!;
    expect(d.querySelectorAll("input:not([type=checkbox])")).toHaveLength(0);
    expect(text(d)).toContain(en["marvel0.ctl.hazard.gripper"]);
    expect(button(d, "Open gripper")!.disabled).toBe(true);
    await tickBox(d, /Hands and objects are clear/);
    await click(button(d, "Open gripper")!);
    await tick(3, 10);
    expect(state.calls[0]).toMatchObject({ op: "gripper" });
    expect(state.calls[0].body).toMatchObject({
      action: "open",
      confirm: "gripper open",
      attest: { hands_clear: true },
    });
  });

  test("the last gripper result is shown: reached, or stalled", async () => {
    const ok = await open(stackDoc("gripper_success"));
    expect(
      ok.host
        .querySelector("[data-testid=marvel0-gripper-last]")!
        .getAttribute("data-outcome"),
    ).toBe("reached");
    const bad = await open(stackDoc("gripper_failed"));
    const last = bad.host.querySelector("[data-testid=marvel0-gripper-last]")!;
    expect(last.getAttribute("data-outcome")).toBe("stalled");
    expect(text(last)).toContain("stalled");
  });

  test("A's FCI check and the checks before C are one click, no dialog", async () => {
    const { host, state } = await open(stackDoc("idle"));
    await click(button(comp(host, "A"), "Check FCI")!);
    await tick(3, 10);
    expect(dialog(host)).toBeNull();
    expect(state.calls).toEqual([
      { op: "preflight", comp: "A", body: { request_id: expect.any(String) } },
    ]);
    const c = await open(stackDoc("b_ready"));
    await click(button(comp(c.host, "C"), "Check before C")!);
    await tick(3, 10);
    expect(c.state.calls[0]).toMatchObject({ op: "preflight", comp: "C" });
  });

  test("the marker: the countdown, adopt, set", async () => {
    const young = stackDoc("idle", (doc) => {
      doc.act.marker = {
        state: "present",
        age_s: 20,
        ctime_age_s: 20,
        ours: true,
        wait_s: 60,
        remaining_s: 40,
      };
    });
    const a = await open(young);
    const countdown = a.host.querySelector(
      "[data-testid=marvel0-marker-countdown]",
    )!;
    expect(
      Number(countdown.getAttribute("data-remaining")),
    ).toBeLessThanOrEqual(40);
    expect(Number(countdown.getAttribute("data-remaining"))).toBeGreaterThan(
      30,
    );
    expect(text(countdown)).toContain("A may start in");
    const foreign = await open(
      stackDoc("idle", (doc) => {
        doc.act.marker = {
          state: "present",
          age_s: 9,
          ours: false,
          wait_s: 60,
          remaining_s: 51,
        };
      }),
    );
    await click(button(foreign.host, "Adopt marker")!);
    const d = dialog(foreign.host)!;
    expect(d.querySelectorAll("input:not([type=checkbox])")).toHaveLength(0);
    await click(button(d, "Adopt marker")!);
    await tick(3, 10);
    expect(foreign.state.calls[0]).toMatchObject({ op: "marker" });
    expect(foreign.state.calls[0].body).toMatchObject({
      op: "adopt",
      confirm: "adopt marker",
    });
    const none = await open(stackDoc("idle"));
    await click(button(none.host, "Set marker")!);
    await click(button(dialog(none.host)!, "Set marker")!);
    await tick(3, 10);
    expect(none.state.calls[0].body).toMatchObject({
      op: "touch",
      confirm: "set marker",
    });
  });

  test("the guided start asks A's checks and A's phrase; while it waits for B the Start of B is marked", async () => {
    const { host, state } = await open(stackDoc("idle"));
    await click(button(host, "Start whole stack")!);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.ctl.hazard.sequence"]);
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start arm stack",
    );
    for (const label of d.querySelectorAll("label"))
      if (label.querySelector("input[type=checkbox]"))
        await click(label.querySelector("input")!);
    await click(button(d, "Start whole stack")!);
    await tick(3, 10);
    expect(state.calls[0]).toMatchObject({ op: "sequence" });
    expect(state.calls[0].body).toMatchObject({
      confirm: "start arm stack",
      attest: { desk_done: true, estop_hand: true },
    });
    const waiting = await open(stackDoc("sequence_waiting_b"));
    expect(
      text(waiting.host.querySelector("[data-testid=marvel0-sequence-wait]")),
    ).toContain("Gripper stack (B)");
    expect(comp(waiting.host, "B").getAttribute("data-next")).toBe("true");
    expect(comp(waiting.host, "C").getAttribute("data-next")).toBeNull();
    // The guided start never starts B or C by itself: its Start is the person's.
    expect(button(comp(waiting.host, "B"), "Start")!.disabled).toBe(false);
    await click(button(waiting.host, "Cancel guided start")!);
    await tick(3, 10);
    expect(waiting.state.calls[0]).toMatchObject({ op: "sequenceCancel" });
  });
});

describe("what is shown while a terminal starts", () => {
  test("the ready lines seen and still missing, and the seconds waited", async () => {
    const { host } = await open(stackDoc("a_starting"));
    const ready = host.querySelector("[data-testid=marvel0-ready-A]")!;
    expect(
      Array.from(ready.querySelectorAll(".rg-ready__seen")).map((l) =>
        l.getAttribute("data-key"),
      ),
    ).toEqual(["fifo"]);
    expect(
      Array.from(ready.querySelectorAll(".rg-ready__missing")).map((l) =>
        l.getAttribute("data-key"),
      ),
    ).toEqual(["spawners", "health_hardware_active"]);
    expect(text(ready)).toContain("1 of 3 ready signals seen");
    const waited = text(ready.querySelector("[data-testid=marvel0-waited-A]"));
    expect(waited).toMatch(/Waiting for 1[2-9] s of at most 90 s/);
    expect(
      ready.querySelector("[role=progressbar]")!.getAttribute("aria-valuenow"),
    ).toBe("1");
  });

  test("a terminal that is ready keeps its evidence one click away; a failed one names its failure", async () => {
    const ready = await open(stackDoc("a_ready"));
    expect(
      ready.host.querySelector("[data-testid=marvel0-ready-A]")!.tagName,
    ).toBe("DETAILS");
    const failed = await open(stackDoc("a_failed"));
    expect(
      failed.host.querySelector("[data-testid=marvel0-failure-A]"),
    ).not.toBeNull();
  });

  test("the notices of the backend are said: the open port of C, the arm that is still held", async () => {
    const { host } = await open(stackDoc("c_stopped_arm_held"));
    const codes = Array.from(
      host.querySelectorAll("[data-testid=marvel0-notice]"),
    ).map((n) => n.getAttribute("data-code"));
    expect(codes).toEqual(["notice.arm_still_held"]);
    expect(text(host.querySelector("[data-testid=marvel0-notice]"))).toContain(
      en["marvel0.ctl.notice.arm_still_held"],
    );
    expect(
      text(comp(host, "C").querySelector("[data-testid=marvel0-stopnote-C]")),
    ).toContain(en["marvel0.ctl.stop_note.c.stop_arm_still_held"]);
  });
});

describe("what the page says about itself", () => {
  test("it says that it is not verified on the real machine and what the gates are worth", async () => {
    const { host } = await open(stackDoc("idle"));
    const honest = text(
      host.querySelector("[data-testid=marvel0-stack-honest]"),
    );
    expect(honest).toBe(en["marvel0.ctl.honest"]);
    expect(honest).toContain("Not verified on the real machine");
    expect(honest).toContain("stop mistakes, not another program");
  });

  test("no control of the step is called an emergency stop", async () => {
    const { host } = await open(stackDoc("c_ready"));
    for (const node of host.querySelectorAll(
      "[data-testid=marvel0-step-arm] button, summary",
    ))
      expect(node.textContent).not.toMatch(/e-?stop|emergency/i);
  });

  test("in Chinese every line is worded, with no English backend sentence in sight", async () => {
    const { host } = await open(stackDoc("a_starting"), true);
    const body = text(host.querySelector("[data-testid=marvel0-step-arm]"));
    expect(body).toContain(zh["marvel0.step.arm"]);
    expect(body).toContain(zh["marvel0.ctl.ready.fifo"]);
    expect(body).not.toContain("The control node got realtime priority");
    expect(body).toContain(zh["marvel0.ctl.honest"]);
  });
});

describe("a dialog confirms the state it was opened on", () => {
  test("when the stack can no longer be read, the dialog closes and what was typed is gone", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    await type(
      dialog(host)!.querySelector("input:not([type=checkbox])"),
      "start arm stack",
    );
    state.doc = null;
    await waitFor(() => dialog(host) === null, {
      label: "the dialog to close",
    });
    expect(state.calls).toHaveLength(0);
  });

  test("when the backend stops offering the action, the dialog closes", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    expect(dialog(host)).not.toBeNull();
    // Something else started A meanwhile.
    state.doc = stackDoc("a_starting");
    await waitFor(() => dialog(host) === null, {
      label: "the dialog to close",
    });
  });
});

describe("polling", () => {
  test("every read stops while the tab is hidden and one is made at once on return", async () => {
    const { state } = await open(stackDoc("idle"));
    await waitFor(() => state.reads >= 3, { label: "reads" });
    const hide = (hidden: boolean) => {
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => (hidden ? "hidden" : "visible"),
      });
      document.dispatchEvent(new Event("visibilitychange"));
    };
    await act(async () => hide(true));
    const at = state.reads;
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 150));
    });
    expect(state.reads - at).toBeLessThanOrEqual(1);
    const hiddenAt = state.reads;
    await act(async () => hide(false));
    await waitFor(() => state.reads > hiddenAt, {
      label: "the read on return",
    });
  });

  test("the age of the answer is on the page and counts", async () => {
    const { host } = await open(stackDoc("idle"));
    const age = host.querySelector("[data-testid=marvel0-stack-age]")!;
    expect(age.textContent).toMatch(/^Updated \d+ s ago$/);
  });
});
