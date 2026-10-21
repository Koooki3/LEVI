import { describe, expect, test } from "bun:test";
import { readFileSync } from "fs";
import { join } from "path";
import en from "@/i18n/en.json";
import zh from "@/i18n/zh.json";
import { readyView, runView, type RunView } from "../autonomy-doc";
import {
  DEFAULT_LIMITS,
  LIMITS,
  LEFTOVERS_PHRASE,
  LIVE_STOP_PHRASE,
  PREPARE_PHRASE,
  armKey,
  blockingRows,
  exitKind,
  groupBySlot,
  hasRun,
  historySig,
  homingStep,
  hostingOff,
  initialForm,
  KEY_PROMPTS,
  TERMINAL_CHECKS,
  WINDOW_CHECKS,
  keyChecks,
  keyFitsPrompt,
  keyPhrase,
  keyVM,
  lockPhase,
  prepareBody,
  prepareErrors,
  promptLine,
  readyPaused,
  readyPollMs,
  runPageHref,
  runPollMs,
  startOff,
  startPhrase,
  stateHintKey,
  stopControl,
  stopSeen,
  visibleKeys,
  type KeyCtx,
  type PrepareForm,
  type SentKey,
} from "../autonomy-logic";
import { CODES, RUN_VIEWS, keyOf, readyDoc, runDoc } from "./autonomy-fixtures";
import { KEY_IDS } from "../autonomy-doc";

const KNOWN = new Set(Object.keys(en));
const view = (
  name: string,
  change?: (doc: ReturnType<typeof runDoc>) => void,
) => runView(runDoc(name, change), KNOWN)!;
const ready = (
  name = "prepared_ready",
  change?: Parameters<typeof readyDoc>[1],
) => readyView(readyDoc(name, change), KNOWN)!;

/** A context in which the page read the document `age` ms ago and showed every
 * allowed key `shown` ms ago. */
/** The clock of these tests: 14 s after the samples' `not_before`. */
const NOW = Date.parse("2026-10-21T10:31:20+08:00");
function ctx(
  run: RunView,
  over: Partial<KeyCtx> & { shown?: number; age?: number } = {},
): KeyCtx {
  const now = NOW;
  const { shown = 10_000, age = 100, ...rest } = over;
  const firstSeen = new Map<string, number>();
  for (const key of run.keys)
    if (key.allowed) firstSeen.set(armKey(run, key), now - shown);
  return {
    now,
    docAt: now - age,
    current: true,
    firstSeen,
    lastKeyAt: null,
    sent: null,
    working: null,
    ...rest,
  };
}
const vmOf = (run: RunView, id: string, c: KeyCtx) =>
  keyVM(run, run.keys.find((k) => k.id === id)!, c);

describe("the constants of the design", () => {
  test("a key waits 1.5 s after it appears and 3 s after the last key; a sent key is 'not seen' after 5 s; a read is stale after 2 s", () => {
    expect(LIMITS.armDelayMs).toBe(1500);
    expect(LIMITS.keyGapMs).toBe(3000);
    expect(LIMITS.sentUnseenMs).toBe(5000);
    expect(LIMITS.docStaleMs).toBe(2000);
    expect(LIMITS.presenceMs).toBe(5000);
    // D's prompts move: a read a second while it lives, every 3 s otherwise.
    expect(LIMITS.runPollMs).toBe(1000);
    expect(LIMITS.idlePollMs).toBe(3000);
  });
});

