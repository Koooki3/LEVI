import { act } from "react";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import { afterEach, describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { LocaleProvider } from "@/components/levi-locale";
import { errorOf, type ApiResult } from "../deploy-api";
import type { RunApi } from "../run-api";
import { RunPanel } from "../run-panel";
import { RunStart } from "../run-start";
import { runView } from "../run-logic";
import { useRunActions } from "../use-run-actions";
import { useRunPoll } from "../use-run-poll";
import { button, tick, type } from "./actions-helpers";
import { RUN_VIEWS, runDoc } from "./ctl-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

type Doc = ReturnType<typeof runDoc>;
interface Call {
  op: string;
  body: Record<string, unknown>;
}

function fake(initial: Doc) {
  const state = {
    doc: initial as Doc | null,
    calls: [] as Call[],
    order: [] as string[],
    answer: ((call: Call): ApiResult | Promise<ApiResult> => {
      if (call.op === "challenge")
        return {
          ok: true,
          status: 200,
          data: {
            challenge: "tok-" + call.body.purpose,
            expires_at: 1,
            ttl_s: 30,
          },
        };
      return { ok: true, status: 202, data: { done: true } };
    }) as (call: Call) => ApiResult | Promise<ApiResult>,
  };
  const write = (op: string) => async (body: unknown) => {
    const call = { op, body: body as Record<string, unknown> };
    state.calls.push(call);
    state.order.push(op);
    return state.answer(call);
  };
  const api: RunApi = {
    read: async () =>
      state.doc
        ? { ok: true, status: 200, data: structuredClone(state.doc) }
        : errorOf(503, {}),
    challenge: write("challenge"),
    start: write("start"),
    key: write("key"),
    hold: write("hold"),
    end: write("end"),
    log: async () => errorOf(404, {}),
  };
  return { api, state };
}

function Harness({ api }: { api: RunApi }) {
  const poll = useRunPoll(api, { pollMs: 25 });
  const actions = useRunActions(api, poll);
  return <RunPanel poll={poll} actions={actions} />;
}

async function open(
  name: string,
  change?: (doc: Doc) => void,
  zhLanguage = false,
) {
  if (zhLanguage) localStorage.setItem("levi-language", "zh");
  const f = fake(runDoc(name, change));
  const element = <Harness api={f.api} />;
  const view = await render(
    zhLanguage ? <LocaleProvider>{element}</LocaleProvider> : element,
  );
  await waitFor(
    () =>
      view.host.querySelector("[data-testid=marvel0-run-age][data-late=false]"),
    { label: "the run" },
  );
  return { ...view, ...f };
}
/** The card's HOLD; null where there is nothing to hold (no client, a
 * frozen one): the card then shows no HOLD frame at all. */
const hold = (host: HTMLElement) =>
  host.querySelector("[data-testid=marvel0-run-hold]") as HTMLButtonElement;
const text = (node: Element | null) => node?.textContent ?? "";
const dialog = (host: HTMLElement) =>
  host.querySelector("[role=dialog]") as HTMLElement | null;

const MAY_MOVE = new Set([
  "starting",
  "at_gate",
  "homing",
  "running",
  "awaiting_label",
  "finishing",
  "lost",
]);

describe("HOLD", () => {
  test("is on in every view of the backend in which the arm may move, and not there in the others (no red frame around a disabled button)", async () => {
    for (const name of RUN_VIEWS) {
      const doc = runDoc(name);
      const { host } = await open(name);
      const expected = MAY_MOVE.has(doc.state) || doc.may_move === true;
      expect([name, hold(host) !== null]).toEqual([name, expected]);
      if (expected) expect([name, hold(host).disabled]).toEqual([name, false]);
      expect([
        name,
        host.querySelector("[data-testid=marvel0-run-hold-frame]") !== null,
      ]).toEqual([name, expected]);
    }
  });

  test("HOLD is the only solid danger button of the card; the page's one e-stop line is the caption beside it", async () => {
    const { host } = await open("running");
    const solid = Array.from(host.querySelectorAll("button")).filter((b) =>
      b.classList.contains("ds-btn--danger"),
    );
    expect(solid).toEqual([hold(host)]);
    const card = host.querySelector("[data-testid=marvel0-run]")!;
    expect(text(card.querySelector(".rg-hold__caption"))).toBe(
      en["marvel0.run.hold_caption"],
    );
    // Said once: the caption, not a second banner under it.
    expect((text(card).match(/emergency stop/gi) ?? []).length).toBe(1);
    expect(host.querySelector("[data-testid=marvel0-run-fixed]")).toBeNull();
  });

  test("is in the same place in every state it is shown in: first under what the client is doing, full width and 64 px high", async () => {
    const positions = new Set<number>();
    for (const name of [
      "at_gate",
      "running",
      "awaiting_label",
      "homing",
      "lost",
    ]) {
      const { host } = await open(name);
      const card = host.querySelector("[data-testid=marvel0-run]")!;
      const wrap = hold(host).closest(".rg-run__hold")!;
      positions.add(Array.from(card.children).indexOf(wrap));
      expect(hold(host).classList.contains("rg-hold")).toBe(true);
    }
    expect(positions.size).toBe(1);
    const css = readFileSync(
      join(import.meta.dir, "../marvel0-styles.css"),
      "utf8",
    );
    expect(css).toMatch(/\.rg-run \.rg-hold\s*{[^}]*width:\s*100%/);
    expect(css).toMatch(/\.rg-run \.rg-hold\s*{[^}]*min-height:\s*64px/);
  });

  test("stays on when the answers stop: the client may still be running", async () => {
    const { host, state } = await open("running");
    expect(hold(host).disabled).toBe(false);
    state.doc = null;
    await waitFor(() => host.querySelector("[data-testid=marvel0-run-stale]"), {
      label: "the stale note",
    });
    expect(hold(host).disabled).toBe(false);
  });

  test("is sent at once: no challenge, no phrase, and not behind another write", async () => {
    let release: (value: ApiResult) => void = () => {};
    const { host, state } = await open("at_gate");
    // A key is in flight (its answer is held back) ...
    state.answer = (call) =>
      call.op === "key"
        ? new Promise<ApiResult>((resolve) => {
            release = resolve;
          })
        : call.op === "challenge"
          ? { ok: true, status: 200, data: { challenge: "t" } }
          : { ok: true, status: 200, data: {} };
    await click(button(host, "Start next episode")!);
    expect(state.order).toEqual(["challenge", "key"]);
    // ... and HOLD goes through anyway, with nothing but its request id.
    await click(hold(host));
    expect(state.order).toEqual(["challenge", "key", "hold"]);
    expect(Object.keys(state.calls[2].body)).toEqual(["request_id"]);
    await act(async () => release({ ok: true, status: 202, data: {} }));
  });

  test("a hold that was refused is shown in words, and HOLD stays on", async () => {
    const { host, state } = await open("running");
    state.answer = () =>
      errorOf(502, { detail: { code: "signal_failed", message: "English" } });
    await click(hold(host));
    await tick(3, 10);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-refused]")),
    ).toContain(en["marvel0.run.refuse.signal_failed"]);
    expect(hold(host).disabled).toBe(false);
  });
});

