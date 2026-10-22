import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import {
  button,
  doubleClick,
  textFields,
  tick,
  tickAll,
} from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  text,
  wait,
} from "./autonomy-helpers";
import {
  callsOf,
  keyOf,
  noAnswer,
  readyDoc,
  refusal,
  runDoc,
} from "./autonomy-fixtures";

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const Z = zh as Record<string, string>;
const stop = (host: ParentNode) =>
  $(host, "marvel0-autonomy-stop") as HTMLButtonElement | null;
const label = (id: string) => E[`marvel0.autonomy.key.${id}`];
/** The Confirm button of the open key window (it carries the key's label). */
const confirmOf = (id: string) =>
  Array.from(dialog()!.querySelectorAll("button")).find((b) =>
    text(b).startsWith(label(id)),
  ) as HTMLButtonElement;
async function pressKey(host: ParentNode, id: string) {
  await waitFor(() => !keyButton(host, id)!.disabled, {
    label: `key ${id} to be on`,
  });
  await click(keyButton(host, id));
}

describe("Stop", () => {
  test("it is a block of its own while D lives, whatever the state, full width, and the only solid danger button of the card", async () => {
    for (const name of [
      "launching",
      "await_phrase",
      "services",
      "await_release",
      "forward_running",
      "busy_external",
      "unknown_drift",
    ]) {
      const { host } = await openStep({ run: runDoc(name) });
      const frame = $(host, "marvel0-autonomy-stop-frame")!;
      expect([name, frame !== null]).toEqual([name, true]);
      expect([name, stop(host)!.disabled]).toEqual([name, false]);
      expect([name, stop(host)!.className]).toEqual([
        name,
        expect.stringContaining("ds-btn--danger"),
      ]);
      expect(host.querySelectorAll(".ds-btn--danger")).toHaveLength(
        1 +
          (host.querySelector("[data-testid=marvel0-autonomy-dock-stop]")
            ? 1
            : 0),
      );
      expect(text(frame)).toContain(E["marvel0.autonomy.stop.button"]);
    }
  });

  test("it says it is not an emergency stop, that the arm stays where it is and that it is sent once", async () => {
    const { host } = await openStep({ run: runDoc("forward_running") });
    const caption = text($(host, "marvel0-autonomy-stop-caption"));
    expect(caption).toBe(E["marvel0.autonomy.stop.ready"]);
    expect(caption).toMatch(/Not an emergency stop/);
    expect(caption).toMatch(/stays where it is and does not go home/);
    expect(caption).toMatch(/Once per run/);
  });

  test("it is not there when no D lives (nothing hosted, ended, exited by a signal)", async () => {
    for (const name of [
      "idle",
      "ended_done",
      "ended_refused",
      "ended_hangup",
    ]) {
      const { host } = await openStep({ run: runDoc(name) });
      expect([name, $(host, "marvel0-autonomy-stop-frame")]).toEqual([
        name,
        null,
      ]);
      expect([name, $(host, "marvel0-autonomy-dock")]).toEqual([name, null]);
    }
  });

  test("one click is one request with the closed body of the route (no seen, no token); a double click is still one", async () => {
    const { host, state } = await openStep({ run: runDoc("forward_running") });
    await doubleClick(stop(host));
    await tick(3, 10);
    const sent = callsOf(state, "stop");
    expect(sent).toHaveLength(1);
    expect(Object.keys(sent[0])).toEqual(["request_id"]);
    expect(sent[0].request_id).toMatch(/^[A-Za-z0-9._:-]{8,80}$/);
  });

  test("after it was sent it says so and is off until the stop phase moves on: one Stop per run", async () => {
    const f = await openStep({ run: runDoc("forward_running") });
    await click(stop(f.host));
    await tick(3, 10);
    expect(stop(f.host)!.disabled).toBe(true);
    // The backend has not changed its phase yet: the page does not offer a second one.
    await click(stop(f.host));
    expect(callsOf(f.state, "stop")).toHaveLength(1);
    await serve(
      f,
      runDoc("stopping", (doc) => (doc.stop.phase = "sent")),
    );
    expect(text($(f.host, "marvel0-autonomy-stop-caption"))).toBe(
      E["marvel0.autonomy.stop.sent"],
    );
    expect(stop(f.host)!.disabled).toBe(true);
    // Acknowledged by D: the same, in other words.
    await serve(f, runDoc("stopping"));
    expect(text($(f.host, "marvel0-autonomy-stop-caption"))).toBe(
      E["marvel0.autonomy.stop.acked"],
    );
    expect(stop(f.host)!.disabled).toBe(true);
  });

  test("an acknowledged stop says that D is wrapping up; the retry is offered once, when the backend opens it, and then no more", async () => {
    const f = await openStep({ run: runDoc("forward_running") });
    await click(stop(f.host));
    await serve(f, runDoc("stopping_unconfirmed"));
    expect(stop(f.host)!.disabled).toBe(false);
    expect(text($(f.host, "marvel0-autonomy-stop-caption"))).toBe(
      E["marvel0.autonomy.stop.retry"],
    );
    await click(stop(f.host));
    await tick(3, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(2);
    expect(stop(f.host)!.disabled).toBe(true);
    await serve(
      f,
      runDoc("stopping_unconfirmed", (doc) => {
        doc.stop.phase = "retried";
        doc.stop.allowed = false;
      }),
    );
    expect(text($(f.host, "marvel0-autonomy-stop-caption"))).toBe(
      E["marvel0.autonomy.stop.spent"],
    );
    expect(stop(f.host)!.disabled).toBe(true);
    await click(stop(f.host));
    expect(callsOf(f.state, "stop")).toHaveLength(2);
  });

  test("it does not wait for a key that is on its way, and a key does not wait for it", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    let release!: () => void;
    f.state.gate = new Promise<void>((resolve) => (release = resolve));
    await click(keyButton(f.host, "quit")); // the key request hangs
    await tick(2, 10);
    expect(callsOf(f.state, "key")).toHaveLength(1);
    await click(stop(f.host));
    await tick(2, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(1);
    release();
    await tick(3, 10);
  });

  test("it stays on when the reads stop: a D that was running when the answers stopped is the case in which it matters", async () => {
    const f = await openStep({
      run: runDoc("forward_running"),
      limits: { docStaleMs: 200 },
    });
    f.state.run = null;
    await wait(500);
    expect($(f.host, "marvel0-autonomy-stale")).not.toBeNull();
    expect(stop(f.host)!.disabled).toBe(false);
    // The keys are off meanwhile.
    expect(keyButton(f.host, "verdict_success")!.disabled).toBe(true);
    expect(
      text(keyButton(f.host, "verdict_success")!.closest(".rg-akey")),
    ).toContain(E["marvel0.autonomy.local.doc_stale"]);
    await click(stop(f.host));
    await tick(3, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(1);
  });

  test("a request that got no answer is sent again with the same id on the next press, and not otherwise", async () => {
    const f = await openStep({ run: runDoc("forward_running") });
    let first = true;
    f.state.answer = (call) => {
      if (call.op !== "stop") return { ok: true, status: 202, data: {} };
      if (first) {
        first = false;
        return noAnswer();
      }
      return { ok: true, status: 202, data: {} };
    };
    await click(stop(f.host));
    await tick(3, 10);
    expect(stop(f.host)!.disabled).toBe(false);
    expect(text($(f.host, "marvel0-autonomy-stop-refused"))).toContain(
      E["marvel0.err.network_write"],
    );
    await click(stop(f.host));
    await tick(3, 10);
    const [a, b] = callsOf(f.state, "stop");
    expect(a.request_id).toBe(b.request_id);
  });

  test("a refusal is shown in words", async () => {
    const f = await openStep({ run: runDoc("forward_running") });
    f.state.answer = (call) =>
      call.op === "stop"
        ? refusal(409, "stop_spent")
        : { ok: true, status: 202, data: {} };
    await click(stop(f.host));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-stop-refused"))).toContain(
      E["marvel0.autonomy.err.stop_spent"],
    );
    expect(stop(f.host)!.disabled).toBe(true);
  });

  test("where the backend does not offer it, it is shown off with the backend's reason (a D that locked itself already holds the arm)", async () => {
    const { host } = await openStep({ run: runDoc("fault_locked") });
    expect(stop(host)!.disabled).toBe(true);
    expect(text($(host, "marvel0-autonomy-stop-caption"))).toContain(
      "fault_locked",
    );
  });

  test("the dock's Stop is off where the backend does not offer one, like the card's", async () => {
    const { host } = await openStep({ run: runDoc("fault_locked") });
    const dockStop = $(host, "marvel0-autonomy-dock-stop") as HTMLButtonElement;
    expect(dockStop.disabled).toBe(true);
  });

  test("the dock repeats it when the card's Stop is out of view, and it is the same Stop: a click on each is one request", async () => {
    const { host, state } = await openStep({ run: runDoc("forward_running") });
    const dock = $(host, "marvel0-autonomy-dock")!;
    expect(dock).not.toBeNull();
    expect(text(dock)).toContain(E["marvel0.autonomy.dock.note"]);
    const dockStop = $(host, "marvel0-autonomy-dock-stop") as HTMLButtonElement;
    await act(async () => {
      dockStop.click();
      stop(host)!.click();
    });
    await tick(3, 10);
    expect(callsOf(state, "stop")).toHaveLength(1);
  });
});

describe("the windows of the other keys", () => {
  test("execute: one tick for each thing D asks and the three of the stack's windows, the backend's phrase sent by the page, nothing typed, once", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await pressKey(f.host, "execute");
    const d = dialog()!;
    expect(textFields(d)).toHaveLength(0);
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(text(d)).toContain(E["marvel0.autonomy.hazard.execute"]);
    expect(d.querySelectorAll("input[type=checkbox]")).toHaveLength(8);
    expect(confirmOf("execute").disabled).toBe(true);
    await tickAll(d);
    expect(confirmOf("execute").disabled).toBe(false);
    await doubleClick(confirmOf("execute"));
    await tick(3, 10);
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    expect(sent[0]).toMatchObject({
      action: "execute",
      confirm: "execute aeri-20261021-ext-r01",
    });
  });

  test("the long start confirmation after an unfinished policy switch: the plain one is off with the backend's reason; the other has its own ticks", async () => {
    const f = await openStep({ run: runDoc("await_phrase_variant") });
    expect(keyButton(f.host, "execute")!.disabled).toBe(true);
    expect(text(keyButton(f.host, "execute")!.closest(".rg-akey"))).toContain(
      E["marvel0.autonomy.err.swap_unclean"],
    );
    await pressKey(f.host, "execute_variant");
    const d = dialog()!;
    // The four items of the policy switch check, the five of the start, the three.
    expect(d.querySelectorAll("input[type=checkbox]")).toHaveLength(12);
    for (const code of [
      "switch_units",
      "switch_ports",
      "switch_gpu",
      "switch_home",
    ])
      expect(text(d)).toContain(E[`marvel0.autonomy.dcheck.${code}`]);
    await tickAll(d);
    await click(confirmOf("execute_variant"));
    await tick(3, 10);
    expect(callsOf(f.state, "key")[0]).toMatchObject({
      action: "execute_variant",
      confirm: "execute aeri-20261021-ext-r01 after policy switch check",
    });
  });

  test("a key of level B sends no phrase", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    await pressKey(f.host, "inventory_yes");
    await click(confirmOf("inventory_yes"));
    await tick(3, 10);
    expect(callsOf(f.state, "key")[0]).not.toHaveProperty("confirm");
  });

  test("o, p and h are three separate windows, each with its own warning, in their own state; the steps are shown", async () => {
    const seen: string[] = [];
    for (const [state, key, hazard] of [
      ["await_home_o", "home_o", "marvel0.autonomy.hazard.home_o"],
      ["await_home_p", "home_p", "marvel0.autonomy.hazard.home_p"],
      ["await_home_h", "home_h", "marvel0.autonomy.hazard.home_h"],
    ] as const) {
      const f = await openStep({ run: runDoc(state) });
      const steps = $(f.host, "marvel0-autonomy-homing")!;
      expect(steps.getAttribute("data-step")).toBe(String(seen.length + 1));
      expect(steps.querySelector("[aria-current=step]")).not.toBeNull();
      // The other two homing keys are not offered in this state.
      for (const other of ["home_o", "home_p", "home_h"].filter(
        (k) => k !== key,
      ))
        expect(keyButton(f.host, other)).toBeNull();
      await pressKey(f.host, key);
      expect(text(dialog()!)).toContain(E[hazard]);
      seen.push(text(dialog()!));
      await click(confirmOf(key));
      await tick(3, 10);
      const sent = callsOf(f.state, "key");
      expect(sent).toHaveLength(1);
      expect(sent[0].action).toBe(key);
      expect(sent[0].token).toBe(keyOf(runDoc(state), key)!.token);
    }
    expect(new Set(seen).size).toBe(3);
  });

  test("a profile without startup homing shows none of the three keys even if the document lists one", async () => {
    const { host } = await openStep({
      run: runDoc("await_home_o", (doc) => (doc.profile.start_home = false)),
    });
    expect(keyButton(host, "home_o")).toBeNull();
    expect(keyButton(host, "quit")).not.toBeNull();
  });

  test("resume: the first step by the reason (ask, or resume now), the second confirmation apart from it", async () => {
    const ask = await openStep({ run: runDoc("await_resume") });
    expect(text(keyButton(ask.host, "resume_ask")!)).toContain(
      E["marvel0.autonomy.key.resume_ask_confirm"],
    );
    const direct = await openStep({ run: runDoc("await_resume_direct") });
    expect(text(keyButton(direct.host, "resume_ask")!)).toContain(
      E["marvel0.autonomy.key.resume_ask"],
    );
    // A document that does not say which gets the stricter one.
    const unsaid = await openStep({
      run: runDoc(
        "await_resume",
        (doc) => (keyOf(doc, "resume_ask")!.variant = null),
      ),
    });
    expect(text(keyButton(unsaid.host, "resume_ask")!)).toContain(
      E["marvel0.autonomy.key.resume_ask"],
    );
    const confirm = await openStep({ run: runDoc("await_resume_confirm") });
    const second = confirm.host.querySelector('[data-slot="second"]')!;
    expect(
      second.querySelector('[data-action="resume_confirm"]'),
    ).not.toBeNull();
    expect(
      confirm.host.querySelector(
        '[data-slot="first"] [data-action="resume_confirm"]',
      ),
    ).toBeNull();
  });

  test("a `todo` that is the name of a console sentence (todo_stop_resume) is not shown as if it were words", async () => {
    const { host } = await openStep({ run: runDoc("await_resume") });
    const quote = text($(host, "marvel0-autonomy-wait-text"));
    expect(quote).toContain("已收到停止请求");
    expect(quote).not.toContain("todo_stop_resume");
  });

  test("the WAIT block quotes D, says why, and puts the reminder to restore the scene before the keys", async () => {
    // The backend marks the wait that needs the scene restored; the sample of a
    // resume with Enter only does not, so this one is marked here.
    const { host } = await openStep({
      run: runDoc(
        "await_resume_direct",
        (doc) => (doc.wait.restore_scene = true),
      ),
    });
    const wait = $(host, "marvel0-autonomy-wait")!;
    expect(text($(host, "marvel0-autonomy-wait-text"))).toContain(
      "本轮没有在终端 D 放行",
    );
    expect(text(wait)).toContain(
      E["marvel0.autonomy.waitreason.subtask_not_released"],
    );
    const restore = $(host, "marvel0-autonomy-restore")!;
    expect(text(restore)).toBe(E["marvel0.autonomy.restore_scene"]);
    const keys = $(host, "marvel0-autonomy-keys")!;
    expect(
      restore.compareDocumentPosition(keys) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    // A wait that does not need the scene restored has no such reminder.
    const plain = await openStep({ run: runDoc("await_resume") });
    expect($(plain.host, "marvel0-autonomy-restore")).toBeNull();
    expect($(plain.host, "marvel0-autonomy-wait-confirm")).not.toBeNull();
  });
});

