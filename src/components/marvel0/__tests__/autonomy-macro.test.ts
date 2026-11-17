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

  test("the homing is offered in each of the three homing states and in no other", () => {
    for (const [name, at] of [
      ["await_home_o", 0],
      ["await_home_p", 1],
      ["await_home_h", 2],
    ] as const) {
      expect([name, macroEntry("home", view(name))]).toEqual([name, at]);
      expect(macroOffered(view(name))).toBe("home");
    }
    for (const name of [
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

describe("the inventory and the release of round 1", () => {
  const inv = (run: RunView, over: Partial<MacroRun> = {}): MacroRun =>
    started(run, { id: "inventory", ...over });
  const afterY = (run: RunView) =>
    inv(run, { next: 1, sentPrompt: "inventory:3305", since: NOW });

  test("y, then the Enter of the release; the sequence starts only at the inventory prompt", () => {
    expect(MACRO_STEPS.inventory).toEqual([
      { key: "inventory_yes", state: "await_inventory" },
      { key: "release_round", state: "await_release" },
    ]);
    expect(MACRO_OF_KEY.inventory_yes).toBe("inventory");
    // the release key stays a button of its own
    expect(MACRO_OF_KEY).not.toHaveProperty("release_round");
    expect(macroEntry("inventory", view("await_inventory"))).toBe(0);
    expect(macroOffered(view("await_inventory"))).toBe("inventory");
    expect(macroEntry("inventory", view("await_release"))).toBeNull();
    expect(macroOffered(view("await_release"))).toBeNull();
  });

  test("y goes at the inventory prompt, with no ticks", () => {
    const run = view("await_inventory");
    const got = move(inv(run), run, [ready(run, "inventory_yes")]);
    expect(got).toMatchObject({ do: "send", attest: [] });
    expect((got as { vm: KeyVM }).vm.key.id).toBe("inventory_yes");
  });

  test("the Enter does not go while D still shows the inventory prompt, nor while it works; it goes on the release prompt, with the scene tick", () => {
    const list = view("await_inventory");
    expect(move(afterY(list), list, [ready(list, "inventory_yes")])).toEqual({
      do: "wait",
    });
    const busy = view("busy_external");
    expect(move(afterY(busy), busy, [])).toEqual({ do: "wait" });
    const release = view("await_release");
    const got = move(afterY(release), release, [
      ready(release, "release_round"),
    ]);
    expect(got).toMatchObject({ do: "send", attest: ["scene_ready"] });
    expect((got as { vm: KeyVM }).vm.key.id).toBe("release_round");
    // not before its own delay
    expect(
      move(afterY(release), release, [ready(release, "release_round", "wait")]),
    ).toEqual({ do: "wait" });
  });

  test("any other screen after the y ends it: the Enter is never sent into it", () => {
    for (const name of [
      "await_phrase",
      "await_home_o",
      "forward_running",
      "await_resume",
      "unknown_drift",
      "stopping",
      "fault_locked",
    ]) {
      const run = view(name);
      expect([name, move(afterY(run), run, [ready(run, "quit")])]).toEqual([
        name,
        { do: "abort", why: "unexpected_screen" },
      ]);
    }
    // a D that has ended is no D to send to
    const ended = view("ended_refused");
    expect(move(afterY(ended), ended, [])).toEqual({
      do: "abort",
      why: "run_changed",
    });
  });

  test("the Enter is not sent later than 3 minutes after the y: the scene stated ready may have changed", () => {
    const busy = view("busy_external");
    const long: Limits = { ...LIMIT, macroReleaseMs: 180_000 };
    expect(
      macroMove(afterY(busy), busy, [], NOW + 150_000, false, long),
    ).toEqual({ do: "wait" });
    expect(
      macroMove(afterY(busy), busy, [], NOW + 181_000, false, long),
    ).toEqual({ do: "abort", why: "timeout" });
    // the release prompt shows up late: ready key, still no Enter
    const release = view("await_release");
    expect(
      macroMove(
        afterY(release),
        release,
        [ready(release, "release_round")],
        NOW + 181_000,
        false,
        long,
      ),
    ).toEqual({ do: "abort", why: "timeout" });
    expect(
      macroMove(
        afterY(release),
        release,
        [ready(release, "release_round")],
        NOW + 150_000,
        false,
        long,
      ),
    ).toMatchObject({ do: "send" });
    // the bounds themselves: 45 s, 3 minutes, 10 minutes
    expect([
      LIMITS.macroHomeMs,
      LIMITS.macroReleaseMs,
      LIMITS.macroStartMs,
    ]).toEqual([45_000, 180_000, 600_000]);
  });
});

describe("the answer to D's start prompt (the click on Start)", () => {
  const run0 = (run: RunView): MacroRun => started(run, { id: "start" });

  test("one step, the execute key on the phrase prompt; no button starts it and no entry state is offered", () => {
    expect(MACRO_STEPS.start).toEqual([
      { key: "execute", state: "await_phrase" },
    ]);
    expect(MACRO_OF_KEY).not.toHaveProperty("execute");
    for (const name of ["await_phrase", "launching", "services", "idle"])
      expect([name, macroOffered(view(name))]).toEqual([name, null]);
    expect(macroEntry("start", view("await_phrase"))).toBeNull();
  });

  test("it waits while D launches and starts its services, and sends the execute key with all seven codes on the phrase prompt", () => {
    for (const name of ["launching", "services"]) {
      const run = view(name);
      expect([name, move(run0(run), run, [])]).toEqual([name, { do: "wait" }]);
    }
    const phrase = view("await_phrase");
    const got = move(run0(phrase), phrase, [ready(phrase, "execute")]);
    expect(got).toMatchObject({ do: "send" });
    expect((got as { attest: string[] }).attest).toHaveLength(7);
    expect((got as { attest: string[] }).attest).toContain("scene");
    expect((got as { attest: string[] }).attest).toContain("no_client");
    // the key's own delay still holds
    expect(
      move(run0(phrase), phrase, [ready(phrase, "execute", "wait")]),
    ).toEqual({ do: "wait" });
  });

  test("any other screen ends it: the long start, a takeover of the keyboard by anything else, a D that stopped", () => {
    for (const name of [
      "await_phrase_variant",
      "await_home_o",
      "await_inventory",
      "await_release",
      "forward_running",
      "unknown_drift",
      "stopping",
      "fault_locked",
      "busy_external",
    ]) {
      const run = view(name);
      expect([name, move(run0(run), run, [])]).toEqual([
        name,
        { do: "abort", why: "unexpected_screen" },
      ]);
    }
    const ended = view("ended_refused");
    expect(move(run0(ended), ended, [])).toEqual({
      do: "abort",
      why: "run_changed",
    });
  });

  test("it uses the long limit: D may take minutes to start its services", () => {
    const run = view("services");
    expect(
      macroMove(run0(run), run, [], NOW + 300_000, false, {
        ...LIMIT,
        macroStartMs: 600_000,
      }),
    ).toEqual({ do: "wait" });
    expect(
      macroMove(run0(run), run, [], NOW + 601_000, false, {
        ...LIMIT,
        macroStartMs: 600_000,
      }),
    ).toEqual({ do: "abort", why: "timeout" });
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
