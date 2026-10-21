import { act } from "react";
import {
  click,
  press,
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
import {
  button,
  doubleClick,
  textFields,
  tick,
  tickAll,
  type,
} from "./actions-helpers";
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
    sequenceStop: write("sequenceStop") as StackApi["sequenceStop"],
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
  test("the confirm window says what the action does, asks the ticks, has Cancel in focus and sends one request on a double click", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    const d = dialog(host)!;
    expect(text(d)).toContain("Start: Arm controller (A)");
    // The fixed sentence, the hazard, what the gates are worth.
    expect(text(d)).toContain(en["marvel0.ctl.estop"]);
    expect(text(d)).toContain(en["marvel0.ctl.hazard.start_A"]);
    expect(text(d)).toContain(en["marvel0.stack.gates_scope"]);
    expect(text(d)).toContain(en["marvel0.ctl.unverified"]);
    // No control in the window is called an emergency stop.
    for (const b of d.querySelectorAll("button"))
      expect(b.textContent).not.toMatch(/e-?stop|emergency/i);
    // Nothing is typed and nothing asks for a phrase; Cancel has the focus, so
    // a stray Enter cancels.
    expect(textFields(d)).toHaveLength(0);
    expect(text(d)).not.toMatch(/type exactly|confirmation phrase/i);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    const go = () => button(d, "Start")!;
    expect(go().disabled).toBe(true);
    await tickBox(d, /Desk is done/);
    expect(go().disabled).toBe(true); // one check is still not ticked
    await tickBox(d, /hand is on the physical e-stop/);
    expect(go().disabled).toBe(false);
    await doubleClick(go());
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({ op: "start", comp: "A" });
    // The page sent the fixed phrase of the catalogue itself.
    expect(state.calls[0].body).toEqual({
      request_id: expect.any(String),
      confirm: "start arm stack",
      attest: { desk_done: true, estop_hand: true },
    });
    expect(
      String(state.calls[0].body!.request_id).length,
    ).toBeGreaterThanOrEqual(8);
    expect(dialog(host)).toBeNull();
  });

  test("Cancel, Escape and the backdrop close the window and send nothing; a tick does not outlive its window", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    const a = comp(host, "A");
    await click(button(a, "Start")!);
    await tickAll(dialog(host)!);
    expect(button(dialog(host)!, "Start")!.disabled).toBe(false);
    await click(button(dialog(host)!, "Cancel")!);
    expect(dialog(host)).toBeNull();
    // Opened again, nothing is ticked.
    await click(button(a, "Start")!);
    expect(
      Array.from(
        dialog(host)!.querySelectorAll<HTMLInputElement>(
          "input[type=checkbox]",
        ),
      ).map((box) => box.checked),
    ).toEqual([false, false]);
    expect(button(dialog(host)!, "Start")!.disabled).toBe(true);
    await press(document.activeElement, "Escape");
    expect(dialog(host)).toBeNull();
    await click(button(a, "Start")!);
    await click(host.ownerDocument.querySelector(".ds-scrim"));
    expect(dialog(host)).toBeNull();
    // A stray Enter on the focused Cancel cancels: Cancel is a button, and
    // the click an Enter makes on it is the one that closed the window above.
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
    await tickAll(d);
    await click(button(d, "Start")!);
    expect(state.calls).toHaveLength(1);
    // The write is open: the card says it is working, and no other control acts.
    expect(host.querySelector("[data-testid=marvel0-working]")).not.toBeNull();
    expect(button(comp(host, "M"), "Start")!.disabled).toBe(true);
    // The window cannot be left, nor the request sent twice, while it is on its way.
    expect(button(d, "Cancel")!.disabled).toBe(true);
    await click(button(d, "Start")!);
    expect(state.calls).toHaveLength(1);
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
      await tickAll(d);
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
    await tickAll(d);
    await click(button(d, "Start")!);
    await tick(3, 10);
    expect(text(host.querySelector("[data-testid=marvel0-refused]"))).toContain(
      "Server only",
    );
  });

  test("the backend refusing the phrase is an internal error with the mismatch, not a prompt to type", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    state.answer = () =>
      errorOf(400, {
        detail: {
          code: "confirm_required",
          message: "Type the confirmation phrase exactly: start arm stack",
          phrase: "start arm stack v2",
        },
      });
    await click(button(comp(host, "A"), "Start")!);
    await tickAll(dialog(host)!);
    await click(button(dialog(host)!, "Start")!);
    await tick(3, 10);
    // The window stays so that the error is read; it has no field to type in.
    const d = dialog(host)!;
    expect(textFields(d)).toHaveLength(0);
    const refused = d.querySelector(
      "[data-testid=marvel0-ctl-dialog-refused]",
    )!;
    expect(refused.getAttribute("data-code")).toBe("confirm_required");
    expect(refused.getAttribute("data-internal")).toBe("true");
    expect(text(refused)).toContain(en["marvel0.ctl.confirm.internal"]);
    expect(text(refused)).toContain("The server expects: start arm stack v2");
    expect(text(refused)).not.toMatch(/type|typed/i);
    expect(text(refused)).not.toContain("Type the confirmation phrase");
    expect(state.calls).toHaveLength(1);
  });

  test("with no answer at all the next press repeats the same request id", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    state.answer = () => errorOf(0, {}) as ApiResult;
    const send = async () => {
      // (the window stays open after a send without an answer, or is opened)
      if (!dialog(host)) await click(button(comp(host, "A"), "Start")!);
      const d = dialog(host)!;
      await tickAll(d);
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
  test("stopping C with a client on it keeps the one typed override; the base phrase is the page's", async () => {
    const { host, state } = await open(stackDoc("c_ready_with_client"));
    const stop = button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!;
    expect(stop.disabled).toBe(false);
    await click(stop);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.ctl.hazard.stop_C"]);
    expect(text(d)).toContain(en["marvel0.ctl.dialog.client_on_server"]);
    const confirm = () => button(d, "Stop")!;
    // The override is the only field, and Cancel has the focus.
    expect(textFields(d)).toHaveLength(1);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(confirm().disabled).toBe(true);
    await type(textFields(d)[0], "stop robot server");
    expect(confirm().disabled).toBe(true); // the base phrase is not the override
    await type(textFields(d)[0], "stop robot server with client connected");
    expect(confirm().disabled).toBe(false);
    await doubleClick(confirm());
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({ op: "stop", comp: "C" });
    expect(state.calls[0].body).toEqual({
      request_id: expect.any(String),
      confirm: "stop robot server",
      client_override: "stop robot server with client connected",
    });
  });

  test("stopping C without a client has nothing to type: the override stays out of the window", async () => {
    const { host, state } = await open(stackDoc("c_ready"));
    await click(button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.ctl.hazard.stop_C"]);
    expect(textFields(d)).toHaveLength(0);
    await click(button(d, "Stop")!);
    await tick(3, 10);
    expect(state.calls[0].body).toEqual({
      request_id: expect.any(String),
      confirm: "stop robot server",
      client_override: "",
    });
  });

  test("a client that appears after the window opened: the refusal asks for the override, which is then typed", async () => {
    const { host, state } = await open(stackDoc("c_ready"));
    state.answer = (call) =>
      (call.body as { client_override: string }).client_override
        ? { ok: true, status: 202, data: { done: "stopping" } }
        : errorOf(409, {
            detail: {
              code: "client_connected",
              message: "A client",
              port: 5056,
            },
          });
    await click(button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!);
    await click(button(dialog(host)!, "Stop")!);
    await tick(3, 10);
    const d = dialog(host)!;
    expect(textFields(d)).toHaveLength(1);
    expect(button(d, "Stop")!.disabled).toBe(true);
    await type(textFields(d)[0], "stop robot server with client connected");
    await click(button(d, "Stop")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(2);
    expect(dialog(host)).toBeNull();
  });

  test("the gripper test is a tick and a click and sends the page's phrase", async () => {
    const { host, state } = await open(stackDoc("b_ready"));
    const test_ = host.querySelector(
      "[data-testid=marvel0-gripper-test]",
    ) as HTMLElement;
    await click(button(test_, "Open gripper")!);
    const d = dialog(host)!;
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(text(d)).toContain(en["marvel0.ctl.hazard.gripper"]);
    expect(button(d, "Open gripper")!.disabled).toBe(true);
    await tickBox(d, /Hands and objects are clear/);
    await doubleClick(button(d, "Open gripper")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
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

  test("A's FCI check and the checks before C are one click, no window", async () => {
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

  test("the marker: the countdown, adopt, set, release; each one click and a window, nothing typed", async () => {
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
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
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

  test("releasing the marker is a window with a danger Confirm, and no phrase to type", async () => {
    // (offered when A is gone and the arm shows no control connection)
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(host, "Release marker")!);
    const d = dialog(host)!;
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    const go = button(d, "Release marker")!;
    expect(go.className).toContain("ds-btn--danger");
    await doubleClick(go);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0].body).toMatchObject({
      op: "release",
      confirm: "clear marker",
    });
  });

  test("the whole stack's start is one window: every hazard line and every check of A, B and C, the pause switch off, the page's phrase, nothing to type", async () => {
    const { host, state } = await open(stackDoc("idle"));
    await click(button(host, "Start whole stack")!);
    const d = dialog(host)!;
    // The sentences of the three single starts, all of them, and the e-stop.
    for (const key of [
      "marvel0.ctl.hazard.sequence_through",
      "marvel0.ctl.hazard.start_A",
      "marvel0.ctl.hazard.start_B",
      "marvel0.ctl.hazard.start_C_sag",
      "marvel0.ctl.hazard.start_C_port",
    ])
      expect(text(d)).toContain(en[key as keyof typeof en]);
    expect(text(d)).toContain(en["marvel0.ctl.estop"]);
    expect(text(d)).toContain(en["marvel0.stack.gates_scope"]);
    expect(text(d)).toContain(en["marvel0.ctl.unverified"]);
    // Four checks, the pause is a switch and not one of them.
    const boxes = Array.from(
      d.querySelectorAll<HTMLInputElement>(
        "input[type=checkbox]:not([role=switch])",
      ),
    );
    expect(boxes).toHaveLength(4);
    for (const label of ["desk_done", "estop_hand", "hands_clear", "no_client"])
      expect(text(d)).toContain(
        en[`marvel0.ctl.attest.${label}` as keyof typeof en],
      );
    const pause = d.querySelector<HTMLInputElement>("input[role=switch]")!;
    expect(pause.checked).toBe(false);
    expect(text(d)).toContain(en["marvel0.ctl.dialog.sequence_pause"]);
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    // No Confirm until every check is ticked; the switch does not count.
    expect(button(d, "Start whole stack")!.disabled).toBe(true);
    await click(pause);
    expect(button(d, "Start whole stack")!.disabled).toBe(true);
    await click(pause);
    await tickAll(d);
    expect(pause.checked).toBe(false);
    expect(button(d, "Start whole stack")!.disabled).toBe(false);
    await doubleClick(button(d, "Start whole stack")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({ op: "sequence" });
    expect(state.calls[0].body).toEqual({
      request_id: expect.any(String),
      confirm: "start arm stack",
      attest: {
        desk_done: true,
        estop_hand: true,
        hands_clear: true,
        no_client: true,
      },
      through: "c",
      pause_before_b: false,
      pause_before_c: false,
    });
  });

  test("with the pause switched on it is sent, and the switch is off again at the next opening", async () => {
    const { host, state } = await open(stackDoc("idle"));
    await click(button(host, "Start whole stack")!);
    let d = dialog(host)!;
    await tickAll(d);
    await click(d.querySelector("input[role=switch]")!);
    await click(button(d, "Start whole stack")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0].body).toMatchObject({
      through: "c",
      pause_before_b: true,
      pause_before_c: true,
    });
    // A cancelled window keeps nothing: ticks and the switch are fresh.
    state.calls.length = 0;
    await click(button(host, "Start whole stack")!);
    d = dialog(host)!;
    await click(d.querySelector("input[role=switch]")!);
    await click(button(d, "Cancel")!);
    await click(button(host, "Start whole stack")!);
    d = dialog(host)!;
    expect(
      d.querySelector<HTMLInputElement>("input[role=switch]")!.checked,
    ).toBe(false);
    expect(
      Array.from(
        d.querySelectorAll<HTMLInputElement>(
          "input[type=checkbox]:not([role=switch])",
        ),
      ).every((box) => !box.checked),
    ).toBe(true);
    expect(state.calls).toHaveLength(0);
  });

  test("a start that waits for B and C (the pause was on) leaves their Start to the person, with its own window; Cancel ends it", async () => {
    const waiting = await open(stackDoc("sequence_waiting_b"));
    expect(
      text(waiting.host.querySelector("[data-testid=marvel0-sequence-wait]")),
    ).toContain("Gripper stack (B)");
    expect(comp(waiting.host, "B").getAttribute("data-next")).toBe("true");
    expect(comp(waiting.host, "C").getAttribute("data-next")).toBeNull();
    // The guided start never starts B or C by itself: its Start is the person's,
    // and it has its own window.
    expect(button(comp(waiting.host, "B"), "Start")!.disabled).toBe(false);
    await click(button(comp(waiting.host, "B"), "Start")!);
    expect(dialog(waiting.host)).not.toBeNull();
    expect(textFields(dialog(waiting.host)!)).toHaveLength(0);
    await click(button(dialog(waiting.host)!, "Cancel")!);
    await click(button(waiting.host, "Cancel guided start")!);
    await tick(3, 10);
    expect(waiting.state.calls[0]).toMatchObject({ op: "sequenceCancel" });
  });
});

describe("no window of the arm stack asks for a typed phrase", () => {
  // Every start, stop, marker and gripper window, with the stack in the state
  // in which its button is on: ticks and a click, nothing to type. The one
  // field that exists is the override of a stop of C with a client on it.
  const cases: Array<{
    name: string;
    view: string;
    open: (host: HTMLElement) => Promise<void> | void;
    fields: number;
    change?: (doc: Record<string, any>) => void; // eslint-disable-line @typescript-eslint/no-explicit-any
  }> = [
    {
      name: "start M",
      view: "idle",
      fields: 0,
      open: (h) => click(button(comp(h, "M"), "Start")!),
    },
    {
      name: "start A",
      view: "ready_to_start_a",
      fields: 0,
      open: (h) => click(button(comp(h, "A"), "Start")!),
    },
    {
      name: "start B",
      view: "a_ready",
      fields: 0,
      open: (h) => click(button(comp(h, "B"), "Start")!),
    },
    {
      name: "start C",
      view: "c_stopped_arm_held",
      fields: 0,
      open: (h) => click(button(comp(h, "C"), "Start")!),
    },
    {
      name: "stop M",
      view: "monitor_ready",
      fields: 0,
      open: (h) => click(button(comp(h, "M"), "Stop")!),
    },
    {
      name: "stop A",
      view: "a_ready",
      fields: 0,
      open: (h) => click(button(comp(h, "A"), en["marvel0.ctl.btn.stop_A"])!),
    },
    {
      name: "stop B",
      view: "b_ready",
      fields: 0,
      open: (h) => click(button(comp(h, "B"), "Stop")!),
    },
    {
      name: "stop C",
      view: "c_ready",
      fields: 0,
      open: (h) => click(button(comp(h, "C"), en["marvel0.ctl.btn.stop_C"])!),
    },
    {
      name: "stop C with a client (the override)",
      view: "c_ready_with_client",
      fields: 1,
      open: (h) => click(button(comp(h, "C"), en["marvel0.ctl.btn.stop_C"])!),
    },
    {
      name: "gripper close",
      view: "b_ready",
      fields: 0,
      open: (h) => click(button(h, "Close gripper")!),
    },
    {
      name: "set marker",
      view: "idle",
      fields: 0,
      open: (h) => click(button(h, "Set marker")!),
    },
    {
      name: "whole stack",
      view: "idle",
      fields: 0,
      open: (h) => click(button(h, "Start whole stack")!),
    },
    {
      name: "stop whole stack",
      view: "stop_all_ready",
      fields: 0,
      open: (h) => click(button(h, "Stop whole stack")!),
    },
  ];
  for (const c of cases)
    test(c.name, async () => {
      const { host, state } = await open(stackDoc(c.view, c.change));
      await c.open(host);
      const d = dialog(host)!;
      expect(d).not.toBeNull();
      expect(textFields(d)).toHaveLength(c.fields);
      expect(document.activeElement).toBe(button(d, "Cancel"));
      expect(state.calls).toHaveLength(0);
      // Nothing in the window reads as a prompt to type.
      expect(text(d)).not.toMatch(/Confirmation phrase|Type the phrase/);
    });
});

describe("Stop whole stack", () => {
  const stopAll = (host: HTMLElement) =>
    host.querySelector<HTMLButtonElement>("[data-testid=marvel0-stop-all]")!;
  const progress = (host: HTMLElement) =>
    host.querySelector("[data-testid=marvel0-sequence-progress]");

  test("it sits beside Start whole stack in step 1 and is on exactly when the backend says so", async () => {
    const on = await open(stackDoc("stop_all_ready"));
    const block = on.host.querySelector("[data-testid=marvel0-sequence]")!;
    // Same row of buttons as Start whole stack.
    expect(stopAll(on.host).closest(".rg-comp__buttons")).toBe(
      button(on.host, "Start whole stack")!.closest(".rg-comp__buttons"),
    );
    expect(block.contains(stopAll(on.host))).toBe(true);
    expect(stopAll(on.host).textContent).toBe("Stop whole stack");
    expect(stopAll(on.host).disabled).toBe(false);
    expect(
      on.host.querySelector("[data-testid=marvel0-block-stop-all]"),
    ).toBeNull();
    // Stopping ends programs: the outlined danger button, never a solid one.
    expect(stopAll(on.host).className).toContain("rg-btn--danger-outline");
    expect(stopAll(on.host).className).not.toContain("ds-btn--danger ");
    // Nothing of ours runs: off, and the backend's reason is beside it.
    const off = await open(stackDoc("stop_all_nothing"));
    expect(stopAll(off.host).disabled).toBe(true);
    const why = off.host.querySelector("[data-testid=marvel0-block-stop-all]")!;
    expect(text(why)).toContain("Stop whole stack is off:");
    expect(text(why)).toContain(en["marvel0.ctl.block.nothing_to_stop"]);
    // A client on C: the reason names the port and says there is no override.
    const blocked = await open(stackDoc("stop_all_blocked"));
    expect(stopAll(blocked.host).disabled).toBe(true);
    const client = text(
      blocked.host.querySelector("[data-testid=marvel0-block-stop-all]"),
    );
    expect(client).toContain("on port 5000");
    expect(client).toContain("no override phrase");
  });

  test("the window lists what it stops in order, says plainly what the arm does, and asks nothing to type or tick", async () => {
    const { host, state } = await open(stackDoc("stop_all_ready"));
    await click(stopAll(host));
    const d = dialog(host)!;
    expect(text(d)).toContain("Stop the whole stack");
    expect(text(d)).toContain(en["marvel0.ctl.dialog.stop_all_intro"]);
    // C, then A, then B; M stays.
    const order = text(
      d.querySelector("[data-testid=marvel0-ctl-dialog-hazards]"),
    );
    expect(order).toContain(en["marvel0.ctl.hazard.stop_all_order"]);
    expect(en["marvel0.ctl.hazard.stop_all_order"]).toMatch(
      /C .*then A .*then B/,
    );
    expect(text(d)).toContain(
      en["marvel0.ctl.dialog.stop_all_intro"].split(".")[2],
    );
    // The arm: held until A stops, then held by nothing.
    expect(text(d)).toContain(en["marvel0.ctl.hazard.stop_all_arm"]);
    expect(en["marvel0.ctl.hazard.stop_all_arm"]).toContain(
      "impedance control",
    );
    expect(en["marvel0.ctl.hazard.stop_all_arm"]).toContain(
      "nothing holds the arm any more",
    );
    expect(text(d)).toContain(en["marvel0.ctl.hazard.stop_all_client"]);
    // Honest: the e-stop sentence, what the gates are worth, not verified.
    expect(text(d)).toContain(en["marvel0.ctl.estop"]);
    expect(text(d)).toContain(en["marvel0.stack.gates_scope"]);
    expect(text(d)).toContain(en["marvel0.ctl.unverified"]);
    for (const b of d.querySelectorAll("button"))
      expect(b.textContent).not.toMatch(/e-?stop|emergency/i);
    expect(textFields(d)).toHaveLength(0);
    expect(d.querySelectorAll("input[type=checkbox]")).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    // The Confirm button is the danger one.
    const go = button(d, "Stop whole stack")!;
    expect(go.className).toContain("ds-btn--danger");
    expect(go.disabled).toBe(false);
    expect(state.calls).toHaveLength(0);
  });

  test("Confirm sends one request with the page's phrase, whatever the clicks; Cancel and Escape send nothing", async () => {
    const { host, state } = await open(stackDoc("stop_all_ready"));
    await click(stopAll(host));
    await click(button(dialog(host)!, "Cancel")!);
    expect(dialog(host)).toBeNull();
    await click(stopAll(host));
    await press(document.activeElement, "Escape");
    expect(dialog(host)).toBeNull();
    expect(state.calls).toHaveLength(0);
    state.answer = () => ({
      ok: true,
      status: 202,
      data: STACK_ANSWERS.sequence_stop,
    });
    await click(stopAll(host));
    await doubleClick(button(dialog(host)!, "Stop whole stack")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({ op: "sequenceStop" });
    expect(state.calls[0].body).toEqual({
      request_id: expect.any(String),
      confirm: "stop whole stack",
      attest: {},
    });
    expect(dialog(host)).toBeNull();
  });

  test("the window stays shut for a stack that cannot be read, and closes when the backend stops offering the stop", async () => {
    const { host, state } = await open(stackDoc("stop_all_ready"));
    await click(stopAll(host));
    expect(dialog(host)).not.toBeNull();
    // Someone else's stop began meanwhile: the window is gone, and so is the offer.
    state.doc = stackDoc("stop_all_started");
    await waitFor(() => dialog(host) === null, {
      label: "the window to close",
    });
    expect(stopAll(host).disabled).toBe(true);
    expect(state.calls).toHaveLength(0);
  });

  test("while another write is on its way the button waits, and it shows working on its own click", async () => {
    let release: (value: ApiResult) => void = () => {};
    const { host, state } = await open(stackDoc("stop_all_ready"));
    state.answer = () =>
      new Promise<ApiResult>((resolve) => {
        release = resolve;
      });
    await click(stopAll(host));
    await click(button(dialog(host)!, "Stop whole stack")!);
    expect(state.calls).toHaveLength(1);
    expect(stopAll(host).getAttribute("aria-busy")).toBe("true");
    expect(stopAll(host).disabled).toBe(true);
    expect(button(host, "Start whole stack")!.disabled).toBe(true);
    expect(
      button(comp(host, "C"), en["marvel0.ctl.btn.stop_C"])!.disabled,
    ).toBe(true);
    await act(async () =>
      release({ ok: true, status: 202, data: STACK_ANSWERS.sequence_stop }),
    );
    await waitFor(() => stopAll(host).getAttribute("aria-busy") !== "true", {
      label: "working to end",
    });
  });

  test("the progress: C, then A, then B, each with how far its stop is, and the step it is on", async () => {
    const running = await open(stackDoc("stop_all_running"));
    const steps = Array.from(progress(running.host)!.querySelectorAll("li"));
    expect(steps.map((li) => li.getAttribute("data-comp"))).toEqual([
      "C",
      "A",
      "B",
    ]);
    expect(steps.map((li) => li.getAttribute("data-step-state"))).toEqual([
      "done",
      "running",
      "pending",
    ]);
    expect(steps.map((li) => li.getAttribute("aria-current"))).toEqual([
      null,
      "step",
      null,
    ]);
    expect(text(steps[0])).toContain("Robot server (C)");
    expect(text(steps[0])).toContain("Stopped");
    expect(text(steps[1])).toContain("Stopping");
    expect(text(steps[2])).toContain("Waiting");
    expect(
      text(running.host.querySelector("[data-testid=marvel0-sequence-step]")),
    ).toBe("Now: stopping A");
    // While it runs: its state is "Stopping", the stop and start buttons are
    // off and the one live control is the cancel of the guided stop.
    const seq = running.host.querySelector("[data-testid=marvel0-sequence]")!;
    expect(seq.getAttribute("data-kind")).toBe("stop");
    expect(text(seq.querySelector(".ds-badge"))).toBe("Stopping");
    expect(stopAll(running.host).disabled).toBe(true);
    expect(button(running.host, "Start whole stack")!.disabled).toBe(true);
    // The state and the steps say it runs: no "off" block that repeats it.
    expect(
      running.host.querySelector("[data-testid=marvel0-block-stop-all]"),
    ).toBeNull();
    await click(button(running.host, "Cancel guided stop")!);
    await tick(3, 10);
    expect(running.state.calls).toEqual([
      { op: "sequenceCancel", body: { request_id: expect.any(String) } },
    ]);
  });

  test("done, failed and cancelled are told; a stop that failed or was cancelled can be pressed again", async () => {
    const done = await open(stackDoc("stop_all_done"));
    expect(
      Array.from(progress(done.host)!.querySelectorAll("li")).map((li) =>
        li.getAttribute("data-step-state"),
      ),
    ).toEqual(["done", "done", "done"]);
    // The words come from the steps that were done, not from the plan.
    const finished = text(
      done.host.querySelector("[data-testid=marvel0-sequence-done]"),
    );
    expect(finished).toBe(
      `Stopped: C, A, B. ${en["marvel0.ctl.seq.stop_done"]}`,
    );
    expect(finished).toContain("M keeps running");
    expect(
      text(done.host.querySelector("[data-testid=marvel0-sequence] .ds-badge")),
    ).toBe("Stopped");
    expect(stopAll(done.host).disabled).toBe(true);

    const failed = await open(stackDoc("stop_all_failed"));
    expect(
      Array.from(progress(failed.host)!.querySelectorAll("li")).map((li) =>
        li.getAttribute("data-step-state"),
      ),
    ).toEqual(["done", "failed", "not_reached"]);
    // The step after the failure was never reached: it does not read "Waiting".
    expect(text(progress(failed.host)!.querySelectorAll("li")[2])).toContain(
      en["marvel0.ctl.seq.step_state.not_reached"],
    );
    expect(text(progress(failed.host))).not.toContain("Waiting");
    const why = failed.host.querySelector(
      "[data-testid=marvel0-sequence-reason]",
    )!;
    expect(why.className).toContain("rg-bad");
    // Where it failed, why, and what pressing again does.
    expect(text(why)).toBe(
      `Failed at Arm controller (A): ${en["marvel0.ctl.block.stop_failed"]}`,
    );
    expect(
      text(failed.host.querySelector("[data-testid=marvel0-sequence-retry]")),
    ).toBe(en["marvel0.ctl.seq.retry_stop"]);
    expect(
      text(
        failed.host.querySelector("[data-testid=marvel0-sequence] .ds-badge"),
      ),
    ).toBe("Stop failed");
    // Pressing again skips what is stopped: the button is on, no cancel is left.
    expect(stopAll(failed.host).disabled).toBe(false);
    expect(button(failed.host, "Cancel guided stop")).toBeUndefined();

    const cancelled = await open(stackDoc("stop_all_cancelled"));
    expect(
      text(
        cancelled.host.querySelector("[data-testid=marvel0-sequence-reason]"),
      ),
    ).toBe(en["marvel0.ctl.seq.reason.cancelled"]);
    expect(stopAll(cancelled.host).disabled).toBe(false);
  });

  test("a refusal of the request is told in words from its code, under the buttons once the window is gone", async () => {
    for (const [name, expected] of [
      [
        "sequence_stop_with_client",
        en["marvel0.ctl.block.stop_all_client_connected"].replace(
          "{port}",
          "5000",
        ),
      ],
      ["sequence_stop_run_active", en["marvel0.ctl.block.run_active"]],
    ] as const) {
      const { host, state } = await open(stackDoc("stop_all_ready"));
      const refusal = STACK_REFUSALS[name];
      state.answer = () => errorOf(refusal.status, { detail: refusal.detail });
      await click(stopAll(host));
      await click(button(dialog(host)!, "Stop whole stack")!);
      await tick(3, 10);
      expect(dialog(host)).toBeNull();
      const shown = host.querySelector(
        "[data-testid=marvel0-toolbar-refused]",
      )!;
      expect(shown.getAttribute("role")).toBe("alert");
      expect(text(shown)).toContain(expected);
      expect(shown.querySelector("[lang=en]")).toBeNull();
    }
  });

  test("the backend refusing the phrase is an internal error with the mismatch, never a prompt to type", async () => {
    const { host, state } = await open(stackDoc("stop_all_ready"));
    const refusal = STACK_REFUSALS.sequence_stop_wrong_phrase;
    state.answer = () => errorOf(refusal.status, { detail: refusal.detail });
    await click(stopAll(host));
    await click(button(dialog(host)!, "Stop whole stack")!);
    await tick(3, 10);
    const d = dialog(host)!;
    const shown = d.querySelector("[data-testid=marvel0-ctl-dialog-refused]")!;
    expect(shown.getAttribute("data-internal")).toBe("true");
    expect(text(shown)).toContain(en["marvel0.ctl.confirm.internal"]);
    expect(text(shown)).toContain("The server expects: stop whole stack");
    expect(text(shown)).not.toContain("Type the confirmation phrase");
    expect(textFields(d)).toHaveLength(0);
  });

  test("a stop needs no control file, but a worktree or temporary instance only shows", async () => {
    const disabled = await open(
      stackDoc("stop_all_ready", (doc) => {
        doc.act.enabled = false;
      }),
    );
    expect(stopAll(disabled.host).disabled).toBe(false);
    const fake = await open(
      stackDoc("stop_all_ready", (doc) => {
        doc.profile = "fake";
      }),
    );
    expect(stopAll(fake.host).disabled).toBe(true);
    // No reason block on an instance that only shows: the page says so once.
    expect(
      fake.host.querySelector("[data-testid=marvel0-block-stop-all]"),
    ).toBeNull();
  });

  test("an older backend without it shows a disabled button and does not break the page", async () => {
    const { host } = await open(
      stackDoc("stop_all_ready", (doc) => {
        delete doc.act.checks.can_stop_all;
        delete doc.act.stop_all_blockers;
        delete doc.act.sequence.kind;
      }),
    );
    expect(stopAll(host).disabled).toBe(true);
    expect(button(host, "Start whole stack")).toBeDefined();
  });

  test("in Chinese the button, the window and the progress are worded", async () => {
    const { host } = await open(stackDoc("stop_all_running"), true);
    expect(stopAll(host).textContent).toBe(zh["marvel0.ctl.seq.stop_button"]);
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-step]")),
    ).toBe("现在：停止 A");
    expect(text(progress(host))).toContain(zh["marvel0.ctl.name.C"]);
    expect(text(progress(host))).toContain(
      zh["marvel0.ctl.seq.step_state.done"],
    );
    const ready = await open(stackDoc("stop_all_ready"), true);
    await click(stopAll(ready.host));
    const d = dialog(ready.host)!;
    expect(text(d)).toContain(zh["marvel0.ctl.dialog.stop_all_title"]);
    expect(text(d)).toContain(zh["marvel0.ctl.hazard.stop_all_arm"]);
    expect(text(d)).not.toContain("Stop the whole stack");
    expect(button(d, zh["marvel0.ctl.btn.cancel"])).toBeDefined();
  });
});

describe("Start whole stack, all the way through C", () => {
  const progress = (host: HTMLElement) =>
    host.querySelector("[data-testid=marvel0-sequence-progress]")!;
  const states = (host: HTMLElement) =>
    Array.from(progress(host).querySelectorAll("li")).map((li) =>
      li.getAttribute("data-step-state"),
    );
  const steps = (host: HTMLElement) =>
    Array.from(progress(host).querySelectorAll("li")).map((li) =>
      li.getAttribute("data-step"),
    );
  const ORDER = [
    "monitor",
    "marker",
    "probe",
    "wait",
    "arm",
    "await_a",
    "b_probe",
    "b",
    "c_preflight",
    "c",
  ];

  test("while it runs: every step of the chain in order with its state, the step it is on, and Cancel; the buttons that start are off", async () => {
    const { host, state } = await open(stackDoc("chain_running_b"));
    expect(steps(host)).toEqual(ORDER);
    expect(states(host)).toEqual([
      "done",
      "done",
      "done",
      "done",
      "done",
      "done",
      "done",
      "running",
      "pending",
      "pending",
    ]);
    const lis = progress(host).querySelectorAll("li");
    expect(lis[7].getAttribute("aria-current")).toBe("step");
    expect(text(lis[7])).toContain(en["marvel0.ctl.seq.chain.b"]);
    expect(text(lis[7])).toContain(
      en["marvel0.ctl.seq.start_step_state.running"],
    );
    expect(text(lis[9])).toContain(
      en["marvel0.ctl.seq.start_step_state.pending"],
    );
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-step]")),
    ).toBe("Now: starting B and waiting for it");
    const seq = host.querySelector("[data-testid=marvel0-sequence]")!;
    expect(seq.getAttribute("data-kind")).toBe("start");
    expect(text(seq.querySelector(".ds-badge"))).toBe("Running");
    expect(
      host.querySelector<HTMLButtonElement>("[data-testid=marvel0-start-all]")!
        .disabled,
    ).toBe(true);
    // The cancel says what it does not do.
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-cancel-note]")),
    ).toBe(en["marvel0.ctl.seq.cancel_note"]);
    await click(button(host, "Cancel guided start")!);
    await tick(3, 10);
    expect(state.calls).toEqual([
      { op: "sequenceCancel", body: { request_id: expect.any(String) } },
    ]);
    // It runs through: no pause line, no waiting line.
    expect(
      host.querySelector("[data-testid=marvel0-sequence-paused-note]"),
    ).toBeNull();
    expect(
      host.querySelector("[data-testid=marvel0-sequence-wait]"),
    ).toBeNull();
  });

  test("the first step, the checks before C and C itself are told in words", async () => {
    for (const [view, now] of [
      ["chain_running_m", "Now: starting M"],
      ["chain_running_c_check", "Now: checking before C"],
      ["chain_running_c", "Now: starting C and waiting for it"],
    ] as const) {
      const { host } = await open(stackDoc(view));
      expect([
        view,
        text(host.querySelector("[data-testid=marvel0-sequence-step]")),
      ]).toEqual([view, now]);
      expect(steps(host)).toEqual(ORDER);
    }
  });

  test("done: all ten steps done and the stack is up", async () => {
    const { host } = await open(stackDoc("chain_done"));
    expect(states(host)).toEqual(Array(10).fill("done"));
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-done]")),
    ).toBe(en["marvel0.ctl.seq.start_done"]);
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence] .ds-badge")),
    ).toBe("Done");
    // Everything is ready: nothing to start, and the page says so.
    expect(
      host.querySelector<HTMLButtonElement>("[data-testid=marvel0-start-all]")!
        .disabled,
    ).toBe(true);
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-needs]")),
    ).toBe(en["marvel0.ctl.seq.needs_all_ready"]);
  });

  test("done: every step that ran says how long it took, and the steps add up", async () => {
    const { host } = await open(stackDoc("chain_done"));
    const took = Array.from(
      progress(host).querySelectorAll("[data-testid=marvel0-step-took]"),
    ).map((x) => Number(x.getAttribute("data-took")));
    expect(took).toHaveLength(10);
    expect(Math.max(...took)).toBeGreaterThan(3); // (the wait of the sample)
    const first = progress(host).querySelector("li")!;
    expect(text(first)).toContain("0 s");
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-took]")),
    ).toBe(
      en["marvel0.ctl.seq.took_total"].replace(
        "{s}",
        String(stackDoc("chain_done").act.sequence.took_s),
      ),
    );
  });

  test("a step that did not run shows no time", async () => {
    const { host } = await open(stackDoc("chain_failed_b"));
    const lis = progress(host).querySelectorAll("li");
    expect(lis[8].querySelector("[data-testid=marvel0-step-took]")).toBeNull();
    expect(
      lis[0].querySelector("[data-testid=marvel0-step-took]"),
    ).not.toBeNull();
  });

  test("waiting for a heavy job: the page says how many, that the end is not known, and has no countdown", async () => {
    const { host } = await open(stackDoc("chain_waiting_heavy"));
    const waiting = host.querySelector(
      "[data-testid=marvel0-sequence-waiting]",
    )!;
    expect(
      Array.from(waiting.querySelectorAll("li")).map((li) =>
        li.getAttribute("data-hold"),
      ),
    ).toEqual(["heavy_jobs_running"]);
    expect(text(waiting)).toContain(en["marvel0.ctl.seq.waiting_on"]);
    expect(text(waiting)).toContain("2 heavy job(s)");
    expect(text(waiting)).toContain("after 90 s the start ends");
    const now = text(host.querySelector("[data-testid=marvel0-sequence-step]"));
    expect(now).toContain("waiting for the marker's age and for heavy jobs");
    expect(now).not.toContain("A may start in"); // (its end is not known)
  });

  test("heavy jobs that cannot be told: the fixed wait of old, said as it is, with a countdown", async () => {
    const { host } = await open(stackDoc("chain_waiting_heavy_unknown"));
    const holds = Array.from(
      host.querySelectorAll("[data-testid=marvel0-sequence-waiting] li"),
    ).map((li) => li.getAttribute("data-hold"));
    expect(holds).toEqual(["heavy_jobs_unknown", "marker_young"]);
    const waiting = text(
      host.querySelector("[data-testid=marvel0-sequence-waiting]"),
    );
    expect(waiting).toContain("fixed wait of 45 s applies");
    expect(waiting).toContain("the marker to be 45 s old (it is 7 s)");
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-step]")),
    ).toContain("A may start in 38 s");
  });

  test("a heavy job that did not stop ends the start: the reason has its count, nothing was started", async () => {
    const { host } = await open(stackDoc("chain_failed_heavy"));
    const why = host.querySelector("[data-testid=marvel0-sequence-reason]")!;
    expect(text(why)).toBe(
      `Failed at ${en["marvel0.ctl.seq.chain.wait"]}: ${en["marvel0.ctl.seq.reason.heavy_job_still_running"].replace("{count}", "2")}`,
    );
    expect(
      host.querySelector("[data-testid=marvel0-sequence-waiting]"),
    ).toBeNull();
    expect(states(host).slice(0, 4)).toEqual([
      "done",
      "done",
      "done",
      "failed",
    ]);
  });

  test("the new words exist in both languages", () => {
    for (const code of [
      "marker_absent",
      "marker_young",
      "heavy_jobs_running",
      "heavy_jobs_unknown",
      "arm_sockets",
    ])
      expect([
        code,
        `marvel0.ctl.seq.hold.${code}` in en &&
          `marvel0.ctl.seq.hold.${code}` in zh,
      ]).toEqual([code, true]);
    for (const key of [
      "marvel0.ctl.seq.waiting_on",
      "marvel0.ctl.seq.took",
      "marvel0.ctl.seq.took_total",
      "marvel0.ctl.block.heavy_jobs_running",
      "marvel0.ctl.seq.reason.heavy_job_still_running",
    ])
      expect([key, key in en && key in zh]).toEqual([key, true]);
  });

  test("failed at B: where, why, the steps after it not reached, and the same button again", async () => {
    const { host, state } = await open(stackDoc("chain_failed_b"));
    expect(states(host)).toEqual([
      "done",
      "done",
      "done",
      "done",
      "done",
      "done",
      "done",
      "failed",
      "not_reached",
      "not_reached",
    ]);
    const lis = progress(host).querySelectorAll("li");
    expect(text(lis[8])).toContain(
      en["marvel0.ctl.seq.start_step_state.not_reached"],
    );
    expect(text(progress(host))).not.toContain("Waiting");
    const why = host.querySelector("[data-testid=marvel0-sequence-reason]")!;
    expect(why.className).toContain("rg-bad");
    expect(text(why)).toBe(
      `Failed at ${en["marvel0.ctl.seq.chain.b"]}: ${en["marvel0.ctl.seq.reason.b_failed"]}`,
    );
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-retry]")),
    ).toBe("Press Start whole stack again: it skips what is already ready.");
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence] .ds-badge")),
    ).toBe("Failed");
    // Pressing again is possible (A is up; B failed, C idle): the window is the
    // same whole-stack window, and it sends the same request.
    const start = host.querySelector<HTMLButtonElement>(
      "[data-testid=marvel0-start-all]",
    )!;
    expect(start.disabled).toBe(false);
    await click(start);
    const d = dialog(host)!;
    await tickAll(d);
    await click(button(d, "Start whole stack")!);
    await tick(3, 10);
    expect(state.calls).toHaveLength(1);
    expect(state.calls[0]).toMatchObject({
      op: "sequence",
      body: { through: "c", pause_before_b: false, pause_before_c: false },
    });
  });

  test("a pause waits for the person's own Start of B, then of C, each with its own window; the chain list does not call the rest 'not reached'", async () => {
    const b = await open(stackDoc("chain_paused_b"));
    expect(states(b.host)).toEqual([
      "done",
      "done",
      "done",
      "done",
      "done",
      "done",
      "pending",
      "pending",
      "pending",
      "pending",
    ]);
    expect(
      text(b.host.querySelector("[data-testid=marvel0-sequence-wait]")),
    ).toContain("Gripper stack (B)");
    expect(
      text(b.host.querySelector("[data-testid=marvel0-sequence] .ds-badge")),
    ).toBe("Waiting for you");
    expect(comp(b.host, "B").getAttribute("data-next")).toBe("true");
    expect(button(comp(b.host, "B"), "Start")!.disabled).toBe(false);
    await click(button(comp(b.host, "B"), "Start")!);
    expect(textFields(dialog(b.host)!)).toHaveLength(0);
    expect(
      dialog(b.host)!.querySelectorAll("input[type=checkbox]"),
    ).toHaveLength(1);
    await click(button(dialog(b.host)!, "Cancel")!);
    const c = await open(stackDoc("chain_paused_c"));
    expect(
      text(c.host.querySelector("[data-testid=marvel0-sequence-wait]")),
    ).toContain("Robot server (C)");
    expect(comp(c.host, "C").getAttribute("data-next")).toBe("true");
    expect(comp(c.host, "B").getAttribute("data-next")).toBeNull();
  });

  test("a start that was asked to pause says where it will stop while it runs", async () => {
    const { host } = await open(
      stackDoc("chain_running_m", (doc) => {
        doc.act.sequence.pauses = { b: true, c: true };
      }),
    );
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-paused-note]")),
    ).toBe(
      "The pause is on: the start stops before B / C and waits for you there.",
    );
  });

  test("A and B up, C not: the same button is offered again and skips what is ready", async () => {
    const { host } = await open(stackDoc("b_ready"));
    const start = host.querySelector<HTMLButtonElement>(
      "[data-testid=marvel0-start-all]",
    )!;
    expect(start.disabled).toBe(false);
    expect(
      host.querySelector("[data-testid=marvel0-sequence-needs]"),
    ).toBeNull();
  });

  test("a failed A is for the person to look at first, with the reason beside the button", async () => {
    const { host } = await open(stackDoc("a_failed"));
    expect(
      host.querySelector<HTMLButtonElement>("[data-testid=marvel0-start-all]")!
        .disabled,
    ).toBe(true);
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-needs]")),
    ).toBe(en["marvel0.ctl.seq.needs_a_unfit"]);
  });

  test("an answer 'already started' (a second tab was first) is told and starts nothing", async () => {
    const { host, state } = await open(stackDoc("b_ready"));
    state.answer = () => ({
      ok: true,
      status: 200,
      data: STACK_ANSWERS.sequence_start_already,
    });
    await click(
      host.querySelector("[data-testid=marvel0-start-all]") as HTMLElement,
    );
    await tickAll(dialog(host)!);
    await click(button(dialog(host)!, "Start whole stack")!);
    await tick(3, 10);
    expect(dialog(host)).toBeNull();
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-already]")),
    ).toBe(en["marvel0.ctl.seq.already_started"]);
  });

  test("a refusal for a missing tick is told in words (the window sends every tick, so this is the page's own mistake only if it ever shows)", async () => {
    const { host, state } = await open(stackDoc("idle"));
    const refusal = STACK_REFUSALS.sequence_start_chain_without_attest;
    state.answer = () => errorOf(refusal.status, { detail: refusal.detail });
    await click(
      host.querySelector("[data-testid=marvel0-start-all]") as HTMLElement,
    );
    await tickAll(dialog(host)!);
    await click(button(dialog(host)!, "Start whole stack")!);
    await tick(3, 10);
    const d = dialog(host)!;
    expect(d).not.toBeNull();
    expect(
      text(d.querySelector("[data-testid=marvel0-ctl-dialog-refused]")),
    ).toContain("hands_clear, no_client");
  });

  test("in Chinese the chain, its states and the failure are worded", async () => {
    const { host } = await open(stackDoc("chain_failed_b"), true);
    expect(text(progress(host))).toContain(zh["marvel0.ctl.seq.chain.b"]);
    expect(text(progress(host))).toContain(
      zh["marvel0.ctl.seq.start_step_state.not_reached"],
    );
    const why = text(
      host.querySelector("[data-testid=marvel0-sequence-reason]"),
    );
    expect(why).toContain(zh["marvel0.ctl.seq.reason.b_failed"]);
    expect(why).toContain(zh["marvel0.ctl.seq.chain.b"]);
    expect(
      text(host.querySelector("[data-testid=marvel0-sequence-retry]")),
    ).toBe(zh["marvel0.ctl.seq.retry_start"]);
    expect(why).not.toContain("Failed at");
  });
});