describe("a key's state (keyVM)", () => {
  test("a level A key is ready at once; there is no delay on n and q", () => {
    const run = view("await_inventory");
    const c = ctx(run, { shown: 0 });
    expect(vmOf(run, "inventory_no", c)).toMatchObject({
      mode: "ready",
      waitMs: 0,
    });
    expect(vmOf(run, "quit", c)).toMatchObject({ mode: "ready" });
  });

  test("a level B key that has just appeared waits the whole 1.5 s, then is ready", () => {
    const run = view("await_release");
    expect(vmOf(run, "release_round", ctx(run, { shown: 0 }))).toMatchObject({
      mode: "wait",
      waitMs: 1500,
    });
    expect(vmOf(run, "release_round", ctx(run, { shown: 1000 }))).toMatchObject(
      {
        mode: "wait",
        waitMs: 500,
      },
    );
    expect(vmOf(run, "release_round", ctx(run, { shown: 1500 }))).toMatchObject(
      {
        mode: "ready",
      },
    );
  });

  test("a key the page has no first time for is new: the whole delay", () => {
    const run = view("await_release");
    const c = { ...ctx(run), firstSeen: new Map<string, number>() };
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "wait",
      waitMs: 1500,
    });
  });

  test("the delay counts per state counter: the same key in a new state is new", () => {
    const run = view("await_release");
    const next = view("await_release", (doc) => (doc.state.seq += 1));
    const c = ctx(run, { shown: 10_000 });
    expect(
      vmOf(next, "release_round", { ...c, firstSeen: c.firstSeen }),
    ).toMatchObject({
      mode: "wait",
    });
  });

  test("the backend's not_before is honoured when it is later than the page's own delay", () => {
    const run = view("await_release");
    const c = ctx(run, { shown: 10_000 });
    run.keys.find((k) => k.id === "release_round")!.notBefore = c.now + 700;
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "wait",
      waitMs: 700,
    });
  });

  test("a browser clock that is minutes behind the server's cannot keep a key off for minutes: the wait from not_before is bounded by the gap", () => {
    const run = view("await_release");
    const c = ctx(run, { shown: 10_000 });
    run.keys.find((k) => k.id === "release_round")!.notBefore =
      c.now + 5 * 60_000;
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "wait",
      waitMs: LIMITS.keyGapMs,
    });
  });

  test("3 s after the last key, a level B key waits for the rest of the gap; a level A key does not", () => {
    const run = view("await_release");
    const c = ctx(run, { lastKeyAt: NOW - 1200 });
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "wait",
      waitMs: 1800,
    });
    expect(vmOf(run, "quit", c)).toMatchObject({ mode: "ready" });
  });

  test("the longest of the three waits counts", () => {
    const run = view("await_release");
    const c = ctx(run, { shown: 1400, lastKeyAt: NOW - 2500 });
    run.keys.find((k) => k.id === "release_round")!.notBefore = c.now + 300;
    expect(vmOf(run, "release_round", c).waitMs).toBe(500);
  });

  test("a key the backend does not offer is off, with the backend's reason; none is invented", () => {
    const run = view("await_release_monitor_red");
    const vm = vmOf(run, "release_round", ctx(run));
    expect(vm.mode).toBe("off");
    // The backend's facts come with it (the monitor's state), as placeholders.
    expect(vm.why?.say).toMatchObject({
      key: "marvel0.autonomy.err.monitor_not_green",
    });
    expect(vmOf(run, "quit", ctx(run)).mode).toBe("ready");
  });

  test("a read older than 2 s, or one that failed, switches every key off", () => {
    const run = view("await_inventory");
    for (const c of [
      ctx(run, { age: 2100 }),
      { ...ctx(run), current: false },
      { ...ctx(run), docAt: null },
    ])
      for (const id of ["inventory_yes", "inventory_no", "quit"]) {
        const vm = vmOf(run, id, c);
        expect([id, vm.mode]).toEqual([id, "off"]);
        expect(vm.why?.say).toEqual({
          key: "marvel0.autonomy.local.doc_stale",
        });
      }
    expect(vmOf(run, "quit", ctx(run, { age: 1900 })).mode).toBe("ready");
  });

  test("while one key is on its way every other key is off and that one is locked", () => {
    const run = view("await_release");
    const c = ctx(run, { working: "release_round" });
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "locked",
      lock: "sending",
    });
    expect(vmOf(run, "quit", c)).toMatchObject({ mode: "off" });
  });
});

describe("the key this page sent", () => {
  const run = view("await_release");
  const sent = (over: Partial<SentKey> = {}): SentKey => ({
    runId: run.runId!,
    seq: run.state.seq,
    action: "release_round",
    at: NOW - 1000,
    histSig: historySig(run),
    lost: false,
    ...over,
  });

  test("sent, then 'not seen taken' after 5 s; never sent again", () => {
    expect(lockPhase(sent(), run, NOW)).toBe("sent");
    expect(lockPhase(sent({ at: NOW - 5000 }), run, NOW)).toBe("unseen");
  });

  test("a request that got no answer is 'not seen' at once", () => {
    expect(lockPhase(sent({ lost: true }), run, NOW)).toBe("unseen");
  });

  test("the backend's key_unconfirmed notice makes it 'not seen' at once", () => {
    const flagged = view("await_release", (doc) =>
      doc.notices.push({ code: "key_unconfirmed", level: "warn" }),
    );
    expect(lockPhase(sent(), flagged, NOW)).toBe("unseen");
  });

  test("the click history shows it taken", () => {
    const taken = view("await_release", (doc) =>
      doc.history.push({
        time: "2026-10-21T10:40:00+08:00",
        key: "release_round",
        byte: "Enter",
        state: "await_release",
        prompt_id: "release:48213",
        round: 2,
      }),
    );
    expect(lockPhase(sent(), taken, NOW)).toBe("taken");
    // A different key in the history is not it.
    const other = view("await_release", (doc) =>
      doc.history.push({
        time: "2026-10-21T10:40:00+08:00",
        key: "quit",
        byte: "q",
        state: "await_release",
        prompt_id: "x",
        round: 2,
      }),
    );
    expect(lockPhase(sent(), other, NOW)).toBe("sent");
  });

  test("once the state counter moves on, or the run is another, there is no lock", () => {
    const next = view("await_release", (doc) => (doc.state.seq += 1));
    expect(lockPhase(sent(), next, NOW)).toBeNull();
    expect(lockPhase(sent({ runId: "another" }), run, NOW)).toBeNull();
    expect(lockPhase(null, run, NOW)).toBeNull();
  });

  test("while the prompt is the same, the sent key is locked and every other key of it is off: one key per prompt", () => {
    const c = ctx(run, { sent: sent() });
    expect(vmOf(run, "release_round", c)).toMatchObject({
      mode: "locked",
      lock: "sent",
    });
    const quit = vmOf(run, "quit", c);
    expect(quit.mode).toBe("off");
    expect(quit.why?.say).toEqual({ key: "marvel0.autonomy.err.key_sent" });
  });
});

