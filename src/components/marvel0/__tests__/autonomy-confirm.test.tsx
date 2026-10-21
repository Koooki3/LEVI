import { act } from "react";
import { afterEach, describe, expect, test } from "bun:test";
import {
  click,
  render,
  setupDom,
  waitFor,
} from "@/components/ds/__tests__/dom";
import en from "@/i18n/en.json";
import { button, doubleClick, textFields, tick } from "./actions-helpers";
import {
  $,
  dialog,
  keyButton,
  openStep,
  serve,
  text,
} from "./autonomy-helpers";
import { callsOf, fakeAutonomy, keyOf, runDoc } from "./autonomy-fixtures";
import { runView, type KeyView } from "../autonomy-doc";
import { keyChecks } from "../autonomy-logic";
import {
  useAutonomyActions,
  type AutonomyActions,
  type AutonomyRunPoll,
} from "../use-autonomy";
import { readyView } from "../autonomy-doc";
import type { Polled } from "../use-polled";
import { readyDoc } from "./autonomy-fixtures";

// The confirm window of every level C key (review R10 #1): the page sends the
// phrase itself, so the window is where a person confirms what terminal D asks
// of them. Every check D itself asks before it takes the phrase is a tick of
// its own, then the three checks of the stack's windows; Confirm stays off
// until every box is ticked; Cancel has the focus; a double click sends once;
// and nothing is sent, and the window closes, when the prompt changes under it.

setupDom();
afterEach(() => {
  try {
    localStorage.clear();
  } catch {
    // no storage in this DOM
  }
});

const E = en as Record<string, string>;
const label = (id: string) => E[`marvel0.autonomy.key.${id}`];
const confirmOf = (id: string) =>
  Array.from(dialog()!.querySelectorAll("button")).find((b) =>
    text(b).startsWith(label(id)),
  ) as HTMLButtonElement;
const boxes = (d: ParentNode) =>
  Array.from(
    d.querySelectorAll<HTMLInputElement>(
      "input[type=checkbox]:not([role=switch])",
    ),
  );
async function tickBox(box: HTMLInputElement) {
  await act(async () => box.click());
}
async function pressKey(host: ParentNode, id: string) {
  await waitFor(() => !keyButton(host, id)!.disabled, {
    label: `key ${id} to be on`,
  });
  await click(keyButton(host, id));
}

/** For a window with n ticks: with each one in turn left out, Confirm is off;
 * with all of them, on. Returns the sentences of the ticks. */
async function everyTickIsNeeded(
  open: () => Promise<void>,
  confirm: () => HTMLButtonElement,
  expected: number,
): Promise<string[]> {
  await open();
  const sentences = boxes(dialog()!).map(
    (box) => box.closest("label")?.textContent ?? "",
  );
  expect(sentences).toHaveLength(expected);
  await click(button(dialog()!, "Cancel")!);
  for (let left = 0; left < expected; left += 1) {
    await open();
    const all = boxes(dialog()!);
    expect(confirm().disabled).toBe(true);
    for (let i = 0; i < all.length; i += 1)
      if (i !== left) await tickBox(all[i]);
    // One short: still off, and the footer says how many are left.
    expect([left, confirm().disabled]).toEqual([left, true]);
    expect(
      text(
        $(
          dialog()!.parentElement ?? document.body,
          "marvel0-autonomy-key-dialog-ticks-left",
        ) ?? document.body.querySelector("[data-testid$=ticks-left]"),
      ),
    ).toContain("1");
    await tickBox(all[left]);
    expect([left, confirm().disabled]).toEqual([left, false]);
    await click(button(dialog()!, "Cancel")!);
  }
  return sentences;
}

