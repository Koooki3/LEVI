import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import { click, press, setupDom, waitFor } from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import { button, doubleClick, textFields, tick } from "./actions-helpers";
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
import {
  RUN_ANSWERS,
  RUN_REFUSALS,
  RUN_VIEWS,
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

/** What each state shows as keys, by hand from the design's key table. */
const KEYS_OF: Record<string, string[]> = {
  idle: [],
  launching: [],
  await_phrase: ["execute"],
  await_phrase_variant: ["execute", "execute_variant"],
  services: [],
  await_home_o: ["home_o", "quit"],
  await_home_p: ["home_p", "quit"],
  await_home_h: ["home_h", "quit"],
  await_inventory: ["inventory_yes", "inventory_no", "quit"],
  await_release: ["release_round", "quit"],
  forward_running: ["verdict_success", "verdict_failure", "verdict_discard"],
  closing: [],
  reverse_running: [],
  await_resume: ["resume_ask", "quit"],
  await_resume_confirm: ["resume_confirm", "resume_cancel", "quit"],
  busy: [],
  stopping: [],
  stalled: [],
  fault_locked: [],
  orphaned: [],
  unknown: [],
  ended: [],
};
const CASES = RUN_VIEWS.map((name) => [name, runDoc(name).state.id as string]);

describe("every state of the backend's samples", () => {
  // One test per sample: the card names the state, says what D waits for and
  // offers exactly the keys of that state.
  for (const [name, id] of CASES)
    test(`${name}: the state in words, what D waits for, the keys of that state`, async () => {
      const doc = runDoc(name);
      const { host } = await openStep({
        run: doc,
        ready: readyDoc("prepared_ready"),
      });
      if (id === "idle") {
        expect($(host, "marvel0-autonomy-run")).toBeNull();
        return;
      }
      const card = $(host, "marvel0-autonomy-run")!;
      expect(card.getAttribute("data-state")).toBe(id);
      expect(text($(host, "marvel0-autonomy-state"))).toBe(
        E[`marvel0.autonomy.state.${id}`],
      );
      expect(text($(host, "marvel0-autonomy-hint"))).toBe(
        E[`marvel0.autonomy.hint.${id}`],
      );
      // A refused key of the state is shown as well, with its reason.
      const expected = KEYS_OF[id].filter((key) =>
        doc.keys.some((k: { id: string }) => k.id === key),
      );
      const shown = shownKeys(host).filter(Boolean) as string[];
      expect([...shown].sort()).toEqual([...expected].sort());
    });

  test("a key the state does not have is not a button at all: nothing is shown disabled for the sake of looking complete", async () => {
    const { host } = await openStep({ run: runDoc("await_release") });
    for (const id of [
      "inventory_yes",
      "home_o",
      "verdict_success",
      "resume_ask",
      "execute",
    ])
      expect(keyButton(host, id)).toBeNull();
  });

  test("every key button carries its byte as a badge and its label as text, not only as a title", async () => {
    const { host } = await openStep({ run: runDoc("await_inventory") });
    const yes = keyButton(host, "inventory_yes")!;
    expect(yes.querySelector("kbd")!.textContent).toBe("y");
    expect(text(yes)).toContain(E["marvel0.autonomy.key.inventory_yes"]);
    expect(yes.getAttribute("title")).toBeNull();
    expect(keyButton(host, "quit")!.querySelector("kbd")!.textContent).toBe(
      "q",
    );
    expect(
      keyButton(host, "inventory_no")!.querySelector("kbd")!.textContent,
    ).toBe("n");
  });

  test("no key of the terminal is a keyboard shortcut of the page", async () => {
    const { host, state } = await openStep({ run: runDoc("await_inventory") });
    for (const key of [
      "y",
      "n",
      "q",
      "Enter",
      "s",
      "f",
      "d",
      "o",
      "p",
      "h",
      "Escape",
      "c",
    ]) {
      await press(document.body, key);
      await press(host, key);
      await act(async () => {
        window.dispatchEvent(
          new KeyboardEvent("keydown", { key, bubbles: true }),
        );
        document.dispatchEvent(
          new KeyboardEvent("keyup", { key, bubbles: true }),
        );
      });
    }
    await tick(3, 10);
    expect(callsOf(state, "key")).toEqual([]);
    expect(callsOf(state, "stop")).toEqual([]);
    expect(document.querySelectorAll("[accesskey]")).toHaveLength(0);
  });
});

describe("a level A key is one click", () => {
  test("n sends one key request with exactly the closed body of the route, and nothing else", async () => {
    const { host, state } = await openStep({ run: runDoc("await_inventory") });
    await doubleClick(keyButton(host, "inventory_no"));
    await tick(3, 10);
    const sent = callsOf(state, "key");
    expect(sent).toHaveLength(1);
    const doc = runDoc("await_inventory");
    expect(sent[0]).toEqual({
      request_id: expect.stringMatching(/^[A-Za-z0-9._:-]{8,80}$/),
      action: "inventory_no",
      seen: {
        run_id: doc.run_id,
        state_seq: doc.state.seq,
        prompt_id: doc.state.prompt.id,
      },
      token: keyOf(doc, "inventory_no")!.token,
      tab_id: expect.any(String),
    });
    expect(Object.keys(sent[0]).sort()).toEqual([
      "action",
      "request_id",
      "seen",
      "tab_id",
      "token",
    ]);
    expect(state.order.filter((op) => op !== "presence")).toEqual(["key"]);
  });

  test("after the click the key is shown as sent, not resent, and every other key of the prompt is off; the page does not move the state on by itself", async () => {
    const { host, state } = await openStep({ run: runDoc("await_inventory") });
    await click(keyButton(host, "quit"));
    await tick(3, 10);
    expect(keyButton(host, "quit")!.disabled).toBe(true);
    expect(
      text(
        $(host, "marvel0-autonomy-keys")!.querySelector(
          '[data-action="quit"]',
        )!,
      ),
    ).toContain(E["marvel0.autonomy.lock.sent"]);
    for (const id of ["inventory_yes", "inventory_no"]) {
      expect(keyButton(host, id)!.disabled).toBe(true);
      expect(text(keyButton(host, id)!.closest(".rg-akey"))).toContain(
        E["marvel0.autonomy.err.key_sent"],
      );
    }
    // Still the same state: nothing was moved on by the page.
    expect($(host, "marvel0-autonomy-run")!.getAttribute("data-state")).toBe(
      "await_inventory",
    );
    expect(callsOf(state, "key")).toHaveLength(1);
  });

  test("the next prompt unlocks the keys of the next prompt", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    await click(keyButton(f.host, "inventory_no"));
    await serve(f, runDoc("await_release"));
    expect(keyButton(f.host, "release_round")).not.toBeNull();
    expect(text(keyButton(f.host, "quit")!.closest(".rg-akey"))).not.toContain(
      E["marvel0.autonomy.lock.sent"],
    );
    expect(keyButton(f.host, "quit")!.disabled).toBe(false);
  });

  test("a key not seen taken after the time is said so, and is not sent again", async () => {
    const f = await openStep({
      run: runDoc("await_inventory"),
      limits: { sentUnseenMs: 150 },
    });
    await click(keyButton(f.host, "quit"));
    await wait(250);
    expect(text(keyButton(f.host, "quit")!.closest(".rg-akey"))).toContain(
      E["marvel0.autonomy.lock.unseen"],
    );
    expect(keyButton(f.host, "quit")!.disabled).toBe(true);
    expect(callsOf(f.state, "key")).toHaveLength(1);
  });

  test("a refusal is shown in words and the keys come back from the next read: a stale page sends nothing and locks nothing", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(409, "stale_page", {}, "English words")
        : { ok: true, status: 202, data: {} };
    await click(keyButton(f.host, "quit"));
    await tick(3, 10);
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.stale_page"],
    );
    expect(keyButton(f.host, "quit")!.disabled).toBe(false);
  });

  test("a key answers a prompt: with no prompt in the document the page sends nothing, and says the page is stale", async () => {
    const f = await openStep({
      run: runDoc("await_inventory", (doc) => (doc.state.prompt = null)),
    });
    await click(keyButton(f.host, "quit"));
    await tick(3, 10);
    expect(callsOf(f.state, "key")).toEqual([]);
    expect(text($(f.host, "marvel0-autonomy-refused"))).toContain(
      E["marvel0.autonomy.err.stale_page"],
    );
  });

  test("a request with no answer may have arrived: it says so and is not sent again", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    f.state.answer = (call) =>
      call.op === "key" ? noAnswer() : { ok: true, status: 202, data: {} };
    await click(keyButton(f.host, "quit"));
    await tick(3, 10);
    expect(keyButton(f.host, "quit")!.disabled).toBe(true);
    expect(text(keyButton(f.host, "quit")!.closest(".rg-akey"))).toContain(
      E["marvel0.autonomy.lock.unseen"],
    );
    await click(keyButton(f.host, "quit"));
    expect(callsOf(f.state, "key")).toHaveLength(1);
  });
});