describe("the key each state waits for", () => {
  const labels = (host: HTMLElement) =>
    Array.from(
      host.querySelectorAll("[data-testid=marvel0-run-armed] button"),
    ).map((b) => b.getAttribute("data-action"));

  test("at the start gate: Start next episode and End at the gate, nothing else; the arm then goes home", async () => {
    const { host } = await open("at_gate");
    expect(labels(host)).toEqual(["enter"]);
    expect(button(host, "End at the gate")).toBeDefined();
    // The ending is apart from HOLD and from the key, at the foot of the card.
    const end = host.querySelector("[data-testid=marvel0-run-end]")!;
    expect(end.contains(button(host, "End at the gate")!)).toBe(true);
    expect(end.contains(hold(host))).toBe(false);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-armed]")),
    ).toContain(en["marvel0.run.blind_home"]);
    expect(button(host, "Start next episode")!.disabled).toBe(false);
  });

  test("running: Success and Failure; waiting for the label: and Discard", async () => {
    expect(labels((await open("running")).host)).toEqual([
      "success",
      "failure",
    ]);
    expect(labels((await open("awaiting_label")).host)).toEqual([
      "success",
      "failure",
      "discard",
    ]);
  });

  test("homing, finishing and lost carry no key; only HOLD is there", async () => {
    for (const name of ["homing", "finishing_episode", "lost"]) {
      const { host } = await open(name);
      expect(host.querySelector("[data-testid=marvel0-run-armed]")).toBeNull();
      expect(hold(host).disabled).toBe(false);
    }
  });

  test("a frozen client offers End and nothing else that acts: the frame says frozen, there is no HOLD to press again", async () => {
    const { host } = await open("held");
    expect(host.querySelector("[data-testid=marvel0-run-armed]")).toBeNull();
    expect(host.querySelector("[data-testid=marvel0-run-exit]")).toBeNull();
    const end = host.querySelector("[data-testid=marvel0-run-end] button")!;
    expect(end.textContent).toContain("End run");
    expect(hold(host)).toBeNull();
    // The frame says "Frozen" and holds the End, the one action left; the
    // hint of a frozen client's End is beside it (a live one has its own).
    const frame = host.querySelector("[data-testid=marvel0-run-frozen]")!;
    expect(text(frame)).toContain(en["marvel0.run.state.held"]);
    expect(frame.contains(end)).toBe(true);
    expect(text(host.querySelector("[data-testid=marvel0-run-end]"))).toContain(
      en["marvel0.run.end_hint"],
    );
  });

  test("End is a danger outline, not full width, apart from HOLD, with its own hint while the client is only starting", async () => {
    const { host } = await open("starting");
    const end = host.querySelector(
      "[data-testid=marvel0-run-end] button",
    ) as HTMLButtonElement;
    expect(end.textContent).toContain("End run");
    expect(end.className).toContain("rg-btn--danger-outline");
    expect(end.className).toContain("ds-btn--secondary");
    expect(end.className).not.toContain("ds-btn--danger");
    expect(hold(host)).not.toBeNull();
    expect(
      hold(host).closest("[data-testid=marvel0-run-hold-frame]")!.contains(end),
    ).toBe(false);
    const hint = text(host.querySelector("[data-testid=marvel0-run-end]"));
    expect(hint).toContain(en["marvel0.run.end_hint_live"]);
    expect(hint).not.toContain("frozen");
    expect(en["marvel0.run.end_hint_live"]).toBe(
      "End kills the client; nothing resumes.",
    );
  });

  test("Success, Failure and Discard weigh the same, and the group says it moves the arm home", async () => {
    const { host } = await open("awaiting_label");
    const armed = host.querySelector("[data-testid=marvel0-run-armed]")!;
    for (const b of armed.querySelectorAll("button")) {
      expect(b.className).toContain("ds-btn--secondary");
      expect(b.className).not.toContain("ds-btn--primary");
    }
    expect(text(armed)).toContain(en["marvel0.run.keys.label"]);
    expect(text(armed)).toContain(en["marvel0.run.keys.tag"]);
    expect(en["marvel0.run.keys.tag"]).toBe("moves the arm home");
  });

  test("keys that are off for the same reason say it once, naming both", async () => {
    const { host } = await open("running_monitor_red");
    const why = host.querySelectorAll("[data-testid^=marvel0-run-why-]");
    expect(why).toHaveLength(1);
    expect(text(why[0])).toContain(
      `${en["marvel0.run.act.success"]}, ${en["marvel0.run.act.failure"]}:`,
    );
    expect(text(why[0])).toContain(en["marvel0.run.why.monitor_red"]);
  });

  test("a key the backend does not offer is off, with the backend's reason beside it", async () => {
    const { host } = await open("at_gate_enter_sent");
    expect(button(host, "Start next episode")!.disabled).toBe(true);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-why-enter]")),
    ).toContain(en["marvel0.run.why.enter_locked"]);
  });

  test("one click is a challenge and then the key with that challenge, and a second click does nothing", async () => {
    const { host, state } = await open("running");
    const success = button(host, "Success")!;
    await act(async () => {
      success.click();
      success.click();
    });
    await tick(3, 10);
    expect(state.order).toEqual(["challenge", "key"]);
    expect(state.calls[0].body).toMatchObject({ purpose: "label" });
    expect(state.calls[1].body).toMatchObject({
      action: "success",
      challenge: "tok-label",
    });
    // Neither request carries a raw key.
    expect(JSON.stringify(state.calls)).not.toMatch(/"key":/);
  });

  test("the key is not sent when no challenge was given", async () => {
    const { host, state } = await open("at_gate");
    state.answer = (call) =>
      call.op === "challenge"
        ? errorOf(409, {
            detail: {
              code: "state_not_allowed",
              message: "x",
              state: "running",
            },
          })
        : { ok: true, status: 202, data: {} };
    await click(button(host, "Start next episode")!);
    await tick(3, 10);
    expect(state.order).toEqual(["challenge"]);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-refused]")),
    ).toContain(
      en["marvel0.run.why.state_not_allowed"].replace("{state}", "running"),
    );
  });

  test("a refusal in words: an expired token, and a code the page does not know", async () => {
    const { host, state } = await open("running");
    state.answer = (call) =>
      call.op === "challenge"
        ? { ok: true, status: 200, data: { challenge: "t" } }
        : errorOf(409, {
            detail: { code: "challenge_expired", message: "English expired" },
          });
    await click(button(host, "Failure")!);
    await tick(3, 10);
    const shown = host.querySelector("[data-testid=marvel0-run-refused]")!;
    expect(shown.getAttribute("data-code")).toBe("challenge_expired");
    // The same boxed block as "Start is off:", with a danger edge and a
    // label ("Refused:"), not a line of prose.
    expect(shown.className).toContain("rg-block--refused");
    expect(text(shown)).toBe(
      `${en["marvel0.ctl.refused"]}${en["marvel0.run.refuse.challenge_expired"]}`,
    );
    expect(shown.querySelector("[lang=en]")).toBeNull();
    state.answer = () =>
      errorOf(409, {
        detail: { code: "brand_new", message: "Server sentence" },
      });
    await click(button(host, "Failure")!);
    await tick(3, 10);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-refused]")),
    ).toContain("Server sentence");
  });
});

