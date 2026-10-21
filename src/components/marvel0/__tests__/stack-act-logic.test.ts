import { describe, expect, test } from "bun:test";
import en from "@/i18n/en.json";
import {
  canCheckFci,
  canCheckServer,
  canStartSequence,
  startNeed,
  canStopSequence,
  actView,
  compPhrase,
  markerRemaining,
  phraseFor,
  readinessProgress,
  startControl,
  stopControl,
  COMPS,
  type ActView,
} from "../stack-act-logic";
import { STACK_VIEWS, stackDoc } from "./ctl-fixtures";
import { stackView } from "../stack-status-logic";

const KNOWN = new Set(Object.keys(en));
const act = (
  name: string,
  change?: (doc: Record<string, any>) => void, // eslint-disable-line @typescript-eslint/no-explicit-any
) => actView(stackDoc(name, change), KNOWN, stackView(stackDoc(name), KNOWN))!;
const states = (view: ActView) =>
  Object.fromEntries(COMPS.map((id) => [id, view.comps[id].state]));

describe("the backend's own views, read by the page", () => {
  test("every named view of the backend reads, and none reads as unreadable", () => {
    expect(STACK_VIEWS.length).toBeGreaterThanOrEqual(15);
    for (const name of STACK_VIEWS) {
      const view = act(name);
      expect(view).not.toBeNull();
      // The page's state is the backend's word, never a re-derivation.
      const raw = stackDoc(name).act.terminals;
      for (const id of COMPS)
        expect(view.comps[id].state).toBe(raw[id.toLowerCase()].state);
    }
  });

  test("states follow the backend", () => {
    expect(states(act("idle"))).toEqual({
      M: "idle",
      A: "idle",
      B: "unknown",
      C: "idle",
    });
    expect(states(act("a_starting")).A).toBe("starting");
    expect(states(act("a_ready")).A).toBe("ready");
    expect(states(act("a_failed")).A).toBe("failed");
    expect(states(act("c_ready"))).toEqual({
      M: "ready",
      A: "ready",
      B: "ready",
      C: "ready",
    });
  });

  test("a start is offered exactly when the backend says so, with its blockers beside it when not", () => {
    const ready = act("ready_to_start_a");
    expect(startControl(ready.comps.A)).toMatchObject({
      enabled: true,
    });
    const idle = act("idle");
    const a = startControl(idle.comps.A);
    expect(a.enabled).toBe(false);
    // The backend's own codes, in the order it sent them.
    expect(a.blockers.map((b) => b.code)).toContain("gate.monitor_fresh");
    expect(a.blockers.map((b) => b.code)).toContain("gate.marker_set");
    // Worded from the catalogue, with the facts that fill the sentence.
    for (const blocker of a.blockers)
      expect(blocker.worded.say?.key ?? "").toMatch(/^marvel0\./);
  });

  test("a blocker the catalogue does not know shows the server's sentence, never a made-up one", () => {
    const view = act("idle", (doc) => {
      doc.act.terminals.a.start_blockers = [
        {
          code: "brand_new",
          level: "required",
          text: "Server words",
          params: {},
        },
      ];
    });
    expect(view.comps.A.startBlockers[0].worded).toEqual({
      say: null,
      text: "Server words",
    });
  });

  test("a sentence whose facts did not arrive is not printed with a bare placeholder", () => {
    const view = act("idle", (doc) => {
      doc.act.terminals.a.start_blockers = [
        {
          code: "gate.fci_probe",
          level: "required",
          text: "English: check FCI",
          params: {},
        },
      ];
    });
    expect(view.comps.A.startBlockers[0].worded).toEqual({
      say: null,
      text: "English: check FCI",
    });
  });

  test("the same blocker twice is listed once", () => {
    const view = act("a_ready");
    const codes = view.comps.M.stopBlockers.map((b) => b.code);
    expect(codes).toEqual([...new Set(codes)]);
  });

  test("the guard is the backend's: outside the product, or with the control file off, nothing is offered", () => {
    const fake = act("ready_to_start_a", (doc) => {
      doc.profile = "fake";
    });
    expect(fake.real).toBe(false);
    for (const id of COMPS) {
      expect(startControl(fake.comps[id]).enabled).toBe(false);
      expect(stopControl(fake.comps[id]).enabled).toBe(false);
    }
    expect(fake.marker.canTouch || fake.marker.canAdopt).toBe(false);
    expect(canCheckFci(fake) || canCheckServer(fake)).toBe(false);
    expect(canStartSequence(fake)).toBe(false);
    const off = act("control_disabled");
    expect(off.enabled).toBe(false);
    expect(startControl(off.comps.A).enabled).toBe(false);
    expect(off.comps.A.startBlockers[0].code).toBe("control_disabled");
  });

  test("a document without the controls reads as none; a terminal missing from it offers nothing", () => {
    expect(actView({ ...stackDoc("idle"), act: undefined }, KNOWN)).toBeNull();
    expect(actView(null, KNOWN)).toBeNull();
    const view = act("ready_to_start_a", (doc) => {
      delete doc.act.terminals.a;
    });
    expect(view.comps.A.state).toBe("unknown");
    expect(startControl(view.comps.A).enabled).toBe(false);
    // A value of the wrong type is false, not true.
    const strict = act("ready_to_start_a", (doc) => {
      doc.act.terminals.a.can_start = "yes";
    });
    expect(startControl(strict.comps.A).enabled).toBe(false);
  });
});