describe("which keys are shown, and where", () => {
  test("a key that does not belong to the state is not shown; one refused for a reason a person should see is", () => {
    const sentRun = view("await_release_key_sent");
    expect(visibleKeys(sentRun).map((k) => k.id)).toEqual([
      "release_round",
      "quit",
    ]);
    const idle = view("await_release");
    expect(visibleKeys(idle).map((k) => k.id)).toEqual([
      "release_round",
      "quit",
    ]);
    const verdict = view("forward_running_verdict_used");
    expect(visibleKeys(verdict).map((k) => k.id)).toEqual([
      "verdict_success",
      "verdict_failure",
      "verdict_discard",
    ]);
    // Foreign siblings of the long start confirmation show why they are off.
    const variant = view("await_phrase_variant");
    expect(visibleKeys(variant).map((k) => k.id)).toEqual([
      "execute",
      "execute_variant",
    ]);
  });

  test("a key that does not belong to the state (the backend lists it as state_not_allowed) is not shown, and one refused for a reason a person should see still is", () => {
    const run = view("await_release", (doc) => {
      doc.keys.push({
        ...doc.keys[0],
        id: "inventory_yes",
        byte: "y",
        allowed: false,
        why_not: { code: "state_not_allowed", params: {} },
        token: null,
      });
      doc.keys.push({
        ...doc.keys[0],
        id: "verdict_success",
        byte: "s",
        slot: "second",
        allowed: false,
        why_not: { code: "monitor_not_green", params: {} },
        token: null,
      });
    });
    expect(visibleKeys(run).map((k) => k.id)).toEqual([
      "release_round",
      "verdict_success",
      "quit",
    ]);
  });

  test("a profile with no startup homing never shows o, p or h", () => {
    const run = view("await_home_o", (doc) => (doc.profile.start_home = false));
    expect(visibleKeys(run).map((k) => k.id)).toEqual(["quit"]);
    expect(visibleKeys(view("await_home_o")).map((k) => k.id)).toEqual([
      "home_o",
      "quit",
    ]);
  });

  test("quit is last, and the second-step keys are apart from the first", () => {
    const confirm = view("await_resume_confirm");
    const groups = groupBySlot(visibleKeys(confirm));
    expect(groups.first.map((k) => k.id)).toEqual(["quit"]);
    expect(groups.second.map((k) => k.id)).toEqual([
      "resume_confirm",
      "resume_cancel",
    ]);
    const fwd = groupBySlot(visibleKeys(view("forward_running")));
    expect(fwd.first).toEqual([]);
    expect(fwd.second.map((k) => k.id)).toEqual([
      "verdict_success",
      "verdict_failure",
      "verdict_discard",
    ]);
  });

  test("the three homing confirmations are three states in order", () => {
    expect(
      ["await_home_o", "await_home_p", "await_home_h", "await_release"].map(
        (id) => homingStep(id as never),
      ),
    ).toEqual([1, 2, 3, 0]);
    for (const [id, key] of [
      ["await_home_o", "home_o"],
      ["await_home_p", "home_p"],
      ["await_home_h", "home_h"],
    ]) {
      const run = view(id);
      expect([id, visibleKeys(run).map((k) => k.id)]).toEqual([
        id,
        [key, "quit"],
      ]);
    }
  });
});

describe("Stop", () => {
  const seenOf = (name: string, change?: Parameters<typeof runDoc>[1]) =>
    stopSeen(view(name, change));

  test("it is there while D lives, whatever the state, and gone when D is", () => {
    for (const name of [
      "launching",
      "await_phrase",
      "services",
      "forward_running",
      "busy_external",
      "unknown_drift",
    ]) {
      const mode = stopControl(seenOf(name), null);
      expect([name, mode.mode, mode.enabled]).toEqual([name, "ready", true]);
    }
    for (const name of ["idle", "ended_done", "ended_hangup"])
      expect([name, stopControl(seenOf(name), null).mode]).toEqual([
        name,
        "hidden",
      ]);
    expect(stopControl(null, null).mode).toBe("hidden");
  });

  test("a stalled wrap-up has had its Stop: it is shown as acknowledged and offers no second one", () => {
    expect(stopControl(seenOf("stalled"), null)).toEqual({
      mode: "acked",
      enabled: false,
    });
  });

  test("it is off where the backend does not offer it, but still shown", () => {
    expect(stopControl(seenOf("fault_locked"), null)).toEqual({
      mode: "ready",
      enabled: false,
    });
    expect(stopControl(seenOf("stopping"), null).enabled).toBe(false);
  });

  test("once sent it stays off until the stop phase moves on; the retry is offered once", () => {
    const idle = seenOf("forward_running");
    const sent = { runId: idle.runId, phase: idle.phase };
    expect(stopControl(idle, sent)).toEqual({ mode: "ready", enabled: false });
    const unconfirmed = seenOf("stopping_unconfirmed");
    expect(stopControl(unconfirmed, sent)).toEqual({
      mode: "retry",
      enabled: true,
    });
    const retried = seenOf("stopping_unconfirmed", (doc) => {
      doc.stop.phase = "retried";
      doc.stop.allowed = false;
    });
    expect(
      stopControl(retried, { runId: retried.runId, phase: "unconfirmed" }),
    ).toEqual({ mode: "spent", enabled: false });
  });

  test("it does not depend on how old the read was: the seen state is kept", () => {
    const seen = seenOf("forward_running");
    expect(stopControl(seen, null).enabled).toBe(true);
  });

  test("a Stop sent for another run does not lock this one", () => {
    const seen = seenOf("forward_running");
    expect(
      stopControl(seen, { runId: "an-earlier-run", phase: "idle" }).enabled,
    ).toBe(true);
  });
});

