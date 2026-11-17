import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import { runView, type RunView } from "../autonomy-doc";
import { LIMITS, type KeyVM, type Limits } from "../autonomy-logic";
import {
  MACRO_OF_KEY,
  MACRO_STEPS,
  macroEntry,
  macroMove,
  macroOffered,
  type MacroRun,
} from "../autonomy-macro";
import { KEY_PROMPTS } from "../autonomy-logic";
import { runDoc } from "./autonomy-fixtures";

// The rule that makes a one-click sequence safe: a key goes out only when
// terminal D shows the prompt that key answers, as a prompt printed after the
// previous key of the sequence; everything else ends the sequence.

const KNOWN = new Set(Object.keys(en));
const view = (
  name: string,
  change?: (doc: ReturnType<typeof runDoc>) => void,
): RunView => runView(runDoc(name, change), KNOWN)!;
const LIMIT: Limits = { ...LIMITS, macroHomeMs: 45_000 };
const NOW = 1_000_000;

const ready = (run: RunView, id: string, mode: KeyVM["mode"] = "ready") =>
  ({
    key: run.keys.find((k) => k.id === id)!,
    mode,
    waitMs: 0,
    why: null,
    lock: null,
  }) as KeyVM;

const started = (run: RunView, over: Partial<MacroRun> = {}): MacroRun => ({
  id: "home",
  runId: run.runId!,
  from: 0,
  next: 0,
  sentPrompt: null,
  since: NOW,
  ...over,
});
const move = (
  m: MacroRun,
  run: RunView | null,
  vms: KeyVM[],
  over: { now?: number; working?: boolean } = {},
) => macroMove(m, run, vms, over.now ?? NOW, over.working ?? false, LIMIT);

describe("what a sequence is", () => {
  test("the homing is o, p, h, each on its own state, in that order", () => {
    expect(MACRO_STEPS.home).toEqual([
      { key: "home_o", state: "await_home_o" },
      { key: "home_p", state: "await_home_p" },
      { key: "home_h", state: "await_home_h" },
    ]);
    expect(MACRO_OF_KEY).toMatchObject({
      home_o: "home",
      home_p: "home",
      home_h: "home",
    });
    // each step's key answers exactly the prompt its state shows
    for (const step of MACRO_STEPS.home)
      expect(KEY_PROMPTS[step.key as keyof typeof KEY_PROMPTS]).toEqual([
        step.state.replace("await_", ""),
      ]);
  });

  test("it is offered in each of the three homing states and in no other", () => {
    for (const [name, at] of [
      ["await_home_o", 0],
      ["await_home_p", 1],
      ["await_home_h", 2],
    ] as const) {
      expect([name, macroEntry("home", view(name))]).toEqual([name, at]);
      expect(macroOffered(view(name))).toBe("home");
    }
    for (const name of [
      "await_inventory",
      "await_release",
      "forward_running",
      "await_phrase",
      "busy_external",
      "ended_done",
    ])
      expect([name, macroOffered(view(name))]).toEqual([name, null]);
    expect(macroEntry("home", null)).toBeNull();
    // D gone: nothing to start
    expect(
      macroEntry(
        "home",
        view("await_home_o", (doc) => (doc.d.alive = false)),
      ),
    ).toBeNull();
  });
});

describe("the first key", () => {
  test("goes when D shows its prompt and the key is ready, with no ticks", () => {
    const run = view("await_home_o");
    const got = move(started(run), run, [ready(run, "home_o")]);
    expect(got).toMatchObject({ do: "send", attest: [] });
    expect((got as { vm: KeyVM }).vm.key.id).toBe("home_o");
  });

  test("waits while the key is not ready (arming delay, gap, backend says no) or another write is on its way", () => {
    const run = view("await_home_o");
    for (const mode of ["wait", "off", "locked"] as const)
      expect(move(started(run), run, [ready(run, "home_o", mode)])).toEqual({
        do: "wait",
      });
    expect(move(started(run), run, [])).toEqual({ do: "wait" });
    expect(
      move(started(run), run, [ready(run, "home_o")], { working: true }),
    ).toEqual({ do: "wait" });
  });

  test("a key that stays unready past the limit ends the sequence", () => {
    const run = view("await_home_o");
    expect(
      move(started(run), run, [ready(run, "home_o", "off")], {
        now: NOW + 46_000,
      }),
    ).toEqual({ do: "abort", why: "timeout" });
  });

  test("another state than the step's ends it before anything is sent", () => {
    for (const name of [
      "await_phrase",
      "await_inventory",
      "await_release",
      "forward_running",
      "busy_external",
      "unknown_drift",
      "stopping",
      "fault_locked",
    ]) {
      const run = view(name);
      expect([name, move(started(run), run, [])]).toEqual([
        name,
        { do: "abort", why: "unexpected_screen" },
      ]);
    }
  });

  test("the right state with a prompt that is not the key's ends it too (a document that lies about its prompt)", () => {
    const run = view(
      "await_home_o",
      (doc) => (doc.state.prompt.kind = "home_h"),
    );
    expect(move(started(run), run, [ready(run, "home_o")])).toEqual({
      do: "abort",
      why: "unexpected_screen",
    });
    const none = view("await_home_o", (doc) => (doc.state.prompt = null));
    expect(move(started(none), none, [ready(none, "home_o")])).toEqual({
      do: "abort",
      why: "unexpected_screen",
    });
  });
});