describe("starting: what has been seen and what is still missing", () => {
  test("A while it starts", () => {
    const a = act("a_starting").comps.A;
    expect(a.readiness!.seen.map((l) => l.key)).toEqual(["fifo"]);
    expect(a.readiness!.missing.map((l) => l.key)).toEqual([
      "spawners",
      "health_hardware_active",
    ]);
    expect(a.readiness!.waitedS).toBe(12);
    expect(a.readiness!.timeoutS).toBe(90);
    expect(readinessProgress(a.readiness)).toEqual({ seen: 1, total: 3 });
    // The lines are worded from the catalogue.
    expect(a.readiness!.seen[0].worded.say).toEqual({
      key: "marvel0.ctl.ready.fifo",
    });
  });

  test("no ready lines, no progress", () => {
    expect(readinessProgress(null)).toBeNull();
    expect(
      readinessProgress({ seen: [], missing: [], waitedS: 0, timeoutS: 1 }),
    ).toBeNull();
  });

  test("a failure is worded and its reason is the backend's code", () => {
    const a = act("a_failed").comps.A;
    expect(a.state).toBe("failed");
    expect(a.failure?.say?.key).toMatch(/^marvel0\.ctl\.failure\./);
  });
});

describe("stopping C with a client on it", () => {
  test("the backend says the override phrase would lift the refusal: the button stays on and asks for it", () => {
    const c = act("c_ready_with_client").comps.C;
    expect(c.canStop).toBe(false);
    expect(c.canStopWithOverride).toBe(true);
    expect(stopControl(c)).toMatchObject({
      enabled: true,
      needsOverride: true,
    });
    expect(c.override).toBe("stop robot server with client connected");
  });

  test("without a client it is a plain stop", () => {
    const c = act("c_ready").comps.C;
    expect(stopControl(c)).toMatchObject({
      enabled: true,
      needsOverride: false,
    });
  });

  test("A cannot be stopped under C, and says why", () => {
    const a = act("c_ready").comps.A;
    expect(stopControl(a).enabled).toBe(false);
    expect(a.stopBlockers.map((b) => b.code)).toEqual(["stop_c_first"]);
  });
});