describe("what the quote, the link and the exit say", () => {
  test("the line D waits on is the backend's quote, else the last line of the screen; none outside a prompt", () => {
    expect(promptLine(view("await_release"))).toContain("放行本轮");
    const noLine = view(
      "await_release",
      (doc) => (doc.state.prompt.line = null),
    );
    expect(promptLine(noLine)).toBe(
      noLine.screen.tail[noLine.screen.tail.length - 1].trim(),
    );
    expect(promptLine(view("services"))).toBeNull();
  });

  test("the run's own page is linked only after the run ended: before that it shows the vision model's judgements", () => {
    for (const name of [
      "launching",
      "await_release",
      "forward_running",
      "stopping",
      "stalled",
      "orphaned",
    ])
      expect([name, runPageHref(view(name))]).toEqual([name, null]);
    expect(runPageHref(view("ended_done"))).toBe(
      "/automatic/runs/aeri-20261021-ext-r01",
    );
    // An ended record whose process is still alive is not ended.
    expect(
      runPageHref(view("ended_done", (doc) => (doc.d.alive = true))),
    ).toBeNull();
    expect(runPageHref(view("idle"))).toBeNull();
    expect(runPageHref(null)).toBeNull();
  });

  test("the name in the link is encoded", () => {
    const run = view("ended_done", (doc) => (doc.name = "a b/c"));
    expect(runPageHref(run)).toBe("/automatic/runs/a%20b%2Fc");
  });

  test("exit classes", () => {
    expect(
      [0, 1, 2, 130, 97, 98, 137, 143, 77, null].map((code) => exitKind(code)),
    ).toEqual([
      "completed",
      "unfinished",
      "refused",
      "cancelled",
      "host",
      "host",
      "signal",
      "signal",
      "other",
      null,
    ]);
  });

  test("no exit code but a signal is a signal; neither is nothing to say", () => {
    expect(exitKind(null, 9)).toBe("signal");
    expect(exitKind(null, null)).toBeNull();
    expect(exitKind(2, 9)).toBe("refused");
  });

  test("there is a run to show for every state but idle", () => {
    expect(hasRun(view("idle"))).toBe(false);
    expect(hasRun(view("idle_blocked"))).toBe(false);
    expect(hasRun(view("launching"))).toBe(true);
    expect(hasRun(view("ended_done"))).toBe(true);
    expect(hasRun(null)).toBe(false);
  });
});

describe("the phrases the page sends itself", () => {
  test("the start phrase puts the name into the backend's template", () => {
    expect(startPhrase("aeri-x")).toBe("start aeri aeri-x");
    expect(startPhrase("aeri-x", "start aeri {name}")).toBe(
      "start aeri aeri-x",
    );
  });

  test("a level C key goes with the backend's phrase, word for word, and with nothing else: the page never builds one", () => {
    const run = view("await_phrase_variant");
    const [execute, variant] = [
      run.keys.find((k) => k.id === "execute")!,
      run.keys.find((k) => k.id === "execute_variant")!,
    ];
    expect(keyPhrase(variant)).toBe(variant.phrase);
    expect(keyPhrase(variant)).toBe(
      "execute aeri-20261021-ext-r01 after policy switch check",
    );
    expect(keyPhrase(execute)).toBe(execute.phrase);
    // No phrase in the document: none is made up from the run's name.
    expect(keyPhrase({ ...execute, phrase: null })).toBeNull();
    expect(keyPhrase({ ...variant, phrase: "" })).toBeNull();
    // A key below level C has none, even if the document carried one.
    const release = view("await_release").keys.find(
      (k) => k.id === "release_round",
    )!;
    expect(keyPhrase({ ...release, phrase: "execute x" })).toBeNull();
  });

  test("the prepare, live-stop and leftovers phrases", () => {
    expect(PREPARE_PHRASE).toBe("prepare aeri run");
    expect(LIVE_STOP_PHRASE).toBe("stop live service");
    expect(LEFTOVERS_PHRASE).toBe("stop leftover aeri units");
  });
});

