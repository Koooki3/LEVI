import { afterEach, describe, expect, test } from "bun:test";
import {
  click,
  setupDom,
  unmountAll,
  waitFor,
} from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { doubleClick, tick } from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  text,
} from "./autonomy-helpers";
import { callsOf, readyDoc, runDoc, type Doc } from "./autonomy-fixtures";
import { ASSIST_KEY_IDS, KEY_BYTE, runView } from "../autonomy-doc";
import { keyVM, type KeyCtx } from "../autonomy-logic";

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
const KNOWN = new Set(Object.keys(en));
const BYTES: Record<string, string> = {
  assist_x_pos: "x",
  assist_x_neg: "X",
  assist_y_pos: "y",
  assist_y_neg: "Y",
  assist_z_pos: "z",
  assist_z_neg: "Z",
  assist_yaw_pos: "r",
  assist_yaw_neg: "R",
  assist_close: "c",
  assist_lift: "u",
  assist_open: "o",
  assist_handback: "g",
  assist_abort: "a",
};

/** The backend's samples are generated from its key table, which does not have
 * the two yaw keys yet (a parallel branch adds them). A document of the future
 * table is the sample with one more key per id, in the order of the page's
 * list: the yaw keys sit between z- and the gripper's. */
function assistDoc(name = "await_assist", change?: (doc: Doc) => void): Doc {
  return runDoc(name, (doc) => {
    const z = doc.keys.find((k: { id: string }) => k.id === "assist_z_neg");
    if (z && !doc.keys.some((k: { id: string }) => k.id === "assist_yaw_pos"))
      for (const id of ["assist_yaw_pos", "assist_yaw_neg"])
        doc.keys.push({
          ...z,
          id,
          byte: BYTES[id],
          token: `${z.token}-${id}`,
        });
    change?.(doc);
  });
}
const block = (host: ParentNode) => $(host, "marvel0-autonomy-takeover");

async function ready(host: ParentNode, id: string) {
  await waitFor(() => !keyButton(host, id)!.disabled, {
    label: `key ${id} to be on`,
  });
}

