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
import { callsOf, keyOf, refusal, runDoc } from "./autonomy-fixtures";

// The inventory answer and the release of round 1 are one button: y, then, when
// terminal D shows the release prompt, Enter. Later rounds have the release as
// a button of its own (autonomy-review2.test.tsx).

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
const macro = (host: ParentNode) => keyButton(host, "macro_inventory")!;
const sentKeys = (state: { calls: { op: string }[] }) =>
  callsOf(state as never, "key").map((body) => body.action);
const doc = (name: string) => runDoc(name);

async function clickMacro(host: ParentNode) {
  await waitFor(() => !macro(host).disabled, { label: "the button to be on" });
  await click(macro(host));
}

describe("the button", () => {
  test("is one button for y and the release, with what the click stands for beside it; n and q stay; no window, no tick", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    expect(shownKeys(f.host).sort()).toEqual([
      "inventory_no",
      "macro_inventory",
      "quit",
    ]);
    expect(keyButton(f.host, "inventory_yes")).toBeNull();
    expect(text(macro(f.host))).toContain(
      E["marvel0.autonomy.macro.inventory.label"],
    );
    expect(macro(f.host).querySelector("kbd")!.textContent).toBe("y Enter");
    expect(text($(f.host, "marvel0-autonomy-hazard-macro_inventory"))).toBe(
      E["marvel0.autonomy.macro.inventory.hazard"],
    );
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    expect(dialog()).toBeNull();
    expect(f.host.querySelectorAll("input[type=checkbox]")).toHaveLength(0);
  });

  test("in Chinese", async () => {
    const { host } = await openStep({ run: doc("await_inventory"), zh: true });
    expect(text(macro(host))).toContain(
      Z["marvel0.autonomy.macro.inventory.label"],
    );
    expect(text($(host, "marvel0-autonomy-hazard-macro_inventory"))).toBe(
      Z["marvel0.autonomy.macro.inventory.hazard"],
    );
    // the bound of the Enter is said beside the button, in both languages
    expect(Z["marvel0.autonomy.macro.inventory.hazard"]).toContain("3 分钟");
    expect(E["marvel0.autonomy.macro.inventory.hazard"]).toContain("3 minutes");
  });

  test("at the release prompt there is the release button and no sequence button", async () => {
    const { host } = await openStep({ run: doc("await_release") });
    expect(keyButton(host, "macro_inventory")).toBeNull();
    expect(keyButton(host, "release_round")).not.toBeNull();
  });
});

describe("the sequence", () => {
  test("y goes at once; the Enter only when D shows the release prompt, carrying the scene tick; then it is over", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    const first = callsOf(f.state, "key")[0];
    expect(first).toMatchObject({
      action: "inventory_yes",
      token: keyOf(doc("await_inventory"), "inventory_yes")!.token,
    });
    expect(first).not.toHaveProperty("attest");
    expect(first).not.toHaveProperty("confirm");
    // D has not printed the release prompt: no Enter, however long it takes
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["inventory_yes"]);
    await serve(f, doc("busy_external")); // the start pose check, the model
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["inventory_yes"]);
    expect($(f.host, "marvel0-autonomy-macro")).not.toBeNull();
    await serve(f, doc("await_release"));
    await waitFor(() => sentKeys(f.state).length === 2, { label: "Enter" });
    const second = callsOf(f.state, "key")[1];
    expect(second).toMatchObject({
      action: "release_round",
      token: keyOf(doc("await_release"), "release_round")!.token,
      attest: ["scene_ready"],
    });
    expect(second).not.toHaveProperty("confirm");
    await serve(f, doc("forward_running"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["inventory_yes", "release_round"]);
    expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    expect($(f.host, "marvel0-autonomy-macro-ended")).toBeNull();
  });

  test("a double click on the button is one sequence", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    await waitFor(() => !macro(f.host).disabled, { label: "the button" });
    await doubleClick(macro(f.host));
    await tick(3, 20);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    await wait(500);
    expect(sentKeys(f.state)).toEqual(["inventory_yes"]);
  });

  test("a screen that is not the release prompt ends it: the Enter is never sent, even if the release prompt comes later", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    await serve(f, doc("await_resume")); // D waits for a person instead
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.unexpected_screen"]
        .replace("{sent}", "1")
        .replace("{total}", "2"),
    );
    await serve(f, doc("await_release"));
    await wait(900);
    expect(sentKeys(f.state)).toEqual(["inventory_yes"]);
    // the release has its own button for the person to press
    expect(keyButton(f.host, "release_round")).not.toBeNull();
  });

  test.each(["ended_refused", "unknown_drift", "stopping", "fault_locked"])(
    "D turning %s after the y ends it",
    async (name) => {
      const f = await openStep({ run: doc("await_inventory") });
      await clickMacro(f.host);
      await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
      await serve(f, doc(name));
      await wait(900);
      expect([name, sentKeys(f.state)]).toEqual([name, ["inventory_yes"]]);
    },
  );

  test("a refused y ends it and shows the refusal; nothing else is sent", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(409, "monitor_not_green")
        : { ok: true, status: 202, data: {} };
    await clickMacro(f.host);
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the note",
    });
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.monitor_not_green"],
    );
    await serve(f, doc("await_release"));
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["inventory_yes"]);
  });

  test("a refused Enter is shown in words, is not sent again, and the sequence is over with y sent", async () => {
    const f = await openStep({ run: doc("await_inventory") });
    f.state.answer = (call) =>
      call.op === "key" && call.body.action === "release_round"
        ? refusal(409, "monitor_not_green")
        : { ok: true, status: 202, data: {} };
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    await serve(f, doc("await_release"));
    await waitFor(() => sentKeys(f.state).length === 2, { label: "Enter" });
    await waitFor(() => $(f.host, "marvel0-autonomy-refused") !== null, {
      label: "the refusal",
    });
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.monitor_not_green"],
    );
    await wait(700);
    expect(sentKeys(f.state)).toEqual(["inventory_yes", "release_round"]);
    expect($(f.host, "marvel0-autonomy-macro")).toBeNull();
    // the release has its own button for the person to press again
    expect(keyButton(f.host, "release_round")).not.toBeNull();
  });

  test("it never reaches back: with the release prompt on the page first, the sequence button is not there", async () => {
    const f = await openStep({ run: doc("await_release") });
    expect(keyButton(f.host, "macro_inventory")).toBeNull();
    await wait(500);
    expect(sentKeys(f.state)).toEqual([]);
  });

  test("a D that never reaches the release prompt ends it after the limit", async () => {
    const f = await openStep({
      run: doc("await_inventory"),
      limits: { macroReleaseMs: 500 },
    });
    await clickMacro(f.host);
    await waitFor(() => sentKeys(f.state).length === 1, { label: "key y" });
    await waitFor(() => $(f.host, "marvel0-autonomy-macro-ended") !== null, {
      label: "the timeout",
    });
    expect(text($(f.host, "marvel0-autonomy-macro-ended"))).toBe(
      E["marvel0.autonomy.macro.ended.timeout"]
        .replace("{sent}", "1")
        .replace("{total}", "2"),
    );
  });
});