describe("the backend's own answers and refusals", () => {
  test("a key accepted the way the backend answers it: the page keeps the key as sent", async () => {
    const f = await openStep({ run: runDoc("await_inventory") });
    f.state.answer = (call) =>
      call.op === "key"
        ? { ok: true, status: 202, data: RUN_ANSWERS.key }
        : { ok: true, status: 202, data: {} };
    await click(keyButton(f.host, "quit"));
    await tick(3, 10);
    expect(text(keyButton(f.host, "quit")!.closest(".rg-akey"))).toContain(
      E["marvel0.autonomy.lock.sent"],
    );
    expect($(f.host, "marvel0-autonomy-refused") === null).toBe(true);
  });

  // Every refusal the backend can give to a key, one test each: it is shown in
  // the page's words (never the server's English), and what the backend locked
  // for the prompt is shown locked at once.
  for (const [code, entry] of Object.entries(RUN_REFUSALS))
    test(`the refusal ${code} of a key is in the page's words`, async () => {
      const f = await openStep({ run: runDoc("await_inventory") });
      f.state.answer = (call) =>
        call.op === "key"
          ? refusal(entry.status, code, entry.detail, "SERVER ENGLISH")
          : { ok: true, status: 202, data: {} };
      await click(keyButton(f.host, "quit"));
      await tick(3, 10);
      const block = $(f.host, "marvel0-autonomy-refused");
      expect(block !== null).toBe(true);
      expect(block!.textContent).not.toContain("SERVER ENGLISH");
      expect(block!.querySelector("[lang=en]") === null).toBe(true);
    });
});