describe("leaving the run", () => {
  test("Exit and go home is folded away, has its own phrase and sends it with the key", async () => {
    const { host, state } = await open("running");
    const exit = host.querySelector(
      "[data-testid=marvel0-run-exit]",
    ) as HTMLDetailsElement;
    expect(exit.open).toBe(false);
    expect(text(exit.querySelector("summary"))).toBe(
      en["marvel0.run.exit_home.summary"],
    );
    const go = button(exit, "Exit and go home")!;
    expect(go.disabled).toBe(true);
    await type(exit.querySelector("input"), "exit and go home");
    expect(go.disabled).toBe(false);
    await click(go);
    await tick(3, 10);
    expect(state.calls[0].body).toMatchObject({ purpose: "exit_home" });
    expect(state.calls[1].body).toMatchObject({
      action: "exit_home",
      confirm: "exit and go home",
    });
  });

  test("it is not there at the gate, where the key is End at the gate", async () => {
    expect(
      (await open("at_gate")).host.querySelector(
        "[data-testid=marvel0-run-exit]",
      ),
    ).toBeNull();
  });

  test("End asks for its phrase in a dialog and sends it", async () => {
    const { host, state } = await open("held");
    await click(host.querySelector("[data-testid=marvel0-run-end] button")!);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.run.end.kills"]);
    const end = button(d, "End run")!;
    expect(end.disabled).toBe(true);
    await type(d.querySelector("input"), "end client run");
    await click(end);
    await tick(3, 10);
    expect(state.calls[0]).toMatchObject({
      op: "end",
      body: { confirm: "end client run" },
    });
  });

  test("a phrase typed before Cancel is gone when the dialog opens again: End asks for it each time", async () => {
    const { host, state } = await open("held");
    const endOpener = () =>
      host.querySelector("[data-testid=marvel0-run-end] button")!;
    await click(endOpener());
    let d = dialog(host)!;
    await type(d.querySelector("input"), "end client run");
    expect(button(d, "End run")!.disabled).toBe(false);
    await click(button(d, en["marvel0.ctl.btn.cancel"])!);
    await tick(3, 10);
    expect(dialog(host)).toBeNull();

    await click(endOpener());
    d = dialog(host)!;
    expect(d.querySelector("input")!.value).toBe("");
    expect(button(d, "End run")!.disabled).toBe(true);
    // Nothing was sent by the Cancel or by the reopening.
    expect(state.calls).toEqual([]);
  });
});