describe("the marker, the checks, the gripper test and the guided start", () => {
  test("the marker counts down from the sample", () => {
    const young = act("idle", (doc) => {
      doc.act.marker = {
        state: "present",
        age_s: 20,
        ctime_age_s: 20,
        ours: true,
        wait_s: 60,
        remaining_s: 40,
      };
    }).marker;
    expect(markerRemaining(young, 0)).toBe(40);
    expect(markerRemaining(young, 15)).toBe(25);
    expect(markerRemaining(young, 100)).toBe(0);
    expect(markerRemaining(act("idle").marker, 5)).toBeNull();
  });

  test("a marker nobody of the page made can be adopted; none at all can be set", () => {
    const foreign = act("idle", (doc) => {
      doc.act.marker = {
        state: "present",
        age_s: 9,
        ours: false,
        wait_s: 60,
        remaining_s: 51,
      };
    }).marker;
    expect(foreign).toMatchObject({
      canAdopt: true,
      canTouch: false,
      canRelease: false,
    });
    expect(act("idle").marker).toMatchObject({
      canTouch: true,
      canAdopt: false,
    });
    // Release is the passive card's (only when the arm shows no control
    // connection and A is gone), never offered on the marker alone.
    expect(act("a_ready").marker.canRelease).toBe(false);
  });

  test("the FCI check is offered before A, never while A runs; the checks before C need A and B", () => {
    expect(canCheckFci(act("idle"))).toBe(true);
    expect(canCheckFci(act("a_ready"))).toBe(false);
    expect(canCheckServer(act("a_ready"))).toBe(false);
    expect(canCheckServer(act("b_ready"))).toBe(true);
    expect(act("a_ready").fci).toMatchObject({ state: "open", fresh: true });
    expect(act("c_ready").server).toMatchObject({
      ok: true,
      qos: true,
      controllers: true,
      eeFrame: true,
    });
    expect(act("idle").fci).toBeNull();
  });

  test("while a backend operation runs, nothing else is offered", () => {
    const view = act("b_ready", (doc) => {
      doc.act.operation = {
        kind: "gripper",
        terminal: "b",
        state: "running",
        started_at: 1,
        finished_at: null,
        code: null,
      };
    });
    expect(
      canCheckFci(view) || canCheckServer(view) || canStartSequence(view),
    ).toBe(false);
  });

  test("the gripper test's last result and its blockers", () => {
    const ok = act("gripper_success").gripper;
    expect(ok.canRun).toBe(true);
    expect(ok.last).toMatchObject({
      action: "close",
      ok: true,
      reached: true,
      stalled: false,
      code: "reached",
    });
    const bad = act("gripper_failed").gripper;
    expect(bad.last).toMatchObject({
      ok: false,
      stalled: true,
      code: "stalled",
    });
    const off = act("idle").gripper;
    expect(off.canRun).toBe(false);
    expect(off.blockers.map((b) => b.code)).toContain("gripper_b_not_ready");
  });

  test("the guided start: running, waiting for the person before B, and when it may be started", () => {
    expect(act("sequence_running").sequence).toMatchObject({
      state: "running",
      step: "monitor",
    });
    expect(act("sequence_waiting_b").sequence).toMatchObject({
      state: "waiting_confirm",
      next: "B",
    });
    expect(canStartSequence(act("idle"))).toBe(true);
    expect(canStartSequence(act("sequence_running"))).toBe(false);
  });

  test("the whole stack's start is offered again after a failure further on, and skips what is ready", () => {
    // A is up, B and C are not: pressing again starts B and C.
    expect(canStartSequence(act("a_ready"))).toBe(true);
    expect(startNeed(act("a_ready"))).toBeNull();
    expect(canStartSequence(act("b_ready"))).toBe(true);
    // Everything is up: nothing to start, and the page says so.
    expect(canStartSequence(act("c_ready"))).toBe(false);
    expect(startNeed(act("c_ready"))).toBe("all_ready");
    // A failed or vanished A is for the person to look at first.
    expect(canStartSequence(act("a_failed"))).toBe(false);
    expect(startNeed(act("a_failed"))).toBe("a_unfit");
    // A start or stop that runs says so itself; an operation is the other reason.
    expect(startNeed(act("sequence_running"))).toBeNull();
    expect(startNeed(act("stop_all_running"))).toBeNull();
    expect(canStartSequence(act("stop_all_running"))).toBe(false);
    expect(
      startNeed(
        act("idle", (doc) => {
          doc.act.operation = {
            kind: "preflight_a",
            terminal: "a",
            state: "running",
            started_at: 1,
            finished_at: null,
            code: null,
          };
        }),
      ),
    ).toBe("free");
    // An instance that only shows says nothing: its own notice does.
    expect(
      startNeed(
        act("idle", (doc) => {
          doc.profile = "fake";
        }),
      ),
    ).toBeNull();
  });

  test("how far a start goes and whether it pauses are read from the sequence", () => {
    const seq = act("sequence_waiting_b", (doc) => {
      doc.act.sequence.through = "c";
      doc.act.sequence.pauses = { b: true, c: false };
    }).sequence;
    expect(seq).toMatchObject({ through: "c", pauseB: true, pauseC: false });
    expect(act("idle").sequence).toMatchObject({
      through: null,
      pauseB: false,
      pauseC: false,
    });
  });
});