describe("a level B key stands behind a window", () => {
  test("the click opens the window, Cancel has the focus, nothing is sent, and the window can be cancelled and opened again", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    await wait(200);
    await click(keyButton(f.host, "release_round"));
    const d = dialog()!;
    expect(d).not.toBeNull();
    expect(document.activeElement).toBe(button(d, "Cancel"));
    expect(text(d)).toContain(E["marvel0.autonomy.hazard.release_round"]);
    expect(text(d)).toContain(E["marvel0.autonomy.estop_line"]);
    expect(text(d)).toContain(E["marvel0.autonomy.gates_scope"]);
    expect(textFields(d)).toHaveLength(0);
    expect(callsOf(f.state, "key")).toEqual([]);
    await click(button(d, "Cancel"));
    expect(dialog()).toBeNull();
    await click(keyButton(f.host, "release_round"));
    expect(document.activeElement).toBe(button(dialog()!, "Cancel"));
    await press(document.activeElement, "Escape");
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("the key is off with the time left under it until the delay after it appeared has passed; then a click opens the window, and two clicks on Confirm send one key", async () => {
    const f = await openStep({
      run: runDoc("await_release"),
      limits: { armDelayMs: 400 },
    });
    const note = () =>
      text(keyButton(f.host, "release_round")!.closest(".rg-akey"));
    expect(keyButton(f.host, "release_round")!.disabled).toBe(true);
    expect(note()).toMatch(/Available in 0\.\d s/);
    // A click on a key that is not on does nothing at all.
    await click(keyButton(f.host, "release_round"));
    expect(dialog()).toBeNull();
    await waitFor(() => !keyButton(f.host, "release_round")!.disabled, {
      label: "the delay to pass",
    });
    expect(note()).not.toMatch(/Available in/);
    await click(keyButton(f.host, "release_round"));
    const label = E["marvel0.autonomy.key.release_round"];
    const confirm = () =>
      Array.from(dialog()!.querySelectorAll("button")).find((b) =>
        text(b).startsWith(label),
      )!;
    expect(confirm().disabled).toBe(false);
    await doubleClick(confirm());
    await tick(3, 10);
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    const doc = runDoc("await_release");
    expect(sent[0]).toMatchObject({
      action: "release_round",
      token: keyOf(doc, "release_round")!.token,
      seen: {
        run_id: doc.run_id,
        state_seq: doc.state.seq,
        prompt_id: doc.state.prompt.id,
      },
    });
    expect(sent[0]).not.toHaveProperty("confirm");
    expect(dialog()).toBeNull();
  });

  test("the real delay is 1.5 s: the key is off when it appears and on after it", async () => {
    const f = await openStep({
      run: runDoc("await_release"),
      limits: { armDelayMs: 1500, docStaleMs: 60_000 },
    });
    const key = () => keyButton(f.host, "release_round")!;
    expect(key().disabled).toBe(true);
    expect(text(key().closest(".rg-akey"))).toMatch(/Available in 1\.[0-5] s/);
    await wait(1000);
    expect(key().disabled).toBe(true);
    await waitFor(() => !key().disabled, {
      timeoutMs: 1500,
      label: "1.5 s to pass",
    });
  });

  test("3 s after a key, the next level B key waits again (the gap), a level A key does not", async () => {
    const f = await openStep({
      run: runDoc("await_inventory"),
      limits: { armDelayMs: 50, keyGapMs: 700, sentUnseenMs: 5000 },
    });
    await waitFor(() => !keyButton(f.host, "inventory_yes")!.disabled, {
      label: "the first delay",
    });
    await click(keyButton(f.host, "inventory_yes"));
    await click(button(dialog()!, E["marvel0.autonomy.key.inventory_yes"])!);
    await tick(3, 10);
    expect(callsOf(f.state, "key")).toHaveLength(1);
    // The backend moves on to the next prompt at once; its level B key is new
    // and sits behind both the 50 ms delay and the gap after the last key.
    await serve(f, runDoc("await_release"));
    await wait(150);
    expect(keyButton(f.host, "release_round")!.disabled).toBe(true);
    expect(
      text(keyButton(f.host, "release_round")!.closest(".rg-akey")),
    ).toMatch(/Available in/);
    // The quit key of the same prompt was never held back.
    expect(keyButton(f.host, "quit")!.disabled).toBe(false);
    await waitFor(() => !keyButton(f.host, "release_round")!.disabled, {
      label: "the gap to pass",
    });
  });

  test("a window whose prompt moved on while it was open closes by itself and sends nothing", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    await wait(200);
    await click(keyButton(f.host, "release_round"));
    expect(dialog()).not.toBeNull();
    await serve(
      f,
      runDoc("await_release", (doc) => {
        doc.state.seq += 1;
        doc.state.prompt.id = "release:99999";
      }),
    );
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("a window whose key is gone from the document closes by itself too", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    await wait(200);
    await click(keyButton(f.host, "release_round"));
    await serve(
      f,
      runDoc(
        "await_release",
        (doc) =>
          (doc.keys = doc.keys.filter(
            (k: { id: string }) => k.id !== "release_round",
          )),
      ),
    );
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("a key whose window is open cannot be sent after the read went stale", async () => {
    const f = await openStep({
      run: runDoc("await_release"),
      limits: { docStaleMs: 300 },
    });
    await wait(200);
    await click(keyButton(f.host, "release_round"));
    const confirm = () =>
      Array.from(dialog()!.querySelectorAll("button")).find((b) =>
        text(b).startsWith(E["marvel0.autonomy.key.release_round"]),
      )!;
    expect(confirm().disabled).toBe(false);
    f.state.run = null; // the reads fail from now on
    await wait(600);
    expect(confirm().disabled).toBe(true);
    await click(confirm());
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("a refusal inside the window is shown in words", async () => {
    const f = await openStep({ run: runDoc("await_release") });
    f.state.answer = (call) =>
      call.op === "key"
        ? refusal(429, "key_too_soon")
        : { ok: true, status: 202, data: {} };
    await wait(200);
    await click(keyButton(f.host, "release_round"));
    await click(
      Array.from(dialog()!.querySelectorAll("button")).find((b) =>
        text(b).startsWith(E["marvel0.autonomy.key.release_round"]),
      )!,
    );
    await tick(3, 10);
    expect(text($(dialog()!, "marvel0-autonomy-key-refused"))).toContain(
      E["marvel0.autonomy.err.key_too_soon"],
    );
  });

  test("the verdict keys sit in the second column and say that the arm moves; the label is the person's", async () => {
    const f = await openStep({ run: runDoc("forward_running") });
    const keys = $(f.host, "marvel0-autonomy-keys")!;
    expect(keys.querySelector('[data-slot="first"]')).toBeNull();
    const second = keys.querySelector('[data-slot="second"]')!;
    expect(second.querySelectorAll("button")).toHaveLength(3);
    expect(text(second)).toContain(E["marvel0.autonomy.keys.second"]);
    expect(text($(f.host, "marvel0-autonomy-moves-verdict_success"))).toBe(
      E["marvel0.autonomy.motion.arm_moves"],
    );
    await wait(200);
    await click(keyButton(f.host, "verdict_discard"));
    expect(text(dialog()!)).toContain(E["marvel0.autonomy.hazard.discard"]);
  });

  test("after a verdict the three keys are off with the backend's reason", async () => {
    const { host } = await openStep({
      run: runDoc("forward_running_verdict_used"),
    });
    for (const id of [
      "verdict_success",
      "verdict_failure",
      "verdict_discard",
    ]) {
      expect(keyButton(host, id)!.disabled).toBe(true);
      expect(text(keyButton(host, id)!.closest(".rg-akey"))).toContain(
        E["marvel0.autonomy.err.verdict_once"],
      );
    }
  });

  test("the progress of the forward subtask and of the rounds, from the journal", async () => {
    const { host } = await openStep({ run: runDoc("forward_running") });
    const progress = $(host, "marvel0-autonomy-progress")!;
    expect(progress.querySelectorAll("[role=progressbar]")).toHaveLength(2);
    expect(text(progress)).toContain("Round 1 of 5");
    expect(text(progress)).toContain("step 66 of 200");
    expect(text(progress)).toContain("Rounds released: 1. Discarded: 0.");
  });
});