describe("what the card says", () => {
  test("the one e-stop line of the card is the caption beside HOLD, word for word; what the keys do is said where they are", async () => {
    const { host } = await open("running");
    expect(text(host.querySelector(".rg-hold__caption"))).toBe(
      "Not an emergency stop: freezes the client at once and the arm keeps the last target it was sent. After a hold the only action is End. Keep a hand on the physical e-stop.",
    );
    expect(zh["marvel0.run.hold_caption"]).toBe(
      "这不是急停：立刻冻结客户端，手臂保持它收到的最后一个目标。保持之后只能结束。手要放在物理急停上。",
    );
    expect(text(host.querySelector("[data-testid=marvel0-run-blind]"))).toBe(
      en["marvel0.run.blind_home"],
    );
  });

  test("with no client the card says how to start one instead of showing a HOLD frame, and heads the reasons 'Start is off:'", async () => {
    const { host } = await open("preflight_failing");
    expect(hold(host)).toBeNull();
    const how = host.querySelector("[data-testid=marvel0-run-how]")!;
    expect(text(how)).toContain(en["marvel0.run.idle.how"]);
    expect(en["marvel0.run.idle.how"]).toBe(
      "To start a run: step 3 → Make the command → Start the client.",
    );
    // A link that goes to the command card.
    expect(button(how as HTMLElement, en["marvel0.run.idle.go"])).toBeDefined();
    const off = host.querySelector("[data-testid=marvel0-run-preflight]")!;
    expect(text(off)).toContain(en["marvel0.ctl.off.start"]);
    expect(off.querySelectorAll("[data-code]").length).toBeGreaterThan(0);
    // No e-stop line on a card that has nothing to hold.
    expect(text(host.querySelector("[data-testid=marvel0-run]"))).not.toMatch(
      /emergency stop/i,
    );
  });

  test("the state in words, the episode and the steps", async () => {
    const { host } = await open("running");
    const card = host.querySelector("[data-testid=marvel0-run]")!;
    expect(card.getAttribute("data-state")).toBe("running");
    expect(text(card)).toContain("Running");
    expect(text(host.querySelector("[data-testid=marvel0-run-episode]"))).toBe(
      "Episode 1 of 5 (0 counted)",
    );
    expect(
      card.querySelector("[role=progressbar]")!.getAttribute("aria-valuenow"),
    ).toBe("137");
    expect(text(host.querySelector("[data-testid=marvel0-run-reason]"))).toBe(
      "Episode 1 is running.",
    );
  });

  test("the backend's notices are said; a monitor that went stale is one of them", async () => {
    const { host } = await open("at_gate_monitor_stale");
    const codes = Array.from(
      host.querySelectorAll("[data-testid=marvel0-run-notices] li"),
    ).map((li) => li.getAttribute("data-code"));
    expect(codes).toContain("monitor_stale");
    expect(text(host)).toContain(en["marvel0.run.notice.monitor_stale"]);
  });

  test("switched off in the control file, unreadable, or outside the product: said, and nothing offered", async () => {
    const off = await open("disabled");
    expect(
      off.host.querySelector("[data-testid=marvel0-run-disabled]"),
    ).not.toBeNull();
    expect(hold(off.host)).toBeNull();
    // Switched off: no how-to-start line either, the notice says why.
    expect(off.host.querySelector("[data-testid=marvel0-run-how]")).toBeNull();
    const none = await open("not_product");
    expect(button(none.host, "Start next episode")).toBeUndefined();
  });

  test("in Chinese: the state, the keys and the refusal are worded", async () => {
    const { host, state } = await open("running", undefined, true);
    await waitFor(() => text(host).includes(zh["marvel0.run.title"]), {
      label: "Chinese",
    });
    expect(text(host)).toContain(zh["marvel0.run.state.running"]);
    expect(button(host, zh["marvel0.run.act.success"])).toBeDefined();
    expect(text(host.querySelector("[data-testid=marvel0-run-reason]"))).toBe(
      zh["marvel0.run.reason.running"].replace("{episode}", "1"),
    );
    state.answer = () =>
      errorOf(409, { detail: { code: "challenge_stale", message: "English" } });
    await click(button(host, zh["marvel0.run.act.success"])!);
    await tick(3, 10);
    expect(text(host.querySelector("[data-testid=marvel0-run-refused]"))).toBe(
      `${zh["marvel0.ctl.refused"]}${zh["marvel0.run.refuse.challenge_stale"]}`,
    );
  });
});