describe("the next key", () => {
  const afterO = (run: RunView) =>
    started(run, { next: 1, sentPrompt: "home_o:2410", since: NOW });

  test("is not sent while D still shows the prompt the last key answered", () => {
    const o = view("await_home_o");
    expect(o.state.prompt!.id).toBe("home_o:2410");
    expect(move(afterO(o), o, [ready(o, "home_o")])).toEqual({ do: "wait" });
  });

  test("is not sent while D works between two prompts (busy)", () => {
    const busy = view("busy_external");
    expect(move(afterO(busy), busy, [])).toEqual({ do: "wait" });
  });

  test("goes when D shows the next prompt, a new one, and the key is ready", () => {
    const p = view("await_home_p");
    const got = move(afterO(p), p, [ready(p, "home_p")]);
    expect((got as { vm: KeyVM }).vm.key.id).toBe("home_p");
    expect(got.do).toBe("send");
  });

  test("never goes on the old prompt even if the state looks right (the same prompt id again)", () => {
    const p = view("await_home_p");
    const same = started(p, {
      next: 1,
      sentPrompt: p.state.prompt!.id,
      since: NOW,
    });
    expect(move(same, p, [ready(p, "home_p")])).toEqual({ do: "wait" });
  });

  test("is not sent in a state that is not the step's, nor busy, nor the old prompt: the sequence ends", () => {
    for (const name of [
      "await_home_h", // skipped a step
      "await_inventory",
      "await_release",
      "await_phrase",
      "forward_running",
      "closing",
      "unknown_drift",
      "stopping",
      "stalled",
      "fault_locked",
      "orphaned",
      "reverse_running",
      "await_resume",
    ]) {
      const run = view(name);
      expect([name, move(afterO(run), run, [ready(run, "home_p")])]).toEqual([
        name,
        { do: "abort", why: "unexpected_screen" },
      ]);
    }
  });

  test("h is the last: after it the sequence is done and nothing more can go", () => {
    const h = view("await_home_h");
    const done = started(h, { next: 3, sentPrompt: "home_h:2608" });
    expect(move(done, h, [ready(h, "home_h")])).toEqual({ do: "done" });
  });

  test("a D that does not reach its next prompt in time ends it; before that it waits", () => {
    const busy = view("busy_external");
    expect(move(afterO(busy), busy, [], { now: NOW + 44_000 })).toEqual({
      do: "wait",
    });
    expect(move(afterO(busy), busy, [], { now: NOW + 46_000 })).toEqual({
      do: "abort",
      why: "timeout",
    });
    const o = view("await_home_o");
    expect(move(afterO(o), o, [], { now: NOW + 46_000 })).toEqual({
      do: "abort",
      why: "timeout",
    });
  });
});

describe("the run", () => {
  test("a missing document, another run or a D that is gone ends the sequence", () => {
    const run = view("await_home_o");
    expect(move(started(run), null, [])).toEqual({
      do: "abort",
      why: "run_changed",
    });
    expect(
      move(started(run, { runId: "20990101T000000-abcdef" }), run, []),
    ).toEqual({ do: "abort", why: "run_changed" });
    const dead = view("await_home_o", (doc) => (doc.d.alive = false));
    expect(move(started(dead), dead, [ready(dead, "home_o")])).toEqual({
      do: "abort",
      why: "run_changed",
    });
  });
});