describe("what the card says besides the keys", () => {
  test("the objects the vision model listed, as a table; confirmed or not", async () => {
    const { host } = await openStep({ run: runDoc("await_inventory") });
    const rows = host.querySelectorAll(
      "[data-testid=marvel0-autonomy-inv-row]",
    );
    expect(rows).toHaveLength(4);
    expect(text(rows[2])).toContain("red_pepper_1");
    expect(text(rows[2])).toContain(E["marvel0.autonomy.inv.graspable"]);
    expect(text(rows[0])).toContain(E["marvel0.autonomy.inv.target"]);
    expect($(host, "marvel0-autonomy-inv-confirmed")).toBeNull();
    const confirmed = await openStep({ run: runDoc("await_release") });
    expect($(confirmed.host, "marvel0-autonomy-inv-confirmed")).not.toBeNull();
  });

  test("the line D waits on is quoted from its screen", async () => {
    const { host } = await openStep({ run: runDoc("await_release") });
    expect(text($(host, "marvel0-autonomy-prompt"))).toBe(
      runDoc("await_release").state.prompt.line,
    );
  });

  test("external mode shows the agent's queue and says the page never answers for it; local mode and a quiet queue do not push it", async () => {
    const { host } = await openStep({ run: runDoc("busy_external") });
    const queue = $(host, "marvel0-autonomy-queue")!;
    expect(queue.getAttribute("data-pending")).toBe("1");
    expect(text(queue)).toContain("1 question(s) wait for the agent");
    expect(text(queue)).toContain(E["marvel0.autonomy.queue.note"]);
    const local = await openStep({
      run: runDoc("busy_external", (doc) => (doc.mode = "local")),
    });
    expect($(local.host, "marvel0-autonomy-queue")).toBeNull();
  });

  test("D's screen, folded, and open when its wording drifted; the clicks made here, folded", async () => {
    const calm = await openStep({ run: runDoc("await_release") });
    expect(
      (
        calm.host.querySelector(
          "[data-testid=marvel0-autonomy-screen]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(false);
    expect(
      (
        calm.host.querySelector(
          "[data-testid=marvel0-autonomy-history]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(false);
    expect(text($(calm.host, "marvel0-autonomy-history"))).toContain(
      E["marvel0.autonomy.history.note"],
    );
    const pre = $(calm.host, "marvel0-autonomy-screen")!.querySelector("pre")!;
    expect(pre.getAttribute("lang")).toBe("zh");
    const drift = await openStep({ run: runDoc("unknown_drift") });
    expect(
      (
        drift.host.querySelector(
          "[data-testid=marvel0-autonomy-screen]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(true);
    expect(text($(drift.host, "marvel0-autonomy-screen"))).toContain(
      E["marvel0.autonomy.screen.drift"],
    );
  });

  test("a state the page does not know is shown as unknown, with Stop and no key", async () => {
    const { host } = await openStep({
      run: runDoc("await_release", (doc) => (doc.state.id = "await_telepathy")),
    });
    expect($(host, "marvel0-autonomy-run")!.getAttribute("data-state")).toBe(
      "unknown",
    );
    expect(text($(host, "marvel0-autonomy-hint"))).toBe(
      E["marvel0.autonomy.hint.unknown"],
    );
  });

  test("the fixed sentence is there in every state and says what the buttons are and are not", async () => {
    for (const name of [
      "idle",
      "await_release",
      "forward_running",
      "stalled",
      "ended_done",
    ]) {
      const { host } = await openStep({ run: runDoc(name) });
      const honest = $(host, "marvel0-autonomy-honest")!;
      expect([name, text(honest)]).toEqual([
        name,
        E["marvel0.autonomy.honest"],
      ]);
      expect(honest.closest("details")).toBeNull();
    }
    expect(E["marvel0.autonomy.honest"]).toMatch(/not an emergency stop/);
    expect(E["marvel0.autonomy.honest"]).toMatch(/Stop is Ctrl\+C/);
    expect(E["marvel0.autonomy.honest"]).toMatch(
      /cannot stop another program of the same account/,
    );
  });

  test("nothing in the card shows the vision model's judgement", async () => {
    for (const name of [
      "await_release",
      "forward_running",
      "reverse_running",
      "ended_done",
    ]) {
      const { host } = await openStep({ run: runDoc(name) });
      expect(text(host)).not.toMatch(
        /subtask_check|subtask_retry|subtask_plan/,
      );
    }
  });
});

describe("how the card is laid out", () => {
  const before = (a: Element | null, b: Element | null) =>
    !!a &&
    !!b &&
    !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);

  test("after the exit, what happened comes first: the exit, what is left behind, the way to the run's page, then the rest", async () => {
    const { host } = await openStep({ run: runDoc("ended_hangup") });
    const ended = $(host, "marvel0-autonomy-ended");
    expect(before(ended, $(host, "marvel0-autonomy-progress"))).toBe(true);
    expect(before($(host, "marvel0-autonomy-status"), ended)).toBe(true);
  });

  test("process, keyboard, locks and ports are folded, and open when something is off with them", async () => {
    const calm = await openStep({ run: runDoc("await_release") });
    expect(
      (
        calm.host.querySelector(
          "[data-testid=marvel0-autonomy-facts-fold]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(false);
    const orphan = await openStep({ run: runDoc("orphaned") });
    expect(
      (
        orphan.host.querySelector(
          "[data-testid=marvel0-autonomy-facts-fold]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(true);
    const red = await openStep({
      run: runDoc("await_release", (doc) => (doc.monitor.state = "red")),
    });
    expect(
      (
        red.host.querySelector(
          "[data-testid=marvel0-autonomy-facts-fold]",
        ) as HTMLDetailsElement
      ).open,
    ).toBe(true);
  });

  test("the keys are in two columns only when both a first and a second step are offered", async () => {
    const both = await openStep({ run: runDoc("await_resume_confirm") });
    expect(both.host.querySelector(".rg-akeys__cols--two")).not.toBeNull();
    const single = await openStep({ run: runDoc("forward_running") });
    expect(single.host.querySelector(".rg-akeys__cols--two")).toBeNull();
    const first = await openStep({ run: runDoc("await_release") });
    expect(first.host.querySelector(".rg-akeys__cols--two")).toBeNull();
  });
});

describe("the dead and the stuck", () => {
  test("ended: no key, no Stop, the exit in words, what is left behind, the labels are yours, and the run's page is linked", async () => {
    const { host } = await openStep({ run: runDoc("ended_done") });
    expect(shownLinks(host)).toEqual(["/automatic/runs/aeri-20261021-ext-r01"]);
    expect(text($(host, "marvel0-autonomy-exit"))).toContain(
      "D finished normally",
    );
    expect(text($(host, "marvel0-autonomy-ended"))).toContain(
      E["marvel0.autonomy.labels_yours"],
    );
    expect(host.querySelectorAll("div.rg-akey")).toHaveLength(0);
    expect(stop(host)).toBeNull();
  });

  test("the link to the run's own page is not there while D lives", async () => {
    for (const name of [
      "launching",
      "await_release",
      "forward_running",
      "stopping",
      "stalled",
      "orphaned",
    ]) {
      const { host } = await openStep({ run: runDoc(name) });
      expect([name, $(host, "marvel0-autonomy-run-link")]).toEqual([
        name,
        null,
      ]);
    }
  });

  test("exited by the hang-up chain's SIGKILL: said with the signal, and the units left behind are listed", async () => {
    const { host } = await openStep({ run: runDoc("ended_hangup") });
    expect(text($(host, "marvel0-autonomy-exit"))).toContain(
      "ended by signal 9",
    );
    const after = text($(host, "marvel0-autonomy-after"));
    expect(after).toContain("2 AERI unit(s) are still there");
    expect(after).toContain("levi-aeri-policy-forward");
    expect(after).toContain(E["marvel0.autonomy.after.swap_dirty"]);
  });

  test("refused before it started", async () => {
    const { host } = await openStep({ run: runDoc("ended_refused") });
    expect($(host, "marvel0-autonomy-exit")!.getAttribute("data-kind")).toBe(
      "refused",
    );
  });

  test("found again after a restart: no keyboard, no key, Stop already sent, and it says so", async () => {
    const { host } = await openStep({ run: runDoc("orphaned") });
    expect(host.querySelectorAll("div.rg-akey")).toHaveLength(0);
    expect(text($(host, "marvel0-autonomy-facts"))).toContain(
      E["marvel0.autonomy.fact.keyboard_no"],
    );
    expect(stop(host)!.disabled).toBe(true);
    expect(text($(host, "marvel0-autonomy-stop-caption"))).toBe(
      E["marvel0.autonomy.stop.sent"],
    );
  });

  test("stalled: the wrap-up is stuck, the fold with the forced end is open; its window is a danger button, asks no phrase and sends the page's once", async () => {
    const f = await openStep({ run: runDoc("stalled") });
    const fold = $(f.host, "marvel0-autonomy-end") as HTMLDetailsElement;
    expect(fold.open).toBe(true);
    const go = button(fold, E["marvel0.autonomy.end.button"])!;
    expect(go.disabled).toBe(false);
    await click(go);
    const d = dialog()!;
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(text(d)).toContain(E["marvel0.autonomy.end.kills"]);
    expect(textFields(d)).toHaveLength(0);
    const confirm = Array.from(d.querySelectorAll("button")).find(
      (b) => text(b) === E["marvel0.autonomy.end.button"],
    )!;
    expect(confirm.className).toContain("ds-btn--danger");
    // The three checks of the stack's windows; Confirm waits for all of them.
    expect(d.querySelectorAll("input[type=checkbox]")).toHaveLength(3);
    expect(confirm.disabled).toBe(true);
    await tickAll(d);
    expect(confirm.disabled).toBe(false);
    await doubleClick(confirm);
    await tick(3, 10);
    expect(callsOf(f.state, "end")).toEqual([
      {
        request_id: expect.any(String),
        confirm: "end terminal D",
        attest: ["estop_hand", "hands_clear", "no_client"],
      },
    ]);
  });

  test("the forced end is shut and off while D can still wrap up", async () => {
    const { host } = await openStep({ run: runDoc("stopping") });
    const fold = $(host, "marvel0-autonomy-end") as HTMLDetailsElement;
    expect(fold.open).toBe(false);
    expect(button(fold, E["marvel0.autonomy.end.button"])!.disabled).toBe(true);
    expect(text($(host, "marvel0-autonomy-end-why"))).toContain(
      E["marvel0.autonomy.err.end_not_allowed"],
    );
  });

  test("locked after a fault: nothing to press, and the sentence says so", async () => {
    const { host } = await openStep({ run: runDoc("fault_locked") });
    expect(host.querySelectorAll("div.rg-akey")).toHaveLength(0);
    expect(text($(host, "marvel0-autonomy-hint"))).toContain("Press nothing");
  });

  test("the read failed: the card says it cannot read; if D was seen alive Stop is there with the way out of a hung page", async () => {
    const f = await openStep({
      run: runDoc("forward_running"),
      limits: { docStaleMs: 100 },
    });
    f.state.run = null;
    await wait(300);
    // The first read worked, so the card keeps what it saw and calls it old.
    expect($(f.host, "marvel0-autonomy-stale")).not.toBeNull();
    const cold = await openStep({ run: null });
    expect(text($(cold.host, "marvel0-autonomy-unreadable"))).toContain(
      E["marvel0.autonomy.run.unreadable"],
    );
    // Never read: the way out that does not need this page is still named.
    expect(text($(cold.host, "marvel0-autonomy-unreadable"))).toContain(
      "scripts/aeri_page_stop.py",
    );
    // Nothing known of D: no Stop to offer, no invented one.
    expect(stop(cold.host)).toBeNull();
  });
});

function shownLinks(host: ParentNode): string[] {
  return Array.from(host.querySelectorAll("a[href^='/automatic/']")).map(
    (a) => a.getAttribute("href")!,
  );
}

describe("presence", () => {
  test("a visible tab in focus tells the backend it is there, with its own id, while D lives; none otherwise", async () => {
    document.hasFocus = () => true;
    const live = await openStep({ run: runDoc("await_release") });
    await wait(250);
    const beats = callsOf(live.state, "presence");
    expect(beats.length).toBeGreaterThanOrEqual(3);
    for (const beat of beats)
      expect(beat).toEqual({ tab_id: expect.any(String), visible: true });
    expect(new Set(beats.map((b) => b.tab_id)).size).toBe(1);
    const keyTab = (callsOf(live.state, "key")[0] ?? {}).tab_id;
    expect(keyTab === undefined || keyTab === beats[0].tab_id).toBe(true);
    const quiet = await openStep({ run: runDoc("idle") });
    await wait(250);
    expect(callsOf(quiet.state, "presence")).toEqual([]);
    const over = await openStep({ run: runDoc("ended_done") });
    await wait(150);
    expect(callsOf(over.state, "presence")).toEqual([]);
  });

  test("the tab id sent with a key is the one the beats carry", async () => {
    document.hasFocus = () => true;
    const f = await openStep({ run: runDoc("await_inventory") });
    await click(keyButton(f.host, "quit"));
    await wait(120);
    const [key] = callsOf(f.state, "key");
    const beat = callsOf(f.state, "presence")[0];
    expect(key.tab_id).toBe(beat.tab_id);
  });

  test("a tab that is not in focus sends nothing", async () => {
    document.hasFocus = () => false;
    const f = await openStep({ run: runDoc("await_release") });
    await wait(250);
    expect(callsOf(f.state, "presence")).toEqual([]);
    document.hasFocus = () => true;
  });
});

describe("hosting off, no site section, a run that cannot be read", () => {
  test("hosting off shows a static plan without run facts or write requests", async () => {
    const f = await openStep({
      run: runDoc("idle_disabled"),
      ready: readyDoc("disabled"),
    });
    const plan = $(f.host, "marvel0-autonomy-plan");
    expect(text(plan)).toContain(E["marvel0.autonomy.plan.title"]);
    expect(plan?.querySelectorAll("ol li")).toHaveLength(6);
    expect(plan?.querySelector("[aria-current], dl, button, a")).toBeNull();
    expect($(f.host, "marvel0-autonomy-run")).toBeNull();
    expect($(f.host, "marvel0-autonomy-prepare-card")).toBeNull();
    expect(f.state.calls).toEqual([]);
  });

  test("idle hosting shows the plan, while a live or unreadable run keeps its card", async () => {
    const idle = await openStep({ run: runDoc("idle") });
    expect($(idle.host, "marvel0-autonomy-plan")).not.toBeNull();
    expect($(idle.host, "marvel0-autonomy-run")).toBeNull();
    expect(idle.state.calls).toEqual([]);
    const live = await openStep({ run: runDoc("forward_running") });
    expect($(live.host, "marvel0-autonomy-plan")).toBeNull();
    expect($(live.host, "marvel0-autonomy-overview")).not.toBeNull();
    const unreadable = await openStep({ run: null });
    expect($(unreadable.host, "marvel0-autonomy-plan")).toBeNull();
    expect($(unreadable.host, "marvel0-autonomy-run")).not.toBeNull();
  });

  test("the control file has no [aeri] section: said, nothing to press", async () => {
    const { host } = await openStep({
      run: runDoc("idle_disabled"),
      ready: readyDoc("unconfigured"),
    });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E["marvel0.autonomy.disabled.missing"],
    );
    expect($(host, "marvel0-autonomy-prepare-card")).toBeNull();
    expect(host.querySelectorAll("div.rg-akey")).toHaveLength(0);
    expect(
      host.querySelectorAll("button[data-testid=marvel0-autonomy-start]"),
    ).toHaveLength(0);
  });

  test("a malformed section counts as off and shows its own sentence", async () => {
    const { host } = await openStep({
      run: runDoc("idle_disabled"),
      ready: readyDoc("unconfigured", (doc) => {
        doc.control.state = "invalid";
        doc.control.errors = [{ code: "aeri.unknown_key" }];
      }),
    });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E["marvel0.autonomy.disabled.invalid"],
    );
    expect($(host, "marvel0-autonomy-prepare-card")).toBeNull();
  });

  test("switched off with the key `enabled = false`", async () => {
    // The readiness document alone says it (the run document, read at another
    // moment, still says it is on): the page is off.
    const { host } = await openStep({
      run: runDoc("idle"),
      ready: readyDoc("prepared_ready", (doc) => (doc.enabled = false)),
    });
    expect(text($(host, "marvel0-autonomy-disabled"))).toBe(
      E["marvel0.autonomy.disabled.off"],
    );
    expect($(host, "marvel0-autonomy-prepare-card")).toBeNull();
  });

  test("the route says aeri_disabled: the same, and the run card has nothing to show", async () => {
    const f = await openStep({
      run: null,
      ready: null,
      fail: refusal(403, "aeri_disabled"),
    });
    expect(text($(f.host, "marvel0-autonomy-disabled"))).toBe(
      E["marvel0.autonomy.disabled.off"],
    );
    expect($(f.host, "marvel0-autonomy-prepare-card") === null).toBe(true);
    expect($(f.host, "marvel0-autonomy-run") === null).toBe(true);
  });

  test("the site file has no [aeri] section, or a malformed one: said apart from 'off'", async () => {
    const none = await openStep({
      run: runDoc("idle"),
      ready: readyDoc("prepared_ready", (doc) => {
        doc.site.state = "unconfigured";
      }),
    });
    expect(
      $(none.host, "marvel0-autonomy-unconfigured")!.getAttribute("data-site"),
    ).toBe("unconfigured");
    expect(text($(none.host, "marvel0-autonomy-unconfigured"))).toBe(
      E["marvel0.autonomy.unconfigured"],
    );
    // Nothing to prepare without a usable section: no empty card either.
    expect($(none.host, "marvel0-autonomy-prepare-card") === null).toBe(true);
    const bad = await openStep({
      run: runDoc("idle"),
      ready: readyDoc("prepared_ready", (doc) => {
        doc.site.state = "invalid";
        doc.site.errors = [{ code: "aeri.pin_sha" }];
      }),
    });
    expect(text($(bad.host, "marvel0-autonomy-unconfigured"))).toContain(
      E["marvel0.autonomy.site_invalid"],
    );
    expect(text($(bad.host, "marvel0-autonomy-unconfigured"))).toContain(
      "aeri.pin_sha",
    );
  });

  test("hosting off while D is alive: its card stays, so Stop does", async () => {
    const { host } = await openStep({
      run: runDoc("forward_running"),
      ready: readyDoc("prepared_ready", (doc) => (doc.enabled = false)),
    });
    expect(stop(host)).not.toBeNull();
  });
});

describe("in Chinese", () => {
  test("the state, the keys, the sentence of the step and a refusal are worded; no English word of the backend leaks in", async () => {
    const f = await openStep({ run: runDoc("await_inventory"), zh: true });
    expect(text($(f.host, "marvel0-autonomy-state"))).toBe(
      Z["marvel0.autonomy.state.await_inventory"],
    );
    expect(text($(f.host, "marvel0-autonomy-honest"))).toBe(
      Z["marvel0.autonomy.honest"],
    );
    expect(text(keyButton(f.host, "inventory_yes")!)).toContain(
      Z["marvel0.autonomy.key.inventory_yes"],
    );
    expect(text(keyButton(f.host, "quit")!)).toContain(
      Z["marvel0.autonomy.key.quit"],
    );
    expect(text($(f.host, "marvel0-autonomy-stop-caption"))).toBe(
      Z["marvel0.autonomy.stop.ready"],
    );
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(409, "key_sent", {}, "English words")
        : { ok: true, status: 202, data: {} };
    await click(keyButton(f.host, "inventory_no"));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      Z["marvel0.autonomy.err.key_sent"],
    );
    expect(f.host.querySelectorAll("[lang=en]")).not.toBeNull();
    const english = Array.from(f.host.querySelectorAll("[lang=en]")).map((n) =>
      text(n),
    );
    // Only D's and the VLM's own words (ids, colours, kinds) stay English.
    for (const word of english)
      expect(word).not.toMatch(/waits for|not an emergency/);
  });

  test("a code the page does not know shows the server's sentence, marked as English", async () => {
    const { host } = await openStep({
      run: runDoc(
        "await_release_key_sent",
        (doc) =>
          (doc.keys[0].why_not = { code: "from_the_future", params: {} }),
      ),
      zh: true,
    });
    const note = keyButton(host, "release_round")!.closest(".rg-akey")!;
    expect(note.querySelector("[lang=en]")!.textContent).toBe(
      "from_the_future",
    );
  });
});
