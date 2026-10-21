import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
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
  executePhrase,
  groupBySlot,
  hasRun,
  historySig,
  homingStep,
  initialForm,
  keyVM,
  lockPhase,
  prepareBody,
  prepareErrors,
  promptLine,
  runPageHref,
  startOff,
  startPhrase,
  stopControl,
  stopSeen,
  visibleKeys,
  type KeyCtx,
  type PrepareForm,
  type SentKey,
} from "../autonomy-logic";
import { readyDoc, runDoc } from "./autonomy-fixtures";

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

  test("execute: the backend's phrase, else the one the terminal asks for", () => {
    const run = view("await_phrase_variant");
    const [execute, variant] = [
      run.keys.find((k) => k.id === "execute")!,
      run.keys.find((k) => k.id === "execute_variant")!,
    ];
    expect(executePhrase(variant, run.name)).toBe(
      "execute aeri-20261021-ext-r01 after policy switch check",
    );
    expect(executePhrase(execute, run.name)).toBe(
      "execute aeri-20261021-ext-r01",
    );
    expect(executePhrase({ ...execute, phrase: null }, "n1")).toBe(
      "execute n1",
    );
    expect(executePhrase({ ...variant, phrase: null }, "n1")).toBe(
      "execute n1 after policy switch check",
    );
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