describe("a guided stop that ended at C because it cannot tell, and one cut short by a restart", () => {
  test("Stop whole stack ended at C: the step is failed, A and B were not reached, the reason says why", async () => {
    const { host } = await open(stackDoc("stop_all_c_unknown"));
    const lis = host.querySelectorAll(
      "[data-testid=marvel0-sequence-progress] li",
    );
    expect(
      Array.from(lis).map((li) => li.getAttribute("data-step-state")),
    ).toEqual(["failed", "not_reached", "not_reached"]);
    const why = text(
      host.querySelector("[data-testid=marvel0-sequence-reason]"),
    );
    expect(why).toContain("Failed at Robot server (C):");
    expect(why).toContain(en["marvel0.ctl.seq.reason.stop_c_unknown"]);
    expect(why).not.toContain("Nothing to stop");
    expect(text(lis[0])).not.toContain("Nothing to stop");
  });

  test("a stop that was asked for and is carried by nothing is a notice that names the unit", async () => {
    const { host } = await open(stackDoc("stop_unfinished"));
    const notice = text(
      host.querySelector(
        "[data-testid=marvel0-notice][data-code='notice.stop_unfinished']",
      ),
    );
    expect(notice).toBe(
      en["marvel0.ctl.notice.stop_unfinished"].replaceAll("{unit}", "C"),
    );
    const zhHost = await open(stackDoc("stop_unfinished"), true);
    expect(
      text(
        zhHost.host.querySelector(
          "[data-testid=marvel0-notice][data-code='notice.stop_unfinished']",
        ),
      ),
    ).toBe(zh["marvel0.ctl.notice.stop_unfinished"].replaceAll("{unit}", "C"));
  });

  test("while a stop runs the starts are off with the backend's reason (the backend's own sample)", async () => {
    const { host } = await open(stackDoc("stop_all_started_blocks_starts"));
    expect(
      text(host.querySelector("[data-testid=marvel0-block-gripper]")),
    ).toContain(en["marvel0.ctl.block.sequence_stopping"]);
    expect(button(host, "Open gripper")!.disabled).toBe(true);
    expect(button(host, "Close gripper")!.disabled).toBe(true);
  });
});