describe("the stop of the whole stack", () => {
  test("offered exactly when the backend says so; the backend's rows say why not", () => {
    for (const name of [
      "c_ready",
      "stop_all_ready",
      "stop_all_failed",
      "stop_all_cancelled",
    ]) {
      const on = act(name);
      expect([name, on.stopAll.canStop, on.stopAll.blockers]).toEqual([
        name,
        true,
        [],
      ]);
      expect(canStopSequence(on)).toBe(true);
    }
    const nothing = act("idle");
    expect(nothing.stopAll.canStop).toBe(false);
    expect(nothing.stopAll.blockers.map((b) => b.code)).toEqual([
      "nothing_to_stop",
    ]);
    expect(nothing.stopAll.blockers[0].worded.say).toEqual({
      key: "marvel0.ctl.block.nothing_to_stop",
    });
    expect(canStopSequence(nothing)).toBe(false);
    // A client on C: no override exists for the whole stack, and the sentence
    // names the port.
    const blocked = act("stop_all_blocked");
    expect(blocked.stopAll.blockers.map((b) => b.code)).toEqual([
      "stop_all_client_connected",
    ]);
    expect(blocked.stopAll.blockers[0].worded.say).toEqual({
      key: "marvel0.ctl.block.stop_all_client_connected",
      params: { port: 5000 },
    });
    expect(canStopSequence(blocked)).toBe(false);
  });

  test("a backend without it (no answer in the checks) offers nothing, and a wrong type reads as no", () => {
    const old = act("c_ready", (doc) => {
      delete doc.act.checks.can_stop_all;
      delete doc.act.stop_all_blockers;
    });
    expect(old.stopAll).toMatchObject({ canStop: false, blockers: [] });
    expect(
      act("c_ready", (doc) => {
        doc.act.checks.can_stop_all = "yes";
      }).stopAll.canStop,
    ).toBe(false);
  });

  test("a stop needs no control file, but a worktree or temporary instance only shows", () => {
    // The control file is switched off: starting is off, stopping is not.
    const disabled = act("c_ready", (doc) => {
      doc.act.enabled = false;
    });
    expect(disabled.enabled).toBe(false);
    expect(disabled.comps.A.canStart).toBe(false);
    expect(disabled.stopAll.canStop).toBe(true);
    const fake = act("c_ready", (doc) => {
      doc.profile = "fake";
    });
    expect(fake.stopAll).toMatchObject({ offered: false, canStop: false });
  });

  test("a running guided start or a read-only check never holds the stop back; a running guided stop does", () => {
    const operation = act("c_ready", (doc) => {
      doc.act.operation = {
        kind: "preflight_c",
        terminal: "c",
        state: "running",
        started_at: doc.sampled_at,
        code: null,
        finished_at: null,
      };
    });
    expect(operation.stopAll.canStop).toBe(true);
    // RIG-04: a read-only check that runs does not hold the way out back (the
    // backend ends it first).
    expect(canStopSequence(operation)).toBe(true);
    // A guided stop already running is the same request: not offered twice.
    expect(canStopSequence(act("stop_all_started"))).toBe(false);
    // A guided START that runs (at any step of the chain) does not hold it back:
    // the backend ends the start first (the review found the button grey for the
    // whole chain).
    expect(
      canStopSequence(
        act("sequence_running", (doc) => {
          doc.act.checks.can_stop_all = true;
        }),
      ),
    ).toBe(true);
    // A guided start that waits for the person does not hold the stop back.
    expect(
      canStopSequence(
        act("sequence_waiting_b", (doc) => {
          doc.act.checks.can_stop_all = true;
        }),
      ),
    ).toBe(true);
  });

  test("the guided stop: its kind, its steps in order and each step's state", () => {
    const view = act("stop_all_running").sequence;
    expect(view).toMatchObject({
      kind: "stop",
      state: "running",
      step: "stop_a",
    });
    expect(view.steps).toEqual([
      { step: "stop_c", terminal: "C", state: "done", tookS: 0 },
      { step: "stop_a", terminal: "A", state: "running", tookS: null },
      { step: "stop_b", terminal: "B", state: "pending", tookS: null },
    ]);
    // Done, failed (at A, with the single stop's own code) and cancelled.
    expect(act("stop_all_done").sequence.steps.map((x) => x.state)).toEqual([
      "done",
      "done",
      "done",
    ]);
    const failed = act("stop_all_failed").sequence;
    // The step after the failure was not reached (it is not "pending").
    expect(failed.steps.map((x) => x.state)).toEqual([
      "done",
      "failed",
      "not_reached",
    ]);
    expect(failed.reason!.say).toEqual({
      key: "marvel0.ctl.block.stop_failed",
    });
    expect(act("stop_all_cancelled").sequence.reason!.say).toEqual({
      key: "marvel0.ctl.seq.reason.cancelled",
    });
  });

  test("a terminal the page does not know is left out of the steps; an unknown step state reads as not yet reached", () => {
    const view = act("stop_all_running", (doc) => {
      doc.act.sequence.steps = [
        { step: "stop_b", terminal: "b", state: "mystery" },
        { step: "stop_x", terminal: "x", state: "pending" },
        { nope: true },
      ];
    }).sequence;
    expect(view.steps).toEqual([
      { step: "stop_b", terminal: "B", state: "pending", tookS: null },
    ]);
  });

  test("the wait before A: what it waits for, the heavy jobs, and a reason with its count", () => {
    const waiting = act("chain_waiting_heavy").sequence;
    expect(waiting.remainingS).toBeNull(); // (a heavy job runs: its end is not known)
    expect(waiting.wait).toEqual({
      ageS: 14,
      minS: 3,
      remainingS: null,
      heavy: { state: "running", count: 2 },
      holds: ["heavy_jobs_running"],
      capS: 90,
      waitedS: 12.5,
    });
    const unknown = act("chain_waiting_heavy_unknown").sequence;
    expect(unknown.wait!.heavy).toEqual({ state: "unknown", count: 0 });
    expect(unknown.wait!.minS).toBe(45); // (the fixed wait of old)
    expect(unknown.wait!.holds).toEqual(["heavy_jobs_unknown", "marker_young"]);
    expect(unknown.remainingS).toBe(38);
    const failed = act("chain_failed_heavy").sequence;
    expect(failed.reason!.say).toEqual({
      key: "marvel0.ctl.seq.reason.heavy_job_still_running",
      params: { count: 2 },
    });
    expect(failed.wait).toBeNull(); // (over: the wait is no more)
    expect(failed.tookS).toBe(104.5);
  });

  test("how long the steps took is read as numbers; anything else is null", () => {
    const done = act("chain_done").sequence;
    expect(done.steps.map((x) => x.tookS)).toEqual([
      0, 1, 1, 4.5, 0.5, 0.5, 1, 1, 1, 1,
    ]);
    expect(done.tookS).toBe(
      done.steps.reduce((sum, x) => sum + (x.tookS ?? 0), 0),
    );
    const odd = act("chain_done", (doc) => {
      doc.act.sequence.steps[0].took_s = "slow";
      delete doc.act.sequence.steps[1].took_s;
      doc.act.sequence.took_s = null;
    }).sequence;
    expect(odd.steps[0].tookS).toBeNull();
    expect(odd.steps[1].tookS).toBeNull();
    expect(odd.tookS).toBeNull();
  });

  test("a wait of the wrong type reads as nothing known; an older backend sends none", () => {
    const odd = act("chain_waiting_heavy", (doc) => {
      doc.act.sequence.wait.heavy = { state: 7, count: "many" };
      doc.act.sequence.wait.holds = "heavy_jobs_running";
      doc.act.sequence.wait.cap_s = "ninety";
    }).sequence.wait!;
    expect(odd.heavy).toEqual({ state: "clear", count: 0 });
    expect(odd.holds).toEqual([]);
    expect(odd.capS).toBeNull();
    const old = act("chain_waiting_heavy", (doc) => {
      delete doc.act.sequence.wait;
      delete doc.act.sequence.took_s;
      delete doc.act.sequence.elapsed_s;
      delete doc.act.sequence.reason_params;
    }).sequence;
    expect(old.wait).toBeNull();
    expect(old.tookS).toBeNull();
    expect(old.elapsedS).toBeNull();
  });

  test("an idle sequence (no kind) and an older backend read as a start with no steps", () => {
    expect(act("idle").sequence).toMatchObject({ kind: "start", steps: [] });
    expect(
      act("idle", (doc) => {
        delete doc.act.sequence.kind;
      }).sequence.kind,
    ).toBe("start");
    expect(act("sequence_waiting_b").sequence.kind).toBe("start");
  });

  test("why a guided stop ended is worded from the sequence's reasons, then from the refusals", () => {
    const ended = (reason: string) =>
      act("stop_all_failed", (doc) => {
        doc.act.sequence.reason = reason;
      }).sequence.reason!;
    expect(ended("stop_all_client_connected").say).toEqual({
      key: "marvel0.ctl.seq.reason.stop_all_client_connected",
    });
    expect(ended("step_timeout").say).toEqual({
      key: "marvel0.ctl.seq.reason.step_timeout",
    });
    expect(ended("stop_left_processes").say).toEqual({
      key: "marvel0.ctl.seq.reason.stop_left_processes",
    });
    expect(ended("not_ours").say).toEqual({
      key: "marvel0.ctl.block.not_ours",
    });
    expect(ended("superseded").say).toEqual({
      key: "marvel0.ctl.seq.reason.superseded",
    });
  });
});