describe("the window of execute", () => {
  test("it asks D's five checks and the stack's three as separate ticks, each one needed; Cancel has the focus and nothing can be typed", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    const sentences = await everyTickIsNeeded(
      () => pressKey(f.host, "execute"),
      () => confirmOf("execute"),
      8,
    );
    expect(sentences).toEqual(
      keyChecks("execute").map((check) => E[check.label]),
    );
    await pressKey(f.host, "execute");
    expect(document.activeElement).toBe(button(dialog()!, "Cancel")!);
    expect(textFields(dialog()!)).toHaveLength(0);
    // Nothing was sent by all of that.
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("it quotes the line D is showing, so the person confirms that prompt", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await pressKey(f.host, "execute");
    const quote = $(dialog()!, "marvel0-autonomy-key-prompt");
    expect(quote).not.toBeNull();
    expect(text(quote)).toBe(f.state.run!.state.prompt.line);
  });

  test("the phrase sent is the backend's own, word for word, whatever the run is called; a double click is one request", async () => {
    const phrase = "execute some-other-run-name-9";
    const f = await openStep({
      run: runDoc("await_phrase", (doc) => {
        keyOf(doc, "execute")!.phrase = phrase;
      }),
    });
    await pressKey(f.host, "execute");
    for (const box of boxes(dialog()!)) await tickBox(box);
    await doubleClick(confirmOf("execute"));
    await tick(3, 10);
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    expect(sent[0].confirm).toBe(phrase);
    expect(sent[0].action).toBe("execute");
    // The closed body of the route: nothing typed, no byte.
    expect(Object.keys(sent[0]).sort()).toEqual([
      "action",
      "confirm",
      "request_id",
      "seen",
      "tab_id",
      "token",
    ]);
  });

  test("a document that names no phrase for the key gets nothing sent: the page never makes one up, and says it is the page's own mistake", async () => {
    const f = await openStep({
      run: runDoc("await_phrase", (doc) => {
        keyOf(doc, "execute")!.phrase = null;
      }),
    });
    await pressKey(f.host, "execute");
    for (const box of boxes(dialog()!)) await tickBox(box);
    await click(confirmOf("execute"));
    await tick(3, 10);
    expect(callsOf(f.state, "key")).toEqual([]);
    const refused = $(dialog()!, "marvel0-autonomy-key-refused");
    expect(refused?.getAttribute("data-code")).toBe("confirm_required");
    expect(refused?.getAttribute("data-internal")).toBe("true");
  });
});

describe("the window of execute_variant", () => {
  test("it asks the four items of the policy switch check, D's five, and the stack's three, each one needed", async () => {
    const f = await openStep({ run: runDoc("await_phrase_variant") });
    const sentences = await everyTickIsNeeded(
      () => pressKey(f.host, "execute_variant"),
      () => confirmOf("execute_variant"),
      12,
    );
    expect(sentences.slice(0, 4)).toEqual([
      E["marvel0.autonomy.dcheck.switch_units"],
      E["marvel0.autonomy.dcheck.switch_ports"],
      E["marvel0.autonomy.dcheck.switch_gpu"],
      E["marvel0.autonomy.dcheck.switch_home"],
    ]);
  });

  test("the phrase sent is the long one of the document, once", async () => {
    const f = await openStep({ run: runDoc("await_phrase_variant") });
    await pressKey(f.host, "execute_variant");
    for (const box of boxes(dialog()!)) await tickBox(box);
    await doubleClick(confirmOf("execute_variant"));
    await tick(3, 10);
    const sent = callsOf(f.state, "key");
    expect(sent).toHaveLength(1);
    expect(sent[0].confirm).toBe(
      f.state.run!.keys.find((k: { id: string }) => k.id === "execute_variant")
        .phrase,
    );
    expect(sent[0].confirm).toMatch(/ after policy switch check$/);
  });
});

describe("the window of the forced end", () => {
  const open = (host: ParentNode) => async () => {
    const fold = $(host as HTMLElement, "marvel0-autonomy-end")!;
    await click(button(fold, E["marvel0.autonomy.end.button"])!);
  };
  const confirm = () =>
    Array.from(dialog()!.querySelectorAll("button")).find(
      (b) => text(b) === E["marvel0.autonomy.end.button"],
    ) as HTMLButtonElement;

  test("it asks the stack's three checks, each one needed, and sends the document's phrase once", async () => {
    const f = await openStep({ run: runDoc("stalled") });
    const sentences = await everyTickIsNeeded(open(f.host), confirm, 3);
    expect(sentences).toEqual(keyChecks("end").map((c) => E[c.label]));
    await open(f.host)();
    for (const box of boxes(dialog()!)) await tickBox(box);
    await doubleClick(confirm());
    await tick(3, 10);
    expect(callsOf(f.state, "end")).toEqual([
      {
        request_id: expect.any(String),
        confirm: f.state.run!.end.phrase,
      },
    ]);
  });
});