describe("there is no s or p anywhere", () => {
  test("no control carries those keys, and no keyboard handler is installed", async () => {
    for (const name of ["at_gate", "running", "awaiting_label", "held"]) {
      const { host } = await open(name);
      for (const b of host.querySelectorAll("button")) {
        expect(b.textContent?.trim()).not.toMatch(/^[sp]$/i);
        expect(b.getAttribute("accesskey")).toBeNull();
        expect(b.getAttribute("aria-keyshortcuts")).toBeNull();
      }
    }
    const sources = [
      "run-panel.tsx",
      "run-start.tsx",
      "use-run-actions.ts",
      "run-api.ts",
      "run-logic.ts",
    ];
    for (const file of sources) {
      const code = readFileSync(join(import.meta.dir, "..", file), "utf8");
      expect(code).not.toMatch(
        /onKey(Down|Up|Press)|addEventListener\(\s*["']key/,
      );
    }
  });
});

describe("starting from the page", () => {
  async function openStart(name = "idle_ready", stale = false) {
    const f = fake(runDoc(name));
    const form = { model_id: "m", prompt: "p", rtc: true };
    function Start() {
      const poll = useRunPoll(f.api, { pollMs: 25 });
      const actions = useRunActions(f.api, poll);
      return (
        <RunStart
          view={poll.value}
          actions={actions}
          form={form}
          group="my_group"
          modelId="m"
          rtc
          stale={stale}
        />
      );
    }
    const view = await render(<Start />);
    await tick(6, 30);
    return { ...view, ...f, form };
  }
  const tickAll = async (d: HTMLElement) => {
    for (const box of d.querySelectorAll("input[type=checkbox]"))
      await click(box);
  };

  test("a dialog asks the three checks and the phrase with the group; then a challenge for this form and the start", async () => {
    const { host, state, form } = await openStart();
    await click(button(host, "Start the client")!);
    const d = dialog(host)!;
    expect(text(d)).toContain(en["marvel0.run.start.estop"]);
    expect(text(d)).toContain(en["marvel0.stack.gates_scope"]);
    const go = () => button(d, "Start the client")!;
    expect(go().disabled).toBe(true);
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start client my_group",
    );
    expect(go().disabled).toBe(true);
    await tickAll(d);
    expect(go().disabled).toBe(false);
    await click(go());
    await tick(3, 10);
    expect(state.order).toEqual(["challenge", "start"]);
    expect(state.calls[0].body).toMatchObject({ purpose: "start", form });
    expect(state.calls[1].body).toMatchObject({
      form,
      challenge: "tok-start",
      confirm: "start client my_group",
      attest: { estop: true, scene_reset: true, area_clear: true },
    });
  });

  test("the phrase names the group: another group's phrase does not count", async () => {
    const { host } = await openStart();
    await click(button(host, "Start the client")!);
    const d = dialog(host)!;
    await tickAll(d);
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start client other",
    );
    expect(button(d, "Start the client")!.disabled).toBe(true);
  });

  test("off while a client runs or a check fails, with the backend's reason; off when the form changed", async () => {
    const live = await openStart("running");
    expect(button(live.host, "Start the client")!.disabled).toBe(true);
    expect(
      text(live.host.querySelector("[data-testid=marvel0-run-start-why]")),
    ).toContain(en["marvel0.run.why.run_live"]);
    const stale = await openStart("idle_ready", true);
    expect(button(stale.host, "Start the client")!.disabled).toBe(true);
    expect(text(stale.host)).toContain(en["marvel0.run.start.stale"]);
  });

  test("a refused start is shown inside the dialog in words", async () => {
    const { host, state } = await openStart();
    state.answer = (call) =>
      call.op === "challenge"
        ? errorOf(409, {
            detail: {
              code: "preflight_failed",
              message: "English",
              first: "policy_ready",
            },
          })
        : { ok: true, status: 202, data: {} };
    await click(button(host, "Start the client")!);
    const d = dialog(host)!;
    await type(
      d.querySelector("input:not([type=checkbox])"),
      "start client my_group",
    );
    await tickAll(d);
    await click(button(d, "Start the client")!);
    await tick(3, 10);
    expect(
      text(host.querySelector("[data-testid=marvel0-run-start-refused]")),
    ).toContain("policy_ready");
    expect(state.order).toEqual(["challenge"]);
  });
});

void runView;
