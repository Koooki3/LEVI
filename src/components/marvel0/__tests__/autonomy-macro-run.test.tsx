import { afterEach, describe, expect, test } from "bun:test";
import { click, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { doubleClick, tick } from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  shownKeys,
  text,
  wait,
} from "./autonomy-helpers";
import { callsOf, keyOf, readyDoc, refusal, runDoc } from "./autonomy-fixtures";

const allMetDoc = () =>
  readyDoc("prepared_ready", (doc) => {
    for (const row of doc.readiness) row.level = "ok";
  });

// The one-click homing: one button, one click, then o, p and h, each sent only
// when terminal D shows its own prompt.

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
const macro = (host: ParentNode) => keyButton(host, "macro_home")!;
const sentKeys = (state: { calls: { op: string }[] }) =>
  callsOf(state as never, "key").map((body) => body.action);
const home = (name: string) => runDoc(name);

/** The button is on once its key's arming delay has passed. */
async function clickMacro(host: ParentNode) {
  await waitFor(() => !macro(host).disabled, { label: "the button to be on" });
  await click(macro(host));
}

describe("the button", () => {
  test("is one button for the three keys, with what the click stands for beside it; no window, no tick, no typing", async () => {
    const f = await openStep({ run: home("await_home_o") });
    const host = f.host;
    expect(shownKeys(host).sort()).toEqual(["macro_home", "quit"]);
    for (const id of ["home_o", "home_p", "home_h"])
      expect(keyButton(host, id)).toBeNull();
    expect(text(macro(host))).toContain(E["marvel0.autonomy.macro.home.label"]);
    expect(E["marvel0.autonomy.macro.home.label"]).toContain("gripper");
    expect(text($(host, "marvel0-autonomy-hazard-macro_home"))).toBe(
      E["marvel0.autonomy.macro.home.hazard"],
    );
    // q stays
    expect(keyButton(host, "quit")).not.toBeNull();
    await clickMacro(host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    expect(dialog()).toBeNull();
    expect(host.querySelectorAll("input[type=checkbox]")).toHaveLength(0);
  });

  test("the same button is there in each of the three homing prompts, and the steps are shown", async () => {
    for (const [name, step] of [
      ["await_home_o", "1"],
      ["await_home_p", "2"],
      ["await_home_h", "3"],
    ] as const) {
      const { host } = await openStep({ run: home(name) });
      expect([name, shownKeys(host).sort()]).toEqual([
        name,
        ["macro_home", "quit"],
      ]);
      expect(
        $(host, "marvel0-autonomy-homing")!.getAttribute("data-step"),
      ).toBe(step);
    }
  });

  test("a profile without the startup home has none of it", async () => {
    const { host } = await openStep({
      run: runDoc("await_home_o", (doc) => (doc.profile.start_home = false)),
    });
    expect(keyButton(host, "macro_home")).toBeNull();
    expect(keyButton(host, "quit")).not.toBeNull();
  });

  test("in Chinese: the words are the page's, with the gripper and the path in them", async () => {
    const { host } = await openStep({ run: home("await_home_o"), zh: true });
    expect(text(macro(host))).toContain(Z["marvel0.autonomy.macro.home.label"]);
    expect(Z["marvel0.autonomy.macro.home.label"]).toBe(
      "回位：夹爪内无物、路径通畅、回位会张开夹爪",
    );
    expect(text($(host, "marvel0-autonomy-hazard-macro_home"))).toBe(
      Z["marvel0.autonomy.macro.home.hazard"],
    );
  });

  test("it waits for the key's own delay like the key would, and a double click is one sequence with one first key", async () => {
    const f = await openStep({ run: home("await_home_o") });
    expect(macro(f.host).disabled).toBe(true);
    await waitFor(() => !macro(f.host).disabled, { label: "the delay" });
    await doubleClick(macro(f.host));
    await tick(3, 20);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await wait(500); // D has not moved: nothing else may go
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });
});

describe("the sequence", () => {
  test("o, p and h go one by one, each only when D shows its own prompt; the first one with its own token and nothing else in the body", async () => {
    const f = await openStep({ run: home("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    const first = callsOf(f.state, "key")[0];
    expect(first).toMatchObject({
      action: "home_o",
      token: keyOf(home("await_home_o"), "home_o")!.token,
    });
    expect(first).not.toHaveProperty("confirm");
    expect(first).not.toHaveProperty("attest");
    // D has not printed the next prompt: no p, however long it takes
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    expect($(f.host, "marvel0-autonomy-macro")).not.toBeNull();
    // D works between two prompts: still nothing
    await serve(f, home("busy_external"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    // D prints the p prompt: p goes, with its token for that prompt
    await serve(f, home("await_home_p"));
    await waitFor(() => sentKeys(f.state).length === 2, { label: "key p" });
    expect(callsOf(f.state, "key")[1]).toMatchObject({
      action: "home_p",
      token: keyOf(home("await_home_p"), "home_p")!.token,
    });
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o", "home_p"]);
    await serve(f, home("await_home_h"));
    await waitFor(() => sentKeys(f.state).length === 3, { label: "key h" });
    expect(callsOf(f.state, "key")[2]).toMatchObject({ action: "home_h" });
    // all three: the sequence is over, nothing more goes
    await serve(f, home("closing"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o", "home_p", "home_h"]);
    expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    expect($(f.host, "marvel0-autonomy-macro-ended")).toBeNull();
    const ids = callsOf(f.state, "key").map((body) => body.request_id);
    expect(new Set(ids).size).toBe(3);
  });

  test("it carries on from the prompt D is at (a page opened in the middle)", async () => {
    const f = await openStep({ run: home("await_home_p") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key p" });
    expect(sentKeys(f.state)).toEqual(["home_p"]);
    await serve(f, home("await_home_h"));
    await waitFor(() => sentKeys(f.state).length === 2, { label: "key h" });
    expect(sentKeys(f.state)).toEqual(["home_p", "home_h"]);
  });

  test("a screen that is not the next prompt ends it: nothing more is sent, even when the next prompt comes later", async () => {
    const f = await openStep({ run: home("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await serve(f, home("await_phrase")); // D shows something else entirely
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.unexpected_screen"]
        .replace("{sent}", "1")
        .replace("{total}", "3"),
    );
    await serve(f, home("await_home_p"));
    await wait(900);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    // the person can start again from the prompt D is at
    expect(macro(f.host)).not.toBeNull();
  });

  test.each(["unknown_drift", "stopping", "fault_locked", "ended_done"])(
    "D turning %s in the middle ends it",
    async (name) => {
      const f = await openStep({ run: home("await_home_o") });
      await clickMacro(f.host);
      await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
      await serve(f, home(name));
      await wait(900);
      expect([name, sentKeys(f.state)]).toEqual([name, ["home_o"]]);
      expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    },
  );

  test("a D that never reaches the next prompt ends it after the limit", async () => {
    const f = await openStep({
      run: home("await_home_o"),
      limits: { macroHomeMs: 500 },
    });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the timeout",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.timeout"]
        .replace("{sent}", "1")
        .replace("{total}", "3"),
    );
    await serve(f, home("await_home_p"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });

  test("the way to stop sending the rest, which does not stop D", async () => {
    const f = await openStep({ run: home("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await click($(f.host, "marvel0-autonomy-macro-cancel"));
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.cancelled"]
        .replace("{sent}", "1")
        .replace("{total}", "3"),
    );
    await serve(f, home("await_home_p"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    expect(callsOf(f.state, "stop")).toHaveLength(0);
  });

  test("a key the backend refuses ends it and the refusal is shown in words", async () => {
    const f = await openStep({ run: home("await_home_o") });
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(409, "monitor_not_green")
        : { ok: true, status: 202, data: {} };
    await clickMacro(f.host);
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.refused"]
        .replace("{sent}", "0")
        .replace("{total}", "3"),
    );
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.monitor_not_green"],
    );
    await serve(f, home("await_home_p"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });

  test("q stays available during it and ends the run; nothing more of the sequence goes", async () => {
    const f = await openStep({ run: home("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await serve(f, home("await_home_p"));
    await waitFor(() => sentKeys(f.state).length === 2, { label: "key p" });
    await serve(f, home("await_home_h"));
    await waitFor(() => keyButton(f.host, "quit") !== null, { label: "q" });
    expect(keyButton(f.host, "quit")).not.toBeNull();
  });

  test("another run's document ends it", async () => {
    const f = await openStep({ run: home("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await serve(
      f,
      runDoc("await_home_p", (doc) => (doc.run_id = "20990101T000000-abcdef")),
    );
    await wait(900);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });
});

describe("a sequence gives way to the person (review of L2-STEP5-SIMPLIFY-1117)", () => {
  /** The homing under way after `o`, with D still on the o prompt. */
  async function underWay() {
    const f = await openStep({ run: runDoc("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    return f;
  }
  const stopBtn = (host: ParentNode) =>
    $(host, "marvel0-autonomy-stop") as HTMLButtonElement;

  /** D shows the p prompt but the backend does not offer p yet (the sequence
   * waits for it); a person can still press the other keys of the prompt. */
  const pNotYet = (level: "A" | "B") => (doc: ReturnType<typeof runDoc>) => {
    keyOf(doc, "home_p")!.allowed = false;
    keyOf(doc, "quit")!.level = level;
  };
  const pPrompt = (level: "A" | "B", on = false) =>
    runDoc("await_home_p", (doc) => {
      if (on) keyOf(doc, "quit")!.level = level;
      else pNotYet(level)(doc);
    });
  async function waitingForP(level: "A" | "B") {
    const f = await openStep({ run: runDoc("await_home_o") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key o" });
    await serve(f, pPrompt(level));
    await wait(400);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    expect($(f.host, "marvel0-autonomy-macro")).not.toBeNull();
    return f;
  }

  test("the window of another key opens: the sequence ends, and p and h are never sent, also when p is offered meanwhile", async () => {
    const f = await waitingForP("B");
    await waitFor(() => !keyButton(f.host, "quit")!.disabled, {
      label: "quit on",
    });
    await click(keyButton(f.host, "quit"));
    expect(dialog()).not.toBeNull();
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.cancelled"]
        .replace("{sent}", "1")
        .replace("{total}", "3"),
    );
    // p is offered while the window is open, and h after it
    f.state.run!.keys.find((k: { id: string }) => k.id === "home_p").allowed =
      true;
    await wait(900);
    await serve(f, runDoc("await_home_h"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });

  test("pressing a key of its own ends it first, so q is the next and last key", async () => {
    const f = await waitingForP("A");
    await waitFor(() => !keyButton(f.host, "quit")!.disabled, {
      label: "quit on",
    });
    await click(keyButton(f.host, "quit"));
    await tick(3, 10);
    f.state.run!.keys.find((k: { id: string }) => k.id === "home_p").allowed =
      true;
    await wait(900);
    expect(sentKeys(f.state)).toEqual(["home_o", "quit"]);
  });

  test("a Stop on its way ends it: nothing is sent after it", async () => {
    const f = await underWay();
    let release!: () => void;
    f.state.gate = new Promise<void>((resolve) => (release = resolve));
    await click(stopBtn(f.host));
    await tick(2, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(1);
    await serve(f, runDoc("await_home_p"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
    expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    release();
    await tick(3, 10);
    await serve(f, runDoc("await_home_h"));
    await wait(500);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });

  test("a Stop that was sent ends it too, also while the document still shows a prompt", async () => {
    const f = await underWay();
    await click(stopBtn(f.host));
    await tick(3, 10);
    expect(callsOf(f.state, "stop")).toHaveLength(1);
    await serve(f, runDoc("await_home_p"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["home_o"]);
  });

  test("a write that is on its way holds the next key back, and the key then goes (the start's own write is still finishing when D asks)", async () => {
    const f = await openStep({ ready: allMetDoc(), run: runDoc("idle") });
    // the reads after the start take a while: the start's write is "working"
    let freeReads!: () => void;
    const hold = new Promise<void>((resolve) => (freeReads = resolve));
    const reads = f.api.ready;
    f.api.ready = async (...args: Parameters<typeof reads>) => {
      await hold;
      return reads(...args);
    };
    await click($(f.host, "marvel0-autonomy-start"));
    await waitFor(() => callsOf(f.state, "start").length === 1, {
      label: "the start",
    });
    await serve(f, runDoc("await_phrase"));
    await wait(700);
    // nothing was sent while the start's write finished, and nothing broke
    expect(sentKeys(f.state)).toEqual([]);
    expect($(f.host, "marvel0-autonomy-macro-ended")).toBeNull();
    freeReads();
    await waitFor(() => sentKeys(f.state).length === 1, { label: "execute" });
    expect(sentKeys(f.state)).toEqual(["execute"]);
    expect($(f.host, "marvel0-autonomy-macro-ended")).toBeNull();
  });
});