describe("Start terminal D: why it is off", () => {
  const live = { evalLive: false, runLive: false, stale: false };
  const codes = (
    r: ReturnType<typeof ready> | null,
    over: Partial<typeof live & { run: RunView | null }> = {},
  ) =>
    startOff(r, { ...live, run: view("idle"), ...over }).map((row) => row.code);

  test("with every row met and a prepared run that is intact, nothing is off", () => {
    const allOk = ready("prepared_ready", (doc) => {
      for (const row of doc.readiness) row.level = "ok";
    });
    expect(codes(allOk)).toEqual([]);
  });

  test("an unknown row refuses like a refusal, and both are listed in the backend's order", () => {
    const r = ready("blocked");
    const refusing = blockingRows(r.rows).map((row) => row.code);
    expect(refusing.length).toBeGreaterThan(2);
    expect(r.rows.some((row) => row.level === "unknown")).toBe(true);
    expect(codes(r)).toEqual(expect.arrayContaining(refusing));
    expect(refusing).toEqual(
      r.rows
        .filter((row) => row.level === "refuse" || row.level === "unknown")
        .map((row) => row.code),
    );
  });

  test("a warning never refuses", () => {
    const warn = ready("prepared_ready", (doc) => {
      for (const row of doc.readiness) row.level = "ok";
      doc.readiness[0].level = "warn";
    });
    expect(codes(warn)).toEqual([]);
  });

  test("nothing read, hosting off, no site section, a live D or an evaluation client, a stale read", () => {
    expect(codes(null)).toEqual(["unreadable"]);
    expect(codes(ready("unconfigured"))).toEqual(["disabled"]);
    const noSite = ready("prepared_ready", (doc) => {
      doc.site.state = "unconfigured";
    });
    expect(codes(noSite)).toEqual(["not_configured"]);
    const allOk = ready("prepared_ready", (doc) => {
      for (const row of doc.readiness) row.level = "ok";
    });
    expect(codes(allOk, { runLive: true })).toContain("run_live");
    expect(codes(allOk, { evalLive: true })).toContain("eval_live");
    expect(codes(allOk, { stale: true })).toContain("ready_stale");
  });

  test("no prepared run, a prepared run that changed, a profile that is not known", () => {
    const allOk = (change: (doc: ReturnType<typeof readyDoc>) => void) =>
      ready("prepared_ready", (doc) => {
        for (const row of doc.readiness) row.level = "ok";
        change(doc);
      });
    expect(codes(allOk((doc) => (doc.prepared = null)))).toEqual([
      "not_prepared",
    ]);
    expect(
      codes(
        allOk((doc) => {
          doc.prepared.intact = false;
          doc.prepared.problems = ["files_changed"];
        }),
      ),
    ).toEqual(["prepared_changed"]);
    expect(codes(allOk((doc) => (doc.profile.known = false)))).toEqual([
      "profile_unknown",
    ]);
  });

  test("the run document's own 'start is not offered' is part of the answer (a terminal driven elsewhere)", () => {
    const allOk = ready("prepared_ready", (doc) => {
      for (const row of doc.readiness) row.level = "ok";
    });
    const blocked = view("idle_blocked");
    const rows = startOff(allOk, { ...live, run: blocked });
    expect(rows.map((row) => row.code)).toEqual(["run_start"]);
    expect(rows[0].worded.say).toEqual({
      key: "marvel0.autonomy.err.driven_elsewhere",
    });
  });
});

describe("the prepare form", () => {
  const form = (over: Partial<PrepareForm> = {}): PrepareForm => ({
    mode: "external",
    task: "pick the pepper into the white plate",
    rounds: "5",
    steps: "200",
    gripperConfirmS: "4",
    startHome: false,
    ...over,
  });

  test("a good form has no errors; the starting values are the backend's defaults", () => {
    expect(prepareErrors(form())).toEqual({});
    const initial = initialForm(ready("prepared_ready"));
    expect(initial).toMatchObject({
      rounds: "5",
      steps: "200",
      gripperConfirmS: "4",
      mode: "external",
      startHome: false,
    });
    expect(initialForm(null).steps).toBe(String(DEFAULT_LIMITS.steps.default));
  });

  test("the task is the new_run.py pattern: lower case, digits, space , . ' - , 3 to 120 characters, one letter at least", () => {
    for (const bad of [
      "",
      "ab",
      "Pick the pepper",
      "put it; rm -rf",
      "pick\nthe",
      "x".repeat(121),
      "123 456",
      "pick ü",
    ])
      expect([
        bad.slice(0, 20),
        prepareErrors(form({ task: bad })).task,
      ]).toEqual([bad.slice(0, 20), "marvel0.autonomy.form.task_bad"]);
    expect(
      prepareErrors(form({ task: "pick the red pepper, put it on the plate" }))
        .task,
    ).toBeUndefined();
  });

  test("rounds, steps and the gripper wait are checked against the backend's limits", () => {
    const limits = ready("prepared_ready").limits!;
    expect(prepareErrors(form({ rounds: "0" }), limits).rounds).toBeDefined();
    expect(prepareErrors(form({ rounds: "51" }), limits).rounds).toBeDefined();
    expect(
      prepareErrors(form({ rounds: "50" }), limits).rounds,
    ).toBeUndefined();
    expect(prepareErrors(form({ steps: "3001" }), limits).steps).toBeDefined();
    expect(
      prepareErrors(form({ steps: "3000" }), limits).steps,
    ).toBeUndefined();
    expect(prepareErrors(form({ steps: "12.5" }), limits).steps).toBeDefined();
    expect(
      prepareErrors(form({ gripperConfirmS: "0.5" }), limits).gripper_confirm_s,
    ).toBeDefined();
    expect(
      prepareErrors(form({ gripperConfirmS: "10.5" }), limits)
        .gripper_confirm_s,
    ).toBeDefined();
    expect(
      prepareErrors(form({ gripperConfirmS: "abc" }), limits).gripper_confirm_s,
    ).toBeDefined();
    // Empty keeps the template's wait.
    expect(
      prepareErrors(form({ gripperConfirmS: "" }), limits).gripper_confirm_s,
    ).toBeUndefined();
  });

  test("the body is the closed body of the route: nothing else, the phrase the backend names", () => {
    const body = prepareBody(
      "req-1234567",
      form({ startHome: true }),
      "prepare aeri run",
    );
    expect(Object.keys(body).sort()).toEqual([
      "confirm",
      "gripper_confirm_s",
      "mode",
      "request_id",
      "rounds",
      "start_home",
      "steps",
      "task",
    ]);
    expect(body).toEqual({
      request_id: "req-1234567",
      mode: "external",
      task: "pick the pepper into the white plate",
      rounds: 5,
      steps: 200,
      gripper_confirm_s: 4,
      start_home: true,
      confirm: "prepare aeri run",
    });
    expect(
      prepareBody("req-1234567", form({ gripperConfirmS: " " }))
        .gripper_confirm_s,
    ).toBeNull();
    expect(
      prepareBody("req-1234567", form({ task: "  pick it up  " })).task,
    ).toBe("pick it up");
  });
});