describe("the takeover buttons (GRASP_ASSIST)", () => {
  test("the table of the page is the backend's: thirteen keys, the bytes of grasp_assist.KEY_CHARS", () => {
    expect([...ASSIST_KEY_IDS]).toEqual(Object.keys(BYTES));
    for (const id of ASSIST_KEY_IDS) expect(KEY_BYTE[id]).toBe(BYTES[id]);
  });

  test("in the takeover there is one button per key, with its byte, a bilingual label and the e-stop line", async () => {
    const { host } = await openStep({ run: assistDoc("await_assist") });
    expect(block(host) !== null).toBe(true);
    for (const id of ASSIST_KEY_IDS) {
      const b = keyButton(host, id)!;
      expect(b.querySelector("kbd")!.textContent).toBe(BYTES[id]);
      expect(text(b)).toContain(E[`marvel0.autonomy.key.${id}`]);
      expect(b.getAttribute("title")).toBeNull();
    }
    expect(text($(host, "marvel0-autonomy-takeover-estop"))).toBe(
      E["marvel0.autonomy.takeover.estop"],
    );
    expect(E["marvel0.autonomy.takeover.estop"]).toMatch(
      /not an emergency stop/,
    );
    expect(Z["marvel0.autonomy.takeover.estop"]).toContain("不是急停");
    // each takeover key has both languages
    for (const id of ASSIST_KEY_IDS)
      expect(Z[`marvel0.autonomy.key.${id}`]).toBeTruthy();
  });

  test("the measured opening and the holding fact are shown", async () => {
    const { host } = await openStep({ run: assistDoc("await_assist") });
    expect(text($(host, "marvel0-autonomy-takeover-opening"))).toBe("0.299");
    expect(
      $(host, "marvel0-autonomy-takeover-fact")!.getAttribute("data-fact"),
    ).toBe("holding_candidate");
    expect(text($(host, "marvel0-autonomy-takeover-fact"))).toBe(
      E["marvel0.autonomy.takeover.fact.holding_candidate"],
    );
    const none = await openStep({ run: assistDoc("await_assist_no_fact") });
    expect(text($(none.host, "marvel0-autonomy-takeover-fact"))).toBe(
      E["marvel0.autonomy.takeover.fact.none"],
    );
  });

  test.each(ASSIST_KEY_IDS.filter((id) => id !== "assist_handback"))(
    "one click on %s sends exactly one key request, with that key and no window",
    async (id) => {
      const f = await openStep({ run: assistDoc("await_assist") });
      await ready(f.host, id);
      await doubleClick(keyButton(f.host, id));
      await tick(3, 10);
      expect(dialog() !== null).toBe(false);
      const sent = callsOf(f.state, "key");
      expect(sent).toHaveLength(1);
      expect(sent[0]).toMatchObject({
        action: id,
        seen: { prompt_id: assistDoc("await_assist").state.prompt.id },
      });
      expect(sent[0].confirm).toBeUndefined();
      expect(sent[0].attest).toBeUndefined();
    },
  );

  test("the two yaw keys are their own row ('Turn the gripper'), in both languages, r and R", async () => {
    const { host } = await openStep({ run: assistDoc("await_assist") });
    const rows = Array.from(
      block(host)!.querySelectorAll(".rg-atake__row"),
    ).map((row) => ({
      title: text(row.querySelector("p")),
      keys: Array.from(row.querySelectorAll("button[data-action]")).map((b) =>
        b.getAttribute("data-action"),
      ),
    }));
    const yaw = rows.find((row) => row.keys.includes("assist_yaw_pos"))!;
    expect(yaw.title).toBe(E["marvel0.autonomy.takeover.yaw"]);
    expect(yaw.keys).toEqual(["assist_yaw_pos", "assist_yaw_neg"]);
    expect(text(keyButton(host, "assist_yaw_pos"))).toContain("Turn gripper +");
    expect(text(keyButton(host, "assist_yaw_neg"))).toContain(
      "Turn gripper \u2212",
    );
    expect(
      keyButton(host, "assist_yaw_pos")!.querySelector("kbd")!.textContent,
    ).toBe("r");
    expect(
      keyButton(host, "assist_yaw_neg")!.querySelector("kbd")!.textContent,
    ).toBe("R");
    expect(Z["marvel0.autonomy.key.assist_yaw_pos"]).toBe("夹爪正转");
    expect(Z["marvel0.autonomy.key.assist_yaw_neg"]).toBe("夹爪反转");
    expect(Z["marvel0.autonomy.takeover.yaw"]).toBeTruthy();
  });

  test("a document without the yaw keys (the backend's table today) shows no yaw button and no empty row", async () => {
    const { host } = await openStep({ run: runDoc("await_assist") });
    expect(keyButton(host, "assist_yaw_pos")).toBeNull();
    expect(keyButton(host, "assist_yaw_neg")).toBeNull();
    expect(text(block(host))).not.toContain(E["marvel0.autonomy.takeover.yaw"]);
    await ready(host, "assist_x_pos");
  });

  test("hand back is clickable on a holding fact and sends its key", async () => {
    const f = await openStep({ run: assistDoc("await_assist") });
    await ready(f.host, "assist_handback");
    await click(keyButton(f.host, "assist_handback"));
    await tick(3, 10);
    expect(callsOf(f.state, "key").map((b) => b.action)).toEqual([
      "assist_handback",
    ]);
  });

  test.each(["await_assist_no_fact", "await_assist_empty"])(
    "hand back is disabled in %s and nothing is sent, whatever is clicked",
    async (name) => {
      const f = await openStep({ run: assistDoc(name) });
      const b = keyButton(f.host, "assist_handback")!;
      await tick(3, 10);
      expect(b.disabled).toBe(true);
      await click(b);
      await tick(3, 10);
      expect(callsOf(f.state, "key")).toHaveLength(0);
      // the others are not held back by the fact
      await ready(f.host, "assist_x_pos");
    },
  );

  test("the page's own check: hand back is off on every fact that is not a holding one, even if the document offered it", () => {
    for (const fact of [null, "empty", "open", "unknown", "refused", "x"]) {
      const doc = assistDoc("await_assist", (d) => {
        d.takeover.fact = fact;
        d.takeover.handback_ok = true; // a document that lies about it
      });
      const run = runView(doc, KNOWN)!;
      const key = run.keys.find((k) => k.id === "assist_handback")!;
      const ctx: KeyCtx = {
        now: Date.now(),
        docAt: Date.now(),
        current: true,
        firstSeen: new Map(),
        lastKeyAt: null,
        sent: null,
        working: null,
      };
      expect([fact, keyVM(run, key, ctx).mode]).toEqual([fact, "off"]);
    }
  });

  test("a document of a profile without the takeover shows no takeover button at all", async () => {
    const doc = assistDoc("await_assist", (d) => {
      d.profile.grasp_assist = false;
    });
    const { host } = await openStep({ run: doc });
    expect(block(host) !== null).toBe(false);
    for (const id of ASSIST_KEY_IDS)
      expect([id, keyButton(host, id) !== null]).toEqual([id, false]);
  });

  test("outside the takeover state no takeover button is shown", async () => {
    for (const name of [
      "await_release",
      "forward_running",
      "reverse_running",
      "await_resume",
      "closing",
      "busy_external",
    ]) {
      await unmountAll(); // one page at a time (see unmountAll)
      const { host } = await openStep({ run: assistDoc(name) });
      expect([name, block(host) !== null]).toEqual([name, false]);
      for (const id of ASSIST_KEY_IDS)
        expect([name, id, keyButton(host, id) !== null]).toEqual([
          name,
          id,
          false,
        ]);
    }
  });

  test("under a drift (a line that is not D's after the waiting line) only the abort is a button, with the reason in words", async () => {
    const doc = assistDoc("unknown_assist_drift");
    expect(doc.state.id).toBe("unknown");
    const f = await openStep({ run: doc });
    expect(block(f.host) !== null).toBe(true);
    for (const id of ASSIST_KEY_IDS)
      expect([id, keyButton(f.host, id) !== null]).toEqual([
        id,
        id === "assist_abort",
      ]);
    expect(text($(f.host, "marvel0-autonomy-takeover-drift"))).toBe(
      E["marvel0.autonomy.takeover.drift"],
    );
    expect(Z["marvel0.autonomy.takeover.drift"]).toBeTruthy();
    expect(text($(f.host, "marvel0-autonomy-takeover-estop"))).toBe(
      E["marvel0.autonomy.takeover.estop"],
    );
    expect($(f.host, "marvel0-autonomy-takeover-facts") === null).toBe(true);
    await ready(f.host, "assist_abort");
    await click(keyButton(f.host, "assist_abort"));
    await tick(3, 10);
    expect(callsOf(f.state, "key").map((b) => b.action)).toEqual([
      "assist_abort",
    ]);
  });

  test("a document that lists the other takeover keys under a drift gets no button for them", async () => {
    const takeover = assistDoc("await_assist");
    const doc = assistDoc("unknown_assist_drift", (d) => {
      d.keys = takeover.keys;
    });
    const { host } = await openStep({ run: doc });
    for (const id of ASSIST_KEY_IDS)
      expect([id, keyButton(host, id) !== null]).toEqual([
        id,
        id === "assist_abort",
      ]);
  });

  test("assist keys that a document lists in another state are not shown at all", async () => {
    const takeover = assistDoc("await_assist");
    for (const name of ["await_release", "forward_running", "await_resume"]) {
      const doc = assistDoc(name, (d) => {
        d.profile.grasp_assist = true;
        d.keys.push(...takeover.keys);
      });
      await unmountAll(); // one page at a time (see unmountAll)
      const { host } = await openStep({ run: doc });
      expect([name, block(host) !== null]).toEqual([name, false]);
      for (const id of ASSIST_KEY_IDS)
        expect([name, id, keyButton(host, id) !== null]).toEqual([
          name,
          id,
          false,
        ]);
    }
  });

  test("a takeover key is not shown when its document says another prompt is open", async () => {
    const doc = assistDoc("await_assist", (d) => {
      d.state.prompt.kind = "release";
    });
    const { host } = await openStep({ run: doc });
    await tick(3, 10);
    for (const id of ASSIST_KEY_IDS) {
      const b = keyButton(host, id);
      if (b) expect(b.disabled).toBe(true);
    }
  });

  test("a held Enter does not repeat the click: a repeating keydown is cancelled", async () => {
    const f = await openStep({ run: assistDoc("await_assist") });
    await ready(f.host, "assist_x_pos");
    const b = keyButton(f.host, "assist_x_pos")!;
    const repeat = new KeyboardEvent("keydown", {
      key: "Enter",
      repeat: true,
      bubbles: true,
      cancelable: true,
    });
    b.dispatchEvent(repeat);
    expect(repeat.defaultPrevented).toBe(true);
    const first = new KeyboardEvent("keydown", {
      key: "Enter",
      repeat: false,
      bubbles: true,
      cancelable: true,
    });
    b.dispatchEvent(first);
    expect(first.defaultPrevented).toBe(false);
  });

  test("after a click the buttons are locked until D prints its next waiting line (a new prompt)", async () => {
    const f = await openStep({ run: assistDoc("await_assist") });
    await ready(f.host, "assist_x_pos");
    await click(keyButton(f.host, "assist_x_pos"));
    await tick(3, 10);
    for (const id of ASSIST_KEY_IDS)
      expect([id, keyButton(f.host, id)!.disabled]).toEqual([id, true]);
    expect(callsOf(f.state, "key")).toHaveLength(1);
    // D prints the next waiting line: a new prompt id and a later state counter
    const next = assistDoc("await_assist", (d) => {
      d.state.prompt.id = "assist:9999";
      d.keys.forEach((k: { token: string }) => (k.token += "-2"));
    });
    next.state.seq = assistDoc("await_assist").state.seq + 1;
    await serve(f, next, 6);
    await ready(f.host, "assist_z_neg");
    await click(keyButton(f.host, "assist_z_neg"));
    await tick(3, 10);
    expect(callsOf(f.state, "key").map((b) => b.action)).toEqual([
      "assist_x_pos",
      "assist_z_neg",
    ]);
  });

  test("the sentences are in Chinese too", async () => {
    const { host } = await openStep({
      run: assistDoc("await_assist"),
      zh: true,
    });
    expect(text(keyButton(host, "assist_handback")!)).toContain(
      Z["marvel0.autonomy.key.assist_handback"],
    );
    expect(text($(host, "marvel0-autonomy-takeover-estop"))).toBe(
      Z["marvel0.autonomy.takeover.estop"],
    );
  });

  test("the offline rehearsal card is not part of the takeover: it keeps its own routes", async () => {
    const { host } = await openStep({
      run: assistDoc("await_assist"),
      ready: readyDoc("prepared_ready"),
    });
    expect(
      block(host)!.closest("[data-testid=marvel0-autonomy-run]") !== null,
    ).toBe(true);
    expect(
      host.querySelector("[data-testid=marvel0-reset-harness]") !== null,
    ).toBe(false);
  });
});