describe("a window closes, and sends nothing, when the prompt changes under it", () => {
  async function ticked(f: Awaited<ReturnType<typeof openStep>>, id: string) {
    await pressKey(f.host, id);
    for (const box of boxes(dialog()!)) await tickBox(box);
    expect(confirmOf(id).disabled).toBe(false);
  }

  test("another state: the window is gone and nothing was sent", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await ticked(f, "execute");
    await serve(f, runDoc("services"));
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("the same prompt printed again (a new prompt id, even with the same counter): the window is gone", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await ticked(f, "execute");
    const again = structuredClone(f.state.run!);
    again.state.prompt.id = `${again.state.prompt.id}9`;
    f.state.run = again; // the counter is the same on purpose
    await tick(4, 30);
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("the same counter and id but a prompt of another kind: the window is gone", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await ticked(f, "execute");
    const other = structuredClone(f.state.run!);
    other.state.prompt.kind = "inventory";
    f.state.run = other;
    await tick(4, 30);
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });

  test("a key the document no longer offers: the window is gone", async () => {
    const f = await openStep({ run: runDoc("await_phrase") });
    await ticked(f, "execute");
    const gone = structuredClone(f.state.run!);
    gone.keys = [];
    gone.state.seq += 1;
    f.state.run = gone;
    await tick(4, 30);
    expect(dialog()).toBeNull();
    expect(callsOf(f.state, "key")).toEqual([]);
  });
});

// The second layer, below the buttons and the window: what `pressKey` itself
// refuses, called with the views a window could be holding.
describe("pressKey on its own", () => {
  function harness() {
    const f = fakeAutonomy({});
    const ready = readyView(readyDoc("prepared_ready"), new Set())!;
    const seen: { actions: AutonomyActions | null } = { actions: null };
    const run = {
      loaded: true,
      value: null,
      current: true,
      problem: null,
      lastOk: Date.now(),
      refresh: async () => {},
      lastSeen: null,
    } as AutonomyRunPoll;
    const polled = {
      loaded: true,
      value: ready,
      current: true,
      problem: null,
      lastOk: Date.now(),
      refresh: async () => {},
    } as Polled<typeof ready>;
    function Probe() {
      seen.actions = useAutonomyActions(f.api, run, polled, {
        tabId: "tab-test-1",
      });
      return null;
    }
    return { f, seen, element: <Probe /> };
  }
  const known = new Set(Object.keys(en));
  const viewOf = (
    name: string,
    change?: (doc: ReturnType<typeof runDoc>) => void,
  ) => runView(runDoc(name, change), known)!;
  const keyIn = (view: ReturnType<typeof viewOf>, id: string): KeyView =>
    view.keys.find((k) => k.id === id)!;

  test("it sends a level C key with the document's phrase, and only that", async () => {
    const { f, seen, element } = harness();
    await render(element);
    const view = viewOf("await_phrase");
    const key = keyIn(view, "execute");
    await act(async () => {
      expect(await seen.actions!.pressKey(key, view, "execute zzz")).toBe(
        false,
      );
      expect(await seen.actions!.pressKey(key, view)).toBe(false);
      expect(await seen.actions!.pressKey(key, view, "")).toBe(false);
    });
    expect(callsOf(f.state, "key")).toEqual([]);
    expect(seen.actions!.refused?.failure.code).toBe("confirm_required");
    await act(async () => {
      expect(await seen.actions!.pressKey(key, view, key.phrase!)).toBe(true);
    });
    expect(callsOf(f.state, "key")).toHaveLength(1);
    expect(callsOf(f.state, "key")[0].confirm).toBe(key.phrase);
  });

  test("it sends nothing while the prompt on D's screen is not the one of the key", async () => {
    const { f, seen, element } = harness();
    await render(element);
    const view = viewOf("await_phrase", (doc) => {
      doc.state.prompt.kind = "release";
    });
    const key = keyIn(view, "execute");
    await act(async () => {
      expect(await seen.actions!.pressKey(key, view, key.phrase!)).toBe(false);
    });
    expect(callsOf(f.state, "key")).toEqual([]);
    expect(seen.actions!.refused?.failure.code).toBe("stale_page");
    // No prompt at all.
    const bare = viewOf("await_phrase", (doc) => {
      doc.state.prompt = null;
    });
    await act(async () => {
      expect(
        await seen.actions!.pressKey(keyIn(bare, "execute"), bare, key.phrase!),
      ).toBe(false);
    });
    expect(callsOf(f.state, "key")).toEqual([]);
  });
});

describe("the window of the Enter that asks to resume", () => {
  // Both kinds of wait are level B now (which kind a wait is rests on an
  // inference, so the asking Enter has the delay, the gap and the presence
  // check too); the window must not say "D resumes at once" for the one that
  // asks a second time, and must not hide it for the one that does.
  test("a wait that asks again: step 1 of 2, with the warning that a D that resumes at once would move the arm", async () => {
    const f = await openStep({ run: runDoc("await_resume") });
    const key = f.state.run!.keys.find(
      (k: { id: string }) => k.id === "resume_ask",
    );
    expect(key.level).toBe("B");
    await pressKey(f.host, "resume_ask");
    const d = text(dialog());
    expect(d).toContain(E["marvel0.autonomy.hazard.resume_ask_confirm"]);
    expect(d).not.toContain(E["marvel0.autonomy.hazard.resume_ask"]);
    expect(d).toContain(E["marvel0.autonomy.motion.may_move"]);
  });

  test("a wait D resumes on at once: said so", async () => {
    const f = await openStep({ run: runDoc("await_resume_direct") });
    await pressKey(f.host, "resume_ask");
    const d = text(dialog());
    expect(d).toContain(E["marvel0.autonomy.hazard.resume_ask"]);
    expect(d).not.toContain(E["marvel0.autonomy.hazard.resume_ask_confirm"]);
  });
});