// -- the checks in front of a level C key (review R10 #1)

const root = join(import.meta.dir, "../../../..");
/** D's own words, as the backend carries them (copied from the AERI sources by
 * tests/marvel0/regen_aeri_prompts.py): a real file, never a paraphrase. */
const PROMPTS = JSON.parse(
  readFileSync(join(root, "levi/marvel0/aeri_prompts.json"), "utf8"),
) as {
  profiles: Record<string, { texts: Record<string, Record<string, string>> }>;
};
const E = en as Record<string, string>;
const Z = zh as Record<string, string>;

/** What D asks a person to check before it takes the start phrase, one item
 * per thing it names, split out of its sentence. */
function itemsOfStart(lang: string, text: string): string[] {
  const body =
    lang === "en"
      ? text.match(/Confirm the reviewed (.+)\.$/)?.[1]
      : text.match(/请确认已核对的(.+)。$/)?.[1];
  return (body ?? "").split(lang === "en" ? /, | and /u : /、|和/u);
}
/** The same for the long phrase after an unfinished policy switch. */
function itemsOfSwitch(lang: string, text: string): string[] {
  const body =
    lang === "en"
      ? text.match(/check (each unit .+)$/)?.[1]
      : text.match(/请(逐个检查.+)$/)?.[1];
  return (body ?? "")
    .replace(/[:：]$/u, "")
    .split(lang === "en" ? /, | and /u : /、|，并/u);
}

describe("the checks in front of a level C key", () => {
  test("execute: everything D asks before the phrase, one tick each, then the three checks of the stack windows", () => {
    const checks = keyChecks("execute");
    expect(checks.map((c) => `${c.from}:${c.code}`)).toEqual([
      "terminal:scene",
      "terminal:limits",
      "terminal:checkpoints",
      "terminal:providers",
      "terminal:hardware",
      "window:estop_hand",
      "window:hands_clear",
      "window:no_client",
    ]);
  });

  test("execute_variant: the items of the policy switch check, then the start ones, then the three", () => {
    const checks = keyChecks("execute_variant");
    expect(checks.map((c) => c.code)).toEqual([
      "switch_units",
      "switch_ports",
      "switch_gpu",
      "switch_home",
      "scene",
      "limits",
      "checkpoints",
      "providers",
      "hardware",
      "estop_hand",
      "hands_clear",
      "no_client",
    ]);
  });

  test("end has the three alone (D asks nothing there); every other key has no tick", () => {
    expect(keyChecks("end").map((c) => c.code)).toEqual([...WINDOW_CHECKS]);
    for (const id of KEY_IDS)
      if (id !== "execute" && id !== "execute_variant")
        expect([id, keyChecks(id)]).toEqual([id, []]);
  });

  test("a tick has a sentence in both languages and no code is ticked twice", () => {
    for (const id of ["execute", "execute_variant", "end"] as const) {
      const checks = keyChecks(id);
      expect(new Set(checks.map((c) => c.code)).size).toBe(checks.length);
      for (const check of checks) {
        expect([check.label, typeof E[check.label]]).toEqual([
          check.label,
          "string",
        ]);
        expect([check.label, typeof Z[check.label]]).toEqual([
          check.label,
          "string",
        ]);
      }
    }
  });

  test("the list is D's own: one tick for each thing its text names, in every profile, in both languages", () => {
    const profiles = Object.entries(PROMPTS.profiles);
    expect(profiles.length).toBeGreaterThanOrEqual(3);
    const needle: Record<string, Record<string, string>> = {
      en: {
        scene: "scene",
        limits: "limits",
        checkpoints: "checkpoints",
        providers: "providers",
        hardware: "hardware",
        switch_units: "systemctl --user status",
        switch_ports: "ss -ltnp",
        switch_gpu: "nvidia-smi",
        switch_home: "home pose",
      },
      zh: {
        scene: "场景",
        limits: "限位",
        checkpoints: "检查点",
        providers: "提供方",
        hardware: "硬件",
        switch_units: "systemctl --user status",
        switch_ports: "ss -ltnp",
        switch_gpu: "nvidia-smi",
        switch_home: "回位姿态",
      },
    };
    for (const [id, profile] of profiles)
      for (const lang of ["en", "zh"]) {
        const texts = profile.texts[lang];
        const start = itemsOfStart(lang, texts.run_confirm_warning);
        const swap = itemsOfSwitch(lang, texts.swap_unclean_head);
        const where = `${id}/${lang}`;
        expect([where, start.length]).toEqual([
          where,
          TERMINAL_CHECKS.start.length,
        ]);
        expect([where, swap.length]).toEqual([
          where,
          TERMINAL_CHECKS.switch.length,
        ]);
        TERMINAL_CHECKS.start.forEach((code, index) =>
          expect([
            where,
            code,
            start[index].includes(needle[lang][code]),
          ]).toEqual([where, code, true]),
        );
        TERMINAL_CHECKS.switch.forEach((code, index) =>
          expect([
            where,
            code,
            swap[index].includes(needle[lang][code]),
          ]).toEqual([where, code, true]),
        );
      }
  });

  test("the three window checks are the ones the stack's start of C asks (stack_act.ATTEST_NEEDED), in the stack's words", () => {
    const source = readFileSync(
      join(root, "levi/marvel0/stack_act.py"),
      "utf8",
    );
    const needed = source.match(/"c": \(([^)]*)\)/)?.[1] ?? "";
    expect(needed.match(/"(\w+)"/g)?.map((x) => x.slice(1, -1))).toEqual([
      ...WINDOW_CHECKS,
    ]);
    for (const code of WINDOW_CHECKS)
      expect(typeof E[`marvel0.ctl.attest.${code}`]).toBe("string");
  });
});