describe("while a guided stop runs, nothing is started, set or tested", () => {
  // The backend adds a required blocker (`sequence_stopping`) to every start,
  // to the gripper test and to "set marker"; the page shows each button off
  // with that reason beside it, in words, in both languages.
  type AnyDoc = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
  const blocked = (doc: AnyDoc) => {
    const row = [
      {
        code: "sequence_stopping",
        level: "required",
        text: "A guided stop is running.",
        params: {},
      },
    ];
    for (const key of ["m", "a", "b", "c"]) {
      doc.act.terminals[key].can_start = false;
      doc.act.terminals[key].start_blockers = row;
    }
    doc.act.gripper_test.can_run = false;
    doc.act.gripper_test.blockers = row;
    doc.act.marker.state = "absent";
    doc.act.marker.ours = false;
    doc.act.marker.touch_blockers = row;
  };

  test("the three starts, the gripper test and Set marker are off, each with the reason beside it", async () => {
    const { host } = await open(stackDoc("stop_all_running", blocked));
    for (const id of ["C", "B"]) {
      expect(button(comp(host, id), "Start")!.disabled).toBe(true);
    }
    const why = (testId: string) =>
      host.querySelector(`[data-testid=${testId}]`)!;
    // A and M are ready or running in this sample: their cards say so by their
    // state; the ones that are idle say the reason.
    for (const testId of [
      "marvel0-block-start-C",
      "marvel0-block-gripper",
      "marvel0-block-marker",
    ]) {
      expect(text(why(testId))).toContain(
        en["marvel0.ctl.block.sequence_stopping"],
      );
      expect(why(testId).querySelector("li")!.getAttribute("data-code")).toBe(
        "sequence_stopping",
      );
    }
    expect(button(host, "Open gripper")!.disabled).toBe(true);
    expect(button(host, "Close gripper")!.disabled).toBe(true);
    expect(button(host, "Set marker")!.disabled).toBe(true);
  });

  test("a click on a button that is off opens nothing and sends nothing", async () => {
    const { host, state } = await open(stackDoc("stop_all_running", blocked));
    for (const node of [
      button(comp(host, "C"), "Start")!,
      button(host, "Set marker")!,
      button(host, "Open gripper")!,
    ])
      await click(node);
    expect(dialog(host)).toBeNull();
    expect(state.calls).toHaveLength(0);
  });

  test("in Chinese the reason is worded", async () => {
    const { host } = await open(stackDoc("stop_all_running", blocked), true);
    expect(
      text(host.querySelector("[data-testid=marvel0-block-marker]")),
    ).toContain(zh["marvel0.ctl.block.sequence_stopping"]);
    expect(
      text(host.querySelector("[data-testid=marvel0-block-marker]")),
    ).toContain(zh["marvel0.ctl.off.marker"]);
  });

  test("a marker that is not blocked is set by the same button", async () => {
    const { host } = await open(
      stackDoc("idle", (doc) => {
        doc.act.marker.state = "absent";
      }),
    );
    expect(button(host, "Set marker")!.disabled).toBe(false);
    expect(host.querySelector("[data-testid=marvel0-block-marker]")).toBeNull();
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
  test("when the stack can no longer be read, the window closes and what was ticked is gone", async () => {
    const { host, state } = await open(stackDoc("ready_to_start_a"));
    await click(button(comp(host, "A"), "Start")!);
    await tickAll(dialog(host)!);
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