describe("the phrases", () => {
  test("a terminal's phrase is the backend's, else the design record's", () => {
    const view = act("idle");
    expect(compPhrase(view, "start", "A")).toBe("start arm stack");
    expect(compPhrase(view, "stop", "C")).toBe("stop robot server");
    expect(phraseFor(view, "gripper_open")).toBe("gripper open");
    expect(phraseFor(null, "gripper_close")).toBe("gripper close");
    expect(compPhrase(null, "start", "B")).toBe("start gripper stack");
    expect(compPhrase(null, "stop", "M")).toBe("stop health monitor");
    const changed = act("idle", (doc) => {
      doc.act.terminals.a.confirm = { start: "begin A", stop: "end A" };
    });
    expect(compPhrase(changed, "start", "A")).toBe("begin A");
  });

  test("the stop of the whole stack has its own phrase: the backend's, else the constant", () => {
    expect(phraseFor(act("c_ready"), "sequence_stop")).toBe("stop whole stack");
    expect(phraseFor(null, "sequence_stop")).toBe("stop whole stack");
    const named = act("idle", (doc) => {
      doc.act.phrases = { ...doc.act.phrases, sequence_stop: "stop it all" };
    });
    expect(phraseFor(named, "sequence_stop")).toBe("stop it all");
  });
});