describe("a key answers its own prompt only", () => {
  test("the table covers every key, and only the prompt kinds the backend lists", () => {
    expect(Object.keys(KEY_PROMPTS).sort()).toEqual([...KEY_IDS].sort());
    const kinds = new Set<string>(CODES.prompt_kinds);
    for (const [id, list] of Object.entries(KEY_PROMPTS))
      for (const kind of list)
        expect([id, kind, kinds.has(kind)]).toEqual([id, kind, true]);
    // ... and no kind the backend lists is left without a key: a new kind
    // would otherwise leave its keys off ("another prompt") without a word.
    const used = new Set<string>(Object.values(KEY_PROMPTS).flat());
    expect([...kinds].filter((kind) => !used.has(kind))).toEqual([]);
  });

  test("every key the backend's samples allow fits the prompt of its own document", () => {
    for (const name of RUN_VIEWS) {
      const run = view(name);
      for (const key of run.keys.filter((k) => k.allowed))
        expect([name, key.id, keyFitsPrompt(run, key.id)]).toEqual([
          name,
          key.id,
          true,
        ]);
    }
  });

  test("a key offered while D shows another prompt stays off, with the page's own reason", () => {
    // `release_round` offered, but the prompt of the document is an inventory.
    const run = view("await_release", (doc) => {
      doc.state.prompt.kind = "inventory";
    });
    const key = run.keys.find((k) => k.id === "release_round")!;
    expect(key.allowed).toBe(true);
    expect(keyFitsPrompt(run, "release_round")).toBe(false);
    const vm = keyVM(run, key, ctx(run));
    expect(vm.mode).toBe("off");
    expect(vm.why?.say).toEqual({
      key: "marvel0.autonomy.local.prompt_mismatch",
    });
    // And with no prompt at all.
    const bare = view("await_release", (doc) => {
      doc.state.prompt = null;
    });
    expect(keyFitsPrompt(bare, "release_round")).toBe(false);
    expect(keyVM(bare, key, ctx(bare)).mode).toBe("off");
  });

  test("quit and the two resume keys follow the prompt kinds of their states", () => {
    for (const [name, key, kind] of [
      ["await_inventory", "inventory_yes", "inventory"],
      ["await_resume", "resume_ask", "resume"],
      ["await_resume_confirm", "resume_confirm", "confirm_resume"],
      ["await_resume_confirm", "resume_cancel", "confirm_resume"],
      ["await_phrase", "execute", "phrase"],
      ["await_phrase_variant", "execute_variant", "phrase_variant"],
      ["forward_running", "verdict_success", "round"],
    ] as const) {
      const run = view(name);
      expect([name, key, run.state.prompt?.kind]).toEqual([name, key, kind]);
      expect(keyFitsPrompt(run, key)).toBe(true);
    }
    expect(keyOf(runDoc("await_inventory"), "quit")).toBeDefined();
  });
});

describe("what the page says after a Stop, by what D has said back", () => {
  const phases = ["idle", "sent", "acked", "unconfirmed", "retried"] as const;
  const hint = (state: Parameters<typeof stateHintKey>[0], phase: string) =>
    stateHintKey(state, phase as (typeof phases)[number]);

  test("only after D acknowledged is D said to wind down and the arm to stay where it is", () => {
    expect(E[hint("stopping", "acked")]).toMatch(/confirmed the Stop/);
    expect(E[hint("stopping", "acked")]).toMatch(/winding down/);
    expect(E[hint("stopping", "acked")]).toMatch(/arm stays where it is/);
    for (const phase of ["sent", "unconfirmed", "retried"]) {
      const sentence = E[hint("stopping", phase)];
      expect([phase, sentence]).toEqual([phase, expect.any(String)]);
      expect([phase, /may still be moving/.test(sentence)]).toEqual([
        phase,
        true,
      ]);
      expect([phase, /winding down|stays where it is/.test(sentence)]).toEqual([
        phase,
        false,
      ]);
      const chinese = Z[hint("stopping", phase)];
      expect([phase, /手臂可能仍在运动/.test(chinese)]).toEqual([phase, true]);
      expect([phase, /急停|物理急停/.test(chinese)]).toEqual([phase, true]);
      expect([phase, /手臂停在原处/.test(chinese)]).toEqual([phase, false]);
    }
  });

  test("a run that ends by itself (no Stop sent) is not said to have been stopped", () => {
    const sentence = E[hint("stopping", "idle")];
    expect(sentence).toMatch(/ending/);
    expect(sentence).not.toMatch(/Stop|arm stays/);
  });

  test("a restart's orphan and a stuck wrap-up say the same, by the same phase", () => {
    for (const state of ["orphaned", "stalled"] as const) {
      expect(hint(state, "acked")).toBe(`marvel0.autonomy.hint.${state}`);
      expect(hint(state, "idle")).toBe(`marvel0.autonomy.hint.${state}`);
      for (const phase of ["sent", "unconfirmed", "retried"])
        expect(E[hint(state, phase)]).toMatch(/may still be moving/);
      expect(E[hint(state, "acked")]).not.toMatch(/may still be moving/);
    }
  });

  test("an unreadable D that another core of LEVI owns is not said to offer Stop", () => {
    expect(hint("unknown", "idle")).toBe("marvel0.autonomy.hint.unknown");
    expect(stateHintKey("unknown", "idle", "screen_drift")).toBe(
      "marvel0.autonomy.hint.unknown",
    );
    const other = stateHintKey("unknown", "idle", "no_keyboard");
    expect(other).toBe("marvel0.autonomy.hint.unknown_other_core");
    expect(E["marvel0.autonomy.hint.unknown"]).toMatch(/Only Stop is offered/);
    expect(E[other]).toMatch(/only watches it/);
    expect(E[other]).not.toMatch(/Only Stop is offered/);
    // Only the state `unknown` is read by its reason.
    expect(stateHintKey("stopping", "acked", "no_keyboard")).toBe(
      "marvel0.autonomy.hint.stopping",
    );
  });

  test("the other states have one sentence whatever the phase", () => {
    for (const state of ["forward_running", "services", "ended"] as const)
      for (const phase of phases)
        expect(hint(state, phase)).toBe(`marvel0.autonomy.hint.${state}`);
  });

  test("the captions under the Stop button: sent and unconfirmed never promise the arm stays", () => {
    for (const mode of ["sent", "retry", "spent"]) {
      const sentence = E[`marvel0.autonomy.stop.${mode}`];
      expect([
        mode,
        /not confirmed|did not confirm|still not confirmed/.test(sentence),
      ]).toEqual([mode, true]);
      expect([mode, /may still be moving/.test(sentence)]).toEqual([
        mode,
        true,
      ]);
      expect([mode, /stays where it is/.test(sentence)]).toEqual([mode, false]);
    }
    expect(E["marvel0.autonomy.stop.acked"]).toMatch(/confirmed the stop/);
    expect(E["marvel0.autonomy.stop.acked"]).toMatch(/stays where it is/);
  });
});

describe("how often the page reads (review R11 M4)", () => {
  test("the readiness document is read at the normal pace only where there is something to prepare", () => {
    expect(LIMITS.slowPollMs).toBe(30_000);
    expect(readyPollMs(ready("prepared_ready"), false)).toBe(
      LIMITS.readyPollMs,
    );
    expect(readyPollMs(null, false)).toBe(LIMITS.readyPollMs);
    // Switched off in the control file: the backend's own sample, then slowly.
    expect(ready("disabled").enabled).toBe(false);
    expect(readyPollMs(ready("disabled"), false)).toBe(LIMITS.slowPollMs);
    // No usable section in the site file (missing, malformed).
    expect(readyPollMs(ready("unconfigured"), false)).toBe(LIMITS.slowPollMs);
    expect(readyPollMs(ready("invalid_section"), false)).toBe(
      LIMITS.slowPollMs,
    );
    // The route itself said "off" (aeri_disabled), before any document.
    expect(readyPollMs(null, true)).toBe(LIMITS.slowPollMs);
    expect(hostingOff(null, false)).toBe(false);
    expect(hostingOff(ready("prepared_ready"), false)).toBe(false);
  });

  test("it is not read at all while an evaluation client is live, once it has been read", () => {
    expect(readyPaused(true, true)).toBe(true);
    expect(readyPaused(true, false)).toBe(false); // the first read is made
    expect(readyPaused(false, true)).toBe(false);
  });

  test("D's document: every second while D lives, 3 s otherwise, slowly when there is nothing to see", () => {
    const alive = view("forward_running");
    const idle = view("idle");
    expect(runPollMs(alive, false)).toBe(LIMITS.runPollMs);
    expect(runPollMs(alive, true)).toBe(LIMITS.runPollMs);
    expect(runPollMs(idle, false)).toBe(LIMITS.idlePollMs);
    expect(runPollMs(null, false)).toBe(LIMITS.idlePollMs);
    expect(runPollMs(idle, true)).toBe(LIMITS.slowPollMs);
    expect(runPollMs(view("ended_done"), true)).toBe(LIMITS.slowPollMs);
  });
});

describe("the example task of the form", () => {
  test("it is no longer the pepper, it is a valid task under the backend's own pattern, and it keeps the wording the policy was run with", () => {
    for (const lang of [E, Z]) {
      const example = lang["marvel0.autonomy.form.task_example"];
      expect(example).not.toMatch(/pepper/);
      expect(example).toMatch(/^pick the [a-z]+ into the (white|blue) plate$/);
      expect(
        prepareErrors({
          ...initialForm(null),
          task: example,
        }),
      ).toEqual({});
    }
    // The pattern the backend sends, from its own sample.
    const limits = ready("prepared_ready").limits!;
    expect(
      new RegExp(`^${limits.taskPattern}$`).test(
        E["marvel0.autonomy.form.task_example"],
      ),
    ).toBe(true);
  });
});
